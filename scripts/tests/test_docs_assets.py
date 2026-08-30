from __future__ import annotations

import struct
import unittest
import zlib
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCREENSHOT = REPOSITORY / "docs/assets/settings-window.png"
HERO_ANIMATION = REPOSITORY / "docs/assets/hero-demo.gif"
HERO_POSTER = REPOSITORY / "docs/assets/hero-demo-poster.png"
SOCIAL_PREVIEW = REPOSITORY / "docs/assets/social-preview.png"
HERO_NOTES = REPOSITORY / "docs/assets/hero-demo.md"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class DocumentationAssetTests(unittest.TestCase):
    @staticmethod
    def _png_dimensions(path: Path) -> tuple[int, int]:
        data = path.read_bytes()
        if data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
            raise AssertionError(f"not a canonical PNG: {path}")
        return struct.unpack(">II", data[16:24])

    @staticmethod
    def _gif_timing(path: Path) -> tuple[int, int]:
        data = path.read_bytes()
        if data[:6] not in {b"GIF87a", b"GIF89a"}:
            raise AssertionError(f"not a GIF: {path}")
        packed = data[10]
        offset = 13
        if packed & 0x80:
            offset += 3 * (2 ** ((packed & 0x07) + 1))

        frame_count = 0
        duration_centiseconds = 0

        def skip_sub_blocks(position: int) -> int:
            while True:
                size = data[position]
                position += 1
                if size == 0:
                    return position
                position += size

        while offset < len(data):
            marker = data[offset]
            offset += 1
            if marker == 0x3B:  # trailer
                break
            if marker == 0x21:  # extension
                label = data[offset]
                offset += 1
                if label == 0xF9:  # graphic control extension
                    block_size = data[offset]
                    offset += 1
                    if block_size != 4:
                        raise AssertionError("invalid GIF graphic control block")
                    duration_centiseconds += struct.unpack(
                        "<H", data[offset + 1 : offset + 3]
                    )[0]
                    offset += block_size
                    if data[offset] != 0:
                        raise AssertionError("unterminated GIF graphic control block")
                    offset += 1
                else:
                    offset = skip_sub_blocks(offset)
                continue
            if marker != 0x2C:  # image descriptor
                raise AssertionError(f"unexpected GIF marker: 0x{marker:02x}")

            frame_count += 1
            descriptor_packed = data[offset + 8]
            offset += 9
            if descriptor_packed & 0x80:
                offset += 3 * (2 ** ((descriptor_packed & 0x07) + 1))
            offset += 1  # LZW minimum code size
            offset = skip_sub_blocks(offset)

        return frame_count, duration_centiseconds

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
        self.assertEqual((width, height), (900, 820))
        self.assertEqual((depth, colour), (8, 2))
        self.assertEqual((compression, filtering, interlace), (0, 0, 0))

    def test_user_documents_embed_the_sanitized_screenshot(self) -> None:
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        english = (REPOSITORY / "README.en.md").read_text(encoding="utf-8")
        chinese = (REPOSITORY / "docs/README.zh-CN.md").read_text(encoding="utf-8")
        self.assertIn("docs/assets/settings-window.png", readme)
        self.assertIn("docs/assets/settings-window.png", english)
        self.assertIn("assets/settings-window.png", chinese)
        self.assertIn("空临时配置", readme)
        self.assertIn("empty temporary profile", english)
        self.assertIn("空临时配置", chinese)

    def test_repository_landing_page_is_chinese_first(self) -> None:
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        english = (REPOSITORY / "README.en.md").read_text(encoding="utf-8")

        self.assertIn('href="README.en.md">English</a>', readme)
        self.assertIn('href="README.md">简体中文</a>', english)
        self.assertIn("## 一分钟安装", readme)
        self.assertNotIn("## Install on Ubuntu", readme)
        self.assertIn("## Install on Ubuntu", english)

        sections = [line for line in readme.splitlines() if line.startswith("## ")]
        self.assertGreaterEqual(len(sections), 8)
        self.assertEqual(sections[0], "## 一分钟安装")
        self.assertTrue(
            all(
                any("\u4e00" <= character <= "\u9fff" for character in section)
                for section in sections
            ),
            "every main README section heading must remain Chinese-first",
        )

    def test_public_pages_keep_microphone_choice_and_lightweight_claim_honest(
        self,
    ) -> None:
        pages = {
            "main README": (REPOSITORY / "README.md").read_text(encoding="utf-8"),
            "English README": (REPOSITORY / "README.en.md").read_text(encoding="utf-8"),
            "Chinese quick guide": (REPOSITORY / "docs/README.zh-CN.md").read_text(
                encoding="utf-8"
            ),
            "Chinese press kit": (REPOSITORY / "docs/press-kit.zh-CN.md").read_text(
                encoding="utf-8"
            ),
        }

        for name, content in pages.items():
            with self.subTest(page=name):
                self.assertNotIn("大疆 >", content)
                self.assertNotIn("DJI >", content)
                self.assertNotIn("推荐右 `Alt`", content)
                self.assertNotIn("按 Right Alt", content)

        main = pages["main README"]
        english = pages["English README"]
        normalised_english = " ".join(english.split())
        self.assertIn("麦克风顺序完全由用户设置", main)
        self.assertIn("using the user's saved priority", normalised_english)
        self.assertIn("413,736", main)
        self.assertIn("413,736", normalised_english)
        self.assertIn("404 KiB", main)
        self.assertIn("404 KiB", normalised_english)
        self.assertIn("2,776 KiB", main)
        self.assertIn("2,776 KiB", normalised_english)
        self.assertIn("2.7 MiB", main)
        self.assertIn("2.7 MiB", normalised_english)
        self.assertIn(
            "does not bundle Electron or local ASR model weights",
            normalised_english,
        )
        self.assertIn(
            "APT may download additional system dependencies", normalised_english
        )
        self.assertNotIn("Right Alt is recommended", english)
        self.assertNotIn("Orange", english)

    def test_generated_hero_assets_have_bounded_publishable_dimensions(self) -> None:
        self.assertTrue(HERO_ANIMATION.is_file())
        self.assertTrue(HERO_POSTER.is_file())
        self.assertTrue(SOCIAL_PREVIEW.is_file())
        self.assertFalse(HERO_ANIMATION.is_symlink())
        self.assertLess(HERO_ANIMATION.stat().st_size, 8_000_000)

        data = HERO_ANIMATION.read_bytes()
        self.assertIn(data[:6], {b"GIF87a", b"GIF89a"})
        self.assertEqual(struct.unpack("<HH", data[6:10]), (960, 540))
        self.assertIn(b"NETSCAPE2.0", data, "hero GIF must loop in README")
        self.assertEqual(self._gif_timing(HERO_ANIMATION), (156, 1_300))

        self.assertEqual(self._png_dimensions(HERO_POSTER), (960, 540))
        self.assertEqual(self._png_dimensions(SOCIAL_PREVIEW), (1200, 600))
        self.assertLess(HERO_POSTER.stat().st_size, 500_000)
        self.assertLess(SOCIAL_PREVIEW.stat().st_size, 700_000)

    def test_hero_asset_notes_keep_demo_and_privacy_boundaries_explicit(self) -> None:
        notes = HERO_NOTES.read_text(encoding="utf-8")
        generator = (REPOSITORY / "scripts/generate_hero_demo.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("合成交互概念演示", notes)
        self.assertIn("no recording", notes)
        self.assertIn("no network access", notes)
        self.assertIn("不冒充产品实录", notes)
        self.assertIn("GNOME 风格的中性标题栏", notes)
        self.assertIn("共 156 帧", notes)
        self.assertIn("12 fps", notes)
        self.assertIn("合成交互演示", generator)
        self.assertIn("自定义快捷键", generator)
        self.assertIn("再按一次完成", generator)
        self.assertIn("个人术语学习", generator)
        self.assertIn("数据由你掌控", generator)
        self.assertNotIn("按住说话", generator)
        self.assertNotIn("松开后提交", generator)
        self.assertNotIn("Right Alt", notes + generator)
        self.assertNotIn("Tap Right Alt", notes + generator)
        self.assertNotIn("#FF6258", generator)
        self.assertNotIn("#FFC04A", generator)
        self.assertNotIn("#35C759", generator)

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
