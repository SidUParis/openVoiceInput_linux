# SPDX-License-Identifier: GPL-3.0-only
"""Press/release interaction policy for the local voice daemon.

This module deliberately contains no global keyboard hook.  It turns bounded
``press``/``release`` events from an explicitly chosen desktop or hardware
integration into the daemon's existing start/stop/cancel operations.  Keeping
the state machine next to the daemon means a lost controller process cannot
leave an unbounded recording behind.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import (
    ConfigError,
    _load_private_bytes,
    _reject_duplicate_json_fields,
    _write_private_json,
)
from .state import CommandReply, VoiceState

INTERACTION_CONFIG_VERSION = 1
MAX_INTERACTION_CONFIG_BYTES = 8 * 1024
INTERACTION_MODES = ("toggle", "push_to_talk")
DEFAULT_INTERACTION_MODE = "toggle"
DEFAULT_MINIMUM_HOLD_MILLISECONDS = 180
DEFAULT_RELEASE_TIMEOUT_SECONDS = 120
MINIMUM_HOLD_MILLISECONDS_LIMIT = 2_000
RELEASE_TIMEOUT_SECONDS_MINIMUM = 5
RELEASE_TIMEOUT_SECONDS_MAXIMUM = 600
TOGGLE_RELEASE_TIMEOUT_SECONDS = 5

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InteractionConfig:
    """User-selected key interaction semantics, independent of a key name."""

    interaction_mode: str = DEFAULT_INTERACTION_MODE
    minimum_hold_milliseconds: int = DEFAULT_MINIMUM_HOLD_MILLISECONDS
    release_timeout_seconds: int = DEFAULT_RELEASE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        _validate_interaction_values(
            self.interaction_mode,
            self.minimum_hold_milliseconds,
            self.release_timeout_seconds,
        )


def default_interaction_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "murmur-ime" / "interaction.json"


def load_interaction_config(
    path: str | os.PathLike[str] | None = None,
) -> InteractionConfig:
    """Load a private interaction policy; an absent file uses toggle mode."""

    config_path = Path(path) if path is not None else default_interaction_config_path()
    raw = _load_private_bytes(
        config_path,
        kind="interaction configuration",
        limit=MAX_INTERACTION_CONFIG_BYTES,
        missing_ok=True,
    )
    if raw is None:
        return InteractionConfig()
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ConfigError(
            "interaction configuration is not valid UTF-8 JSON"
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "version",
        "interaction_mode",
        "minimum_hold_milliseconds",
        "release_timeout_seconds",
    }:
        raise ConfigError("interaction configuration has unsupported fields")
    if (
        type(document.get("version")) is not int
        or document["version"] != INTERACTION_CONFIG_VERSION
    ):
        raise ConfigError("interaction configuration uses an unsupported version")
    return InteractionConfig(
        interaction_mode=document.get("interaction_mode"),
        minimum_hold_milliseconds=document.get("minimum_hold_milliseconds"),
        release_timeout_seconds=document.get("release_timeout_seconds"),
    )


def save_interaction_config(
    interaction_mode: str,
    minimum_hold_milliseconds: int = DEFAULT_MINIMUM_HOLD_MILLISECONDS,
    release_timeout_seconds: int = DEFAULT_RELEASE_TIMEOUT_SECONDS,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Atomically save the small, private interaction policy."""

    config = InteractionConfig(
        interaction_mode=interaction_mode,
        minimum_hold_milliseconds=minimum_hold_milliseconds,
        release_timeout_seconds=release_timeout_seconds,
    )
    config_path = Path(path) if path is not None else default_interaction_config_path()
    return _write_private_json(
        config_path,
        {
            "version": INTERACTION_CONFIG_VERSION,
            "interaction_mode": config.interaction_mode,
            "minimum_hold_milliseconds": config.minimum_hold_milliseconds,
            "release_timeout_seconds": config.release_timeout_seconds,
        },
        kind="interaction configuration",
        temporary_prefix=".interaction.json.",
    )


def _validate_interaction_values(
    mode: Any,
    minimum_hold_milliseconds: Any,
    release_timeout_seconds: Any,
) -> None:
    if not isinstance(mode, str) or mode not in INTERACTION_MODES:
        raise ConfigError("interaction mode is unsupported")
    if (
        type(minimum_hold_milliseconds) is not int
        or minimum_hold_milliseconds < 0
        or minimum_hold_milliseconds > MINIMUM_HOLD_MILLISECONDS_LIMIT
    ):
        raise ConfigError("minimum hold duration is invalid")
    if (
        type(release_timeout_seconds) is not int
        or release_timeout_seconds < RELEASE_TIMEOUT_SECONDS_MINIMUM
        or release_timeout_seconds > RELEASE_TIMEOUT_SECONDS_MAXIMUM
    ):
        raise ConfigError("release timeout is invalid")


ConfigReader = Callable[[], InteractionConfig]
TimerFactory = Callable[[float, Callable[[], None]], Any]


class InteractionController:
    """Translate key edges into safe, idempotent session operations.

    ``push_to_talk`` owns only sessions it successfully started.  A stray
    release therefore cannot stop a recording started through another control
    path.  A short hold cancels and discards its own utterance; a missing
    release is bounded by a watchdog which requests a normal stop.
    """

    def __init__(
        self,
        session: Any,
        *,
        config_reader: ConfigReader = load_interaction_config,
        monotonic: Callable[[], float] = time.monotonic,
        timer_factory: TimerFactory = threading.Timer,
    ) -> None:
        self._session = session
        self._config_reader = config_reader
        self._monotonic = monotonic
        self._timer_factory = timer_factory
        self._lock = threading.RLock()
        self._pressed = False
        self._owns_session = False
        self._press_started = 0.0
        self._press_mode = DEFAULT_INTERACTION_MODE
        self._minimum_hold_seconds = 0.0
        self._serial = 0
        self._release_timer: Any | None = None
        self._last_event_time = 0.0

    def press(self, *, event_time: float | None = None) -> CommandReply:
        """Handle one physical key-down edge; key-repeat is harmless."""

        pressed_at = float(event_time) if event_time is not None else self._monotonic()
        with self._lock:
            if pressed_at < self._last_event_time:
                return CommandReply(True, "stale-edge-ignored", self._session.state)
            self._last_event_time = pressed_at
            if self._pressed:
                return CommandReply(True, "repeat-press-ignored", self._session.state)
            try:
                config = self._config_reader()
            except (ConfigError, OSError):
                return CommandReply(
                    False, "interaction-config-invalid", self._session.state
                )

            self._pressed = True
            self._owns_session = False
            self._press_started = pressed_at
            self._press_mode = config.interaction_mode
            self._minimum_hold_seconds = config.minimum_hold_milliseconds / 1000.0
            self._serial += 1
            serial = self._serial
            watchdog_seconds = (
                TOGGLE_RELEASE_TIMEOUT_SECONDS
                if config.interaction_mode == "toggle"
                else config.release_timeout_seconds
            )
            try:
                self._arm_release_timer_locked(serial, watchdog_seconds)
            except Exception:
                logger.error("Interaction release watchdog could not be started")
                self._clear_press_locked()
                return CommandReply(
                    False, "interaction-safety-unavailable", self._session.state
                )

            if config.interaction_mode == "toggle":
                reply = self._session.toggle()
            elif self._session.state is VoiceState.OBSERVING:
                # toggle() atomically finishes the observation lease before
                # starting the next utterance.
                reply = self._session.toggle()
            elif self._session.state is VoiceState.IDLE:
                reply = self._session.start()
            else:
                reply = CommandReply(False, "session-active", self._session.state)

            if not reply.ok:
                self._clear_press_locked()
                return reply
            if config.interaction_mode == "push_to_talk" and reply.state in (
                VoiceState.STARTING,
                VoiceState.RECORDING,
            ):
                self._owns_session = True
            return reply

    def release(self, *, event_time: float | None = None) -> CommandReply:
        """Handle one key-up edge and stop/cancel only an owned PTT session."""

        released_at = float(event_time) if event_time is not None else self._monotonic()
        with self._lock:
            if released_at < self._last_event_time:
                return CommandReply(True, "stale-edge-ignored", self._session.state)
            self._last_event_time = released_at
            if not self._pressed:
                return CommandReply(True, "stray-release-ignored", self._session.state)
            held_seconds = max(0.0, released_at - self._press_started)
            mode = self._press_mode
            owns_session = self._owns_session
            minimum_hold_seconds = self._minimum_hold_seconds
            self._clear_press_locked()

            if mode == "toggle" or not owns_session:
                return CommandReply(True, "released", self._session.state)
            if held_seconds < minimum_hold_seconds:
                reply = self._session.cancel()
                return CommandReply(
                    reply.ok,
                    "short-press-cancelled" if reply.ok else reply.code,
                    reply.state,
                )
            if self._session.state in (VoiceState.STARTING, VoiceState.RECORDING):
                return self._session.stop()
            # Provider completion/error may have advanced the state before the
            # physical release was delivered.  Never turn that release into a
            # new toggle or cancel an accepted final.
            return CommandReply(True, "released", self._session.state)

    def cancel(self) -> CommandReply:
        """ESC semantics: clear the held key and cancel any active lifecycle."""

        with self._lock:
            self._clear_press_locked()
            return self._session.cancel()

    def reset_for_explicit_command(self) -> None:
        """Disown a held key before a legacy start/stop/toggle command."""

        with self._lock:
            self._clear_press_locked()

    def close(self) -> None:
        with self._lock:
            self._clear_press_locked()

    def _arm_release_timer_locked(self, serial: int, seconds: int) -> None:
        try:
            timer = self._timer_factory(
                float(seconds), lambda: self._on_release_timeout(serial)
            )
            if hasattr(timer, "daemon"):
                timer.daemon = True
            self._release_timer = timer
            timer.start()
        except Exception:
            # Starting without a working safety timer would defeat the lost
            # release guarantee.  Leave a sentinel; press() will fail closed.
            self._release_timer = None
            self._pressed = False
            raise

    def _on_release_timeout(self, serial: int) -> None:
        with self._lock:
            if serial != self._serial or not self._pressed:
                return
            mode = self._press_mode
            owns_session = self._owns_session
            self._clear_press_locked(cancel_timer=False)
            if (
                mode == "push_to_talk"
                and owns_session
                and self._session.state in (VoiceState.STARTING, VoiceState.RECORDING)
            ):
                logger.warning("Push-to-talk release timeout stopped dictation")
                self._session.stop()

    def _clear_press_locked(self, *, cancel_timer: bool = True) -> None:
        timer = self._release_timer
        self._release_timer = None
        if cancel_timer and timer is not None:
            timer.cancel()
        self._pressed = False
        self._owns_session = False
        self._press_started = 0.0
        self._minimum_hold_seconds = 0.0
