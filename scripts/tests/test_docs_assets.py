from __future__ import annotations

import struct
import unittest
import zlib
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCREENSHOT = REPOSITORY / "docs/assets/settings-window.png"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class DocumentationAssetTests(unittest.TestCase):
    def test_settings_screenshot_is_bounded_and_metadata_free(self) -> None:
        self.assertTrue(SCREENSHOT.is_file())
        self.assertFalse(SCREENSHOT.is_symlink())
        data = SCREENSHOT.read_bytes()
        self.assertLess(len(data), 200_000)
        self.assertEqual(data[:8], PNG_SIGNATURE)

        offset = len(PNG_SIGNATURE)
        chunks: list[tuple[bytes, bytes]] = []
        while offset < len(data):
            self.assertGreaterEqual(len(data) - offset, 12)
            length = struct.unpack(">I", data[offset : offset + 4])[0]
            chunk_end = offset + 12 + length
            self.assertLessEqual(
                chunk_end,
                len(data),
                "PNG chunk extends beyond the end of the screenshot",
            )
            kind = data[offset + 4 : offset + 8]
            payload = data[offset + 8 : offset + 8 + length]
            checksum = struct.unpack(
                ">I", data[offset + 8 + length : offset + 12 + length]
            )[0]
            self.assertEqual(checksum, zlib.crc32(kind + payload) & 0xFFFFFFFF)
            chunks.append((kind, payload))
            offset = chunk_end

        self.assertEqual(offset, len(data))
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], b"IHDR")
        self.assertEqual(chunks[-1][0], b"IEND")
        self.assertEqual(len(chunks[0][1]), 13)
        self.assertEqual(chunks[-1][1], b"")
        self.assertTrue(all(kind in {b"IHDR", b"IDAT", b"IEND"} for kind, _ in chunks))
        width, height, depth, colour, compression, filtering, interlace = struct.unpack(
            ">IIBBBBB", chunks[0][1]
        )
        self.assertEqual((width, height), (620, 760))
        self.assertEqual((depth, colour), (8, 2))
        self.assertEqual((compression, filtering, interlace), (0, 0, 0))

    def test_user_documents_embed_the_sanitized_screenshot(self) -> None:
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        chinese = (REPOSITORY / "docs/README.zh-CN.md").read_text(encoding="utf-8")
        self.assertIn("docs/assets/settings-window.png", readme)
        self.assertIn("assets/settings-window.png", chinese)
        self.assertIn("empty temporary profile", readme)
        self.assertIn("空临时配置", chinese)

    def test_remote_dataset_guide_keeps_mount_and_backup_boundaries_explicit(
        self,
    ) -> None:
        guide = (REPOSITORY / "docs/remote-dataset-storage.md").read_text(
            encoding="utf-8"
        )
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        chinese = (REPOSITORY / "docs/README.zh-CN.md").read_text(encoding="utf-8")

        self.assertIn("Collection remains off", guide)
        self.assertIn("teacher-unreviewed", guide)
        self.assertIn("no fallback local spool", guide)
        self.assertIn("mountpoint -q", guide)
        self.assertIn("guarantee the modes stored on the remote server", guide)
        self.assertIn("sftp_server=/usr/lib/openssh/sftp-server -u 077", guide)
        self.assertIn("remote host itself", guide)
        self.assertIn("`rclone mount` as the live collection destination", guide)
        self.assertIn("rclone copy", guide)
        self.assertIn("--exclude '/.pending/**'", guide)
        self.assertIn("browser OAuth consent", guide)
        self.assertIn("remote-dataset-storage.md", readme)
        self.assertIn("remote-dataset-storage.md", chinese)


if __name__ == "__main__":
    unittest.main()
