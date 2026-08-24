from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.verify_preview_bundle import (
    VerificationError,
    extract_archive,
    verify_bundle_shape,
    verify_manifest,
)


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


class PreviewBundleTests(unittest.TestCase):
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

    def test_bundle_shape_rejects_local_configuration(self) -> None:
        for forbidden in ("voice.json", "vocabulary.json", "corrections.json"):
            with (
                self.subTest(forbidden=forbidden),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                for relative in (
                    "BUNDLE-INFO",
                    "LICENSE",
                    "README.md",
                    "packaging/open-voice-input-settings",
                    "scripts/install-user.sh",
                    "scripts/uninstall-user.sh",
                    "voice/pyproject.toml",
                    "wheelhouse/murmur_ime_voice-0.1.0-py3-none-any.whl",
                    forbidden,
                ):
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()

                with self.assertRaisesRegex(VerificationError, "local configuration"):
                    verify_bundle_shape(root)


if __name__ == "__main__":
    unittest.main()
