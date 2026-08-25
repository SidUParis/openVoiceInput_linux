from __future__ import annotations

import unittest

from murmur_ime_engine.ibus_engine import MurmurEngine


class _PresentationRecorder:
    def __init__(self) -> None:
        self.preedits = []
        self.commits = []

    def update_preedit_text_with_mode(self, text, cursor, visible, mode) -> None:
        self.preedits.append((text.get_text(), cursor, visible, mode))

    def commit_text(self, text) -> None:
        self.commits.append(text.get_text())


class MurmurEnginePresentationTests(unittest.TestCase):
    def test_empty_partial_heartbeat_is_invisible_and_never_commits(self) -> None:
        recorder = _PresentationRecorder()

        MurmurEngine._set_preedit(recorder, "")

        self.assertEqual(len(recorder.preedits), 1)
        text, cursor, visible, _mode = recorder.preedits[0]
        self.assertEqual(text, "")
        self.assertEqual(cursor, 0)
        self.assertFalse(visible)
        self.assertEqual(recorder.commits, [])


if __name__ == "__main__":
    unittest.main()
