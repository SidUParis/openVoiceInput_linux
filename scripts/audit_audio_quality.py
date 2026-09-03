#!/usr/bin/env python3
"""Audit immutable OpenVoice WAV records and emit signal-only sidecars.

The utility deliberately does not inspect transcript semantics, play audio, or
contact a network service.  It validates the existing WAV/JSON pair and writes
deterministic ``quality-v1/<utterance-id>/quality.json`` sidecars through
atomic directory reservations with a completion marker when ``--sidecar-dir``
is used. A complete manifest is the sole snapshot commit marker.
"""

from __future__ import annotations

import argparse
import array
import collections
from contextlib import ExitStack
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
import wave
from datetime import datetime
from typing import Any, BinaryIO, Iterable
from zoneinfo import ZoneInfo


ANALYZER_NAME = "openvoice-signal-quality-audit"
ANALYZER_VERSION = 1
POLICY_VERSION = 1
MAX_RECORD_BYTES = 1_048_576
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
EXPECTED_SAMPLE_RATE = 16_000
EXPECTED_CHANNELS = 1
EXPECTED_SAMPLE_WIDTH = 2
MAX_AUDIO_SECONDS = 600
MAX_AUDIO_FRAMES = EXPECTED_SAMPLE_RATE * MAX_AUDIO_SECONDS
MAX_WAV_BYTES = MAX_AUDIO_FRAMES * EXPECTED_SAMPLE_WIDTH + 1_048_576
CLIPPING_THRESHOLD_ABS = 32_760
FRAME_SAMPLES = 320  # 20 ms at 16 kHz
DBFS_FLOOR = -120.0
TIERS = ("high", "usable", "low", "reject")
TIER_SEVERITY = {tier: index for index, tier in enumerate(TIERS)}
MICROPHONE_CATEGORIES = frozenset({"dji", "headset", "external", "built-in"})
UTC_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z\Z"
)
COMPLETE_MARKER_NAME = "complete"
MAX_COMPLETE_MARKER_BYTES = 1024
CONCURRENT_PUBLICATION_WAIT_SECONDS = 2.0


class AuditError(RuntimeError):
    """A record cannot be audited safely."""


class PublishError(AuditError):
    """A deterministic sidecar cannot be published safely."""


class IncompletePublication(PublishError):
    """A reserved destination exists without its final completion marker."""


def _amplitude_dbfs(amplitude: float | int) -> float:
    if amplitude <= 0:
        return DBFS_FLOOR
    value = max(DBFS_FLOOR, 20 * math.log10(amplitude / 32_768))
    rounded = round(value, 3)
    return 0.0 if rounded == 0 else rounded


def _validate_private_directory(details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode):
        raise AuditError("audit directory is not a real directory")
    if details.st_uid != os.getuid():
        raise AuditError("audit directory has the wrong owner")
    if stat.S_IMODE(details.st_mode) != 0o700:
        raise AuditError("audit directory mode is not private")


def _directory_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_directory_path(path: Path) -> int:
    """Open every path component with O_NOFOLLOW and retain the final inode."""

    candidate = path if path.is_absolute() else Path.cwd() / path
    if any(part == ".." for part in candidate.parts):
        raise AuditError("audit directory path contains parent traversal")
    descriptor = os.open("/", _directory_flags())
    try:
        for part in candidate.parts[1:]:
            if part in ("", "."):
                continue
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        _validate_private_directory(os.fstat(descriptor))
        return descriptor
    except OSError as error:
        os.close(descriptor)
        raise AuditError("audit directory path is unsafe") from error
    except Exception:
        os.close(descriptor)
        raise


def _open_private_child_directory(
    parent_fd: int,
    name: str,
    *,
    create: bool = False,
) -> int:
    if Path(name).name != name or name in ("", ".", ".."):
        raise AuditError("audit child directory name is invalid")
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        _validate_private_directory(os.fstat(descriptor))
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_private_regular_at(
    directory_fd: int,
    name: str,
    *,
    maximum: int,
) -> tuple[BinaryIO, os.stat_result]:
    if Path(name).name != name or name in ("", ".", ".."):
        raise AuditError("record file name is invalid")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise AuditError("record file cannot be opened safely") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise AuditError("record file is not regular")
        if details.st_uid != os.getuid():
            raise AuditError("record file has the wrong owner")
        if stat.S_IMODE(details.st_mode) not in {0o600, 0o700}:
            raise AuditError("record file mode is not private")
        if details.st_size < 1 or details.st_size > maximum:
            raise AuditError("record file size is outside the audit boundary")
        return os.fdopen(descriptor, "rb", closefd=True), details
    except Exception:
        os.close(descriptor)
        raise


def _stat_identity(details: os.stat_result) -> tuple[int, int, int, int]:
    return (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)


def _load_record_at(
    directory_fd: int,
    name: str,
) -> tuple[dict[str, Any], str, tuple[int, int, int, int]]:
    stream, before = _open_private_regular_at(
        directory_fd,
        name,
        maximum=MAX_RECORD_BYTES,
    )
    with stream:
        payload = stream.read(MAX_RECORD_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise AuditError("record changed while it was read")
    try:
        document = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuditError("record JSON is invalid") from error
    if not isinstance(document, dict):
        raise AuditError("record JSON root is invalid")
    return document, hashlib.sha256(payload).hexdigest(), _stat_identity(after)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise AuditError("record JSON contains a duplicate key")
        document[key] = value
    return document


class _Accumulator:
    def __init__(self) -> None:
        self.count = 0
        self.clipped = 0
        self.zero = 0
        self.total = 0
        self.square = 0
        self.peak = 0

    def add(self, values: Iterable[int]) -> None:
        for value in values:
            absolute = abs(value)
            self.count += 1
            self.clipped += absolute >= CLIPPING_THRESHOLD_ABS
            self.zero += value == 0
            self.total += value
            self.square += value * value
            self.peak = max(self.peak, absolute)

    def document(self) -> dict[str, int | float]:
        if self.count < 1:
            raise AuditError("audio has no samples")
        return {
            "sample_count": self.count,
            "clipped_fraction": round(self.clipped / self.count, 8),
            "rms_dbfs": _amplitude_dbfs(math.sqrt(self.square / self.count)),
            "peak_dbfs": _amplitude_dbfs(self.peak),
            "dc_offset_fraction": round(self.total / self.count / 32_768, 8),
            "zero_fraction": round(self.zero / self.count, 8),
        }


def _read_pcm_metrics_at(
    directory_fd: int,
    name: str,
) -> tuple[dict[str, Any], str, str, tuple[int, int, int, int]]:
    stream, before = _open_private_regular_at(
        directory_fd,
        name,
        maximum=MAX_WAV_BYTES,
    )
    with stream:
        payload = stream.read(MAX_WAV_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise AuditError("WAV changed while it was read")
    if len(payload) != before.st_size:
        raise AuditError("WAV read was incomplete")
    file_digest = hashlib.sha256(payload)
    reader: wave.Wave_read | None = None
    try:
        reader = wave.open(io.BytesIO(payload), "rb")
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        compression = reader.getcomptype()
        declared_frames = reader.getnframes()
    except (EOFError, wave.Error) as error:
        raise AuditError("WAV container is invalid") from error
    try:
        if (
            channels != EXPECTED_CHANNELS
            or sample_width != EXPECTED_SAMPLE_WIDTH
            or sample_rate != EXPECTED_SAMPLE_RATE
            or compression != "NONE"
        ):
            raise AuditError("WAV format is unsupported")
        if declared_frames < 1 or declared_frames > MAX_AUDIO_FRAMES:
            raise AuditError("WAV duration is outside the audit boundary")

        pcm_digest = hashlib.sha256()
        overall = _Accumulator()
        first_second = _Accumulator()
        active = _Accumulator()
        frame_count = 0
        active_frames = 0
        silent_frames = 0
        longest_zero_run = 0
        zero_run = 0
        pending = array.array("h")
        while raw := reader.readframes(8192):
            if len(raw) % EXPECTED_SAMPLE_WIDTH:
                raise AuditError("WAV PCM data has an incomplete sample")
            pcm_digest.update(raw)
            values = array.array("h")
            try:
                values.frombytes(raw)
            except ValueError as error:
                raise AuditError("WAV PCM data is invalid") from error
            if sys.byteorder != "little":
                values.byteswap()
            overall.add(values)
            remaining = EXPECTED_SAMPLE_RATE - first_second.count
            if remaining > 0:
                first_second.add(values[:remaining])
            for value in values:
                if value == 0:
                    zero_run += 1
                    longest_zero_run = max(longest_zero_run, zero_run)
                else:
                    zero_run = 0
            pending.extend(values)
            while len(pending) >= FRAME_SAMPLES:
                frame = pending[:FRAME_SAMPLES]
                del pending[:FRAME_SAMPLES]
                frame_count += 1
                square_sum = sum(value * value for value in frame)
                frame_rms = _amplitude_dbfs(math.sqrt(square_sum / len(frame)))
                if frame_rms > -45:
                    active_frames += 1
                    active.add(frame)
                if frame_rms <= -50:
                    silent_frames += 1
        if pending:
            frame_count += 1
            square_sum = sum(value * value for value in pending)
            frame_rms = _amplitude_dbfs(math.sqrt(square_sum / len(pending)))
            if frame_rms > -45:
                active_frames += 1
                active.add(pending)
            if frame_rms <= -50:
                silent_frames += 1
        if overall.count != declared_frames:
            raise AuditError("WAV frame count is inconsistent")
    except AuditError:
        raise
    except (EOFError, wave.Error) as error:
        raise AuditError("WAV PCM data is invalid") from error
    finally:
        reader.close()

    metrics: dict[str, Any] = {
        "analysis_version": ANALYZER_VERSION,
        "clipping_threshold_abs": CLIPPING_THRESHOLD_ABS,
        "duration_seconds": round(overall.count / EXPECTED_SAMPLE_RATE, 6),
        "overall": overall.document(),
        "first_second": first_second.document(),
        "frame_analysis": {
            "frame_ms": 20,
            "active_threshold_dbfs": -45,
            "silent_threshold_dbfs": -50,
            "frame_count": frame_count,
            "active_frame_fraction": round(active_frames / frame_count, 8),
            "silent_frame_fraction": round(silent_frames / frame_count, 8),
            "active_rms_dbfs": active.document()["rms_dbfs"]
            if active.count
            else DBFS_FLOOR,
            "longest_zero_run_ms": round(
                longest_zero_run / EXPECTED_SAMPLE_RATE * 1000, 3
            ),
        },
        "wav": {
            "channels": channels,
            "sample_width_bytes": sample_width,
            "sample_rate_hz": sample_rate,
            "frames": declared_frames,
            "compression": compression,
            "file_bytes": before.st_size,
        },
    }
    return (
        metrics,
        file_digest.hexdigest(),
        pcm_digest.hexdigest(),
        _stat_identity(after),
    )


def _rehash_wav_at(
    directory_fd: int,
    name: str,
) -> tuple[str, str, tuple[int, int, int, int]]:
    stream, before = _open_private_regular_at(
        directory_fd,
        name,
        maximum=MAX_WAV_BYTES,
    )
    with stream:
        payload = stream.read(MAX_WAV_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (
        _stat_identity(before) != _stat_identity(after)
        or len(payload) != before.st_size
    ):
        raise AuditError("WAV changed while it was rehashed")
    reader: wave.Wave_read | None = None
    try:
        reader = wave.open(io.BytesIO(payload), "rb")
        if (
            reader.getnchannels() != EXPECTED_CHANNELS
            or reader.getsampwidth() != EXPECTED_SAMPLE_WIDTH
            or reader.getframerate() != EXPECTED_SAMPLE_RATE
            or reader.getcomptype() != "NONE"
            or reader.getnframes() < 1
            or reader.getnframes() > MAX_AUDIO_FRAMES
        ):
            raise AuditError("WAV changed while it was rehashed")
        pcm_digest = hashlib.sha256()
        frame_count = 0
        while raw := reader.readframes(8192):
            if len(raw) % EXPECTED_SAMPLE_WIDTH:
                raise AuditError("WAV changed while it was rehashed")
            pcm_digest.update(raw)
            frame_count += len(raw) // EXPECTED_SAMPLE_WIDTH
        if frame_count != reader.getnframes():
            raise AuditError("WAV changed while it was rehashed")
    except (EOFError, wave.Error) as error:
        raise AuditError("WAV changed while it was rehashed") from error
    finally:
        if reader is not None:
            reader.close()
    return (
        hashlib.sha256(payload).hexdigest(),
        pcm_digest.hexdigest(),
        _stat_identity(after),
    )


def _raise_tier(current: str, candidate: str) -> str:
    return candidate if TIER_SEVERITY[candidate] > TIER_SEVERITY[current] else current


def _classify(
    metrics: dict[str, Any],
) -> tuple[str, list[str], list[str]]:
    overall = metrics["overall"]
    first = metrics["first_second"]
    frames = metrics["frame_analysis"]
    tier = "high"
    reasons: list[str] = []
    notes: list[str] = []

    def add(candidate: str, reason: str) -> None:
        nonlocal tier
        tier = _raise_tier(tier, candidate)
        reasons.append(reason)

    duration = metrics["duration_seconds"]
    clipping = overall["clipped_fraction"]
    dc_offset = abs(overall["dc_offset_fraction"])
    if duration < 0.3:
        add("reject", "duration-too-short")
    if overall["peak_dbfs"] <= -40 or frames["active_frame_fraction"] < 0.02:
        add("reject", "near-silence")
    elif frames["active_rms_dbfs"] < -35 or overall["peak_dbfs"] < -24:
        add("low", "very-low-level")
    elif frames["active_rms_dbfs"] < -30 or overall["peak_dbfs"] < -12:
        add("usable", "low-level")

    if clipping >= 0.10:
        add("reject", "severe-clipping")
    elif clipping >= 0.01:
        add("low", "heavy-clipping")
    elif clipping > 0:
        add("usable", "clipping-detected")

    if dc_offset >= 0.20:
        add("reject", "severe-dc-offset")
    elif dc_offset >= 0.05:
        add("low", "high-dc-offset")
    elif dc_offset >= 0.01:
        add("usable", "dc-offset")

    startup_clipping = first["clipped_fraction"]
    startup_dc = abs(first["dc_offset_fraction"])
    if startup_clipping >= 0.10 or startup_dc >= 0.20:
        add("low", "startup-contamination")
    elif startup_clipping >= 0.01 or startup_dc >= 0.05:
        add("usable", "startup-contamination")

    longest_zero_run = frames["longest_zero_run_ms"]
    if longest_zero_run >= max(1000.0, duration * 500):
        add("reject", "extended-digital-silence")
    elif longest_zero_run >= 1000:
        add("low", "digital-dropout")
    elif longest_zero_run >= 250:
        add("usable", "possible-digital-dropout")

    if duration > 30:
        notes.append("needs-segmentation")

    return tier, sorted(set(reasons)), notes


def _stored_quality_matches(stored: Any, computed: dict[str, Any]) -> bool | None:
    if stored is None:
        return None
    if not isinstance(stored, dict):
        return False
    expected = {
        "analysis_version": 1,
        "clipping_threshold_abs": CLIPPING_THRESHOLD_ABS,
        "overall": computed["overall"],
        "first_second": computed["first_second"],
    }
    return stored == expected


def _read_published_document(
    destination_fd: int,
    directory_name: str,
    filename: str,
    maximum: int,
) -> bytes | None:
    try:
        document_fd = _open_private_child_directory(destination_fd, directory_name)
    except FileNotFoundError:
        return None
    try:
        names = set(os.listdir(document_fd))
        if COMPLETE_MARKER_NAME not in names:
            raise IncompletePublication("published sidecar is incomplete")
        if names != {filename, COMPLETE_MARKER_NAME}:
            raise PublishError("published sidecar directory is invalid")
        stream, _ = _open_private_regular_at(
            document_fd,
            filename,
            maximum=maximum,
        )
        with stream:
            payload = stream.read(maximum + 1)
        marker_stream, _ = _open_private_regular_at(
            document_fd,
            COMPLETE_MARKER_NAME,
            maximum=MAX_COMPLETE_MARKER_BYTES,
        )
        with marker_stream:
            marker = marker_stream.read(MAX_COMPLETE_MARKER_BYTES + 1)
        if marker != _completion_marker(payload):
            raise PublishError("published sidecar completion marker is invalid")
        return payload
    finally:
        os.close(document_fd)


def _completion_marker(payload: bytes) -> bytes:
    return f"sha256={hashlib.sha256(payload).hexdigest()}\n".encode("ascii")


def _read_published_document_after_wait(
    destination_fd: int,
    directory_name: str,
    filename: str,
    maximum: int,
) -> bytes | None:
    deadline = time.monotonic() + CONCURRENT_PUBLICATION_WAIT_SECONDS
    while True:
        try:
            return _read_published_document(
                destination_fd,
                directory_name,
                filename,
                maximum,
            )
        except IncompletePublication:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _write_reserved_file(directory_fd: int, filename: str, payload: bytes) -> None:
    temporary = f".{filename}.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            details = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) not in {0o600, 0o700}
            ):
                raise PublishError("staged sidecar file mode is not private")
        os.rename(
            temporary,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _publish_directory_document(
    destination_fd: int,
    directory_name: str,
    filename: str,
    document: dict[str, Any],
    *,
    maximum_existing_bytes: int = MAX_RECORD_BYTES,
) -> str:
    payload = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    existing = _read_published_document_after_wait(
        destination_fd,
        directory_name,
        filename,
        maximum_existing_bytes,
    )
    if existing is not None:
        if existing == payload:
            return "unchanged"
        raise PublishError("existing sidecar conflicts with deterministic output")

    try:
        os.mkdir(directory_name, 0o700, dir_fd=destination_fd)
    except FileExistsError:
        existing = _read_published_document_after_wait(
            destination_fd,
            directory_name,
            filename,
            maximum_existing_bytes,
        )
        if existing == payload:
            return "unchanged"
        raise PublishError("existing sidecar conflicts with deterministic output")

    document_fd = _open_private_child_directory(destination_fd, directory_name)
    completed = False
    try:
        _write_reserved_file(document_fd, filename, payload)
        _write_reserved_file(
            document_fd,
            COMPLETE_MARKER_NAME,
            _completion_marker(payload),
        )
        completed = True
        os.fsync(destination_fd)
        return "created"
    finally:
        os.close(document_fd)
        if not completed:
            # Ordinary exceptions are cleaned up. A process crash may leave an
            # incomplete reserved directory; readers reject it because it has
            # no valid completion marker.
            try:
                cleanup_fd = _open_private_child_directory(
                    destination_fd,
                    directory_name,
                )
            except (FileNotFoundError, AuditError):
                cleanup_fd = None
            if cleanup_fd is not None:
                try:
                    if COMPLETE_MARKER_NAME not in set(os.listdir(cleanup_fd)):
                        for entry in os.listdir(cleanup_fd):
                            try:
                                os.unlink(entry, dir_fd=cleanup_fd)
                            except OSError:
                                pass
                finally:
                    os.close(cleanup_fd)
                try:
                    os.rmdir(directory_name, dir_fd=destination_fd)
                except OSError:
                    pass


def _valid_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _microphone_groups(microphone: Any) -> tuple[str, str, list[str]]:
    if not isinstance(microphone, dict):
        return (
            "missing",
            "missing",
            [] if microphone is None else ["microphone-metadata-invalid"],
        )
    warnings: list[str] = []
    selection = microphone.get("selection")
    if isinstance(selection, dict):
        selected = _safe_microphone_category(selection.get("category"))
        if selected == "unknown":
            warnings.append("microphone-metadata-invalid")
    else:
        selected = "unknown"
        warnings.append("microphone-metadata-invalid")
    actual = microphone.get("actual")
    routes: list[str] = []
    if not isinstance(actual, dict):
        return (
            selected,
            "unknown",
            sorted(set([*warnings, "microphone-metadata-invalid"])),
        )
    raw_routes = actual.get("routes")
    if not isinstance(raw_routes, list) or len(raw_routes) > 16:
        return (
            selected,
            "unknown",
            sorted(set([*warnings, "microphone-metadata-invalid"])),
        )
    previous = -1
    for route in raw_routes:
        if not isinstance(route, dict):
            return (
                selected,
                "unknown",
                sorted(set([*warnings, "microphone-metadata-invalid"])),
            )
        category = _safe_microphone_category(route.get("category"))
        observed_ms = route.get("first_observed_ms")
        if (
            category == "unknown"
            or type(observed_ms) is not int
            or observed_ms < previous
            or observed_ms < 0
            or observed_ms > 600_000
        ):
            return (
                selected,
                "unknown",
                sorted(set([*warnings, "microphone-metadata-invalid"])),
            )
        previous = observed_ms
        routes.append(category)
    changed = actual.get("route_changed")
    if type(changed) is not bool or changed != (len(routes) > 1):
        warnings.append("microphone-metadata-invalid")
    elif changed:
        warnings.append("source-changed-during-capture")
    route_group = ">".join(routes) if routes else "missing"
    if len(routes) > 4:
        route_group = "multiple"
    return selected, route_group, sorted(set(warnings))


def _safe_microphone_category(value: Any) -> str:
    if not isinstance(value, str):
        raise AuditError("microphone metadata is invalid")
    return value if value in MICROPHONE_CATEGORIES else "unknown"


def _local_day(timestamp: Any, timezone: ZoneInfo) -> str:
    if (
        not isinstance(timestamp, str)
        or UTC_TIMESTAMP_PATTERN.fullmatch(timestamp) is None
    ):
        raise AuditError("record timestamp is not UTC")
    try:
        parsed = datetime.fromisoformat(f"{timestamp[:-1]}+00:00")
    except ValueError as error:
        raise AuditError("record timestamp is invalid") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise AuditError("record timestamp is not UTC")
    return parsed.astimezone(timezone).date().isoformat()


def _assert_child_directory_stable(parent_fd: int, name: str, child_fd: int) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise AuditError("audit directory changed during inspection") from error
    opened = os.fstat(child_fd)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        raise AuditError("audit directory changed during inspection")


def _assert_regular_file_stable(
    directory_fd: int,
    name: str,
    expected: tuple[int, int, int, int],
) -> None:
    stream, details = _open_private_regular_at(
        directory_fd,
        name,
        maximum=MAX_WAV_BYTES if name == "audio.wav" else MAX_RECORD_BYTES,
    )
    stream.close()
    if _stat_identity(details) != expected:
        raise AuditError("record file changed during inspection")


def _same_directory(left_fd: int, right_fd: int) -> bool:
    left = os.fstat(left_fd)
    right = os.fstat(right_fd)
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _directory_is_within(child_fd: int, ancestor_fd: int) -> bool:
    target = os.fstat(ancestor_fd)
    target_identity = (target.st_dev, target.st_ino)
    current_fd = os.dup(child_fd)
    try:
        for _ in range(256):
            current = os.fstat(current_fd)
            if (current.st_dev, current.st_ino) == target_identity:
                return True
            parent_fd = os.open("..", _directory_flags(), dir_fd=current_fd)
            parent = os.fstat(parent_fd)
            if (parent.st_dev, parent.st_ino) == (current.st_dev, current.st_ino):
                os.close(parent_fd)
                return False
            os.close(current_fd)
            current_fd = parent_fd
    finally:
        os.close(current_fd)
    raise AuditError("audit directory ancestry is too deep")


def audit_dataset(
    root: Path, sidecar_dir: Path | None, timezone: ZoneInfo
) -> dict[str, Any]:
    with ExitStack() as resources:
        root_fd = _open_directory_path(root)
        resources.callback(os.close, root_fd)
        utterances_fd = _open_private_child_directory(root_fd, "utterances")
        resources.callback(os.close, utterances_fd)
        marker, _, _ = _load_record_at(root_fd, "dataset.json")
        if (
            type(marker.get("schema_version")) is not int
            or marker["schema_version"] != 1
        ):
            raise AuditError("dataset marker schema is unsupported")
        if marker.get("kind") != "openvoiceinput-personal-asr-dataset":
            raise AuditError("dataset marker kind is invalid")
        dataset_id = marker.get("dataset_id")
        if not _valid_id(dataset_id):
            raise AuditError("dataset marker identity is invalid")

        quality_fd = manifests_fd = None
        quality_parent_fd = None
        parent_is_dataset_root = False
        if sidecar_dir is not None:
            if sidecar_dir.name != "quality-v1":
                raise AuditError(
                    "sidecar directory must be the versioned quality-v1 directory"
                )
            quality_parent_fd = _open_directory_path(sidecar_dir.parent)
            resources.callback(os.close, quality_parent_fd)
            parent_is_dataset_root = _same_directory(quality_parent_fd, root_fd)
            if not parent_is_dataset_root and _directory_is_within(
                quality_parent_fd, root_fd
            ):
                raise AuditError("sidecar directory overlaps immutable dataset content")

        snapshot = sorted(os.listdir(utterances_fd))
        if len(snapshot) > 100_000:
            raise AuditError("dataset has too many utterance entries")
        tiers: collections.Counter[str] = collections.Counter()
        tier_seconds: collections.Counter[str] = collections.Counter()
        reasons: collections.Counter[str] = collections.Counter()
        provenance_warnings: collections.Counter[str] = collections.Counter()
        schemas: collections.Counter[str] = collections.Counter()
        selected_mics: dict[str, collections.Counter[str]] = collections.defaultdict(
            collections.Counter
        )
        actual_mics: dict[str, collections.Counter[str]] = collections.defaultdict(
            collections.Counter
        )
        local_days: dict[str, collections.Counter[str]] = collections.defaultdict(
            collections.Counter
        )
        local_day_seconds: dict[str, float] = collections.defaultdict(float)
        stored_quality: collections.Counter[str] = collections.Counter()
        structural_failures: collections.Counter[str] = collections.Counter()
        total_seconds = 0.0
        emitted: collections.Counter[str] = collections.Counter()
        manifest_entries: list[dict[str, Any]] = []
        sidecar_documents: list[tuple[str, dict[str, Any]]] = []
        source_identities: list[
            tuple[
                str,
                tuple[int, int],
                tuple[int, int, int, int],
                tuple[int, int, int, int],
                str,
                str,
                str,
            ]
        ] = []
        recorded_timestamps: list[str] = []

        for name in snapshot:
            record_hash: str | None = None
            try:
                if not _valid_id(name):
                    raise AuditError("utterance directory identity is invalid")
                try:
                    record_fd = _open_private_child_directory(utterances_fd, name)
                except OSError as error:
                    raise AuditError(
                        "utterance entry is not a private directory"
                    ) from error
                try:
                    if set(os.listdir(record_fd)) != {"audio.wav", "record.json"}:
                        raise AuditError("utterance directory contents are invalid")
                    directory_details = os.fstat(record_fd)
                    directory_identity = (
                        directory_details.st_dev,
                        directory_details.st_ino,
                    )
                    record, record_hash, record_file_identity = _load_record_at(
                        record_fd,
                        "record.json",
                    )
                    if (
                        record.get("utterance_id") != name
                        or record.get("dataset_id") != dataset_id
                    ):
                        raise AuditError("record identity does not match its directory")
                    schema_version = record.get("schema_version")
                    if type(schema_version) is not int or schema_version not in (
                        1,
                        2,
                        3,
                        4,
                    ):
                        raise AuditError("record schema version is unsupported")
                    audio = record.get("audio")
                    if not isinstance(audio, dict):
                        raise AuditError("record audio envelope is invalid")
                    (
                        metrics,
                        file_sha256,
                        pcm_sha256,
                        audio_file_identity,
                    ) = _read_pcm_metrics_at(record_fd, "audio.wav")
                    wav = metrics["wav"]
                    expected = {
                        "file": "audio.wav",
                        "format": "wav-pcm-s16le",
                        "sample_rate_hz": EXPECTED_SAMPLE_RATE,
                        "channels": EXPECTED_CHANNELS,
                        "frames": wav["frames"],
                        "file_sha256": file_sha256,
                        "pcm_sha256": pcm_sha256,
                    }
                    integer_fields = {"sample_rate_hz", "channels", "frames"}
                    for key, value in expected.items():
                        if audio.get(key) != value or (
                            key in integer_fields and type(audio.get(key)) is not int
                        ):
                            raise AuditError(f"record audio {key} does not match WAV")
                    stored_match = _stored_quality_matches(
                        audio.get("quality"), metrics
                    )
                    tier, reason_codes, processing_notes = _classify(metrics)
                    microphone = record.get("microphone")
                    selected, actual, record_warnings = _microphone_groups(microphone)
                    recorded_at_utc = record.get("recorded_at_utc")
                    local_day = _local_day(recorded_at_utc, timezone)
                    assert isinstance(recorded_at_utc, str)
                    _assert_child_directory_stable(utterances_fd, name, record_fd)
                    _assert_regular_file_stable(
                        record_fd,
                        "record.json",
                        record_file_identity,
                    )
                    _assert_regular_file_stable(
                        record_fd,
                        "audio.wav",
                        audio_file_identity,
                    )
                finally:
                    os.close(record_fd)

                duration = metrics["duration_seconds"]
                sidecar = {
                    "schema_version": 1,
                    "dataset_id": dataset_id,
                    "utterance_id": name,
                    "assessment": {
                        "scope": "signal-only",
                        "classification_basis": "heuristic-threshold-policy-v1",
                        "tier": tier,
                        "reason_codes": reason_codes,
                        "processing_notes": processing_notes,
                        "transcript_review_status": "not-evaluated",
                        "training_label_status": "non-gold-signal-audit-only",
                    },
                    "provenance": {"warnings": record_warnings},
                    "record_identity": {"record_json_sha256": record_hash},
                    "audio_identity": {
                        "file_sha256": file_sha256,
                        "pcm_sha256": pcm_sha256,
                    },
                    "analyzer": {
                        "name": ANALYZER_NAME,
                        "version": ANALYZER_VERSION,
                        "policy_version": POLICY_VERSION,
                    },
                    "metrics": metrics,
                }
                # Accumulate only after every structural check for this record
                # has succeeded. Publication is a separate second phase after
                # the complete snapshot is revalidated.
                schemas[str(schema_version)] += 1
                total_seconds += duration
                tiers[tier] += 1
                tier_seconds[tier] += duration
                reasons.update(reason_codes)
                provenance_warnings.update(record_warnings)
                stored_quality[
                    "missing"
                    if stored_match is None
                    else "match"
                    if stored_match
                    else "mismatch"
                ] += 1
                selected_mics[selected][tier] += 1
                actual_mics[actual][tier] += 1
                local_days[local_day][tier] += 1
                local_day_seconds[local_day] += duration
                manifest_entries.append(
                    {
                        "utterance_id": name,
                        "status": "audited",
                        "tier": tier,
                        "record_identity": sidecar["record_identity"],
                        "audio_identity": sidecar["audio_identity"],
                    }
                )
                source_identities.append(
                    (
                        name,
                        directory_identity,
                        record_file_identity,
                        audio_file_identity,
                        record_hash,
                        file_sha256,
                        pcm_sha256,
                    )
                )
                sidecar_documents.append((name, sidecar))
                recorded_timestamps.append(recorded_at_utc)
            except PublishError:
                raise
            except OSError:
                structural_failures["record-filesystem-error"] += 1
                tiers["reject"] += 1
                reasons["structural-invalid"] += 1
                manifest_entries.append(
                    {
                        "entry_name_sha256": hashlib.sha256(
                            os.fsencode(name)
                        ).hexdigest(),
                        "status": "structural-invalid",
                    }
                )
            except AuditError as error:
                structural_failures[str(error)] += 1
                tiers["reject"] += 1
                reasons["structural-invalid"] += 1
                entry = {
                    "entry_name_sha256": hashlib.sha256(os.fsencode(name)).hexdigest(),
                    "status": "structural-invalid",
                }
                if record_hash is not None:
                    entry["record_identity"] = {"record_json_sha256": record_hash}
                manifest_entries.append(entry)

        for (
            name,
            expected_directory,
            expected_record,
            expected_audio,
            expected_record_hash,
            expected_file_hash,
            expected_pcm_hash,
        ) in source_identities:
            try:
                stable_fd = _open_private_child_directory(utterances_fd, name)
            except OSError as error:
                raise AuditError("utterance changed after validation") from error
            try:
                details = os.fstat(stable_fd)
                if (details.st_dev, details.st_ino) != expected_directory:
                    raise AuditError("utterance changed after validation")
                _assert_regular_file_stable(
                    stable_fd,
                    "record.json",
                    expected_record,
                )
                _assert_regular_file_stable(
                    stable_fd,
                    "audio.wav",
                    expected_audio,
                )
                _, current_record_hash, current_record_identity = _load_record_at(
                    stable_fd,
                    "record.json",
                )
                (
                    current_file_hash,
                    current_pcm_hash,
                    current_audio_identity,
                ) = _rehash_wav_at(stable_fd, "audio.wav")
                if (
                    current_record_hash != expected_record_hash
                    or current_file_hash != expected_file_hash
                    or current_pcm_hash != expected_pcm_hash
                    or current_record_identity != expected_record
                    or current_audio_identity != expected_audio
                ):
                    raise AuditError("utterance content changed after validation")
            finally:
                os.close(stable_fd)
        _assert_child_directory_stable(root_fd, "utterances", utterances_fd)

        def grouped(values: dict[str, collections.Counter[str]]) -> dict[str, Any]:
            return {
                key: {
                    "count": sum(counter.values()),
                    "tiers": dict(sorted(counter.items())),
                }
                for key, counter in sorted(values.items())
            }

        today = datetime.now(timezone).date().isoformat()
        day_rows = {
            day: {
                "count": sum(counter.values()),
                "duration_seconds": round(local_day_seconds[day], 6),
                "tiers": dict(sorted(counter.items())),
            }
            for day, counter in sorted(local_days.items())
        }
        manifest_result = None
        if sidecar_dir is not None and structural_failures:
            manifest_result = {"status": "not-published-structural-failures"}
        elif sidecar_dir is not None:
            assert quality_parent_fd is not None
            quality_fd = _open_private_child_directory(
                quality_parent_fd,
                "quality-v1",
                create=True,
            )
            resources.callback(os.close, quality_fd)
            if (
                _same_directory(quality_fd, root_fd)
                or (
                    not parent_is_dataset_root
                    and (
                        _directory_is_within(quality_fd, root_fd)
                        or _directory_is_within(root_fd, quality_fd)
                    )
                )
                or _same_directory(quality_fd, utterances_fd)
                or _directory_is_within(quality_fd, utterances_fd)
                or _directory_is_within(utterances_fd, quality_fd)
            ):
                raise AuditError("sidecar directory overlaps immutable dataset content")
            manifests_fd = _open_private_child_directory(
                quality_fd,
                "manifests",
                create=True,
            )
            resources.callback(os.close, manifests_fd)

            identity = {
                "dataset_id": dataset_id,
                "scope": "audio-signal-sidecar-snapshot",
                "classification_basis": "heuristic-threshold-policy-v1",
                "transcript_review_status": "not-evaluated",
                "training_label_status": "non-gold-signal-audit-only",
                "timezone": str(timezone),
                "as_of_recorded_at_utc": (
                    max(recorded_timestamps) if recorded_timestamps else None
                ),
                "analyzer": {
                    "name": ANALYZER_NAME,
                    "version": ANALYZER_VERSION,
                    "policy_version": POLICY_VERSION,
                },
                "records": manifest_entries,
            }
            identity_payload = json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            snapshot_digest = hashlib.sha256(identity_payload).hexdigest()
            manifest = {
                "schema_version": 1,
                "snapshot_sha256": snapshot_digest,
                **identity,
                "snapshot_record_count": len(snapshot),
            }

            # Preflight every deterministic destination before publishing any
            # new sidecar. Atomic mkdir reservation still closes later races.
            publication_plan = [
                (quality_fd, name, "quality.json", document, MAX_RECORD_BYTES)
                for name, document in sidecar_documents
            ]
            publication_plan.append(
                (
                    manifests_fd,
                    snapshot_digest,
                    "manifest.json",
                    manifest,
                    MAX_MANIFEST_BYTES,
                )
            )
            for (
                destination_fd,
                directory_name,
                filename,
                document,
                maximum,
            ) in publication_plan:
                existing = _read_published_document_after_wait(
                    destination_fd,
                    directory_name,
                    filename,
                    maximum,
                )
                if (
                    existing is not None
                    and existing
                    != (
                        json.dumps(
                            document,
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        )
                        + "\n"
                    ).encode()
                ):
                    raise PublishError(
                        "existing sidecar conflicts with deterministic output"
                    )

            for name, document in sidecar_documents:
                emitted[
                    _publish_directory_document(
                        quality_fd,
                        name,
                        "quality.json",
                        document,
                    )
                ] += 1
            manifest_status = _publish_directory_document(
                manifests_fd,
                snapshot_digest,
                "manifest.json",
                manifest,
                maximum_existing_bytes=MAX_MANIFEST_BYTES,
            )
            _assert_child_directory_stable(
                quality_parent_fd,
                "quality-v1",
                quality_fd,
            )
            manifest_result = {
                "status": manifest_status,
                "snapshot_sha256": snapshot_digest,
            }

        return {
            "audit": {
                "analyzer": ANALYZER_NAME,
                "version": ANALYZER_VERSION,
                "policy_version": POLICY_VERSION,
                "scope": "signal-only",
                "classification_basis": "heuristic-threshold-policy-v1",
                "transcript_review_status": "not-evaluated",
                "training_label_status": "non-gold-signal-audit-only",
                "network_or_asr_used": False,
                "timezone": str(timezone),
            },
            "snapshot": {
                "records": len(snapshot),
                "duration_seconds": round(total_seconds, 6),
                "duration_hours": round(total_seconds / 3600, 6),
                "tiers": {
                    tier: {
                        "count": tiers[tier],
                        "duration_seconds": round(tier_seconds[tier], 6),
                    }
                    for tier in TIERS
                },
                "reason_counts": dict(sorted(reasons.items())),
                "provenance_warning_counts": dict(sorted(provenance_warnings.items())),
                "schema_versions": dict(sorted(schemas.items())),
                "stored_quality": dict(sorted(stored_quality.items())),
                "structural_failures": dict(sorted(structural_failures.items())),
                "microphone_selection": grouped(selected_mics),
                "microphone_actual_routes": grouped(actual_mics),
                "local_days": day_rows,
                "today": day_rows.get(
                    today,
                    {"count": 0, "duration_seconds": 0.0, "tiers": {}},
                ),
            },
            "sidecars": {
                "layout": "quality-v1/<utterance-id>/{quality.json,complete}",
                "publication": "atomic-mkdir-reservation-complete-marker",
                "snapshot_commit": "complete-manifest-only",
                "interrupted_publish": (
                    "complete-sidecars-may-remain-as-cache; consumers-ignore-"
                    "anything-not-referenced-by-a-complete-manifest"
                ),
                "records": dict(sorted(emitted.items())),
                "manifest": manifest_result,
            },
        }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_root", type=Path)
    parser.add_argument(
        "--sidecar-dir",
        type=Path,
        help="optional separate private quality-v1 directory; immutable source records are never edited",
    )
    parser.add_argument("--timezone", default="UTC")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    try:
        timezone = ZoneInfo(arguments.timezone)
        summary = audit_dataset(arguments.dataset_root, arguments.sidecar_dir, timezone)
    except (AuditError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {"ok": True, **summary}, ensure_ascii=False, sort_keys=True, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
