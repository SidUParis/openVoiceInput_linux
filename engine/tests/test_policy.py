from __future__ import annotations

import unittest
from unittest.mock import patch

from murmur_ime_engine.policy import (
    MAX_TEXT_CODEPOINTS,
    is_private_input,
    is_real_input_client,
    valid_preedit_text,
    valid_utterance_id,
)


class PolicyTests(unittest.TestCase):
    def test_private_purposes_and_hint_are_blocked(self) -> None:
        self.assertTrue(is_private_input(8, 0))
        self.assertTrue(is_private_input(9, 0))
        self.assertTrue(is_private_input(0, 1 << 11))
        self.assertFalse(is_private_input(0, 0))
        self.assertFalse(is_private_input(6, 0))

    def test_ibus_fake_client_is_not_an_editable_focus(self) -> None:
        self.assertFalse(is_real_input_client("fake"))
        self.assertTrue(is_real_input_client("gtk4-im:org.gnome.TextEditor"))
        self.assertTrue(is_real_input_client("xim"))
        self.assertTrue(is_real_input_client(""))

    def test_utterance_id_rejects_controls_and_oversize(self) -> None:
        self.assertTrue(valid_utterance_id("voice-1234:a"))
        self.assertFalse(valid_utterance_id(""))
        self.assertFalse(valid_utterance_id("voice id"))
        self.assertFalse(valid_utterance_id("voice\nnext"))
        self.assertFalse(valid_utterance_id("a" * 129))

    def test_text_is_bounded_by_codepoints_and_utf8_bytes(self) -> None:
        self.assertTrue(valid_preedit_text("这是一段实时草稿。"))
        self.assertTrue(valid_preedit_text(""))
        self.assertFalse(valid_preedit_text("\x00"))
        self.assertFalse(valid_preedit_text("a" * (MAX_TEXT_CODEPOINTS + 1)))
        # Exercise the byte guard independently of the normal codepoint cap.
        with patch("murmur_ime_engine.policy.MAX_TEXT_CODEPOINTS", 10_000):
            self.assertFalse(valid_preedit_text("😀" * 4097))


if __name__ == "__main__":
    unittest.main()
