"""Dependency-free command and lifecycle values shared by daemon modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class VoiceState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class CommandReply:
    ok: bool
    code: str
    state: VoiceState

    def as_dict(self) -> dict[str, str | bool]:
        return {"ok": self.ok, "code": self.code, "state": self.state.value}
