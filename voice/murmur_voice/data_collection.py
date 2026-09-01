# SPDX-License-Identifier: GPL-3.0-only
"""Optional local audio/provider-final records for personal ASR research.

Collection is disabled unless a private configuration explicitly enables it.
The real-time audio callback only appends bounded immutable chunks in memory.
After an authoritative final, the complete record is offered without blocking
to one background writer.  Filesystem access, WAV encoding, fsync, and atomic
publication therefore never run in the ASR callback or VoiceSession lock.
"""

from __future__ import annotations

import array
import fcntl
import hashlib
import json
import logging
import math
import os
import queue
import shutil
import stat
import sys
import threading
import time
import uuid
import wave
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    ConfigError,
    _load_private_bytes,
    _reject_duplicate_json_fields,
    _write_private_json,
)
from .microphone_metadata import MicrophoneCaptureMetadata
from .output_style import OutputDelivery, deliver_output, validate_output_delivery

DATA_COLLECTION_CONFIG_VERSION = 1
DATASET_MARKER_VERSION = 1
DATA_RECORD_VERSION = 3
DATA_USAGE_SUMMARY_VERSION = 2
DATA_FEEDBACK_VERSION = 1
MAX_DATA_COLLECTION_CONFIG_BYTES = 16 * 1024
MAX_STORAGE_PATH_CHARACTERS = 4096
MAX_PROVIDER_FINAL_BYTES = 256 * 1024
MAX_FEEDBACK_BYTES = 64 * 1024
WRITER_QUEUE_RECORDS = 2

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
MAX_AUDIO_BYTES = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES * 600
MAX_AUDIO_CHUNK_BYTES = 1024 * 1024
PCM_QUALITY_ANALYSIS_VERSION = 1
PCM_CLIPPING_THRESHOLD_ABS = 32_760
PCM_DBFS_FLOOR = -120.0

_DATASET_DIRECTORY = "openvoiceinput-dataset-v1"
_DATASET_MARKER_BASE = {
    "schema_version": DATASET_MARKER_VERSION,
    "kind": "openvoiceinput-personal-asr-dataset",
}
logger = logging.getLogger(__name__)


class DataCollectionError(RuntimeError):
    """Safe, content-free failure at the optional local-recording boundary."""


class _CollectionRevoked(DataCollectionError):
    """The user disabled or redirected collection before publication."""


@dataclass(frozen=True, slots=True, repr=False)
class DataCollectionConfig:
    """Private opt-in state; the selected path is hidden from repr/logging."""

    enabled: bool = False
    directory: Path | None = field(default=None, repr=False)
    dataset_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class _FrozenRecord:
    directory: Path = field(repr=False)
    dataset_id: str
    utterance_id: str
    collection_session_id: str
    recorded_at_utc: str
    chunks: tuple[bytes, ...] = field(repr=False)
    frames: int
    pcm_sha256: str
    provider_final: str = field(repr=False)
    delivery: OutputDelivery = field(repr=False)
    provider_name: str
    provider_model: str
    provider_resource_id: str | None
    microphone: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class _FrozenFeedback:
    directory: Path = field(repr=False)
    dataset_id: str
    utterance_id: str
    document: dict[str, Any] = field(repr=False)


@dataclass(slots=True)
class _PcmQualityAccumulator:
    sample_count: int = 0
    clipped_samples: int = 0
    zero_samples: int = 0
    sample_sum: int = 0
    square_sum: int = 0
    peak_abs: int = 0

    def as_document(self) -> dict[str, int | float]:
        if self.sample_count < 1:
            raise DataCollectionError("data collection audio quality is unavailable")
        rms = math.sqrt(self.square_sum / self.sample_count)
        return {
            "sample_count": self.sample_count,
            "clipped_fraction": round(
                self.clipped_samples / self.sample_count,
                8,
            ),
            "rms_dbfs": _amplitude_dbfs(rms),
            "peak_dbfs": _amplitude_dbfs(self.peak_abs),
            "dc_offset_fraction": round(
                self.sample_sum / self.sample_count / 32_768,
                8,
            ),
            "zero_fraction": round(self.zero_samples / self.sample_count, 8),
        }


def default_data_collection_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "murmur-ime" / "data-collection.json"


def load_data_collection_config(
    path: str | os.PathLike[str] | None = None,
) -> DataCollectionConfig:
    """Load an optional private config; a missing file means disabled."""

    config_path = (
        Path(path) if path is not None else default_data_collection_config_path()
    )
    raw = _load_private_bytes(
        config_path,
        kind="data collection configuration",
        limit=MAX_DATA_COLLECTION_CONFIG_BYTES,
        missing_ok=True,
    )
    if raw is None:
        return DataCollectionConfig()
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ConfigError(
            "data collection configuration is not valid UTF-8 JSON"
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "version",
        "enabled",
        "directory",
        "dataset_id",
    }:
        raise ConfigError("data collection configuration has unsupported fields")
    version = document.get("version")
    if type(version) is not int or version != DATA_COLLECTION_CONFIG_VERSION:
        raise ConfigError("data collection configuration uses an unsupported version")
    enabled = document.get("enabled")
    if type(enabled) is not bool:
        raise ConfigError("data collection enabled state must be a boolean")
    directory = _validate_storage_directory_value(document.get("directory"))
    dataset_id = _validate_dataset_id(document.get("dataset_id"))
    if enabled and (directory is None or dataset_id is None):
        raise ConfigError(
            "enabled data collection requires an initialized storage directory"
        )
    return DataCollectionConfig(
        enabled=enabled,
        directory=directory,
        dataset_id=dataset_id,
    )


def save_data_collection_config(
    enabled: bool,
    directory: str | os.PathLike[str] | None,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Atomically save the disabled-by-default collection choice."""

    if type(enabled) is not bool:
        raise ConfigError("data collection enabled state must be a boolean")
    validated_directory = _validate_storage_directory_value(
        os.fspath(directory) if directory is not None else None
    )
    if enabled and validated_directory is None:
        raise ConfigError("enabled data collection requires a storage directory")
    config_path = (
        Path(path) if path is not None else default_data_collection_config_path()
    )
    dataset_id = (
        initialize_data_collection_directory(validated_directory)
        if enabled and validated_directory is not None
        else None
    )
    document = {
        "version": DATA_COLLECTION_CONFIG_VERSION,
        "enabled": enabled,
        "directory": (
            os.fspath(validated_directory) if validated_directory is not None else None
        ),
        "dataset_id": dataset_id,
    }
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _configuration_lock(config_path):
        return _write_private_json(
            config_path,
            document,
            kind="data collection configuration",
            temporary_prefix=".data-collection.json.",
        )


def _validate_storage_directory_value(value: Any) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("data collection directory must be a path string or null")
    if (
        not value
        or len(value) > MAX_STORAGE_PATH_CHARACTERS
        or any(character in value for character in "\x00\r\n")
    ):
        raise ConfigError("data collection directory is invalid")
    directory = Path(value)
    if not directory.is_absolute():
        raise ConfigError("data collection directory must be absolute")
    return directory


def _validate_dataset_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _safe_identifier(value):
        raise ConfigError("data collection dataset identifier is invalid")
    return value


def initialize_data_collection_directory(directory: Path) -> str:
    """Initialize or reopen the fixed dataset child of a chosen directory."""

    if not directory.is_absolute() or not directory.is_dir():
        raise ConfigError("data collection directory is unavailable")
    dataset_root = directory / _DATASET_DIRECTORY
    created = False
    try:
        dataset_root.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    try:
        _secure_dataset_directory(dataset_root, allow_chmod=True)
        marker = dataset_root / "dataset.json"
        try:
            marker.lstat()
        except FileNotFoundError:
            marker_exists = False
        else:
            marker_exists = True
        if marker_exists:
            dataset_id = _read_dataset_marker(marker)
        else:
            # Never claim a non-empty directory that was not initialized by us.
            if any(dataset_root.iterdir()):
                raise DataCollectionError(
                    "data collection dataset root is unrecognized"
                )
            dataset_id = uuid.uuid4().hex
            _write_json(marker, _dataset_marker(dataset_id), exclusive=True)
            _fsync_directory(dataset_root)
        for child_name in (".pending", "utterances", "usage"):
            child = dataset_root / child_name
            try:
                child.mkdir(mode=0o700)
            except FileExistsError:
                pass
            _secure_dataset_directory(child, allow_chmod=True)
        _fsync_directory(dataset_root)
    except (DataCollectionError, OSError) as error:
        if created:
            try:
                shutil.rmtree(dataset_root)
            except OSError:
                pass
        if isinstance(error, DataCollectionError):
            raise ConfigError(str(error)) from error
        raise ConfigError("data collection dataset could not be initialized") from error
    return dataset_id


class DataCollectionRuntime:
    """Reload consent per utterance and own the isolated filesystem writer."""

    def __init__(
        self,
        config_path: Path | None = None,
        *,
        session_id: str | None = None,
        queue_records: int = WRITER_QUEUE_RECORDS,
    ) -> None:
        if queue_records < 1 or queue_records > 64:
            raise DataCollectionError("data collection queue size is invalid")
        self._config_path = config_path or default_data_collection_config_path()
        self._session_id = session_id or uuid.uuid4().hex
        self._queue: queue.Queue[_FrozenRecord | _FrozenFeedback] = queue.Queue(
            maxsize=queue_records
        )
        self._lock = threading.RLock()
        self._closed = False
        self._stop_event = threading.Event()
        self._last_status_code = "none"
        self._writer = threading.Thread(
            target=self._writer_main,
            name="openvoice-data-writer",
            daemon=True,
        )
        self._writer.start()

    def validate(self) -> DataCollectionConfig:
        return load_data_collection_config(self._config_path)

    def begin(self, utterance_id: str) -> DatasetRecorder | None:
        config = load_data_collection_config(self._config_path)
        if not config.enabled:
            with self._lock:
                self._last_status_code = "none"
            return None
        assert config.directory is not None
        assert config.dataset_id is not None
        with self._lock:
            if self._closed:
                raise DataCollectionError("data collection runtime is closed")
            self._last_status_code = "none"
        return DatasetRecorder(
            self,
            config.directory,
            config.dataset_id,
            utterance_id,
            collection_session_id=self._session_id,
        )

    def record_feedback(self, utterance_id: str, document: Any) -> bool:
        """Queue one immutable sidecar only while collection remains enabled."""

        config = load_data_collection_config(self._config_path)
        if not config.enabled:
            return False
        if not _safe_identifier(utterance_id):
            raise DataCollectionError("data collection identifier is invalid")
        assert config.directory is not None
        assert config.dataset_id is not None
        feedback = _validate_feedback_document(document)
        self._enqueue(
            _FrozenFeedback(
                directory=config.directory,
                dataset_id=config.dataset_id,
                utterance_id=utterance_id,
                document=feedback,
            )
        )
        return True

    def close(self, timeout: float = 10.0) -> bool:
        """Stop accepting records and give queued local writes bounded time."""

        with self._lock:
            if self._closed:
                return not self._writer.is_alive()
            self._closed = True
            self._stop_event.set()
        self._writer.join(max(timeout, 0.0))
        return not self._writer.is_alive()

    def status_code(self) -> str:
        with self._lock:
            return self._last_status_code

    def wait_until_idle(self, timeout: float = 5.0) -> bool:
        """Test/diagnostic wait; normal dictation never calls this."""

        deadline = time.monotonic() + max(timeout, 0.0)
        while time.monotonic() < deadline:
            with self._queue.mutex:
                unfinished = self._queue.unfinished_tasks
            if unfinished == 0:
                return True
            time.sleep(0.01)
        return False

    def _still_authorized(self, directory: Path, dataset_id: str) -> bool:
        try:
            current = load_data_collection_config(self._config_path)
        except ConfigError:
            return False
        return (
            current.enabled
            and current.directory == directory
            and current.dataset_id == dataset_id
        )

    def _enqueue(self, record: _FrozenRecord | _FrozenFeedback) -> None:
        with self._lock:
            if self._closed:
                raise DataCollectionError("data collection runtime is closed")
        try:
            self._queue.put_nowait(record)
        except queue.Full as error:
            raise DataCollectionError("data collection writer is busy") from error

    def _writer_main(self) -> None:
        while True:
            if self._stop_event.is_set() and self._queue.empty():
                return
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if not self._still_authorized(item.directory, item.dataset_id):
                    with self._lock:
                        self._last_status_code = "none"
                    continue
                if isinstance(item, _FrozenRecord):
                    _publish_record(item, self._publish_if_still_authorized)
                else:
                    _publish_feedback(item, self._publish_feedback_if_authorized)
                with self._lock:
                    self._last_status_code = "none"
            except _CollectionRevoked:
                with self._lock:
                    self._last_status_code = "none"
            except Exception:
                # Paths, transcript text, and audio are deliberately omitted.
                logger.error("Optional local data record write failed")
                with self._lock:
                    self._last_status_code = "data-collection-failed"
            finally:
                self._queue.task_done()

    def _publish_if_still_authorized(
        self,
        record: _FrozenRecord,
        stage: Path,
        final: Path,
    ) -> None:
        # Saving disable/path changes and the final atomic rename share this
        # short lock. Once a settings save returns, an older queued record can
        # no longer become visible.
        with _configuration_lock(self._config_path):
            if not self._still_authorized(record.directory, record.dataset_id):
                raise _CollectionRevoked(
                    "data collection was disabled before completion"
                )
            _validate_dataset_root(record.directory, record.dataset_id)
            try:
                final.lstat()
            except FileNotFoundError:
                pass
            else:
                raise DataCollectionError("data collection record already exists")
            stage.rename(final)

    def _publish_feedback_if_authorized(
        self,
        feedback: _FrozenFeedback,
        temporary: Path,
        final: Path,
    ) -> None:
        with _configuration_lock(self._config_path):
            if not self._still_authorized(feedback.directory, feedback.dataset_id):
                raise _CollectionRevoked(
                    "data collection was disabled before feedback completion"
                )
            _validate_dataset_root(feedback.directory, feedback.dataset_id)
            if temporary.parent != final.parent:
                raise DataCollectionError("data collection feedback path is invalid")
            _secure_dataset_directory(final.parent, allow_chmod=False)
            _validate_private_regular_file(temporary)
            try:
                final.lstat()
            except FileNotFoundError:
                pass
            else:
                raise DataCollectionError("data collection feedback already exists")
            temporary.rename(final)


class DatasetRecorder:
    """One bounded in-memory utterance offered to the background writer."""

    def __init__(
        self,
        runtime: DataCollectionRuntime,
        selected_directory: Path,
        dataset_id: str,
        utterance_id: str,
        *,
        collection_session_id: str,
        recorded_at_utc: str | None = None,
    ) -> None:
        if (
            not _safe_identifier(utterance_id)
            or not _safe_identifier(collection_session_id)
            or not _safe_identifier(dataset_id)
        ):
            raise DataCollectionError("data collection identifier is invalid")
        self._runtime = runtime
        self._directory = selected_directory
        self._dataset_id = dataset_id
        self._utterance_id = utterance_id
        self._collection_session_id = collection_session_id
        self._recorded_at_utc = recorded_at_utc or datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        self._lock = threading.RLock()
        self._chunks: list[bytes] = []
        self._bytes = 0
        self._digest = hashlib.sha256()
        self._failed = False
        self._stopped = False
        self._committed = False
        self._provider_name = "volcengine"
        self._provider_model = "bigmodel_async"
        self._provider_resource_id: str | None = "volc.seedasr.sauc.duration"
        self._microphone_metadata: MicrophoneCaptureMetadata | None = None

    def set_provider_identity(
        self,
        name: str,
        model: str,
        resource_id: str | None = None,
    ) -> None:
        """Bind secret-free backend provenance before the first audio chunk."""

        values = (name, model) + (() if resource_id is None else (resource_id,))
        if any(
            not isinstance(value, str)
            or not value
            or len(value) > 128
            or any(not character.isprintable() for character in value)
            for value in values
        ):
            raise DataCollectionError("data collection provider identity is invalid")
        with self._lock:
            if self._chunks or self._stopped or self._committed:
                raise DataCollectionError("data collection provider identity is late")
            self._provider_name = name
            self._provider_model = model
            self._provider_resource_id = resource_id

    def set_microphone_metadata(self, metadata: MicrophoneCaptureMetadata) -> None:
        """Update secret-free selection/route facts while capture continues."""

        if not isinstance(metadata, MicrophoneCaptureMetadata):
            raise DataCollectionError("data collection microphone metadata is invalid")
        with self._lock:
            if self._committed:
                return
            self._microphone_metadata = metadata

    def add_audio(self, data: bytes) -> None:
        """Append one exact PCM chunk without disk or a blocking queue put."""

        if (
            not isinstance(data, bytes)
            or not data
            or len(data) > MAX_AUDIO_CHUNK_BYTES
            or len(data) % SAMPLE_WIDTH_BYTES
        ):
            with self._lock:
                self._failed = True
            return
        with self._lock:
            if self._failed or self._stopped or self._committed:
                return
            if self._bytes + len(data) > MAX_AUDIO_BYTES:
                self._failed = True
                return
            self._chunks.append(data)
            self._bytes += len(data)
            self._digest.update(data)

    def stop_audio(self) -> bool:
        with self._lock:
            self._stopped = True
            return not self._failed

    def commit(
        self,
        provider_final: str,
        delivery: OutputDelivery | None = None,
    ) -> None:
        """Freeze and non-blockingly offer an authoritative teacher record."""

        if not isinstance(provider_final, str) or not provider_final:
            self.discard()
            raise DataCollectionError("provider final is unavailable")
        if len(provider_final.encode("utf-8")) > MAX_PROVIDER_FINAL_BYTES:
            self.discard()
            raise DataCollectionError("provider final is too large")
        if delivery is None:
            delivery = deliver_output(provider_final, "faithful")
        try:
            validate_output_delivery(provider_final, delivery)
        except (TypeError, ValueError) as error:
            self.discard()
            raise DataCollectionError("output delivery is invalid") from error
        with self._lock:
            self._stopped = True
            if self._failed or self._committed or self._bytes < SAMPLE_WIDTH_BYTES:
                self._chunks.clear()
                raise DataCollectionError("data collection audio is unavailable")
            frozen = _FrozenRecord(
                directory=self._directory,
                dataset_id=self._dataset_id,
                utterance_id=self._utterance_id,
                collection_session_id=self._collection_session_id,
                recorded_at_utc=self._recorded_at_utc,
                chunks=tuple(self._chunks),
                frames=self._bytes // SAMPLE_WIDTH_BYTES,
                pcm_sha256=self._digest.hexdigest(),
                provider_final=provider_final,
                delivery=delivery,
                provider_name=self._provider_name,
                provider_model=self._provider_model,
                provider_resource_id=self._provider_resource_id,
                microphone=(
                    self._microphone_metadata.as_record_document()
                    if self._microphone_metadata is not None
                    else None
                ),
            )
        try:
            self._runtime._enqueue(frozen)
        except Exception:
            self.discard()
            raise
        with self._lock:
            self._chunks.clear()
            self._committed = True

    def discard(self) -> None:
        with self._lock:
            if self._committed:
                return
            self._stopped = True
            self._chunks.clear()
            self._bytes = 0


def _publish_record(
    record: _FrozenRecord,
    finalizer: Any,
) -> None:
    dataset_root = _validate_dataset_root(record.directory, record.dataset_id)
    pending_root = dataset_root / ".pending"
    records_root = dataset_root / "utterances"
    if not pending_root.is_dir() or not records_root.is_dir():
        raise DataCollectionError("data collection dataset is incomplete")
    stage = pending_root / record.utterance_id
    final = records_root / record.utterance_id
    stage.mkdir(mode=0o700)
    published = False
    try:
        audio_path = stage / "audio.wav"
        _write_wav(audio_path, record.chunks)
        file_sha256 = _sha256_file(audio_path)
        try:
            audio_quality = _summarize_pcm_quality(record.chunks)
        except Exception:
            # Diagnostics are advisory. A future implementation or platform
            # failure must never discard otherwise valid opted-in audio.
            logger.error("Optional PCM quality summary could not be computed")
            audio_quality = None
        metadata = {
            "schema_version": DATA_RECORD_VERSION,
            "dataset_id": record.dataset_id,
            "utterance_id": record.utterance_id,
            "collection_session_id": record.collection_session_id,
            "recorded_at_utc": record.recorded_at_utc,
            "consent": "explicit-opt-in",
            "audio": {
                "file": "audio.wav",
                "format": "wav-pcm-s16le",
                "sample_rate_hz": SAMPLE_RATE,
                "channels": CHANNELS,
                "frames": record.frames,
                "pcm_sha256": record.pcm_sha256,
                "file_sha256": file_sha256,
                **({"quality": audio_quality} if audio_quality is not None else {}),
            },
            "provider": {
                "name": record.provider_name,
                "model": record.provider_model,
                **(
                    {"resource_id": record.provider_resource_id}
                    if record.provider_resource_id is not None
                    else {}
                ),
            },
            **(
                {"microphone": record.microphone}
                if record.microphone is not None
                else {}
            ),
            "labels": {
                "provider_final": {
                    "text": record.provider_final,
                    "review_status": "teacher-unreviewed",
                },
                "spoken_verbatim": {
                    "text": None,
                    "review_status": "unreviewed",
                },
                "preferred_output": {
                    "text": None,
                    "review_status": "unreviewed",
                },
            },
            "delivery": record.delivery.as_record_document(),
        }
        _write_json(stage / "record.json", metadata)
        _fsync_directory(stage)
        finalizer(record, stage, final)
        published = True
        _fsync_directory(records_root)
        usage_root = _ensure_usage_directory(dataset_root)
        _publish_usage_summary(record, usage_root)
    finally:
        if not published:
            try:
                shutil.rmtree(stage)
            except OSError:
                pass


def _summarize_pcm_quality(chunks: tuple[bytes, ...]) -> dict[str, Any]:
    """Compute post-hoc numeric diagnostics in the background writer only."""

    overall_count = 0
    overall_clipped = 0
    overall_zero = 0
    overall_sum = 0
    overall_square_sum = 0
    overall_peak = 0
    first_count = 0
    first_clipped = 0
    first_zero = 0
    first_sum = 0
    first_square_sum = 0
    first_peak = 0
    for chunk in chunks:
        samples = array.array("h")
        samples.frombytes(chunk)
        if sys.byteorder != "little":
            samples.byteswap()
        for sample in samples:
            absolute = abs(sample)
            overall_count += 1
            overall_clipped += absolute >= PCM_CLIPPING_THRESHOLD_ABS
            overall_zero += sample == 0
            overall_sum += sample
            overall_square_sum += sample * sample
            if absolute > overall_peak:
                overall_peak = absolute
            if first_count < SAMPLE_RATE:
                first_count += 1
                first_clipped += absolute >= PCM_CLIPPING_THRESHOLD_ABS
                first_zero += sample == 0
                first_sum += sample
                first_square_sum += sample * sample
                if absolute > first_peak:
                    first_peak = absolute
    overall = _PcmQualityAccumulator(
        overall_count,
        overall_clipped,
        overall_zero,
        overall_sum,
        overall_square_sum,
        overall_peak,
    )
    first_second = _PcmQualityAccumulator(
        first_count,
        first_clipped,
        first_zero,
        first_sum,
        first_square_sum,
        first_peak,
    )
    return {
        "analysis_version": PCM_QUALITY_ANALYSIS_VERSION,
        "clipping_threshold_abs": PCM_CLIPPING_THRESHOLD_ABS,
        "overall": overall.as_document(),
        "first_second": first_second.as_document(),
    }


def _amplitude_dbfs(amplitude: float | int) -> float:
    if amplitude <= 0:
        return PCM_DBFS_FLOOR
    value = max(PCM_DBFS_FLOOR, 20 * math.log10(amplitude / 32_768))
    rounded = round(value, 3)
    return 0.0 if rounded == 0 else rounded


def _ensure_usage_directory(dataset_root: Path) -> Path:
    """Create the separate summary index without changing utterance records."""

    usage_root = dataset_root / "usage"
    created = False
    try:
        usage_root.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    _secure_dataset_directory(usage_root, allow_chmod=False)
    if created:
        _fsync_directory(dataset_root)
    return usage_root


def _publish_usage_summary(record: _FrozenRecord, usage_root: Path) -> None:
    """Atomically publish content-free counters after the core v1 pair."""

    temporary = usage_root / f".{record.utterance_id}.{uuid.uuid4().hex}.tmp"
    final = usage_root / f"{record.utterance_id}.json"
    try:
        try:
            final.lstat()
        except FileNotFoundError:
            pass
        else:
            raise DataCollectionError("data collection usage summary already exists")
        _write_json(
            temporary,
            {
                "schema_version": DATA_USAGE_SUMMARY_VERSION,
                "kind": "openvoiceinput-private-usage-summary",
                "utterance_id": record.utterance_id,
                "recorded_at_utc": record.recorded_at_utc,
                "audio_duration_ms": round(record.frames * 1000 / SAMPLE_RATE),
                "character_count_basis": "delivered-text",
                "non_whitespace_character_count": sum(
                    not character.isspace() for character in record.delivery.text
                ),
            },
        )
        temporary.rename(final)
        _fsync_directory(usage_root)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _publish_feedback(
    feedback: _FrozenFeedback,
    finalizer: Any,
) -> None:
    dataset_root = _validate_dataset_root(feedback.directory, feedback.dataset_id)
    utterance_root = dataset_root / "utterances" / feedback.utterance_id
    try:
        metadata = utterance_root.lstat()
    except OSError as error:
        raise DataCollectionError("data collection utterance is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or utterance_root.is_symlink()
    ):
        raise DataCollectionError("data collection utterance is unavailable")
    try:
        utterance_files = {path.name for path in utterance_root.iterdir()}
    except OSError as error:
        raise DataCollectionError("data collection utterance is unavailable") from error
    if utterance_files != {"audio.wav", "record.json"}:
        raise DataCollectionError("data collection utterance is incomplete")
    for name in ("audio.wav", "record.json"):
        _validate_private_regular_file(utterance_root / name)

    feedback_root = dataset_root / "feedback"
    try:
        feedback_root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _secure_dataset_directory(feedback_root, allow_chmod=False)
    event_root = feedback_root / feedback.utterance_id
    try:
        event_root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _secure_dataset_directory(event_root, allow_chmod=False)
    _fsync_directory(feedback_root)
    event_id = uuid.uuid4().hex
    temporary = event_root / f".{event_id}.json"
    final = event_root / f"{event_id}.json"
    published = False
    try:
        document = {
            "schema_version": DATA_FEEDBACK_VERSION,
            "kind": "openvoiceinput-correction-feedback",
            "dataset_id": feedback.dataset_id,
            "utterance_id": feedback.utterance_id,
            "source": "post-commit-edit",
            "result": feedback.document,
        }
        _write_json(temporary, document)
        finalizer(feedback, temporary, final)
        published = True
        _fsync_directory(event_root)
    finally:
        if not published:
            try:
                temporary.unlink()
            except OSError:
                pass


def _validate_private_regular_file(path: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise DataCollectionError("data collection utterance is incomplete") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        # Some owner-mapped FUSE filesystems deliberately expose every
        # private inode as 0700 even when the remote regular file is 0600.
        # Owner execute does not broaden visibility; group/other bits still
        # fail closed, and owner read/write remain mandatory.
        or mode not in {0o600, 0o700}
    ):
        raise DataCollectionError("data collection utterance is incomplete")


def _validate_feedback_document(document: Any) -> dict[str, Any]:
    """Validate bounded edit feedback without accepting surrounding text."""

    scalar_fields = {
        "reason_code",
        "captured_count",
        "activated_count",
        "candidate_count",
        "conflicted_count",
        "replacement_hunks",
        "corrections",
    }
    if not isinstance(document, dict) or set(document) != scalar_fields:
        raise DataCollectionError("data collection feedback is invalid")
    reason = document.get("reason_code")
    if (
        not isinstance(reason, str)
        or not 1 <= len(reason) <= 64
        or any(
            not (character.islower() or character.isdigit() or character == "-")
            for character in reason
        )
    ):
        raise DataCollectionError("data collection feedback is invalid")
    count_names = (
        "captured_count",
        "activated_count",
        "candidate_count",
        "conflicted_count",
        "replacement_hunks",
    )
    counts: dict[str, int] = {}
    for name in count_names:
        value = document.get(name)
        if type(value) is not int or value < 0 or value > 500:
            raise DataCollectionError("data collection feedback is invalid")
        counts[name] = value
    raw_corrections = document.get("corrections")
    if not isinstance(raw_corrections, list) or len(raw_corrections) > 8:
        raise DataCollectionError("data collection feedback is invalid")
    corrections: list[dict[str, str]] = []
    for raw in raw_corrections:
        if not isinstance(raw, dict) or set(raw) != {
            "wrong",
            "canonical",
            "category",
            "evidence",
            "state",
        }:
            raise DataCollectionError("data collection feedback is invalid")
        wrong = raw.get("wrong")
        canonical = raw.get("canonical")
        if any(
            not isinstance(text, str)
            or not text.strip()
            or len(text.strip()) > 64
            or any(not character.isprintable() for character in text)
            for text in (wrong, canonical)
        ):
            raise DataCollectionError("data collection feedback is invalid")
        category = raw.get("category")
        evidence = raw.get("evidence")
        state_value = raw.get("state")
        if category not in {"recognition", "terminology", "formatting"}:
            raise DataCollectionError("data collection feedback is invalid")
        if evidence not in {"strong", "medium", "explicit"}:
            raise DataCollectionError("data collection feedback is invalid")
        if state_value not in {
            "candidate",
            "active",
            "conflicted",
            "suspended",
            "archived",
        }:
            raise DataCollectionError("data collection feedback is invalid")
        corrections.append(
            {
                "wrong": wrong.strip(),
                "canonical": canonical.strip(),
                "category": category,
                "evidence": evidence,
                "state": state_value,
            }
        )
    sanitized = {"reason_code": reason, **counts, "corrections": corrections}
    payload_size = len(
        json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if payload_size > MAX_FEEDBACK_BYTES:
        raise DataCollectionError("data collection feedback is too large")
    return sanitized


def _dataset_marker(dataset_id: str) -> dict[str, Any]:
    return {**_DATASET_MARKER_BASE, "dataset_id": dataset_id}


def _read_dataset_marker(marker: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(marker, flags)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise DataCollectionError("data collection dataset marker is invalid")
            raw = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        if len(raw) > 4096:
            raise DataCollectionError("data collection dataset marker is invalid")
        observed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_fields,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise DataCollectionError(
            "data collection dataset marker is invalid"
        ) from error
    if not isinstance(observed, dict) or set(observed) != {
        "schema_version",
        "kind",
        "dataset_id",
    }:
        raise DataCollectionError("data collection dataset marker is invalid")
    dataset_id = observed.get("dataset_id")
    if not _safe_identifier(dataset_id) or observed != _dataset_marker(dataset_id):
        raise DataCollectionError("data collection dataset marker is invalid")
    return dataset_id


def _validate_dataset_root(selected: Path, dataset_id: str) -> Path:
    if not selected.is_absolute() or not selected.is_dir():
        raise DataCollectionError("data collection directory is unavailable")
    dataset_root = selected / _DATASET_DIRECTORY
    _secure_dataset_directory(dataset_root, allow_chmod=False)
    if _read_dataset_marker(dataset_root / "dataset.json") != dataset_id:
        raise DataCollectionError("data collection dataset identity changed")
    for child_name in (".pending", "utterances"):
        _secure_dataset_directory(
            dataset_root / child_name,
            allow_chmod=False,
        )
    return dataset_root


def _secure_dataset_directory(path: Path, *, allow_chmod: bool) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise DataCollectionError("data collection dataset is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise DataCollectionError("data collection dataset is unavailable")
    if allow_chmod:
        try:
            path.chmod(0o700)
        except OSError as error:
            raise DataCollectionError(
                "data collection dataset permissions could not be secured"
            ) from error
    elif stat.S_IMODE(metadata.st_mode) != 0o700:
        raise DataCollectionError("data collection dataset permissions are invalid")


def _write_wav(path: Path, chunks: tuple[bytes, ...]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w+b") as raw_audio:
        with wave.open(raw_audio, "wb") as output:
            output.setnchannels(CHANNELS)
            output.setsampwidth(SAMPLE_WIDTH_BYTES)
            output.setframerate(SAMPLE_RATE)
            for chunk in chunks:
                output.writeframesraw(chunk)
        raw_audio.flush()
        os.fsync(raw_audio.fileno())


def _write_json(path: Path, document: Any, *, exclusive: bool = True) -> None:
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(
            character.isascii() and (character.isalnum() or character in "-_")
            for character in value
        )
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _configuration_lock(config_path: Path):
    lock_path = config_path.with_name(".data-collection.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ConfigError("data collection configuration lock is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
