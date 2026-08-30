from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
RENDERER_PATH = REPOSITORY / "scripts" / "render_systemd_units.py"
SPEC = importlib.util.spec_from_file_location("render_systemd_units", RENDERER_PATH)
assert SPEC is not None and SPEC.loader is not None
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


class SystemdUnitTests(unittest.TestCase):
    def test_quote_disables_specifier_and_environment_expansion(self) -> None:
        quoted = renderer.systemd_quote('/tmp/a space/"quoted"/%h/$HOME\\bin')
        self.assertEqual(
            quoted,
            '"/tmp/a space/\\"quoted\\"/%%h/$$HOME\\\\bin"',
        )

    def test_quote_rejects_relative_and_control_paths(self) -> None:
        for value in ("relative/path", "/tmp/new\nline", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                renderer.systemd_quote(value)

    def test_render_requires_exact_placeholder_set(self) -> None:
        with self.assertRaises(ValueError):
            renderer.render_unit("ExecStart=@EXEC@\n", {})
        with self.assertRaises(ValueError):
            renderer.render_unit(
                "ExecStart=@EXEC@\n", {"EXEC": "/bin/true", "EXTRA": "/tmp/x"}
            )

    def test_voice_unit_is_idle_safe_and_systemd_valid(self) -> None:
        engine_template = (
            REPOSITORY / "packaging/systemd/murmur-ime-engine.service.in"
        ).read_text(encoding="utf-8")
        voice_template = (
            REPOSITORY / "packaging/systemd/murmur-ime-voice.service.in"
        ).read_text(encoding="utf-8")
        engine_unit = renderer.render_unit(
            engine_template, {"ENGINE_EXEC": "/bin/true"}
        )
        voice_unit = renderer.render_unit(
            voice_template,
            {
                "VOICE_EXEC": "/bin/true",
                "VOICE_CONFIG": "/dev/null",
                "VOICE_VOCABULARY": "/tmp/vocabulary.json",
                "VOICE_CORRECTIONS": "/tmp/corrections.json",
                "VOICE_ADAPTIVE_CORRECTIONS": "/tmp/adaptive-corrections.json",
                "VOICE_DATA_COLLECTION": "/tmp/data-collection.json",
            },
        )

        self.assertNotIn("ConditionPathExists=", voice_unit)
        self.assertIn("After=graphical-session.target", engine_unit)
        self.assertIn("PartOf=graphical-session.target", engine_unit)
        self.assertIn("StartLimitIntervalSec=10", engine_unit)
        self.assertIn("StartLimitBurst=10", engine_unit)
        self.assertIn("Restart=always", engine_unit)
        self.assertIn("RestartSec=2", engine_unit)
        self.assertIn("UMask=0077", engine_unit)
        self.assertIn("Environment=PYTHONDONTWRITEBYTECODE=1", engine_unit)
        self.assertIn("Environment=PYTHONNOUSERSITE=1", engine_unit)
        self.assertIn("WantedBy=graphical-session.target", engine_unit)
        self.assertNotIn("WantedBy=default.target", engine_unit)
        self.assertIn(
            'ExecStart="/bin/true" run --config "/dev/null" '
            '--vocabulary "/tmp/vocabulary.json" '
            '--corrections "/tmp/corrections.json"',
            voice_unit,
        )
        self.assertIn(
            '--data-collection "/tmp/data-collection.json"',
            voice_unit,
        )
        self.assertIn("UMask=0077", voice_unit)
        self.assertIn("RuntimeDirectory=murmur-ime", voice_unit)
        self.assertIn("RuntimeDirectoryMode=0700", voice_unit)
        self.assertIn("TimeoutStopSec=30", voice_unit)
        self.assertIn("Environment=PYTHONNOUSERSITE=1", voice_unit)
        self.assertIn("RestartPreventExitStatus=2", voice_unit)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", voice_unit)
        self.assertNotIn("/bin/sh", voice_unit)

        analyzer = shutil.which("systemd-analyze")
        if analyzer is None:
            self.skipTest("systemd-analyze is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine_path = root / "murmur-ime-engine.service"
            voice_path = root / "murmur-ime-voice.service"
            engine_path.write_text(engine_unit, encoding="utf-8")
            voice_path.write_text(voice_unit, encoding="utf-8")
            result = subprocess.run(
                [analyzer, "verify", str(engine_path), str(voice_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
