# SPDX-License-Identifier: GPL-3.0-only
# Portions adapted from Doubao Murmur, Copyright (c) 2026 lilong7676,
# supplied under MIT. Modifications Copyright (c) 2026 Open Voice Input Linux
# contributors. See NOTICE.md for source paths and the preserved MIT terms.
"""Route one strict utterance through the existing Preedit1 engine ABI."""

# ruff: noqa: E402 -- GI version selection must precede repository imports.

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .engine_restore import (
    PREEDIT_ENGINE,
    EngineRestoreState,
    RestoreError,
    command_candidates,
    parse_engine_name,
    restore_saved_engine,
)

PREEDIT_BUS_NAME = "org.murmur.IME.Preedit1"
PREEDIT_OBJECT_PATH = "/org/murmur/IME/Preedit1"
PREEDIT_INTERFACE = "org.murmur.IME.Preedit1"

_DBUS_TIMEOUT_MS = 250
_IBUS_TIMEOUT_SECONDS = 3
_ENGINE_SWITCH_VERIFY_SECONDS = 1.0
_MAX_UINT64 = (1 << 64) - 1
_MAX_UTTERANCE_ID_LENGTH = 128
_MAX_TEXT_CODEPOINTS = 4096
_MAX_TEXT_UTF8_BYTES = 16 * 1024
_UTTERANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

logger = logging.getLogger(__name__)


class AcquireResult(Enum):
    """Why the focused IBus context did or did not accept voice preedit."""

    ACQUIRED = "acquired"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True, repr=False)
class ObservationSnapshot:
    """One bounded post-commit surrounding-text observation.

    Text is deliberately hidden from reprs and must never be logged. Offsets
    are Unicode code-point positions in ``baseline_text``/``current_text``.
    """

    baseline_text: str
    committed_start: int
    committed_end: int
    current_text: str
    cursor: int
    anchor: int


def _default_proxy_factory() -> Gio.DBusProxy:
    return Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SESSION,
        Gio.DBusProxyFlags.DO_NOT_AUTO_START,
        None,
        PREEDIT_BUS_NAME,
        PREEDIT_OBJECT_PATH,
        PREEDIT_INTERFACE,
        None,
    )


class PreeditClient:
    """Temporarily select murmur-voice and preserve one D-Bus sender.

    A single instance owns a single Gio.DBusProxy for its lifetime. The engine
    binds that proxy's unique sender, the utterance id, and its current focus
    token. Failed text delivery is never converted into clipboard injection.
    """

    def __init__(
        self,
        *,
        proxy_factory: Callable[[], Any] | None = None,
        command_provider: Callable[[str], list[list[str]]] | None = None,
        command_runner: Callable[..., Any] | None = None,
        dbus_timeout_ms: int = _DBUS_TIMEOUT_MS,
        acquire_retry_seconds: float = 1.0,
        acquire_retry_interval: float = 0.05,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        restore_state: EngineRestoreState | None = None,
    ) -> None:
        self._proxy_factory = proxy_factory or _default_proxy_factory
        self._command_provider = command_provider or command_candidates
        self._command_runner = command_runner or subprocess.run
        self._dbus_timeout_ms = max(1, int(dbus_timeout_ms))
        self._acquire_retry_seconds = max(0.0, float(acquire_retry_seconds))
        self._acquire_retry_interval = max(0.001, float(acquire_retry_interval))
        self._monotonic = monotonic or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._restore_state = restore_state

        self._proxy: Any | None = None
        self._utterance_id: str | None = None
        self._last_revision = 0
        self._original_engine: str | None = None
        self._switched_engine = False
        self._pending_restore_engine: str | None = None
        self._lock = threading.RLock()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._utterance_id is not None

    @property
    def utterance_id(self) -> str | None:
        with self._lock:
            return self._utterance_id

    @property
    def last_revision(self) -> int:
        with self._lock:
            return self._last_revision

    @property
    def restore_pending(self) -> bool:
        with self._lock:
            return self._pending_restore_engine is not None

    def current_engine(self) -> str | None:
        for prefix in self._ibus_command_candidates():
            try:
                result = self._command_runner(
                    prefix + ["engine"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=_IBUS_TIMEOUT_SECONDS,
                )
            except Exception:
                continue
            engine = self._parse_engine_name(getattr(result, "stdout", ""))
            if engine is not None:
                return engine
        logger.warning("Could not determine the current IBus engine")
        return None

    def acquire(self, utterance_id: str) -> bool:
        return self.acquire_result(utterance_id) is AcquireResult.ACQUIRED

    def acquire_result(self, utterance_id: str) -> AcquireResult:
        """Acquire after the temporary IBus fake-focus switch has settled."""

        if not self._valid_utterance_id(utterance_id):
            return AcquireResult.REJECTED
        with self._lock:
            if self._utterance_id is not None:
                return AcquireResult.REJECTED
            if not self._retry_pending_restore():
                return AcquireResult.REJECTED
            if not self._recover_saved_engine():
                return AcquireResult.UNAVAILABLE

            original_engine = self.current_engine()
            if original_engine is None:
                return AcquireResult.UNAVAILABLE
            self._original_engine = original_engine
            self._switched_engine = original_engine != PREEDIT_ENGINE
            if self._switched_engine:
                state = self._state()
                if state is None:
                    self._clear_session_state()
                    return AcquireResult.UNAVAILABLE
                try:
                    state.record(original_engine)
                except RestoreError:
                    logger.warning("Could not record private IBus restore state")
                    self._clear_session_state()
                    return AcquireResult.UNAVAILABLE
                if not self._set_engine(PREEDIT_ENGINE):
                    self._restore_original_engine()
                    self._clear_session_state()
                    return AcquireResult.UNAVAILABLE

            accepted = False
            outcome = AcquireResult.REJECTED
            saw_rejection = False
            saw_failure = False
            try:
                deadline = self._monotonic() + self._acquire_retry_seconds
                while True:
                    response = self._call_optional_bool(
                        "Acquire",
                        GLib.Variant("(s)", (utterance_id,)),
                        log_failure=False,
                    )
                    if response is None:
                        saw_failure = True
                    elif response is False:
                        saw_rejection = True
                    else:
                        accepted = True
                        outcome = AcquireResult.ACQUIRED
                        break

                    if self._monotonic() >= deadline:
                        if saw_rejection:
                            outcome = AcquireResult.REJECTED
                        elif self._proxy_has_owner() is False:
                            outcome = AcquireResult.UNAVAILABLE
                        else:
                            outcome = AcquireResult.REJECTED
                        break
                    self._sleeper(self._acquire_retry_interval)
            finally:
                if not accepted:
                    if saw_failure:
                        logger.warning("Murmur preedit Acquire call failed")
                    self._restore_original_engine()
                    self._clear_session_state()

            if accepted:
                self._utterance_id = utterance_id
                self._last_revision = 0
            return outcome

    def partial(self, utterance_id: str, revision: int, text: str) -> bool:
        with self._lock:
            if not self._valid_event(utterance_id, revision, text):
                return False
            accepted = self._call_bool(
                "Partial",
                GLib.Variant("(sts)", (utterance_id, revision, text)),
            )
            if accepted:
                self._last_revision = revision
            return accepted

    def final(self, utterance_id: str, revision: int, text: str) -> bool:
        with self._lock:
            if not self._valid_event(utterance_id, revision, text):
                return False
            accepted = self._call_bool(
                "Final",
                GLib.Variant("(sts)", (utterance_id, revision, text)),
            )
            if accepted:
                self._last_revision = revision
                # Keep murmur-voice selected for the short observation lease.
                # finish_observation(), cancel(), or close() always restores it.
                return True
            # A timeout can hide a Final that the engine already accepted.
            # Best-effort Cancel before dropping the utterance releases that
            # possible observation lease; it is harmless after an explicit
            # rejection and never attempts to undo already committed text.
            self._call_optional_bool(
                "Cancel",
                GLib.Variant("(s)", (utterance_id,)),
                log_failure=False,
            )
            self._restore_original_engine()
            self._clear_session_state()
            return False

    def finish_observation(self, utterance_id: str) -> ObservationSnapshot | None:
        """Consume one engine observation and always restore the prior engine."""

        with self._lock:
            if utterance_id != self._utterance_id:
                return None
            try:
                unpacked = self._call_unpacked(
                    "FinishObservation",
                    GLib.Variant("(s)", (utterance_id,)),
                )
                if (
                    not isinstance(unpacked, tuple)
                    or len(unpacked) != 7
                    or type(unpacked[0]) is not bool
                    or not isinstance(unpacked[1], str)
                    or type(unpacked[2]) is not int
                    or type(unpacked[3]) is not int
                    or not isinstance(unpacked[4], str)
                    or type(unpacked[5]) is not int
                    or type(unpacked[6]) is not int
                    or not unpacked[0]
                ):
                    return None
                baseline, start, end, current, cursor, anchor = unpacked[1:]
                if (
                    not self._valid_text(baseline)
                    or not self._valid_text(current)
                    or not (0 <= start <= end <= len(baseline))
                    or not (0 <= cursor <= len(current))
                    or not (0 <= anchor <= len(current))
                ):
                    return None
                return ObservationSnapshot(
                    baseline_text=baseline,
                    committed_start=start,
                    committed_end=end,
                    current_text=current,
                    cursor=cursor,
                    anchor=anchor,
                )
            finally:
                self._restore_original_engine()
                self._clear_session_state()

    def cancel(self, utterance_id: str) -> bool:
        with self._lock:
            if utterance_id != self._utterance_id:
                return False
            try:
                return self._call_bool("Cancel", GLib.Variant("(s)", (utterance_id,)))
            finally:
                self._restore_original_engine()
                self._clear_session_state()

    def close(self) -> None:
        with self._lock:
            utterance_id = self._utterance_id
            if utterance_id is not None:
                self.cancel(utterance_id)
            self._retry_pending_restore()

    def _valid_event(self, utterance_id: str, revision: int, text: str) -> bool:
        return (
            utterance_id == self._utterance_id
            and isinstance(revision, int)
            and not isinstance(revision, bool)
            and self._last_revision < revision <= _MAX_UINT64
            and self._valid_text(text)
        )

    def _call_bool(self, method: str, parameters: GLib.Variant) -> bool:
        return self._call_optional_bool(method, parameters) is True

    def _call_optional_bool(
        self,
        method: str,
        parameters: GLib.Variant,
        *,
        log_failure: bool = True,
    ) -> bool | None:
        unpacked = self._call_unpacked(method, parameters, log_failure=log_failure)
        if (
            not isinstance(unpacked, tuple)
            or len(unpacked) != 1
            or type(unpacked[0]) is not bool
        ):
            return None
        return unpacked[0]

    def _call_unpacked(
        self,
        method: str,
        parameters: GLib.Variant,
        *,
        log_failure: bool = True,
    ) -> object | None:
        try:
            if self._proxy is None:
                self._proxy = self._proxy_factory()
            result = self._proxy.call_sync(
                method,
                parameters,
                Gio.DBusCallFlags.NO_AUTO_START,
                self._dbus_timeout_ms,
                None,
            )
            return result.unpack()
        except Exception:
            # Remote exceptions may reflect method parameters, so never log
            # their string form.
            if log_failure:
                logger.warning("Murmur preedit D-Bus call failed (%s)", method)
            return None

    def _proxy_has_owner(self) -> bool | None:
        try:
            if self._proxy is None:
                self._proxy = self._proxy_factory()
            getter = getattr(self._proxy, "get_name_owner", None)
            return None if getter is None else bool(getter())
        except Exception:
            return None

    def _restore_original_engine(self) -> bool:
        original_engine = self._original_engine or self._pending_restore_engine
        if not self._switched_engine or original_engine is None:
            return True
        restored = self._restore_engine_preserving_user_choice(original_engine)
        if restored:
            self._pending_restore_engine = None
        else:
            self._pending_restore_engine = original_engine
            logger.warning("Could not restore the previous IBus engine")
        return restored

    def _retry_pending_restore(self) -> bool:
        engine = self._pending_restore_engine
        if engine is None:
            return True
        if not self._restore_engine_preserving_user_choice(engine):
            return False
        self._pending_restore_engine = None
        return True

    def _restore_engine_preserving_user_choice(self, engine: str) -> bool:
        current = self.current_engine()
        if current is None:
            return False
        if current == PREEDIT_ENGINE and not self._set_engine(engine):
            return False
        # If current is neither murmur nor the saved engine, the user selected
        # a newer real engine during the observation. Preserve that choice and
        # retire only our stale crash-recovery record.
        return self._clear_restore_state(engine)

    def _recover_saved_engine(self) -> bool:
        state = self._state()
        if state is None:
            return False
        return restore_saved_engine(
            state,
            current_engine=self.current_engine,
            set_engine=self._set_engine,
        )

    def _clear_restore_state(self, engine: str) -> bool:
        state = self._state()
        if state is None:
            return False
        try:
            state.clear(engine)
        except RestoreError:
            logger.warning("Could not clear private IBus restore state")
            return False
        return True

    def _state(self) -> EngineRestoreState | None:
        if self._restore_state is None:
            try:
                self._restore_state = EngineRestoreState()
            except RestoreError:
                logger.warning("Private IBus restore state is unavailable")
                return None
        return self._restore_state

    def _set_engine(self, engine: str) -> bool:
        for prefix in self._ibus_command_candidates():
            try:
                self._command_runner(
                    prefix + ["engine", engine],
                    # Ubuntu IBus may return 1 even when the switch succeeded.
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_IBUS_TIMEOUT_SECONDS,
                )
            except Exception:
                continue
            deadline = self._monotonic() + _ENGINE_SWITCH_VERIFY_SECONDS
            while True:
                if self.current_engine() == engine:
                    return True
                if self._monotonic() >= deadline:
                    break
                self._sleeper(self._acquire_retry_interval)
        logger.warning("Could not switch the IBus engine")
        return False

    def _ibus_command_candidates(self) -> list[list[str]]:
        commands = [list(item) for item in self._command_provider("ibus")]
        host_commands = [
            command
            for command in commands
            if command[:2] == ["flatpak-spawn", "--host"]
        ]
        return host_commands or commands

    def _clear_session_state(self) -> None:
        self._utterance_id = None
        self._last_revision = 0
        self._original_engine = None
        self._switched_engine = False

    @staticmethod
    def _valid_utterance_id(value: str) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and len(value) <= _MAX_UTTERANCE_ID_LENGTH
            and _UTTERANCE_ID_RE.fullmatch(value) is not None
        )

    @staticmethod
    def _valid_text(value: str) -> bool:
        return (
            isinstance(value, str)
            and "\x00" not in value
            and len(value) <= _MAX_TEXT_CODEPOINTS
            and len(value.encode("utf-8")) <= _MAX_TEXT_UTF8_BYTES
        )

    @staticmethod
    def _parse_engine_name(output: Any) -> str | None:
        return parse_engine_name(output)
