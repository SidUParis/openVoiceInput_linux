from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.generate_preview_sbom import (
    SBOMError,
    _safe_wheel_member,
    build_sbom,
    main,
    read_wheelhouse,
    render_sbom,
)


def _record_hash(content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
    return f"sha256={digest.rstrip(b'=').decode('ascii')}"


def write_test_wheel(
    wheelhouse: Path,
    *,
    filename: str,
    name: str,
    version: str,
    license_expression: str = "MIT",
    requirements: tuple[str, ...] = (),
    corrupt_record: bool = False,
    module_name: str | None = None,
    module_content: bytes = b"",
    extra_files: dict[str, bytes] | None = None,
) -> Path:
    distribution = name.replace("-", "_")
    dist_info = f"{distribution}-{version}.dist-info"
    metadata_lines = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        f"License-Expression: {license_expression}",
    ]
    metadata_lines.extend(
        f"Requires-Dist: {requirement}" for requirement in requirements
    )
    metadata = ("\n".join(metadata_lines) + "\n\nTest wheel.\n").encode()
    wheel = b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files = {
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": wheel,
        f"{module_name or distribution}/__init__.py": module_content,
    }
    if extra_files is not None:
        files.update(extra_files)
    record_stream = io.StringIO(newline="")
    writer = csv.writer(record_stream, lineterminator="\n")
    for path, content in sorted(files.items()):
        digest = _record_hash(content)
        if corrupt_record and path.endswith("/METADATA"):
            digest = _record_hash(b"different")
        writer.writerow((path, digest, len(content)))
    record_path = f"{dist_info}/RECORD"
    writer.writerow((record_path, "", ""))
    files[record_path] = record_stream.getvalue().encode()

    path = wheelhouse / filename
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member, content in sorted(files.items()):
            archive.writestr(member, content)
    return path


def write_test_bundle(root: Path, *, project_module_content: bytes = b"") -> None:
    (root / "BUNDLE-INFO").write_text(
        "source_commit=0123456789abcdef0123456789abcdef01234567\n"
        "target=ubuntu-24.04-x86_64-py3.12\n"
        "python=Python 3.12.3\n",
        encoding="utf-8",
    )
    voice = root / "voice"
    package = voice / "murmur_voice"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"")
    (voice / "LICENSE").write_bytes(b"test project license\n")
    (voice / "NOTICE.md").write_bytes(b"test project notice\n")
    (voice / "pyproject.toml").write_text(
        "[project]\n"
        'name = "murmur-ime-voice"\n'
        'version = "0.1.0a1"\n'
        "[project.scripts]\n"
        'murmur-voice-daemon = "murmur_voice.cli:main"\n'
        'open-voice-input-settings = "murmur_voice.settings_app:main"\n',
        encoding="utf-8",
    )
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir()
    project_dist_info = "murmur_ime_voice-0.1.0a1.dist-info"
    write_test_wheel(
        wheelhouse,
        filename="murmur_ime_voice-0.1.0a1-py3-none-any.whl",
        name="murmur-ime-voice",
        version="0.1.0a1",
        license_expression="GPL-3.0-only",
        module_name="murmur_voice",
        module_content=project_module_content,
        extra_files={
            f"{project_dist_info}/entry_points.txt": (
                b"[console_scripts]\n"
                b"murmur-voice-daemon = murmur_voice.cli:main\n"
                b"open-voice-input-settings = murmur_voice.settings_app:main\n"
            ),
            f"{project_dist_info}/top_level.txt": b"murmur_voice\n",
            f"{project_dist_info}/licenses/LICENSE": b"test project license\n",
            f"{project_dist_info}/licenses/NOTICE.md": b"test project notice\n",
        },
        requirements=(
            'sounddevice>=0.4.6,<1; python_version >= "3.12"',
            "websockets>=13,<18",
            'not-installed; extra == "test"',
        ),
    )
    write_test_wheel(
        wheelhouse,
        filename="sounddevice-0.5.6-py3-none-any.whl",
        name="sounddevice",
        version="0.5.6",
        requirements=("cffi",),
    )
    write_test_wheel(
        wheelhouse,
        filename="websockets-17.0.1-py3-none-any.whl",
        name="websockets",
        version="17.0.1",
        license_expression="BSD-3-Clause",
    )
    write_test_wheel(
        wheelhouse,
        filename="cffi-2.1.1-py3-none-any.whl",
        name="cffi",
        version="2.1.1",
        license_expression="MIT-0",
        requirements=('pycparser; implementation_name != "pypy"',),
    )
    write_test_wheel(
        wheelhouse,
        filename="pycparser-3.0-py3-none-any.whl",
        name="pycparser",
        version="3.0",
        license_expression="BSD-3-Clause",
    )


class PreviewSBOMTests(unittest.TestCase):
    def test_canonical_wheel_directory_entries_are_allowed(self) -> None:
        self.assertEqual(
            _safe_wheel_member("package/", is_directory=True).as_posix(),
            "package",
        )
        with self.assertRaisesRegex(SBOMError, "unsafe wheel member"):
            _safe_wheel_member("package/./", is_directory=True)

    def test_deterministic_cyclonedx_inventory_and_dependency_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root)

            first = build_sbom(root)
            second = build_sbom(root)

            self.assertEqual(first, second)
            self.assertEqual(render_sbom(first), render_sbom(second))
            self.assertEqual(first["bomFormat"], "CycloneDX")
            self.assertEqual(first["specVersion"], "1.5")
            self.assertNotIn("timestamp", json.dumps(first))
            metadata = first["metadata"]
            assert isinstance(metadata, dict)
            project = metadata["component"]
            assert isinstance(project, dict)
            self.assertEqual(project["name"], "murmur-ime-voice")
            self.assertEqual(
                project["hashes"][0]["content"],
                hashlib.sha256(
                    (
                        root / "wheelhouse/murmur_ime_voice-0.1.0a1-py3-none-any.whl"
                    ).read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(
                metadata["properties"],
                [
                    {
                        "name": "openvoice:source-commit",
                        "value": "0123456789abcdef0123456789abcdef01234567",
                    },
                    {
                        "name": "openvoice:target",
                        "value": "ubuntu-24.04-x86_64-py3.12",
                    },
                    {"name": "openvoice:python", "value": "Python 3.12.3"},
                ],
            )
            components = first["components"]
            assert isinstance(components, list)
            self.assertEqual(
                [item["name"] for item in components],
                ["cffi", "pycparser", "sounddevice", "websockets"],
            )
            all_components = [project, *components]
            self.assertEqual(len(all_components), 5)
            self.assertTrue(
                all(
                    item["purl"].startswith("pkg:pypi/")
                    and item["hashes"][0]["alg"] == "SHA-256"
                    and len(item["hashes"][0]["content"]) == 64
                    and item["licenses"]
                    for item in all_components
                )
            )
            graph = {
                entry["ref"]: entry["dependsOn"] for entry in first["dependencies"]
            }
            self.assertEqual(
                graph["pkg:pypi/murmur-ime-voice@0.1.0a1"],
                [
                    "pkg:pypi/sounddevice@0.5.6",
                    "pkg:pypi/websockets@17.0.1",
                ],
            )
            self.assertEqual(
                graph["pkg:pypi/sounddevice@0.5.6"],
                ["pkg:pypi/cffi@2.1.1"],
            )
            self.assertEqual(
                graph["pkg:pypi/cffi@2.1.1"],
                ["pkg:pypi/pycparser@3.0"],
            )
            self.assertEqual(graph["pkg:pypi/pycparser@3.0"], [])
            self.assertEqual(graph["pkg:pypi/websockets@17.0.1"], [])

            original_serial = first["serialNumber"]
            bundle_info = root / "BUNDLE-INFO"
            bundle_info.write_text(
                bundle_info.read_text(encoding="utf-8").replace(
                    "Python 3.12.3", "Python 3.12.4"
                ),
                encoding="utf-8",
            )
            patched_python = build_sbom(root)
            self.assertNotEqual(patched_python["serialNumber"], original_serial)

    def test_generator_cli_writes_only_the_fixed_bundle_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root)
            output = root / "SBOM.cdx.json"

            self.assertEqual(
                main(["--bundle-root", str(root), "--output", str(output)]),
                0,
            )
            self.assertEqual(output.read_bytes(), render_sbom(build_sbom(root)))
            self.assertEqual(
                main(["--bundle-root", str(root), "--output", str(output)]),
                2,
            )

    def test_missing_active_dependency_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root)
            (root / "wheelhouse/pycparser-3.0-py3-none-any.whl").unlink()

            with self.assertRaisesRegex(
                SBOMError, "missing runtime dependency 'pycparser'"
            ):
                read_wheelhouse(root)

    def test_unrelated_wheel_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root)
            write_test_wheel(
                root / "wheelhouse",
                filename="surplus-1.0-py3-none-any.whl",
                name="surplus",
                version="1.0",
            )

            with self.assertRaisesRegex(SBOMError, "unrelated runtime wheels"):
                read_wheelhouse(root)

    def test_noncanonical_wheel_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root)
            project_wheel = root / (
                "wheelhouse/murmur_ime_voice-0.1.0a1-py3-none-any.whl"
            )
            with zipfile.ZipFile(project_wheel, mode="a") as archive:
                archive.writestr("murmur_voice/./alias.py", b"")

            with self.assertRaisesRegex(SBOMError, "unsafe wheel member"):
                read_wheelhouse(root)

    def test_unsatisfied_dependency_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root)
            wheel = root / "wheelhouse/sounddevice-0.5.6-py3-none-any.whl"
            wheel.unlink()
            write_test_wheel(
                root / "wheelhouse",
                filename="sounddevice-1.1-py3-none-any.whl",
                name="sounddevice",
                version="1.1",
                requirements=("cffi",),
            )

            with self.assertRaisesRegex(SBOMError, "does not satisfy"):
                read_wheelhouse(root)

    def test_record_tampering_is_rejected_before_metadata_is_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_bundle(root)
            wheel = root / "wheelhouse/websockets-17.0.1-py3-none-any.whl"
            wheel.unlink()
            write_test_wheel(
                root / "wheelhouse",
                filename=wheel.name,
                name="websockets",
                version="17.0.1",
                license_expression="BSD-3-Clause",
                corrupt_record=True,
            )

            with self.assertRaisesRegex(SBOMError, "RECORD digest mismatch"):
                read_wheelhouse(root)


if __name__ == "__main__":
    unittest.main()
