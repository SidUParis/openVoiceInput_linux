from __future__ import annotations

import unittest

from murmur_ime_engine.session import SessionGuard


class SessionGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = SessionGuard()
        self.assertTrue(self.guard.acquire(":1.40", "utt-1", 7))

    def test_acquire_is_idempotent_only_for_exact_session(self) -> None:
        self.assertTrue(self.guard.acquire(":1.40", "utt-1", 7))
        self.assertFalse(self.guard.acquire(":1.41", "utt-1", 7))
        self.assertFalse(self.guard.acquire(":1.40", "utt-2", 7))
        self.assertFalse(self.guard.acquire(":1.40", "utt-1", 8))

    def test_owner_utterance_and_focus_must_all_match(self) -> None:
        self.assertFalse(self.guard.accept_text(":1.41", "utt-1", 7, 1, final=False))
        self.assertFalse(self.guard.accept_text(":1.40", "utt-2", 7, 1, final=False))
        self.assertFalse(self.guard.accept_text(":1.40", "utt-1", 8, 1, final=False))
        self.assertTrue(self.guard.accept_text(":1.40", "utt-1", 7, 1, final=False))

    def test_revisions_are_strictly_increasing(self) -> None:
        self.assertTrue(self.guard.accept_text(":1.40", "utt-1", 7, 4, final=False))
        self.assertFalse(self.guard.accept_text(":1.40", "utt-1", 7, 4, final=False))
        self.assertFalse(self.guard.accept_text(":1.40", "utt-1", 7, 3, final=False))
        self.assertTrue(self.guard.accept_text(":1.40", "utt-1", 7, 5, final=False))

    def test_final_can_be_accepted_only_once(self) -> None:
        self.assertTrue(self.guard.accept_text(":1.40", "utt-1", 7, 1, final=True))
        self.assertFalse(self.guard.accept_text(":1.40", "utt-1", 7, 2, final=True))
        self.assertFalse(self.guard.acquire(":1.40", "utt-1", 7))
        self.guard.finish()
        self.assertIsNone(self.guard.active)

    def test_cancel_is_bound_to_owner_and_utterance(self) -> None:
        self.assertFalse(self.guard.cancel(":1.41", "utt-1"))
        self.assertFalse(self.guard.cancel(":1.40", "utt-2"))
        self.assertTrue(self.guard.cancel(":1.40", "utt-1"))
        self.assertIsNone(self.guard.active)


if __name__ == "__main__":
    unittest.main()
