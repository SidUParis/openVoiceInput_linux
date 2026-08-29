"""Focus-bound voice-session state, independent of IBus and D-Bus."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

OBSERVATION_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True, repr=False)
class ObservationResult:
    """One consumed post-commit surrounding-text observation."""

    consumed: bool = False
    accepted: bool = False
    baseline_text: str = ""
    committed_start: int = 0
    committed_end: int = 0
    current_text: str = ""
    cursor: int = 0
    anchor: int = 0


@dataclass(frozen=True, slots=True, repr=False)
class SurroundingSnapshot:
    """One bounded surrounding-text update from the focused input context."""

    revision: int
    text: str
    cursor: int
    anchor: int


@dataclass(slots=True, repr=False)
class ActiveSession:
    owner: str
    utterance_id: str
    focus_token: int
    last_revision: int = -1
    final_seen: bool = False
    observation_started: bool = False
    observation_supported: bool = False
    observation_start_revision: int = -1
    final_text: str = ""
    baseline_attempted: bool = False
    baseline: SurroundingSnapshot | None = None
    current: SurroundingSnapshot | None = None
    committed_start: int = 0
    committed_end: int = 0
    observation_deadline: float = 0.0


class SessionGuard:
    """Reject stale, reordered, cross-process, and duplicate text events."""

    def __init__(
        self,
        *,
        monotonic: Any | None = None,
        observation_timeout: float = OBSERVATION_TIMEOUT_SECONDS,
    ) -> None:
        self._active: ActiveSession | None = None
        self._monotonic = monotonic or time.monotonic
        self._observation_timeout = max(0.1, min(30.0, float(observation_timeout)))

    @property
    def active(self) -> ActiveSession | None:
        return self._active

    @property
    def owner(self) -> str | None:
        session = self.active
        return session.owner if session else None

    @property
    def observing(self) -> bool:
        session = self.active
        return bool(
            session is not None and session.final_seen and session.observation_started
        )

    def acquire(self, owner: str, utterance_id: str, focus_token: int) -> bool:
        if not owner or not utterance_id:
            return False
        self.expire_observation()
        if self._active is None:
            self._active = ActiveSession(owner, utterance_id, focus_token)
            return True
        # A retry of the same Acquire call is harmless.  A second utterance
        # must explicitly cancel or finish the first one.
        return (
            not self._active.final_seen
            and self._active.owner == owner
            and self._active.utterance_id == utterance_id
            and self._active.focus_token == focus_token
        )

    def accept_text(
        self,
        owner: str,
        utterance_id: str,
        focus_token: int,
        revision: int,
        *,
        final: bool,
    ) -> bool:
        session = self._active
        if (
            session is None
            or session.final_seen
            or session.owner != owner
            or session.utterance_id != utterance_id
            or session.focus_token != focus_token
            or revision <= session.last_revision
        ):
            return False
        session.last_revision = revision
        session.final_seen = final
        return True

    def cancel(self, owner: str, utterance_id: str) -> bool:
        session = self._active
        if (
            session is None
            or session.owner != owner
            or session.utterance_id != utterance_id
        ):
            return False
        self._active = None
        return True

    def begin_observation(
        self,
        owner: str,
        utterance_id: str,
        focus_token: int,
        *,
        surrounding_revision: int,
        final_text: str,
        supported: bool,
    ) -> bool:
        """Arm one post-commit observation before the commit is emitted."""

        session = self._active
        if (
            session is None
            or not session.final_seen
            or session.observation_started
            or session.owner != owner
            or session.utterance_id != utterance_id
            or session.focus_token != focus_token
            or surrounding_revision < 0
        ):
            return False
        session.observation_started = True
        session.observation_supported = bool(supported and final_text)
        session.observation_start_revision = surrounding_revision
        session.observation_deadline = self._monotonic() + self._observation_timeout
        session.final_text = final_text
        return True

    def update_surrounding(
        self,
        focus_token: int,
        revision: int,
        text: str | None,
        cursor: int = 0,
        anchor: int = 0,
    ) -> bool:
        """Latch the first valid post-commit baseline, then track later updates."""

        session = self._active
        if (
            session is None
            or not session.final_seen
            or not session.observation_started
            or session.focus_token != focus_token
            or revision <= session.observation_start_revision
            or not session.observation_supported
        ):
            return False
        if self._monotonic() >= session.observation_deadline:
            session.observation_supported = False
            session.final_text = ""
            session.baseline = None
            session.current = None
            return False

        if text is None:
            session.observation_supported = False
            session.baseline = None
            session.current = None
            return False

        snapshot = SurroundingSnapshot(revision, text, cursor, anchor)
        if session.baseline is None:
            if session.baseline_attempted:
                return False
            session.baseline_attempted = True
            final_length = len(session.final_text)
            start = cursor - final_length
            if (
                cursor != anchor
                or start < 0
                or text[start:cursor] != session.final_text
            ):
                session.observation_supported = False
                return False
            session.baseline = snapshot
            session.current = snapshot
            session.committed_start = start
            session.committed_end = cursor
            return True

        if revision <= session.current.revision:
            return False
        session.current = snapshot
        return True

    def finish_observation(
        self,
        owner: str,
        utterance_id: str,
        focus_token: int,
    ) -> ObservationResult:
        """Consume a matching observation and return bounded snapshots once."""

        session = self._active
        if (
            session is None
            or not session.final_seen
            or not session.observation_started
            or session.owner != owner
            or session.utterance_id != utterance_id
            or session.focus_token != focus_token
        ):
            return ObservationResult()

        self._active = None
        baseline = session.baseline
        current = session.current
        if (
            self._monotonic() >= session.observation_deadline
            or not session.observation_supported
            or baseline is None
            or current is None
        ):
            return ObservationResult(consumed=True)
        return ObservationResult(
            consumed=True,
            accepted=True,
            baseline_text=baseline.text,
            committed_start=session.committed_start,
            committed_end=session.committed_end,
            current_text=current.text,
            cursor=current.cursor,
            anchor=current.anchor,
        )

    def finish(self) -> None:
        self._active = None

    def expire_observation(
        self,
        owner: str | None = None,
        utterance_id: str | None = None,
        focus_token: int | None = None,
    ) -> bool:
        """Drop an expired observation and all text retained for it."""

        session = self._active
        if (
            session is None
            or not session.final_seen
            or not session.observation_started
            or (owner is not None and session.owner != owner)
            or (utterance_id is not None and session.utterance_id != utterance_id)
            or (focus_token is not None and session.focus_token != focus_token)
            or self._monotonic() < session.observation_deadline
        ):
            return False
        self._active = None
        return True

    def invalidate(self) -> bool:
        had_session = self._active is not None
        self._active = None
        return had_session
