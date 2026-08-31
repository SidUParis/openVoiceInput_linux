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
        self.assertTrue(
            self.guard.begin_observation(
                ":1.40",
                "utt-1",
                7,
                surrounding_revision=3,
                final_text="final",
                supported=True,
            )
        )
        self.guard.finish()
        self.assertIsNone(self.guard.active)

    def test_first_new_surrounding_locks_baseline_and_latest_update(self) -> None:
        self.assertTrue(self.guard.accept_text(":1.40", "utt-1", 7, 1, final=True))
        self.assertTrue(
            self.guard.begin_observation(
                ":1.40",
                "utt-1",
                7,
                surrounding_revision=10,
                final_text="奔驰 Mark",
                supported=True,
            )
        )
        self.assertFalse(self.guard.update_surrounding(7, 10, "旧缓存", 3, 3))
        baseline = "前文奔驰 Mark后文"
        baseline_cursor = len("前文奔驰 Mark")
        self.assertTrue(
            self.guard.update_surrounding(
                7,
                11,
                baseline,
                baseline_cursor,
                baseline_cursor,
            )
        )
        current = "前文bench Mark后文"
        current_cursor = len("前文bench")
        self.assertTrue(
            self.guard.update_surrounding(
                7,
                12,
                current,
                current_cursor,
                current_cursor,
            )
        )

        result = self.guard.finish_observation(":1.40", "utt-1", 7)

        self.assertTrue(result.consumed)
        self.assertTrue(result.accepted)
        self.assertEqual(result.baseline_text, baseline)
        self.assertEqual(result.committed_start, len("前文"))
        self.assertEqual(result.committed_end, baseline_cursor)
        self.assertEqual(result.current_text, current)
        self.assertEqual(result.cursor, current_cursor)
        self.assertEqual(result.anchor, current_cursor)
        self.assertIsNone(self.guard.active)

    def test_first_post_commit_snapshot_must_match_exact_final_without_selection(
        self,
    ) -> None:
        self.assertTrue(self.guard.accept_text(":1.40", "utt-1", 7, 1, final=True))
        self.assertTrue(
            self.guard.begin_observation(
                ":1.40",
                "utt-1",
                7,
                surrounding_revision=2,
                final_text="final",
                supported=True,
            )
        )
        self.assertFalse(self.guard.update_surrounding(7, 3, "not final", 9, 8))
        self.assertFalse(self.guard.update_surrounding(7, 4, "final", 5, 5))

        result = self.guard.finish_observation(":1.40", "utt-1", 7)

        self.assertTrue(result.consumed)
        self.assertFalse(result.accepted)
        self.assertEqual(result.baseline_text, "")

    def test_transient_selection_then_single_replacement_can_be_observed(self) -> None:
        self.assertTrue(self.guard.accept_text(":1.40", "utt-1", 7, 1, final=True))
        self.assertTrue(
            self.guard.begin_observation(
                ":1.40",
                "utt-1",
                7,
                surrounding_revision=0,
                final_text="Ostro",
                supported=True,
            )
        )
        self.assertTrue(self.guard.update_surrounding(7, 1, "Ostro", 5, 5))
        self.assertTrue(self.guard.update_surrounding(7, 2, "Ostro", 5, 0))
        self.assertTrue(self.guard.update_surrounding(7, 3, "Austral", 7, 7))

        result = self.guard.finish_observation(":1.40", "utt-1", 7)

        self.assertTrue(result.accepted)
        self.assertEqual(result.current_text, "Austral")
        self.assertEqual((result.cursor, result.anchor), (7, 7))

    def test_unsupported_observation_is_consumed_without_text(self) -> None:
        self.assertTrue(self.guard.accept_text(":1.40", "utt-1", 7, 1, final=True))
        self.assertTrue(
            self.guard.begin_observation(
                ":1.40",
                "utt-1",
                7,
                surrounding_revision=0,
                final_text="final",
                supported=False,
            )
        )
        self.assertFalse(self.guard.observation_supported(":1.40", "utt-1", 7))

        result = self.guard.finish_observation(":1.40", "utt-1", 7)

        self.assertTrue(result.consumed)
        self.assertFalse(result.accepted)
        self.assertIsNone(self.guard.active)

    def test_observation_capability_requires_exact_live_session(self) -> None:
        self.assertTrue(self.guard.accept_text(":1.40", "utt-1", 7, 1, final=True))
        self.assertTrue(
            self.guard.begin_observation(
                ":1.40",
                "utt-1",
                7,
                surrounding_revision=0,
                final_text="final",
                supported=True,
            )
        )

        self.assertTrue(self.guard.observation_supported(":1.40", "utt-1", 7))
        self.assertFalse(self.guard.observation_supported(":1.41", "utt-1", 7))
        self.assertFalse(self.guard.observation_supported(":1.40", "utt-2", 7))
        self.assertFalse(self.guard.observation_supported(":1.40", "utt-1", 8))

    def test_delayed_finish_consumes_without_returning_text_after_deadline(
        self,
    ) -> None:
        now = [10.0]
        guard = SessionGuard(monotonic=lambda: now[0], observation_timeout=5.0)
        self.assertTrue(guard.acquire(":1.40", "utt-1", 7))
        self.assertTrue(guard.accept_text(":1.40", "utt-1", 7, 1, final=True))
        self.assertTrue(
            guard.begin_observation(
                ":1.40",
                "utt-1",
                7,
                surrounding_revision=0,
                final_text="Ostro",
                supported=True,
            )
        )
        self.assertTrue(guard.update_surrounding(7, 1, "Ostro", 5, 5))
        now[0] = 15.001

        result = guard.finish_observation(":1.40", "utt-1", 7)

        self.assertTrue(result.consumed)
        self.assertFalse(result.accepted)
        self.assertNotIn("Ostro", repr(result))

    def test_expired_observation_is_dropped_before_a_new_acquire(self) -> None:
        now = [10.0]
        guard = SessionGuard(monotonic=lambda: now[0], observation_timeout=5.0)
        self.assertTrue(guard.acquire(":1.40", "utt-old", 7))
        self.assertTrue(guard.accept_text(":1.40", "utt-old", 7, 1, final=True))
        self.assertTrue(
            guard.begin_observation(
                ":1.40",
                "utt-old",
                7,
                surrounding_revision=0,
                final_text="private-old-final",
                supported=True,
            )
        )
        self.assertTrue(guard.update_surrounding(7, 1, "private-old-final", 17, 17))
        now[0] = 15.0

        self.assertTrue(guard.acquire(":1.41", "utt-new", 7))
        self.assertEqual(guard.owner, ":1.41")
        self.assertNotIn("private-old-final", repr(guard.active))

    def test_mismatched_finish_does_not_consume_observation(self) -> None:
        self.assertTrue(self.guard.accept_text(":1.40", "utt-1", 7, 1, final=True))
        self.assertTrue(
            self.guard.begin_observation(
                ":1.40",
                "utt-1",
                7,
                surrounding_revision=0,
                final_text="final",
                supported=True,
            )
        )

        self.assertFalse(self.guard.finish_observation(":1.41", "utt-1", 7).consumed)
        self.assertFalse(self.guard.finish_observation(":1.40", "utt-2", 7).consumed)
        self.assertFalse(self.guard.finish_observation(":1.40", "utt-1", 8).consumed)
        self.assertIsNotNone(self.guard.active)

    def test_cancel_is_bound_to_owner_and_utterance(self) -> None:
        self.assertFalse(self.guard.cancel(":1.41", "utt-1"))
        self.assertFalse(self.guard.cancel(":1.40", "utt-2"))
        self.assertTrue(self.guard.cancel(":1.40", "utt-1"))
        self.assertIsNone(self.guard.active)


if __name__ == "__main__":
    unittest.main()
