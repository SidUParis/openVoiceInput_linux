from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
RENDERER_PATH = REPOSITORY / "scripts" / "render_desktop_entry.py"
TEMPLATE_PATH = (
    REPOSITORY
    / "packaging/desktop/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop.in"
)
ICON_PATH = (
    REPOSITORY / "packaging/icons/io.github.SidUParis.OpenVoiceInputLinux.Settings.svg"
)
SPEC = importlib.util.spec_from_file_location("render_desktop_entry", RENDERER_PATH)
assert SPEC is not None and SPEC.loader is not None
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


class DesktopEntryTests(unittest.TestCase):
    def test_quote_preserves_literal_path_metacharacters(self) -> None:
        quoted = renderer.desktop_exec_quote(
            '/tmp/a space/"quoted"/%value/$HOME/`tick`/back\\slash'
        )
        self.assertEqual(
            quoted,
            '"/tmp/a space/\\\\\\"quoted\\\\\\"/%%value/'
            '\\\\$HOME/\\\\`tick\\\\`/back\\\\\\\\slash"',
        )

    def test_quote_rejects_relative_and_control_paths(self) -> None:
        for value in ("relative/path", "/tmp/new\nline", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                renderer.desktop_exec_quote(value)

    def test_render_requires_an_exact_unique_placeholder_set(self) -> None:
        with self.assertRaises(ValueError):
            renderer.render_entry("Exec=@EXEC@\n", {})
        with self.assertRaises(ValueError):
            renderer.render_entry(
                "Exec=@EXEC@\n", {"EXEC": "/bin/true", "EXTRA": "/tmp/x"}
            )
        with self.assertRaises(ValueError):
            renderer.render_entry("Exec=@EXEC@ @EXEC@\n", {"EXEC": "/bin/true"})

    def test_rendered_project_entry_is_valid_when_validator_is_available(self) -> None:
        rendered = renderer.render_entry(
            TEMPLATE_PATH.read_text(encoding="utf-8"),
            {"SETTINGS_EXEC": "/tmp/xdg data/%literal$/open settings"},
        )
        self.assertIn(
            'Exec=/usr/bin/env -- "/tmp/xdg data/%%literal\\\\$/open settings"',
            rendered,
        )
        self.assertIn("Name[zh_CN]=Open Voice Input Linux 设置", rendered)
        self.assertIn("DBusActivatable=false", rendered)
        self.assertNotIn("@SETTINGS_EXEC@", rendered)

        validator = shutil.which("desktop-file-validate")
        if validator is None:
            self.skipTest("desktop-file-validate is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "settings.desktop"
            entry.write_text(rendered, encoding="utf-8")
            result = subprocess.run(
                [validator, str(entry)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_gio_launch_preserves_space_percent_and_dollar_in_exec_path(self) -> None:
        launcher = shutil.which("gio")
        if launcher is None:
            self.skipTest("gio is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "a space" / "%literal$" / "open settings"
            executable.parent.mkdir(parents=True)
            marker = executable.with_name("launched")
            executable.write_text(
                "#!/usr/bin/python3\n"
                "from pathlib import Path\n"
                "Path(__file__).with_name('launched').touch()\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            entry = root / "settings.desktop"
            entry.write_text(
                renderer.render_entry(
                    TEMPLATE_PATH.read_text(encoding="utf-8"),
                    {"SETTINGS_EXEC": str(executable)},
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [launcher, "launch", str(entry)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            for _ in range(20):
                if marker.exists():
                    break
                time.sleep(0.05)
            launched = marker.exists()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(launched)

    def test_svg_is_local_static_xml(self) -> None:
        root = ET.parse(ICON_PATH).getroot()
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        forbidden_elements = {"foreignObject", "image", "script", "use"}
        forbidden_schemes = ("data:", "file:", "http:", "https:", "javascript:")
        for element in root.iter():
            local_name = element.tag.rsplit("}", 1)[-1]
            self.assertNotIn(local_name, forbidden_elements)
            for value in element.attrib.values():
                normalized = value.casefold().replace(" ", "")
                self.assertFalse(
                    any(scheme in normalized for scheme in forbidden_schemes)
                )
                self.assertNotIn("url(", normalized)


if __name__ == "__main__":
    unittest.main()
