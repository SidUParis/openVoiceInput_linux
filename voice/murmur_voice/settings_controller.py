"""Secret-safe settings operations for the native GTK application."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import (
    ConfigError,
    default_config_path,
    default_vocabulary_path,
    load_config,
    load_vocabulary,
    normalize_vocabulary_terms,
    save_api_key,
    save_vocabulary,
)
from .control import ControlError, request_command

VOICE_SERVICE = "murmur-ime-voice.service"
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMCTL_TIMEOUT_SECONDS = 5.0

_ACTIVE_STATES = frozenset(
    {
        "active",
        "activating",
        "deactivating",
        "failed",
        "inactive",
        "reloading",
    }
)
_SESSION_STATES = frozenset({"idle", "starting", "recording", "stopping"})
_STATUS_CODES = frozenset(
    {
        "audio-backpressure",
        "capture-start-failed",
        "cancelled",
        "daemon-closed",
        "daemon-shutdown",
        "final-timeout",
        "none",
        "preedit-final-rejected",
        "preedit-lost",
        "preedit-rejected",
        "preedit-unavailable",
        "provider-auth",
        "provider-error",
        "recording-limit-warning",
        "status",
    }
)


class SettingsError(RuntimeError):
    """A safe-to-display settings error without secrets or vocabulary terms."""


class KeyState(str, Enum):
    """Whether a usable private key-only configuration exists."""

    MISSING = "missing"
    READY = "ready"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    """Allowlisted service and dictation lifecycle state for presentation."""

    active_state: str
    session_state: str | None = None
    status_code: str | None = None


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str | bytes | None


Runner = Callable[..., CompletedProcessLike]
StatusReader = Callable[[str], dict[str, Any]]


class SettingsController:
    """Keep GTK widgets separate from private files and service processes."""

    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        vocabulary_path: str | Path | None = None,
        runner: Runner = subprocess.run,
        status_reader: StatusReader = request_command,
    ) -> None:
        self._config_path = (
            Path(config_path) if config_path is not None else default_config_path()
        )
        self._vocabulary_path = (
            Path(vocabulary_path)
            if vocabulary_path is not None
            else default_vocabulary_path()
        )
        self._runner = runner
        self._status_reader = status_reader

    def key_state(self) -> KeyState:
        """Validate the key file without returning its value to the view."""

        try:
            load_config(self._config_path)
        except ConfigError:
            if not self._config_path.exists() and not self._config_path.is_symlink():
                return KeyState.MISSING
            return KeyState.INVALID
        return KeyState.READY

    def load_vocabulary(self) -> tuple[str, ...]:
        """Load the explicit private vocabulary for editing."""

        try:
            return load_vocabulary(self._vocabulary_path)
        except ConfigError as error:
            raise SettingsError(
                "The personal vocabulary could not be loaded safely."
            ) from error

    def save_key(self, api_key: str) -> None:
        """Persist a replacement key without testing it or restarting services."""

        try:
            save_api_key(api_key, self._config_path)
        except ConfigError as error:
            raise SettingsError("The API key could not be saved safely.") from error

    def save_vocabulary_text(self, text: str) -> int:
        """Store nonblank lines and return only the resulting entry count."""

        terms = [line for line in text.split("\n") if line.strip()]
        try:
            normalized = normalize_vocabulary_terms(terms)
            save_vocabulary(normalized, self._vocabulary_path)
        except ConfigError as error:
            raise SettingsError(
                "The personal vocabulary could not be saved safely."
            ) from error
        return len(normalized)

    def service_status(self) -> ServiceSnapshot:
        """Read service state and, when available, the bounded daemon status."""

        result = self._run_systemctl("is-active")
        raw_state = result.stdout if isinstance(result.stdout, str) else ""
        active_state = raw_state.strip()
        if active_state not in _ACTIVE_STATES:
            active_state = "unknown"
        if active_state != "active":
            return ServiceSnapshot(active_state)

        try:
            response = self._status_reader("status")
        except (ControlError, OSError):
            return ServiceSnapshot("active", "unavailable", "unavailable")
        if not isinstance(response, dict):
            return ServiceSnapshot("active", "unknown", "unknown")

        raw_session = response.get("state")
        session_state = raw_session if raw_session in _SESSION_STATES else "unknown"
        raw_code = response.get("code")
        status_code = raw_code if raw_code in _STATUS_CODES else "unknown"
        return ServiceSnapshot("active", session_state, status_code)

    def start_service(self) -> None:
        """Explicitly enable and start the service after local validation."""

        if self.key_state() is not KeyState.READY:
            raise SettingsError(
                "A valid saved API key is required to start the service."
            )
        try:
            load_vocabulary(self._vocabulary_path)
        except ConfigError as error:
            raise SettingsError(
                "A valid personal vocabulary is required to start the service."
            ) from error
        result = self._run_systemctl("start")
        if result.returncode != 0:
            raise SettingsError("The voice service could not be started.")

    def stop_service(self) -> None:
        """Explicitly disable and stop; this may cancel active dictation."""

        result = self._run_systemctl("stop")
        if result.returncode != 0:
            raise SettingsError("The voice service could not be stopped.")

    def _run_systemctl(self, action: str) -> CompletedProcessLike:
        if action not in {"is-active", "start", "stop"}:
            raise SettingsError("Unsupported service operation.")
        commands = {
            "is-active": (SYSTEMCTL, "--user", "is-active", VOICE_SERVICE),
            "start": (SYSTEMCTL, "--user", "enable", "--now", VOICE_SERVICE),
            "stop": (SYSTEMCTL, "--user", "disable", "--now", VOICE_SERVICE),
        }
        command = commands[action]
        try:
            return self._runner(
                command,
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=SYSTEMCTL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SettingsError("The user service manager is unavailable.") from error
