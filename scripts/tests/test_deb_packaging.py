from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from zipfile import ZipFile, ZipInfo

from scripts.build_deb_support import (
    DebBuildError,
    build_sbom,
    package_version,
    pep440_to_debian,
    render_control,
    unpack_runtime_wheels,
    verified_runtime_wheels,
)


REPOSITORY = Path(__file__).resolve().parents[2]
DEBIAN = REPOSITORY / "packaging" / "debian"


def write_wheel(
    path: Path,
    *,
    name: str,
    version: str,
    members: dict[str, bytes] | None = None,
) -> None:
    distribution = name.replace("-", "_")
    dist_info = f"{distribution}-{version}.dist-info"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        for member_name, content in (members or {}).items():
            archive.writestr(member_name, content)


def write_lock(path: Path, wheels: list[tuple[str, str, Path]]) -> None:
    lines = ["# synthetic lock used only by this unit test\n"]
    for name, version, wheel in wheels:
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        lines.extend(
            [
                f"{name}=={version} \\\n",
                f"    --hash=sha256:{digest}\n",
            ]
        )
    path.write_text("".join(lines), encoding="utf-8")


class DebianVersionTests(unittest.TestCase):
    def test_pep440_prereleases_sort_as_debian_prereleases(self) -> None:
        self.assertEqual(pep440_to_debian("0.1.0a6"), "0.1.0~alpha6-1")
        self.assertEqual(pep440_to_debian("1.2.3b2"), "1.2.3~beta2-1")
        self.assertEqual(pep440_to_debian("2.0.0rc1"), "2.0.0~rc1-1")
        self.assertEqual(pep440_to_debian("2.0.0"), "2.0.0-1")

    def test_version_mapping_rejects_unreviewed_pep440_forms(self) -> None:
        for value in ("1.0", "1.0.0.dev1", "1.0.0.post1", "01.0.0", "1!1.0.0"):
            with self.subTest(value=value), self.assertRaises(DebBuildError):
                pep440_to_debian(value)

    def test_package_version_validates_commit_provenance_without_embedding_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pyproject = Path(temporary) / "pyproject.toml"
            pyproject.write_text('[project]\nversion = "0.1.0a6"\n', encoding="utf-8")
            self.assertEqual(
                package_version(pyproject, "1788092310", "b7e0ecbcde30"),
                "0.1.0~alpha6-1",
            )
            with self.assertRaises(DebBuildError):
                package_version(pyproject, "now", "b7e0ecbcde30")
            with self.assertRaises(DebBuildError):
                package_version(pyproject, "1788092310", "not-a-commit")


class DebianWheelTests(unittest.TestCase):
    def test_locked_wheels_unpack_without_using_adjacent_project_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            runtime = wheelhouse / "example_runtime-1.2.3-py3-none-any.whl"
            write_wheel(
                runtime,
                name="example-runtime",
                version="1.2.3",
                members={"example_runtime/__init__.py": b"VALUE = 1\n"},
            )
            project = wheelhouse / "murmur_ime_voice-9.9-py3-none-any.whl"
            write_wheel(
                project,
                name="murmur-ime-voice",
                version="9.9",
                members={"murmur_voice/untrusted.py": b"UNTRUSTED = True\n"},
            )
            lock = root / "requirements.txt"
            write_lock(lock, [("example-runtime", "1.2.3", runtime)])

            self.assertEqual(verified_runtime_wheels(lock, wheelhouse), [runtime])
            output = root / "vendor"
            unpack_runtime_wheels(lock, wheelhouse, output)
            self.assertEqual(
                (output / "example_runtime/__init__.py").read_bytes(), b"VALUE = 1\n"
            )
            self.assertFalse((output / "murmur_voice").exists())

    def test_wheel_hash_and_unlocked_extras_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            runtime = wheelhouse / "example_runtime-1-py3-none-any.whl"
            write_wheel(runtime, name="example-runtime", version="1")
            lock = root / "requirements.txt"
            write_lock(lock, [("example-runtime", "1", runtime)])
            runtime.write_bytes(runtime.read_bytes() + b"tampered")
            with self.assertRaisesRegex(DebBuildError, "hash mismatch"):
                verified_runtime_wheels(lock, wheelhouse)

            runtime.unlink()
            write_wheel(runtime, name="example-runtime", version="1")
            write_lock(lock, [("example-runtime", "1", runtime)])
            extra = wheelhouse / "extra-1-py3-none-any.whl"
            write_wheel(extra, name="extra", version="1")
            with self.assertRaisesRegex(DebBuildError, "unlocked wheel"):
                verified_runtime_wheels(lock, wheelhouse)

    def test_wheel_symbolic_link_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            runtime = wheelhouse / "example_runtime-1-py3-none-any.whl"
            write_wheel(runtime, name="example-runtime", version="1")
            with ZipFile(runtime, "a") as archive:
                linked = ZipInfo("example_runtime/linked.py")
                linked.create_system = 3
                linked.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(linked, "target.py")
            lock = root / "requirements.txt"
            write_lock(lock, [("example-runtime", "1", runtime)])
            with self.assertRaisesRegex(DebBuildError, "symbolic link"):
                unpack_runtime_wheels(lock, wheelhouse, root / "vendor")


class DebianMetadataTests(unittest.TestCase):
    def test_appstream_metadata_presents_the_native_lightweight_ui(self) -> None:
        metadata = (
            DEBIAN / "io.github.SidUParis.OpenVoiceInputLinux.metainfo.xml"
        ).read_text(encoding="utf-8")
        self.assertIn("native GTK4 client is intentionally small", metadata)
        self.assertIn("原生 GTK4 客户端保持轻量", metadata)
        self.assertIn("docs/assets/settings-window.png", metadata)
        self.assertIn("中文优先的原生 GTK4 设置界面", metadata)
        self.assertIn('<release version="0.1.0-alpha.6" date="2026-09-01">', metadata)

    def test_builder_enforces_lightweight_package_budgets(self) -> None:
        builder = (REPOSITORY / "scripts/build-deb.sh").read_text(encoding="utf-8")
        self.assertIn("installed_size > 10 * 1024", builder)
        self.assertIn("package_bytes > 5 * 1024 * 1024", builder)
        self.assertIn("10 MiB lightweight-client budget", builder)
        self.assertIn("5 MiB lightweight-client budget", builder)

    def test_control_renderer_allows_maintainer_email_but_no_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "control"
            render_control(
                DEBIAN / "control.in",
                output,
                version="0.1.0~alpha6-1",
                installed_size="2048",
            )
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("Version: 0.1.0~alpha6-1", rendered)
            self.assertIn("@users.noreply.github.com", rendered)
            self.assertNotIn("@PACKAGE_VERSION@", rendered)
            self.assertNotIn("@INSTALLED_SIZE@", rendered)

    def test_sbom_is_deterministic_and_records_commit_and_wheel_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "example_runtime-1-py3-none-any.whl"
            write_wheel(wheel, name="example-runtime", version="1")
            lock = root / "requirements.txt"
            write_lock(lock, [("example-runtime", "1", wheel)])
            arguments = {
                "source_commit": "a" * 40,
                "source_epoch": "1788092310",
                "package_version_value": "0.1.0~alpha6-1",
            }
            first = build_sbom(lock, **arguments)
            second = build_sbom(lock, **arguments)
            self.assertEqual(
                json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
            )
            self.assertEqual(first["bomFormat"], "CycloneDX")
            properties = first["metadata"]["component"]["properties"]
            self.assertIn(
                {"name": "openvoiceinput:source-commit", "value": "a" * 40},
                properties,
            )
            component = first["components"][0]
            self.assertEqual(
                component["hashes"][0]["content"],
                hashlib.sha256(wheel.read_bytes()).hexdigest(),
            )


class DebianMaintainerScriptTests(unittest.TestCase):
    def run_script(
        self, script: str, *arguments: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if environment:
            env.update(environment)
        return subprocess.run(
            ["sh", str(DEBIAN / script), *arguments],
            cwd=REPOSITORY,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def test_preinst_refuses_standard_preview_shadow_without_reading_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit = root / "home/test/.config/systemd/user/murmur-ime-engine.service"
            unit.parent.mkdir(parents=True)
            unit.write_text("legacy\n", encoding="utf-8")
            private = root / "home/test/.config/murmur-ime/voice.json"
            private.parent.mkdir(parents=True)
            private.write_text('{"api_key":"must-remain"}\n', encoding="utf-8")
            private.chmod(0)

            result = self.run_script(
                "preinst", "install", environment={"DPKG_ROOT": str(root)}
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("source-preview installation", result.stderr)
            self.assertEqual(unit.read_text(encoding="utf-8"), "legacy\n")
            private.chmod(0o600)
            self.assertEqual(
                private.read_text(encoding="utf-8"), '{"api_key":"must-remain"}\n'
            )

    def test_preinst_accepts_root_without_preview_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_script(
                "preinst", "install", environment={"DPKG_ROOT": temporary}
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_lifecycle_calls_only_user_systemd_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            log = root / "calls.log"
            helper = fake_bin / "deb-systemd-invoke"
            helper.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    printf '%s\\n' "$*" >>"$MOCK_LOG"
                    """
                ),
                encoding="utf-8",
            )
            helper.chmod(0o755)
            env = {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "MOCK_LOG": str(log),
            }

            self.assertEqual(
                self.run_script("postinst", "configure", environment=env).returncode, 0
            )
            self.assertEqual(
                self.run_script(
                    "postinst", "configure", "0.1.0~alpha2-1", environment=env
                ).returncode,
                0,
            )
            self.assertEqual(
                self.run_script("prerm", "remove", environment=env).returncode, 0
            )
            self.assertEqual(
                self.run_script("postrm", "remove", environment=env).returncode, 0
            )
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                [
                    "--user daemon-reload",
                    "--user start murmur-ime-engine.service",
                    "--user daemon-reload",
                    "--user restart murmur-ime-engine.service murmur-ime-voice.service",
                    "--user stop murmur-ime-voice.service murmur-ime-engine.service",
                    "--user daemon-reload",
                ],
            )

    def test_maintainer_scripts_never_name_private_configuration(self) -> None:
        private_names = (
            "voice.json",
            "vocabulary.json",
            "corrections.json",
            "adaptive-corrections.json",
            "data-collection.json",
            "microphone-priority.json",
            "openvoiceinput-dataset-v1",
        )
        for name in ("preinst", "postinst", "prerm", "postrm"):
            content = (DEBIAN / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                self.assertFalse(any(private in content for private in private_names))


class DebianUnitTests(unittest.TestCase):
    def test_units_use_only_system_launchers_and_default_user_config_resolution(
        self,
    ) -> None:
        engine = (DEBIAN / "murmur-ime-engine.service").read_text(encoding="utf-8")
        voice = (DEBIAN / "murmur-ime-voice.service").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/bin/murmur-ime-engine", engine)
        self.assertIn("ExecStart=/usr/bin/murmur-voice-daemon run", voice)
        self.assertIn("ExecStopPost=/usr/bin/murmur-voice-daemon restore-engine", voice)
        self.assertNotIn("%h", voice)
        self.assertNotIn(".config", voice)


if __name__ == "__main__":
    unittest.main()
