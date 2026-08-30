# SPDX-License-Identifier: GPL-3.0-only
"""Optional local audio/provider-final records for personal ASR research.

Collection is disabled unless a private configuration explicitly enables it.
The real-time audio callback only appends bounded immutable chunks in memory.
After an authoritative final, the complete record is offered without blocking
to one background writer.  Filesystem access, WAV encoding, fsync, and atomic
publication therefore never run in the ASR callback or VoiceSession lock.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import queue
import shutil
import stat
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

DATA_COLLECTION_CONFIG_VERSION = 1
DATA_RECORD_VERSION = 1
MAX_DATA_COLLECTION_CONFIG_BYTES = 16 * 1024
MAX_STORAGE_PATH_CHARACTERS = 4096
MAX_PROVIDER_FINAL_BYTES = 256 * 1024
WRITER_QUEUE_RECORDS = 2

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
MAX_AUDIO_BYTES = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES * 600
MAX_AUDIO_CHUNK_BYTES = 1024 * 1024

_DATASET_DIRECTORY = "openvoiceinput-dataset-v1"
_DATASET_MARKER_BASE = {
    "schema_version": DATA_RECORD_VERSION,
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
        for child_name in (".pending", "utterances"):
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
        self._queue: queue.Queue[_FrozenRecord] = queue.Queue(maxsize=queue_records)
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

    def _enqueue(self, record: _FrozenRecord) -> None:
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
                _publish_record(item, self._publish_if_still_authorized)
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

    def commit(self, provider_final: str) -> None:
        """Freeze and non-blockingly offer an authoritative teacher record."""

        if not isinstance(provider_final, str) or not provider_final:
            self.discard()
            raise DataCollectionError("provider final is unavailable")
        if len(provider_final.encode("utf-8")) > MAX_PROVIDER_FINAL_BYTES:
            self.discard()
            raise DataCollectionError("provider final is too large")
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
            },
            "provider": {
                "name": "volcengine",
                "model": "bigmodel_async",
                "resource_id": "volc.seedasr.sauc.duration",
            },
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
        }
        _write_json(stage / "record.json", metadata)
        _fsync_directory(stage)
        finalizer(record, stage, final)
        published = True
        _fsync_directory(records_root)
    finally:
        if not published:
            try:
                shutil.rmtree(stage)
            except OSError:
                pass


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
