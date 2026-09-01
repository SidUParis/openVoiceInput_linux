from __future__ import annotations

import hashlib
import inspect
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify_preview_bundle import (
    VerificationError,
    _offline_environment,
    extract_archive,
    install_explicit_wheels,
    main as verify_main,
    verify_bundle_shape,
    verify_host_target,
    verify_install_wheelhouse,
    verify_installed_wheel_sources,
    verify_manifest,
    verify_offline_wheel_install,
    verify_project_wheel_source,
    verify_runtime_lock,
    verify_sbom,
)
from scripts.generate_preview_sbom import build_sbom, read_wheelhouse, render_sbom
from scripts.tests.test_preview_sbom import write_test_bundle, write_test_wheel


def write_manifest(root: Path) -> None:
    entries = []
    for path in sorted(
        candidate for candidate in root.rglob("*") if candidate.is_file()
    ):
        if path.name == "SHA256SUMS":
            continue
        relative = path.relative_to(root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(f"{digest}  {relative}\n")
    (root / "SHA256SUMS").write_text("".join(entries), encoding="utf-8")


def write_runtime_lock(root: Path) -> None:
    sbom = build_sbom(root)
    components = sbom["components"]
    assert isinstance(components, list)
    lines = ["# Test runtime lock.\n\n"]
    for component in components:
        lines.append(
            f"{component['name']}=={component['version']} \\\n"
            f"    --hash=sha256:{component['hashes'][0]['content']}\n"
        )
    path = root / "packaging/requirements-preview-ubuntu-24.04-x86_64-cp312.txt"
    path.parent.mkdir()
    path.write_text("".join(lines), encoding="utf-8")


class PreviewBundleTests(unittest.TestCase):
    def test_offline_install_subprocesses_use_isolated_python(self) -> None:
        source = inspect.getsource(verify_offline_wheel_install)
        self.assertIn('sys.executable,\n                "-I",', source)
        self.assertGreaterEqual(source.count('"-I",'), 3)
        self.assertIn("cwd=venv", source)

    def test_manifest_covers_exact_payload_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            payload = root / "nested/payload.txt"
            payload.write_text("original\n", encoding="utf-8")
            write_manifest(root)

            verify_manifest(root)
            payload.write_text("tampered\n", encoding="utf-8")

            with self.assertRaisesRegex(VerificationError, "SHA256 mismatch"):
                verify_manifest(root)

    def test_manifest_rejects_an_unlisted_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            write_manifest(root)
            (root / "local.token").write_text("not-a-secret\n", encoding="utf-8")

            with self.assertRaisesRegex(VerificationError, "file set differs"):
                verify_manifest(root)

    def test_manifest_rejects_an_unlisted_sbom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            write_manifest(root)
            (root / "SBOM.cdx.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(VerificationError, "file set differs"):
                verify_manifest(root)

    def test_sbom_rejects_component_hash_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root)
            sbom = build_sbom(root)
            (root / "SBOM.cdx.json").write_bytes(render_sbom(sbom))
            verify_sbom(root)
            components = sbom["components"]
            assert isinstance(components, list)
            components[0]["hashes"][0]["content"] = "0" * 64
            (root / "SBOM.cdx.json").write_text(
                json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(VerificationError, "does not exactly match"):
                verify_sbom(root)

    def test_sbom_rejects_noncanonical_or_duplicate_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root)
            sbom = build_sbom(root)
            (root / "SBOM.cdx.json").write_text(
                json.dumps(sbom, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(VerificationError, "canonical JSON"):
                verify_sbom(root)

            (root / "SBOM.cdx.json").write_text(
                '{"bomFormat":"CycloneDX","bomFormat":"CycloneDX"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(VerificationError, "duplicate JSON key"):
                verify_sbom(root)

    def test_runtime_lock_must_match_sbom_component_versions_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root)
            write_runtime_lock(root)

            verify_runtime_lock(root)
            lock = root / (
                "packaging/requirements-preview-ubuntu-24.04-x86_64-cp312.txt"
            )
            lock.write_text(
                lock.read_text(encoding="utf-8").replace(
                    "websockets==17.0.1", "websockets==13.0"
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(VerificationError, "version/hash lock"):
                verify_runtime_lock(root)

    def test_fixed_runtime_lock_rejects_a_different_declared_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root)
            write_runtime_lock(root)
            info = root / "BUNDLE-INFO"
            info.write_text(
                info.read_text(encoding="utf-8").replace(
                    "ubuntu-24.04-x86_64-py3.12",
                    "ubuntu-22.04-x86_64-py3.12",
                ),
                encoding="utf-8",
            )
            (root / "SBOM.cdx.json").write_bytes(render_sbom(build_sbom(root)))

            with self.assertRaisesRegex(VerificationError, "runtime lock target"):
                verify_runtime_lock(root)

    def test_host_must_match_the_declared_preview_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root)
            matching = {
                "implementation": "cpython",
                "python_version": "3.12",
                "machine": "x86_64",
                "pointer_bits": 64,
                "os_id": "ubuntu",
                "os_version": "24.04",
            }
            verify_host_target(root, **matching)
            for field, value in (
                ("implementation", "pypy"),
                ("python_version", "3.13"),
                ("machine", "aarch64"),
                ("pointer_bits", 32),
                ("os_id", "debian"),
                ("os_version", "22.04"),
            ):
                mismatch = dict(matching)
                mismatch[field] = value
                with (
                    self.subTest(field=field),
                    self.assertRaisesRegex(VerificationError, "host does not match"),
                ):
                    verify_host_target(root, **mismatch)

    def test_install_wheelhouse_rejects_extra_missing_or_wrong_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            root.mkdir()
            write_test_bundle(root)
            write_runtime_lock(root)
            (root / "SBOM.cdx.json").write_bytes(render_sbom(build_sbom(root)))
            custom = Path(temporary) / "custom wheelhouse %literal"
            shutil.copytree(root / "wheelhouse", custom)

            expected = verify_install_wheelhouse(root, custom)
            self.assertEqual(
                [path.name for path in expected],
                sorted(path.name for path in custom.iterdir()),
            )
            self.assertEqual(
                verify_main(
                    [
                        "--bundle-root",
                        str(root),
                        "--check-install-wheelhouse",
                        str(custom),
                    ]
                ),
                0,
            )

            extra = custom / "extra-1.0-py3-none-any.whl"
            extra.write_bytes(b"extra")
            with self.assertRaisesRegex(VerificationError, "file set and hashes"):
                verify_install_wheelhouse(root, custom)

            extra.unlink()

            missing = custom / "websockets-17.0.1-py3-none-any.whl"
            original = missing.read_bytes()
            missing.unlink()
            with self.assertRaisesRegex(VerificationError, "file set and hashes"):
                verify_install_wheelhouse(root, custom)
            missing.write_bytes(original)

            missing.write_bytes(original + b"tampered")
            with self.assertRaisesRegex(VerificationError, "file set and hashes"):
                verify_install_wheelhouse(root, custom)

    def test_project_wheel_code_must_match_verified_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root, project_module_content=b"untrusted = True\n")
            with self.assertRaisesRegex(VerificationError, "package bytes"):
                verify_project_wheel_source(root)

    def test_explicit_install_shadows_preinstalled_satisfying_runtime_packages(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "bundle with %literal space"
            root.mkdir()
            write_test_bundle(root)
            _, components, _ = read_wheelhouse(root)
            environment = _offline_environment()
            venv = base / "venv"
            subprocess.run(
                [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
                check=True,
                env=environment,
            )
            python = venv / "bin/python"

            old_wheelhouse = base / "old-system-site-wheels"
            old_wheelhouse.mkdir()
            old_wheels = [
                write_test_wheel(
                    old_wheelhouse,
                    filename="sounddevice-0.4.6-py3-none-any.whl",
                    name="sounddevice",
                    version="0.4.6",
                    requirements=("cffi",),
                ),
                write_test_wheel(
                    old_wheelhouse,
                    filename="websockets-13.0-py3-none-any.whl",
                    name="websockets",
                    version="13.0",
                    license_expression="BSD-3-Clause",
                ),
                write_test_wheel(
                    old_wheelhouse,
                    filename="cffi-1.17.0-py3-none-any.whl",
                    name="cffi",
                    version="1.17.0",
                    license_expression="MIT-0",
                    requirements=("pycparser",),
                ),
                write_test_wheel(
                    old_wheelhouse,
                    filename="pycparser-2.22-py3-none-any.whl",
                    name="pycparser",
                    version="2.22",
                    license_expression="BSD-3-Clause",
                ),
            ]
            simulated_system_site = base / "simulated-system-site"
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-index",
                    "--no-deps",
                    "--target",
                    str(simulated_system_site),
                    *(str(wheel) for wheel in old_wheels),
                ],
                check=True,
                env=environment,
                stdout=subprocess.DEVNULL,
            )
            purelib = Path(
                subprocess.run(
                    [
                        str(python),
                        "-c",
                        "import sysconfig; print(sysconfig.get_path('purelib'))",
                    ],
                    check=True,
                    env=environment,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.strip()
            )
            (purelib / "system-site-simulation.pth").write_text(
                f"{simulated_system_site}\n", encoding="utf-8"
            )
            old_versions = subprocess.run(
                [
                    str(python),
                    "-c",
                    "from importlib.metadata import version; "
                    "print(' '.join(version(name) for name in "
                    "('sounddevice','websockets','cffi','pycparser')))",
                ],
                check=True,
                env=environment,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            self.assertEqual(old_versions, "0.4.6 13.0 1.17.0 2.22")

            staged_wheelhouse = base / "private staged wheelhouse"
            shutil.copytree(root / "wheelhouse", staged_wheelhouse)
            wheels = [
                staged_wheelhouse / component.filename
                for component in sorted(
                    components.values(), key=lambda item: item.filename
                )
            ]
            environment["PIP_DRY_RUN"] = "1"
            environment["PIP_TARGET"] = str(base / "hostile-pip-target")
            install_explicit_wheels(python, wheels, environment)
            installed = verify_installed_wheel_sources(
                root,
                venv,
                components,
                environment,
                module_names={
                    "murmur-ime-voice": "murmur_voice",
                    "sounddevice": "sounddevice",
                    "websockets": "websockets",
                    "cffi": "cffi",
                    "pycparser": "pycparser",
                },
                wheelhouse=staged_wheelhouse,
            )

            self.assertEqual(
                {name: record["version"] for name, record in installed.items()},
                {name: component.version for name, component in components.items()},
            )
            self.assertTrue(
                all(
                    purelib.resolve()
                    in Path(str(record["module_origin"])).resolve().parents
                    for record in installed.values()
                )
            )

    def test_installed_inventory_rejects_site_root_outside_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            root.mkdir()
            write_test_bundle(root)
            _, components, _ = read_wheelhouse(root)
            inventory = json.dumps(
                {"site_roots": ["/usr/lib/python3/dist-packages"], "records": {}}
            )

            with (
                patch(
                    "scripts.verify_preview_bundle.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, inventory),
                ),
                self.assertRaisesRegex(VerificationError, "outside the staged venv"),
            ):
                verify_installed_wheel_sources(
                    root, Path(temporary) / "venv", components, {}
                )

    def test_imported_module_must_be_owned_by_its_distribution_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            root.mkdir()
            write_test_bundle(root)
            _, components, _ = read_wheelhouse(root)
            venv = Path(temporary) / "venv"
            site = venv / "lib/python3.12/site-packages"
            records = {}
            for name, component in components.items():
                module = site / f"{name}-imported.py"
                records[name] = [
                    {
                        "version": component.version,
                        "location": str(site),
                        "files": [str(site / f"{name}-owned.py")],
                        "module_origin": str(module),
                        "direct_url": {
                            "url": (root / "wheelhouse" / component.filename)
                            .resolve()
                            .as_uri(),
                            "archive_info": {"hashes": {"sha256": component.sha256}},
                        },
                    }
                ]
            inventory = json.dumps({"site_roots": [str(site)], "records": records})

            with (
                patch(
                    "scripts.verify_preview_bundle.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, inventory),
                ),
                self.assertRaisesRegex(VerificationError, "not owned"),
            ):
                verify_installed_wheel_sources(root, venv, components, {})

    def test_extractor_rejects_parent_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive = base / "unsafe.tar.gz"
            with tarfile.open(archive, mode="w:gz") as output:
                info = tarfile.TarInfo("preview/../../escape")
                content = b"escape\n"
                info.size = len(content)
                output.addfile(info, io.BytesIO(content))

            with self.assertRaisesRegex(VerificationError, "unsafe archive path"):
                extract_archive(archive, base / "extract")

    def test_extractor_rejects_noncanonical_path_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive = base / "ambiguous.tar.gz"
            with tarfile.open(archive, mode="w:gz") as output:
                for name in ("preview/value", "preview/./value"):
                    info = tarfile.TarInfo(name)
                    content = name.encode("utf-8")
                    info.size = len(content)
                    output.addfile(info, io.BytesIO(content))

            with self.assertRaisesRegex(VerificationError, "unsafe archive path"):
                extract_archive(archive, base / "extract")

    def test_bundle_shape_rejects_local_configuration(self) -> None:
        for forbidden in (
            "voice.json",
            "vocabulary.json",
            "corrections.json",
            "adaptive-corrections.json",
            "data-collection.json",
            "interaction.json",
            "microphone-priority.json",
            "output-style.json",
        ):
            with (
                self.subTest(forbidden=forbidden),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                for relative in (
                    "BUNDLE-INFO",
                    "LICENSE",
                    "README.md",
                    "SBOM.cdx.json",
                    "packaging/desktop/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop.in",
                    "packaging/icons/io.github.SidUParis.OpenVoiceInputLinux.Settings.svg",
                    "packaging/open-voice-input-settings",
                    "packaging/requirements-preview-ubuntu-24.04-x86_64-cp312.txt",
                    "scripts/generate_preview_sbom.py",
                    "scripts/render_desktop_entry.py",
                    "scripts/install-user.sh",
                    "scripts/uninstall-user.sh",
                    "scripts/verify_preview_bundle.py",
                    "voice/pyproject.toml",
                    "wheelhouse/murmur_ime_voice-0.1.0a7-py3-none-any.whl",
                    forbidden,
                ):
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()

                with self.assertRaisesRegex(VerificationError, "local configuration"):
                    verify_bundle_shape(root)

    def test_bundle_shape_rejects_personal_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in (
                "BUNDLE-INFO",
                "LICENSE",
                "README.md",
                "SBOM.cdx.json",
                "packaging/desktop/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop.in",
                "packaging/icons/io.github.SidUParis.OpenVoiceInputLinux.Settings.svg",
                "packaging/open-voice-input-settings",
                "packaging/requirements-preview-ubuntu-24.04-x86_64-cp312.txt",
                "scripts/generate_preview_sbom.py",
                "scripts/render_desktop_entry.py",
                "scripts/install-user.sh",
                "scripts/uninstall-user.sh",
                "scripts/verify_preview_bundle.py",
                "voice/pyproject.toml",
                "wheelhouse/murmur_ime_voice-0.1.0a7-py3-none-any.whl",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            leaked = root / "openvoiceinput-dataset-v1/utterances/private/audio.wav"
            leaked.parent.mkdir(parents=True)
            leaked.write_bytes(b"private audio")

            with self.assertRaisesRegex(VerificationError, "personal dataset leaked"):
                verify_bundle_shape(root)


if __name__ == "__main__":
    unittest.main()
