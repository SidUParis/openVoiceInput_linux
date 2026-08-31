from __future__ import annotations

import unittest

import gi

gi.require_version("IBus", "1.0")
from gi.repository import IBus  # noqa: E402

from murmur_ime_engine.ibus_engine import MurmurEngine  # noqa: E402
from murmur_ime_engine.registry import EngineRegistry  # noqa: E402
from murmur_ime_engine.session import SessionGuard  # noqa: E402


class _PresentationRecorder:
    def __init__(self) -> None:
        self.preedits = []
        self.commits = []

    def update_preedit_text_with_mode(self, text, cursor, visible, mode) -> None:
        self.preedits.append((text.get_text(), cursor, visible, mode))

    def commit_text(self, text) -> None:
        self.commits.append(text.get_text())


class _Bus:
    @staticmethod
    def get_connection():
        return None


class _RegistryRecorder:
    def __init__(self) -> None:
        self.invalidations = 0

    def invalidated(self, engine) -> None:
        del engine
        self.invalidations += 1


class _EngineHarness(_PresentationRecorder):
    def __init__(self, *, surrounding: bool = True) -> None:
        super().__init__()
        self._registry = _RegistryRecorder()
        self._sessions = SessionGuard()
        self._focus_token = 7
        self._focus_context = "/org/murmur/TestContext"
        self._focus_client = "gtk4-im:test"
        self._focused = True
        self._enabled = True
        self._surrounding_revision = 3
        self._observation_timeout_source_id = 0
        self._purpose = int(IBus.InputPurpose.FREE_FORM)
        self._hints = int(IBus.InputHints.NONE)
        self._capabilities = int(IBus.Capabilite.PREEDIT_TEXT)
        if surrounding:
            self._capabilities |= int(IBus.Capabilite.SURROUNDING_TEXT)
        self._sessions.acquire(":1.40", "utt-1", self._focus_token)

    @staticmethod
    def can_acquire() -> bool:
        return True

    def _clear_preedit(self) -> None:
        MurmurEngine._clear_preedit(self)

    def _clear_surrounding_cache(self) -> None:
        MurmurEngine._clear_surrounding_cache(self)

    def _invalidate_voice(self, reason: str) -> None:
        MurmurEngine._invalidate_voice(self, reason)

    def _arm_observation_timeout(self, *args) -> None:
        del args
        pass

    def _cancel_observation_timeout(self) -> None:
        pass


class MurmurEnginePresentationTests(unittest.TestCase):
    def test_constructor_requests_active_surrounding_updates(self) -> None:
        engine = MurmurEngine(_Bus(), "/org/murmur/TestEngine", EngineRegistry())

        self.assertTrue(engine.props.active_surrounding_text)

    def test_empty_partial_heartbeat_is_invisible_and_never_commits(self) -> None:
        recorder = _PresentationRecorder()

        MurmurEngine._set_preedit(recorder, "")

        self.assertEqual(len(recorder.preedits), 1)
        text, cursor, visible, _mode = recorder.preedits[0]
        self.assertEqual(text, "")
        self.assertEqual(cursor, 0)
        self.assertFalse(visible)
        self.assertEqual(recorder.commits, [])

    def test_final_retains_session_until_observation_is_consumed(self) -> None:
        engine = _EngineHarness()

        self.assertTrue(MurmurEngine.final(engine, ":1.40", "utt-1", 1, "奔驰 Mark"))
        self.assertTrue(MurmurEngine.observation_supported(engine, ":1.40", "utt-1"))
        self.assertFalse(MurmurEngine.observation_supported(engine, ":1.41", "utt-1"))
        self.assertEqual(engine.commits, ["奔驰 Mark"])
        self.assertIsNotNone(engine._sessions.active)
        self.assertEqual(engine._registry.invalidations, 0)

        baseline = "前奔驰 Mark后"
        cursor = len("前奔驰 Mark")
        self.assertTrue(
            MurmurEngine._cache_surrounding_text(engine, baseline, cursor, cursor)
        )
        current = "前bench Mark后"
        current_cursor = len("前bench")
        self.assertTrue(
            MurmurEngine._cache_surrounding_text(
                engine,
                current,
                current_cursor,
                current_cursor,
            )
        )

        result = MurmurEngine.finish_observation(engine, ":1.40", "utt-1")

        self.assertTrue(result.accepted)
        self.assertEqual(result.baseline_text, baseline)
        self.assertEqual(result.committed_start, 1)
        self.assertEqual(result.committed_end, cursor)
        self.assertEqual(result.current_text, current)
        self.assertIsNone(engine._sessions.active)
        self.assertEqual(engine._registry.invalidations, 1)

    def test_missing_surrounding_capability_does_not_block_final(self) -> None:
        engine = _EngineHarness(surrounding=False)

        self.assertTrue(MurmurEngine.final(engine, ":1.40", "utt-1", 1, "final"))
        self.assertFalse(MurmurEngine.observation_supported(engine, ":1.40", "utt-1"))
        result = MurmurEngine.finish_observation(engine, ":1.40", "utt-1")

        self.assertTrue(result.consumed)
        self.assertFalse(result.accepted)
        self.assertEqual(engine.commits, ["final"])

    def test_unbounded_surrounding_invalidates_only_observation_data(self) -> None:
        engine = _EngineHarness()
        self.assertTrue(MurmurEngine.final(engine, ":1.40", "utt-1", 1, "final"))

        self.assertFalse(
            MurmurEngine._cache_surrounding_text(engine, "a" * 4097, 4097, 4097)
        )
        result = MurmurEngine.finish_observation(engine, ":1.40", "utt-1")

        self.assertTrue(result.consumed)
        self.assertFalse(result.accepted)
        self.assertEqual(engine.commits, ["final"])

    def test_focus_and_disable_each_clear_pending_observation(self) -> None:
        invalidators = (
            lambda engine: MurmurEngine._focus_out(engine, "/org/murmur/TestContext"),
            lambda engine: MurmurEngine.do_disable(engine),
        )
        for invalidate in invalidators:
            with self.subTest(invalidate=invalidate):
                engine = _EngineHarness()
                self.assertTrue(
                    MurmurEngine.final(engine, ":1.40", "utt-1", 1, "final")
                )

                invalidate(engine)

                self.assertIsNone(engine._sessions.active)
                self.assertFalse(
                    MurmurEngine.finish_observation(engine, ":1.40", "utt-1").consumed
                )

    def test_private_content_type_drops_observation_text_and_local_cache(self) -> None:
        engine = _EngineHarness()
        self.assertTrue(MurmurEngine.final(engine, ":1.40", "utt-1", 1, "hunter2"))
        self.assertTrue(MurmurEngine._cache_surrounding_text(engine, "hunter2", 7, 7))

        MurmurEngine.do_set_content_type(
            engine,
            int(IBus.InputPurpose.PASSWORD),
            int(IBus.InputHints.PRIVATE),
        )

        self.assertIsNone(engine._sessions.active)
        self.assertFalse(hasattr(engine, "_surrounding_text"))
        self.assertFalse(hasattr(engine, "_surrounding_cursor"))
        self.assertFalse(hasattr(engine, "_surrounding_anchor"))

    def test_engine_timeout_drops_observation_and_releases_registry(self) -> None:
        engine = _EngineHarness()
        self.assertTrue(MurmurEngine.final(engine, ":1.40", "utt-1", 1, "final"))
        engine._sessions._active.observation_deadline = 0.0

        self.assertFalse(
            MurmurEngine._on_observation_timeout(engine, ":1.40", "utt-1", 7)
        )
        self.assertIsNone(engine._sessions.active)
        self.assertEqual(engine._registry.invalidations, 1)

    def test_old_timeout_does_not_clear_a_new_session(self) -> None:
        engine = _EngineHarness()
        self.assertTrue(MurmurEngine.final(engine, ":1.40", "utt-1", 1, "final"))
        engine._sessions._active.observation_deadline = 0.0
        self.assertTrue(engine._sessions.acquire(":1.41", "utt-2", 7))

        self.assertFalse(
            MurmurEngine._on_observation_timeout(engine, ":1.40", "utt-1", 7)
        )
        self.assertEqual(engine._sessions.owner, ":1.41")
        self.assertEqual(engine._registry.invalidations, 0)

    def test_real_engine_clears_parent_surrounding_cache_on_disable(self) -> None:
        engine = MurmurEngine(_Bus(), "/org/murmur/TestCache", EngineRegistry())
        engine._enabled = True
        engine._focused = True
        engine._focus_client = "gtk4-im:test"
        engine._capabilities = int(
            IBus.Capabilite.PREEDIT_TEXT | IBus.Capabilite.SURROUNDING_TEXT
        )
        self.assertTrue(engine.acquire(":1.40", "utt-cache"))
        self.assertTrue(engine.final(":1.40", "utt-cache", 1, "secret"))
        engine.do_set_surrounding_text(IBus.Text.new_from_string("secret"), 6, 6)
        cached, _cursor, _anchor = engine.get_surrounding_text()
        self.assertEqual(cached.get_text(), "secret")

        engine.do_disable()

        cleared, cursor, anchor = engine.get_surrounding_text()
        self.assertEqual(cleared.get_text(), "")
        self.assertEqual((cursor, anchor), (0, 0))

        engine.do_set_content_type(
            int(IBus.InputPurpose.PASSWORD),
            int(IBus.InputHints.PRIVATE),
        )
        engine.do_set_surrounding_text(
            IBus.Text.new_from_string("new-private-secret"),
            18,
            18,
        )
        still_empty, cursor, anchor = engine.get_surrounding_text()
        self.assertEqual(still_empty.get_text(), "")
        self.assertEqual((cursor, anchor), (0, 0))

    def test_gtk_reset_during_observation_preserves_same_focus_lease(self) -> None:
        engine = _EngineHarness()
        self.assertTrue(MurmurEngine.final(engine, ":1.40", "utt-1", 1, "Ostro"))

        MurmurEngine.do_reset(engine)

        self.assertTrue(engine._sessions.observing)
        self.assertEqual(engine._focus_token, 7)

    def test_reset_before_final_still_invalidates_active_preedit(self) -> None:
        engine = _EngineHarness()

        MurmurEngine.do_reset(engine)

        self.assertIsNone(engine._sessions.active)


if __name__ == "__main__":
    unittest.main()
