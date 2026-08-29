"""Routes one external voice session to the one focused IBus engine."""

from __future__ import annotations

from typing import Protocol

from .session import ObservationResult


class EngineTarget(Protocol):
    @property
    def active_owner(self) -> str | None: ...

    @property
    def has_active_session(self) -> bool: ...

    def can_acquire(self) -> bool: ...

    def acquire(self, owner: str, utterance_id: str) -> bool: ...

    def partial(
        self, owner: str, utterance_id: str, revision: int, text: str
    ) -> bool: ...

    def final(
        self, owner: str, utterance_id: str, revision: int, text: str
    ) -> bool: ...

    def finish_observation(
        self, owner: str, utterance_id: str
    ) -> ObservationResult: ...

    def cancel(self, owner: str, utterance_id: str) -> bool: ...

    def cancel_owner(self, owner: str) -> bool: ...


class EngineRegistry:
    """A deliberately single-target broker for the prototype."""

    def __init__(self) -> None:
        self._engines: list[EngineTarget] = []
        self._target: EngineTarget | None = None

    def register(self, engine: EngineTarget) -> None:
        if not any(candidate is engine for candidate in self._engines):
            self._engines.append(engine)

    def unregister(self, engine: EngineTarget) -> None:
        self._engines = [item for item in self._engines if item is not engine]
        if self._target is engine:
            self._target = None

    def invalidated(self, engine: EngineTarget) -> None:
        if self._target is engine:
            self._target = None

    def acquire(self, owner: str, utterance_id: str) -> bool:
        if self._target is not None:
            return self._target.acquire(owner, utterance_id)
        candidates = [engine for engine in self._engines if engine.can_acquire()]
        if len(candidates) != 1:
            return False
        target = candidates[0]
        if not target.acquire(owner, utterance_id):
            return False
        self._target = target
        return True

    def partial(
        self,
        owner: str,
        utterance_id: str,
        revision: int,
        text: str,
    ) -> bool:
        target = self._target
        if target is None:
            return False
        accepted = target.partial(owner, utterance_id, revision, text)
        if not accepted and not target.has_active_session:
            self._target = None
        return accepted

    def final(
        self,
        owner: str,
        utterance_id: str,
        revision: int,
        text: str,
    ) -> bool:
        target = self._target
        if target is None:
            return False
        accepted = target.final(owner, utterance_id, revision, text)
        if not target.has_active_session:
            self._target = None
        return accepted

    def finish_observation(
        self,
        owner: str,
        utterance_id: str,
    ) -> ObservationResult:
        target = self._target
        if target is None:
            return ObservationResult()
        result = target.finish_observation(owner, utterance_id)
        if result.consumed or not target.has_active_session:
            self._target = None
        return result

    def cancel(self, owner: str, utterance_id: str) -> bool:
        target = self._target
        if target is None:
            return False
        accepted = target.cancel(owner, utterance_id)
        if accepted or not target.has_active_session:
            self._target = None
        return accepted

    def owner_vanished(self, owner: str) -> None:
        target = self._target
        if target is not None and target.cancel_owner(owner):
            self._target = None

    def shutdown(self) -> None:
        target = self._target
        if target is not None and target.active_owner is not None:
            target.cancel_owner(target.active_owner)
        self._target = None
