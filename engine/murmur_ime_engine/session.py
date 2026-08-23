"""Focus-bound voice-session state, independent of IBus and D-Bus."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ActiveSession:
    owner: str
    utterance_id: str
    focus_token: int
    last_revision: int = -1
    final_seen: bool = False


class SessionGuard:
    """Reject stale, reordered, cross-process, and duplicate text events."""

    def __init__(self) -> None:
        self._active: ActiveSession | None = None

    @property
    def active(self) -> ActiveSession | None:
        return self._active

    @property
    def owner(self) -> str | None:
        return self._active.owner if self._active else None

    def acquire(self, owner: str, utterance_id: str, focus_token: int) -> bool:
        if not owner or not utterance_id:
            return False
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

    def finish(self) -> None:
        self._active = None

    def invalidate(self) -> bool:
        had_session = self._active is not None
        self._active = None
        return had_session
