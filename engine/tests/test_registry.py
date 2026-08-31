from __future__ import annotations

import unittest

from murmur_ime_engine.registry import EngineRegistry
from murmur_ime_engine.session import ObservationResult


class FakeEngine:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.owner: str | None = None
        self.utterance_id: str | None = None
        self.last_revision = -1
        self.final_seen = False
        self.observation_available = True

    @property
    def active_owner(self) -> str | None:
        return self.owner

    @property
    def has_active_session(self) -> bool:
        return self.owner is not None

    def can_acquire(self) -> bool:
        return self.available

    def acquire(self, owner: str, utterance_id: str) -> bool:
        if self.owner is None:
            self.owner = owner
            self.utterance_id = utterance_id
            return True
        return self.owner == owner and self.utterance_id == utterance_id

    def partial(self, owner: str, utterance_id: str, revision: int, text: str) -> bool:
        if (
            self.final_seen
            or owner != self.owner
            or utterance_id != self.utterance_id
            or revision <= self.last_revision
        ):
            return False
        self.last_revision = revision
        return True

    def final(self, owner: str, utterance_id: str, revision: int, text: str) -> bool:
        if not self.partial(owner, utterance_id, revision, text):
            return False
        self.final_seen = True
        return True

    def finish_observation(self, owner: str, utterance_id: str) -> ObservationResult:
        if (
            owner != self.owner
            or utterance_id != self.utterance_id
            or not self.final_seen
        ):
            return ObservationResult()
        self.owner = None
        self.utterance_id = None
        self.final_seen = False
        return ObservationResult(
            consumed=True,
            accepted=self.observation_available,
            baseline_text="final" if self.observation_available else "",
            committed_end=5 if self.observation_available else 0,
            current_text="final" if self.observation_available else "",
            cursor=5 if self.observation_available else 0,
            anchor=5 if self.observation_available else 0,
        )

    def observation_supported(self, owner: str, utterance_id: str) -> bool:
        return bool(
            owner == self.owner
            and utterance_id == self.utterance_id
            and self.final_seen
            and self.observation_available
        )

    def cancel(self, owner: str, utterance_id: str) -> bool:
        if owner != self.owner or utterance_id != self.utterance_id:
            return False
        self.owner = None
        self.utterance_id = None
        self.final_seen = False
        return True

    def cancel_owner(self, owner: str) -> bool:
        if owner != self.owner:
            return False
        self.owner = None
        self.utterance_id = None
        self.final_seen = False
        return True


class RegistryTests(unittest.TestCase):
    def test_exactly_one_focused_engine_is_required(self) -> None:
        registry = EngineRegistry()
        self.assertFalse(registry.acquire(":1.2", "u1"))
        first = FakeEngine()
        second = FakeEngine()
        registry.register(first)
        registry.register(second)
        self.assertFalse(registry.acquire(":1.2", "u1"))
        second.available = False
        self.assertTrue(registry.acquire(":1.2", "u1"))

    def test_target_routes_revisions_and_finishes(self) -> None:
        registry = EngineRegistry()
        engine = FakeEngine()
        registry.register(engine)
        self.assertTrue(registry.acquire(":1.2", "u1"))
        self.assertTrue(registry.partial(":1.2", "u1", 1, "draft"))
        self.assertFalse(registry.partial(":1.2", "u1", 1, "stale"))
        self.assertTrue(registry.final(":1.2", "u1", 2, "final"))
        self.assertFalse(registry.final(":1.2", "u1", 3, "duplicate"))
        self.assertTrue(engine.has_active_session)
        self.assertTrue(registry.observation_supported(":1.2", "u1"))
        self.assertFalse(registry.observation_supported(":1.3", "u1"))
        result = registry.finish_observation(":1.2", "u1")
        self.assertTrue(result.accepted)
        self.assertFalse(engine.has_active_session)

    def test_unavailable_observation_still_releases_target(self) -> None:
        registry = EngineRegistry()
        engine = FakeEngine()
        engine.observation_available = False
        registry.register(engine)
        self.assertTrue(registry.acquire(":1.2", "u1"))
        self.assertTrue(registry.final(":1.2", "u1", 1, "final"))

        result = registry.finish_observation(":1.2", "u1")

        self.assertTrue(result.consumed)
        self.assertFalse(result.accepted)
        self.assertFalse(engine.has_active_session)

    def test_mismatched_finish_does_not_release_target(self) -> None:
        registry = EngineRegistry()
        engine = FakeEngine()
        registry.register(engine)
        self.assertTrue(registry.acquire(":1.2", "u1"))
        self.assertTrue(registry.final(":1.2", "u1", 1, "final"))

        self.assertFalse(registry.finish_observation(":1.3", "u1").consumed)
        self.assertFalse(registry.acquire(":1.3", "u2"))
        self.assertTrue(engine.has_active_session)

    def test_disappearing_dbus_owner_cancels_target(self) -> None:
        registry = EngineRegistry()
        engine = FakeEngine()
        registry.register(engine)
        self.assertTrue(registry.acquire(":1.2", "u1"))
        self.assertTrue(registry.final(":1.2", "u1", 1, "final"))
        registry.owner_vanished(":1.2")
        self.assertFalse(engine.has_active_session)
        self.assertFalse(registry.partial(":1.2", "u1", 1, "late"))


if __name__ == "__main__":
    unittest.main()
