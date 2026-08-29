"""Secret-safe settings operations for the native GTK application."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from .adaptive_runtime import load_adaptive_ledger
from .config import (
    ConfigError,
    MAX_CORRECTION_PAIRS,
    MAX_CORRECTION_TEXT_CHARACTERS,
    default_corrections_path,
    default_config_path,
    default_vocabulary_path,
    default_adaptive_corrections_path,
    delete_api_key,
    load_config,
    load_corrections as load_corrections_file,
    load_vocabulary,
    normalize_correction_pairs,
    normalize_vocabulary_terms,
    save_api_key,
    save_corrections as save_corrections_file,
    save_vocabulary,
)
from .control import ControlError, request_command

VOICE_SERVICE = "murmur-ime-voice.service"
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMCTL_TIMEOUT_SECONDS = 5.0
CORRECTION_PAIR_LIMIT = MAX_CORRECTION_PAIRS
CORRECTION_TEXT_LIMIT = MAX_CORRECTION_TEXT_CHARACTERS

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
_SESSION_STATES = frozenset({"idle", "starting", "recording", "stopping", "observing"})
_STATUS_CODES = frozenset(
    {
        "adaptive-correction-failed",
        "adaptive-correction-learned",
        "audio-backpressure",
        "capture-start-failed",
        "cancelled",
        "daemon-closed",
        "daemon-shutdown",
        "final-timeout",
        "microphone-unavailable",
        "none",
        "preedit-final-rejected",
        "preedit-lost",
        "preedit-rejected",
        "preedit-unavailable",
        "provider-auth",
        "provider-error",
        "recording-limit-warning",
        "recognition-context-invalid",
        "start-timeout",
        "status",
    }
)


class SettingsError(RuntimeError):
    """A safe-to-display settings error without secrets or private terms."""


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
        corrections_path: str | Path | None = None,
        adaptive_corrections_path: str | Path | None = None,
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
        self._corrections_path = (
            Path(corrections_path)
            if corrections_path is not None
            else default_corrections_path()
        )
        self._adaptive_corrections_path = (
            Path(adaptive_corrections_path)
            if adaptive_corrections_path is not None
            else default_adaptive_corrections_path()
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

    def load_corrections(self) -> tuple[tuple[str, str], ...]:
        """Load explicit corrections without exposing them through errors."""

        try:
            pairs = load_corrections_file(self._corrections_path)
        except ConfigError as error:
            raise SettingsError(
                "The explicit corrections could not be loaded safely."
            ) from error
        return tuple((pair.wrong, pair.canonical) for pair in pairs)

    def save_key(self, api_key: str) -> None:
        """Persist a replacement key without testing it or restarting services."""

        try:
            save_api_key(api_key, self._config_path)
        except ConfigError as error:
            raise SettingsError("The API key could not be saved safely.") from error

    def clear_key(self) -> bool:
        """Remove the local key only after proving the service is inactive."""

        stop_message = (
            "Disable and stop the voice service before clearing the saved API key."
        )
        try:
            active_state = self._service_active_state()
        except SettingsError as error:
            raise SettingsError(stop_message) from error
        if active_state != "inactive":
            raise SettingsError(stop_message)
        try:
            return delete_api_key(self._config_path)
        except ConfigError as error:
            raise SettingsError(
                "The saved API key could not be removed safely."
            ) from error

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

    def save_corrections(self, pairs: Any) -> int:
        """Store explicit correction pairs locally and return only their count."""

        try:
            if not isinstance(pairs, (list, tuple)):
                raise ConfigError("explicit correction pairs must be a list")
            documents: list[dict[str, Any]] = []
            for pair in pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise ConfigError("explicit correction pair is invalid")
                documents.append({"wrong": pair[0], "canonical": pair[1]})
            normalized = normalize_correction_pairs(documents)
            save_corrections_file(normalized, self._corrections_path)
        except ConfigError as error:
            raise SettingsError(
                "The explicit corrections could not be saved safely."
            ) from error
        return len(normalized)

    def service_status(self) -> ServiceSnapshot:
        """Read service state and, when available, the bounded daemon status."""

        active_state = self._service_active_state()
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

    def _service_active_state(self) -> str:
        """Return only an allowlisted systemd active state."""

        result = self._run_systemctl("is-active")
        raw_state = result.stdout if isinstance(result.stdout, str) else ""
        active_state = raw_state.strip()
        if active_state not in _ACTIVE_STATES:
            return "unknown"
        return active_state

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
        try:
            load_corrections_file(self._corrections_path)
        except ConfigError as error:
            raise SettingsError(
                "Valid explicit corrections are required to start the service."
            ) from error
        try:
            load_adaptive_ledger(self._adaptive_corrections_path)
        except ConfigError as error:
            raise SettingsError(
                "Valid adaptive corrections are required to start the service."
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
