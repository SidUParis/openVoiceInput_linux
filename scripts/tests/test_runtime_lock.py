from __future__ import annotations

import re
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
LOCK_ENTRY = re.compile(
    r"(?m)^([a-z0-9-]+)==([^\s\\]+) \\\n"
    r"    --hash=sha256:([0-9a-f]{64})$"
)


class PreviewRuntimeLockTests(unittest.TestCase):
    def test_runtime_lock_has_exact_target_wheels(self) -> None:
        lock = (
            REPOSITORY / "packaging/requirements-preview-ubuntu-24.04-x86_64-cp312.txt"
        ).read_text(encoding="utf-8")
        entries = {
            name: (version, digest)
            for name, version, digest in LOCK_ENTRY.findall(lock)
        }
        self.assertEqual(
            entries,
            {
                "sounddevice": (
                    "0.5.6",
                    "de099612311ad81e55d31ccbd83f43ea6bf4d87b48f9b6ea55a1fbcde0eee4e0",
                ),
                "websockets": (
                    "17.0.1",
                    "f47b0815af3948ec6a440b3afa02f05b18cc0939549e91b5c677b5d9c2c8472a",
                ),
                "cffi": (
                    "2.1.1",
                    "c1453022f490d2459a11819d83ad1d586e9ff65a12ac3e705ffebd46d3685dcf",
                ),
                "pycparser": (
                    "3.0",
                    "b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992",
                ),
            },
        )

    def test_build_backend_is_separately_pinned_and_hashed(self) -> None:
        lock = (
            REPOSITORY / "packaging/requirements-preview-build-cp312.txt"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            LOCK_ENTRY.findall(lock),
            [
                (
                    "setuptools",
                    "83.0.0",
                    "29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3",
                )
            ],
        )

    def test_builder_enforces_hashes_binary_wheels_and_fixed_target(self) -> None:
        script = (REPOSITORY / "scripts/build-preview-bundle.sh").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(script.count("--require-hashes"), 2)
        self.assertGreaterEqual(script.count("--only-binary=:all:"), 2)
        self.assertGreaterEqual(script.count("-m pip --isolated"), 4)
        self.assertIn("${VERSION_ID:-} != 24.04", script)
        self.assertIn("${python_identity[0]} != CPython", script)
        self.assertIn("${python_identity[1]} != 3.12", script)
        self.assertIn("--no-build-isolation", script)
        self.assertIn('SOURCE_DATE_EPOCH="$source_epoch"', script)
        self.assertIn("umask 022", script)
        self.assertNotIn("PYTHONHASHSEED", script)
        self.assertIn("unset PYTHONHOME PYTHONPATH", script)
        self.assertNotRegex(script, r'(?m)^\s*"\$build_python" (?!-I(?: |$))')
        self.assertNotRegex(
            script,
            r'(?m)^\s*"\$build_environment/bin/python" (?!-I(?: |$))',
        )
        self.assertIn("if observed != locked:", script)
        self.assertIn(
            "The disposable build environment unexpectedly contains setuptools",
            script,
        )

    def test_builder_publishes_artifacts_without_clobbering_late_arrivals(self) -> None:
        script = (REPOSITORY / "scripts/build-preview-bundle.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('>"$archive_path"', script)
        self.assertNotIn('>"$archive_checksum"', script)
        self.assertGreaterEqual(script.count("move-no-clobber"), 2)
        self.assertIn('mktemp "$output_dir/.${bundle_name}.tar.gz.tmp.', script)
        self.assertIn("os.fsync(descriptor)", script)


if __name__ == "__main__":
    unittest.main()
