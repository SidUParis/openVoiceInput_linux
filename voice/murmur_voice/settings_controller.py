"""Secret-safe settings operations for the native GTK application."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from .adaptive_runtime import (
    adaptive_review_entries,
    adaptive_status_document,
    confirm_adaptive_correction,
    load_adaptive_ledger,
    MAX_EXPLICIT_FEEDBACK_TEXT_CHARACTERS,
    submit_explicit_feedback,
)
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
    save_provider_config,
    save_vocabulary,
)
from .control import (
    ControlError,
    LastReview,
    ReviewSubmitReply,
    request_command,
    request_last_review,
    submit_last_review as request_review_submission,
)
from .data_collection import (
    DataCollectionConfig,
    default_data_collection_config_path,
    load_data_collection_config,
    save_data_collection_config,
)
from .interaction import (
    InteractionConfig,
    default_interaction_config_path,
    load_interaction_config,
    save_interaction_config,
)
from .microphone_policy import (
    MicrophonePolicyConfig,
    default_microphone_policy_config_path,
    load_microphone_policy_config,
    save_microphone_policy_config,
)
from .output_style import (
    OutputStyleConfig,
    default_output_style_config_path,
    load_output_style_config,
    save_output_style_config,
)
from .output_target import (
    OutputTargetConfig,
    default_output_target_config_path,
    load_output_target_config,
    save_output_target_config,
)

VOICE_SERVICE = "murmur-ime-voice.service"
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMCTL_TIMEOUT_SECONDS = 5.0
CORRECTION_PAIR_LIMIT = MAX_CORRECTION_PAIRS
CORRECTION_TEXT_LIMIT = MAX_CORRECTION_TEXT_CHARACTERS
_DATASET_DIRECTORY = "openvoiceinput-dataset-v1"
_DATASET_MARKER_KIND = "openvoiceinput-personal-asr-dataset"
_USAGE_SUMMARY_KIND = "openvoiceinput-private-usage-summary"
_USAGE_SUMMARY_MAX_BYTES = 16 * 1024
_DATASET_STATISTICS_MAX_RECORDS = 100_000
ADAPTIVE_FEEDBACK_TEXT_LIMIT = MAX_EXPLICIT_FEEDBACK_TEXT_CHARACTERS

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
        "adaptive-correction-candidate",
        "adaptive-correction-conflicted",
        "adaptive-correction-learned",
        "adaptive-correction-skipped",
        "audio-backpressure",
        "capture-start-failed",
        "clipboard-armed",
        "clipboard-copy-failed",
        "clipboard-ready",
        "clipboard-unavailable",
        "cancelled",
        "daemon-closed",
        "daemon-shutdown",
        "data-collection-failed",
        "data-collection-unavailable",
        "final-timeout",
        "microphone-unavailable",
        "microphone-policy-invalid",
        "output-style-invalid",
        "output-target-invalid",
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


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    """Secret-free provider state safe to render in settings."""

    provider: str
    model: str | None


@dataclass(frozen=True, slots=True)
class DatasetStatistics:
    """Content-free counters derived only from local usage metadata."""

    state: str
    today_characters: int = 0
    today_seconds: float = 0.0
    today_utterances: int = 0
    total_characters: int = 0
    total_seconds: float = 0.0
    total_utterances: int = 0
    latest_recorded_at: datetime | None = None
    invalid_summaries: int = 0


@dataclass(frozen=True, slots=True, repr=False)
class AdaptiveReviewEntry:
    wrong: str
    canonical: str
    state: str
    support: int
    category: str


@dataclass(frozen=True, slots=True, repr=False)
class AdaptiveLearningSnapshot:
    statistics: dict[str, int]
    last_result: dict[str, Any] | None
    review_entries: tuple[AdaptiveReviewEntry, ...]
    provider_view: dict[str, Any] = field(default_factory=dict)


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str | bytes | None


Runner = Callable[..., CompletedProcessLike]
StatusReader = Callable[[str], dict[str, Any]]
ReviewReader = Callable[[], LastReview | None]
ReviewSubmitter = Callable[[str, str], ReviewSubmitReply]


class SettingsController:
    """Keep GTK widgets separate from private files and service processes."""

    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        vocabulary_path: str | Path | None = None,
        corrections_path: str | Path | None = None,
        adaptive_corrections_path: str | Path | None = None,
        data_collection_path: str | Path | None = None,
        microphone_policy_path: str | Path | None = None,
        interaction_path: str | Path | None = None,
        output_style_path: str | Path | None = None,
        output_target_path: str | Path | None = None,
        runner: Runner = subprocess.run,
        status_reader: StatusReader = request_command,
        review_reader: ReviewReader = request_last_review,
        review_submitter: ReviewSubmitter = request_review_submission,
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
        self._data_collection_path = (
            Path(data_collection_path)
            if data_collection_path is not None
            else default_data_collection_config_path()
        )
        self._microphone_policy_path = (
            Path(microphone_policy_path)
            if microphone_policy_path is not None
            else default_microphone_policy_config_path()
        )
        self._interaction_path = (
            Path(interaction_path)
            if interaction_path is not None
            else default_interaction_config_path()
        )
        self._output_style_path = (
            Path(output_style_path)
            if output_style_path is not None
            else default_output_style_config_path()
        )
        self._output_target_path = (
            Path(output_target_path)
            if output_target_path is not None
            else default_output_target_config_path()
        )
        self._runner = runner
        self._status_reader = status_reader
        self._review_reader = review_reader
        self._review_submitter = review_submitter

    def key_state(self) -> KeyState:
        """Validate the key file without returning its value to the view."""

        try:
            load_config(self._config_path)
        except ConfigError:
            if not self._config_path.exists() and not self._config_path.is_symlink():
                return KeyState.MISSING
            return KeyState.INVALID
        return KeyState.READY

    def provider_selection(self) -> ProviderSelection | None:
        """Return the selected backend without ever returning its key."""

        try:
            config = load_config(self._config_path)
        except ConfigError:
            return None
        return ProviderSelection(config.provider, config.model)

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

    def load_adaptive_learning(self) -> AdaptiveLearningSnapshot:
        """Load counts, recent reason, and locally reviewable candidates."""

        try:
            status = adaptive_status_document(
                self._adaptive_corrections_path,
                corrections_path=self._corrections_path,
                vocabulary_path=self._vocabulary_path,
            )
            entries = adaptive_review_entries(self._adaptive_corrections_path)
        except ConfigError as error:
            raise SettingsError(
                "Adaptive learning information could not be loaded safely."
            ) from error
        return AdaptiveLearningSnapshot(
            statistics=dict(status["statistics"]),
            last_result=(
                dict(status["last_result"])
                if status["last_result"] is not None
                else None
            ),
            review_entries=tuple(
                AdaptiveReviewEntry(
                    entry.wrong,
                    entry.canonical,
                    entry.state,
                    entry.support,
                    entry.category,
                )
                for entry in entries
            ),
            provider_view=dict(status.get("provider_view", {})),
        )

    def confirm_adaptive_learning(self, wrong: str, canonical: str) -> bool:
        """Explicitly activate one candidate without restarting the service."""

        return self._confirm_adaptive_learning(wrong, canonical).activated_count > 0

    def confirm_adaptive_learning_reason(self, wrong: str, canonical: str) -> str:
        """Activate one candidate and return its content-free compiler reason."""

        return self._confirm_adaptive_learning(wrong, canonical).reason_code

    def _confirm_adaptive_learning(self, wrong: str, canonical: str) -> Any:
        """Run one verified confirmation while keeping pair text out of errors."""

        try:
            result = confirm_adaptive_correction(
                self._adaptive_corrections_path,
                self._corrections_path,
                wrong,
                canonical,
            )
        except (ConfigError, ValueError) as error:
            raise SettingsError(
                "The adaptive correction could not be confirmed safely."
            ) from error
        return result

    def submit_adaptive_feedback(
        self,
        provider_text: str,
        preferred_text: str,
    ) -> str:
        """Submit an explicit whole-utterance edit when auto-capture is absent."""

        try:
            result = submit_explicit_feedback(
                self._adaptive_corrections_path,
                self._corrections_path,
                self._vocabulary_path,
                provider_text,
                preferred_text,
            )
        except (ConfigError, ValueError) as error:
            raise SettingsError(
                "The explicit adaptive feedback could not be saved safely."
            ) from error
        return result.reason_code

    def load_last_review(self) -> LastReview | None:
        """Load one recent provider final from the host-only volatile channel."""

        try:
            return self._review_reader()
        except (ControlError, OSError, ValueError) as error:
            raise SettingsError(
                "The recent recognition result could not be loaded safely."
            ) from error

    def submit_last_review(
        self,
        utterance_id: str,
        spoken_verbatim: str,
    ) -> ReviewSubmitReply:
        """Submit one ID-bound review through the daemon-owned transaction."""

        try:
            result = self._review_submitter(utterance_id, spoken_verbatim)
        except ControlError as error:
            if str(error) == "stale-review":
                raise SettingsError(
                    "The recent recognition result expired or was replaced."
                ) from error
            if str(error) == "session-active":
                raise SettingsError("请先结束当前听写，再重新提交这次复核。") from error
            raise SettingsError(
                "The reviewed recognition result could not be submitted safely."
            ) from error
        if not isinstance(result, ReviewSubmitReply) or not result.ok:
            raise SettingsError(
                "The reviewed recognition result was not accepted by the daemon."
            )
        return result

    def save_key(self, api_key: str) -> None:
        """Persist a replacement key without testing it or restarting services."""

        try:
            try:
                current = load_config(self._config_path)
            except ConfigError:
                current = None
            if current is None or (
                current.provider == "volcengine" and current.model is None
            ):
                save_api_key(api_key, self._config_path)
            else:
                save_provider_config(
                    api_key,
                    current.provider,
                    current.model,
                    self._config_path,
                )
        except ConfigError as error:
            raise SettingsError("The API key could not be saved safely.") from error

    def save_provider(
        self,
        api_key: str,
        provider: str,
        model: str | None = None,
    ) -> ProviderSelection:
        """Save one ready backend and its replacement key as one transaction."""

        try:
            save_provider_config(api_key, provider, model, self._config_path)
            config = load_config(self._config_path)
        except ConfigError as error:
            raise SettingsError(
                "The recognition provider could not be saved safely."
            ) from error
        return ProviderSelection(config.provider, config.model)

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

    def load_data_collection(self) -> DataCollectionConfig:
        """Return only the explicit local-retention choice and selected path."""

        try:
            return load_data_collection_config(self._data_collection_path)
        except ConfigError as error:
            raise SettingsError(
                "The local data collection setting could not be loaded safely."
            ) from error

    def load_dataset_statistics(
        self,
        *,
        now: datetime | None = None,
    ) -> DatasetStatistics:
        """Aggregate content-free local usage summaries without opening transcripts."""

        collection = self.load_data_collection()
        if not collection.enabled:
            return DatasetStatistics("disabled")
        if collection.directory is None or collection.dataset_id is None:
            return DatasetStatistics("unavailable")

        dataset_root = collection.directory / _DATASET_DIRECTORY
        try:
            marker = _read_bounded_json(
                dataset_root / "dataset.json",
                _USAGE_SUMMARY_MAX_BYTES,
            )
            if marker != {
                "schema_version": 1,
                "kind": _DATASET_MARKER_KIND,
                "dataset_id": collection.dataset_id,
            }:
                return DatasetStatistics("unavailable")
            usage_root = dataset_root / "usage"
            try:
                usage_metadata = usage_root.lstat()
            except FileNotFoundError:
                # Datasets made before the private summary index remain valid.
                # Do not inspect their transcript-bearing v1 records to backfill.
                return DatasetStatistics("unindexed")
            if (
                not stat.S_ISDIR(usage_metadata.st_mode)
                or usage_metadata.st_uid != os.getuid()
                or stat.S_IMODE(usage_metadata.st_mode) != 0o700
            ):
                return DatasetStatistics("unavailable")
        except (OSError, ValueError):
            return DatasetStatistics("unavailable")

        local_now = now or datetime.now().astimezone()
        if local_now.tzinfo is None:
            local_now = local_now.astimezone()
        today = local_now.date()
        today_characters = 0
        today_seconds = 0.0
        today_utterances = 0
        total_characters = 0
        total_seconds = 0.0
        total_utterances = 0
        latest_recorded_at: datetime | None = None
        invalid_summaries = 0

        try:
            summaries = usage_root.iterdir()
            for index, summary_path in enumerate(summaries):
                if index >= _DATASET_STATISTICS_MAX_RECORDS:
                    invalid_summaries += 1
                    break
                if summary_path.name.startswith("."):
                    continue
                if summary_path.suffix != ".json" or not _safe_summary_identifier(
                    summary_path.stem
                ):
                    invalid_summaries += 1
                    continue
                try:
                    summary = _read_bounded_json(
                        summary_path,
                        _USAGE_SUMMARY_MAX_BYTES,
                    )
                    recorded_at, duration_ms, character_count = _validate_usage_summary(
                        summary,
                        summary_path.stem,
                    )
                except (OSError, ValueError):
                    invalid_summaries += 1
                    continue
                total_utterances += 1
                total_characters += character_count
                total_seconds += duration_ms / 1000
                if latest_recorded_at is None or recorded_at > latest_recorded_at:
                    latest_recorded_at = recorded_at
                if recorded_at.astimezone(local_now.tzinfo).date() == today:
                    today_utterances += 1
                    today_characters += character_count
                    today_seconds += duration_ms / 1000
        except OSError:
            return DatasetStatistics("unavailable")

        if total_utterances:
            state = "ready"
        elif invalid_summaries:
            state = "limited"
        else:
            state = "empty"
        return DatasetStatistics(
            state=state,
            today_characters=today_characters,
            today_seconds=today_seconds,
            today_utterances=today_utterances,
            total_characters=total_characters,
            total_seconds=total_seconds,
            total_utterances=total_utterances,
            latest_recorded_at=latest_recorded_at,
            invalid_summaries=invalid_summaries,
        )

    def load_microphone_policy(self) -> MicrophonePolicyConfig:
        """Return the private, fixed-category input priority for presentation."""

        try:
            return load_microphone_policy_config(self._microphone_policy_path)
        except ConfigError as error:
            raise SettingsError(
                "The microphone priority setting could not be loaded safely."
            ) from error

    def load_interaction(self) -> InteractionConfig:
        """Return the local press/release interaction preference."""

        try:
            return load_interaction_config(self._interaction_path)
        except ConfigError as error:
            raise SettingsError(
                "The shortcut interaction setting could not be loaded safely."
            ) from error

    def load_output_style(self) -> OutputStyleConfig:
        """Return the terminal output preference without starting dictation."""

        try:
            return load_output_style_config(self._output_style_path)
        except ConfigError as error:
            raise SettingsError(
                "The output style setting could not be loaded safely."
            ) from error

    def save_output_style(self, mode: str) -> OutputStyleConfig:
        """Save locally; a running utterance keeps its frozen start-time mode."""

        try:
            save_output_style_config(mode, self._output_style_path)
            return load_output_style_config(self._output_style_path)
        except (ConfigError, OSError) as error:
            raise SettingsError(
                "The output style setting could not be saved safely."
            ) from error

    def load_output_target(self) -> OutputTargetConfig:
        """Return the explicit final-delivery target without reading clipboard."""

        try:
            return load_output_target_config(self._output_target_path)
        except ConfigError as error:
            raise SettingsError(
                "The output target setting could not be loaded safely."
            ) from error

    def save_output_target(self, target: str) -> OutputTargetConfig:
        """Save locally; the daemon freezes the choice at the next start."""

        try:
            save_output_target_config(target, self._output_target_path)
            return load_output_target_config(self._output_target_path)
        except (ConfigError, OSError) as error:
            raise SettingsError(
                "The output target setting could not be saved safely."
            ) from error

    def save_interaction(
        self,
        interaction_mode: str,
        minimum_hold_milliseconds: int,
        release_timeout_seconds: int,
    ) -> InteractionConfig:
        """Save controls locally; the daemon hot-loads them on the next press."""

        try:
            save_interaction_config(
                interaction_mode,
                minimum_hold_milliseconds,
                release_timeout_seconds,
                self._interaction_path,
            )
            return load_interaction_config(self._interaction_path)
        except (ConfigError, OSError) as error:
            raise SettingsError(
                "The shortcut interaction setting could not be saved safely."
            ) from error

    def save_microphone_priority(self, priority: Any) -> MicrophonePolicyConfig:
        """Save one complete category order without touching audio or services."""

        try:
            current = load_microphone_policy_config(self._microphone_policy_path)
        except ConfigError:
            # This is an explicit Save action over a setting the UI already
            # reported as invalid. A complete allowlisted order repairs it;
            # unparseable per-device preferences cannot be preserved.
            preferred_sources = ()
        else:
            preferred_sources = current.preferred_sources
        try:
            save_microphone_policy_config(
                priority,
                self._microphone_policy_path,
                preferred_sources=preferred_sources,
            )
            return load_microphone_policy_config(self._microphone_policy_path)
        except (ConfigError, OSError) as error:
            raise SettingsError(
                "The microphone priority setting could not be saved safely."
            ) from error

    def save_data_collection(
        self,
        enabled: bool,
        directory: str | Path | None,
    ) -> DataCollectionConfig:
        """Save an opt-in choice without starting audio or contacting a provider."""

        selected = Path(directory) if directory is not None and str(directory) else None
        if enabled and (selected is None or not selected.is_absolute()):
            raise SettingsError(
                "Choose an absolute storage folder before enabling local collection."
            )
        if enabled and (not selected.exists() or not selected.is_dir()):
            raise SettingsError(
                "The selected local data collection folder is unavailable."
            )
        try:
            save_data_collection_config(
                enabled,
                selected,
                self._data_collection_path,
            )
            return load_data_collection_config(self._data_collection_path)
        except (ConfigError, OSError) as error:
            raise SettingsError(
                "The local data collection setting could not be saved safely."
            ) from error

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


def _read_bounded_json(path: Path, limit: int) -> Any:
    """Read one small metadata file without following a final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > limit
        ):
            raise ValueError("metadata file is invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            payload = source.read(limit + 1)
        if len(payload) > limit:
            raise ValueError("metadata file is too large")
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("metadata file is invalid") from error
    finally:
        os.close(descriptor)


def _validate_usage_summary(
    document: Any,
    expected_utterance_id: str,
) -> tuple[datetime, int, int]:
    common_fields = {
        "schema_version",
        "kind",
        "utterance_id",
        "recorded_at_utc",
        "audio_duration_ms",
        "non_whitespace_character_count",
    }
    if not isinstance(document, dict):
        raise ValueError("usage summary is invalid")
    version = document.get("schema_version")
    if type(version) is not int:
        raise ValueError("usage summary identity is invalid")
    if version == 1:
        if set(document) != common_fields:
            raise ValueError("usage summary is invalid")
    elif version == 2:
        if (
            set(document) != common_fields | {"character_count_basis"}
            or document.get("character_count_basis") != "delivered-text"
        ):
            raise ValueError("usage summary is invalid")
    else:
        raise ValueError("usage summary identity is invalid")
    if (
        document["kind"] != _USAGE_SUMMARY_KIND
        or document["utterance_id"] != expected_utterance_id
    ):
        raise ValueError("usage summary identity is invalid")
    duration_ms = document["audio_duration_ms"]
    character_count = document["non_whitespace_character_count"]
    if (
        not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or not 0 <= duration_ms <= 600_000
        or not isinstance(character_count, int)
        or isinstance(character_count, bool)
        or not 0 <= character_count <= 1_000_000
    ):
        raise ValueError("usage summary counters are invalid")
    raw_recorded_at = document["recorded_at_utc"]
    if not isinstance(raw_recorded_at, str):
        raise ValueError("usage summary timestamp is invalid")
    try:
        recorded_at = datetime.fromisoformat(raw_recorded_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("usage summary timestamp is invalid") from error
    if recorded_at.tzinfo is None:
        raise ValueError("usage summary timestamp is invalid")
    return recorded_at, duration_ms, character_count


def _safe_summary_identifier(value: str) -> bool:
    return 1 <= len(value) <= 128 and all(
        character.isascii() and (character.isalnum() or character in "-_")
        for character in value
    )
