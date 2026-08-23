#!/usr/bin/env python3
"""Validate and record one per-user Open Voice Input installation.

This helper never imports code from an existing installation.  It treats the
manifest as an ownership claim only after all fixed paths, metadata, and
content digests have been checked independently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import stat
from pathlib import Path
from typing import Any, Sequence

MANIFEST_FORMAT = "openvoice-user-install"
MANIFEST_VERSION = 1
MANIFEST_NAME = "install-manifest.json"
MAX_MANIFEST_BYTES = 64 * 1024

_INSTALL_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_DIGESTS = frozenset(
    {
        "engine_launcher",
        "settings_launcher",
        "voice_launcher",
        "engine_package",
        "voice_package",
        "voice_marker",
        "previous_engine_state",
        "engine_unit",
        "voice_unit",
    }
)
_EXPECTED_ROOT_ENTRIES = frozenset(
    {
        MANIFEST_NAME,
        "murmur-ime-engine",
        "murmur-voice-daemon",
        "murmur_ime_engine",
        "open-voice-input-settings",
        "previous-ibus-engine",
        "voice-venv",
    }
)


class ManifestError(RuntimeError):
    """A safe validation failure without manifest-controlled path output."""


def _require_absolute(path: Path, kind: str) -> None:
    if not path.is_absolute():
        raise ManifestError(f"{kind} path must be absolute")


def secure_directory(
    path: Path,
    *,
    kind: str,
    create: bool = False,
    private: bool = False,
) -> None:
    """Require a real, current-user-owned, non-writable directory."""

    _require_absolute(path, kind)
    if create:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise ManifestError(f"{kind} directory could not be created") from error
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ManifestError(f"{kind} directory is unavailable") from error
    forbidden = 0o077 if private else 0o022
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & forbidden
    ):
        qualifier = "private and " if private else ""
        raise ManifestError(
            f"{kind} directory must be {qualifier}user-owned and not linked"
        )


def _regular_digest(
    path: Path,
    *,
    kind: str,
    executable: bool = False,
    private: bool = False,
) -> str:
    _require_absolute(path, kind)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ManifestError(f"{kind} file could not be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ManifestError(f"{kind} file must be regular and user-owned")
        if mode & 0o022:
            raise ManifestError(f"{kind} file must not be group/other writable")
        if private and mode & 0o077:
            raise ManifestError(f"{kind} file must be private")
        if executable and not mode & 0o100:
            raise ManifestError(f"{kind} file must be owner-executable")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 128 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _tree_digest(path: Path, *, kind: str) -> str:
    secure_directory(path, kind=kind)
    digest = hashlib.sha256()
    file_count = 0
    for directory, names, files in os.walk(path, topdown=True, followlinks=False):
        directory_path = Path(directory)
        directory_metadata = directory_path.lstat()
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.getuid()
            or stat.S_IMODE(directory_metadata.st_mode) & 0o022
        ):
            raise ManifestError(f"{kind} contains an unsafe directory")
        for name in names:
            child = directory_path / name
            child_metadata = child.lstat()
            if (
                not stat.S_ISDIR(child_metadata.st_mode)
                or child_metadata.st_uid != os.getuid()
                or stat.S_IMODE(child_metadata.st_mode) & 0o022
            ):
                raise ManifestError(f"{kind} contains an unsafe directory")
        names[:] = sorted(name for name in names if name != "__pycache__")
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            child = directory_path / name
            relative = child.relative_to(path).as_posix()
            child_digest = _regular_digest(child, kind=kind)
            encoded = relative.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
            digest.update(bytes.fromhex(child_digest))
            file_count += 1
    if file_count == 0:
        raise ManifestError(f"{kind} package is empty")
    return digest.hexdigest()


def _voice_package_path(root: Path) -> Path:
    candidates = sorted(
        path
        for path in (root / "voice-venv" / "lib").glob(
            "python*/site-packages/murmur_voice"
        )
        if path.exists() or path.is_symlink()
    )
    if len(candidates) != 1:
        raise ManifestError("managed voice environment has no unique voice package")
    return candidates[0]


def _critical_digests(
    root: Path,
    engine_unit: Path,
    voice_unit: Path,
    *,
    install_id: str,
) -> dict[str, str]:
    secure_directory(root, kind="installation root")
    secure_directory(root / "murmur_ime_engine", kind="engine package")
    secure_directory(root / "voice-venv", kind="managed voice environment")
    marker = root / "voice-venv" / ".murmur-ime-managed"
    marker_digest = _regular_digest(marker, kind="voice ownership marker", private=True)
    try:
        marker_value = marker.read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise ManifestError("voice ownership marker is invalid") from error
    if marker_value != f"{install_id}\n":
        raise ManifestError("voice ownership marker does not match the install id")
    return {
        "engine_launcher": _regular_digest(
            root / "murmur-ime-engine", kind="engine launcher", executable=True
        ),
        "settings_launcher": _regular_digest(
            root / "open-voice-input-settings",
            kind="settings launcher",
            executable=True,
        ),
        "voice_launcher": _regular_digest(
            root / "murmur-voice-daemon", kind="voice launcher", executable=True
        ),
        "engine_package": _tree_digest(
            root / "murmur_ime_engine", kind="engine package"
        ),
        "voice_package": _tree_digest(_voice_package_path(root), kind="voice package"),
        "voice_marker": marker_digest,
        "previous_engine_state": _regular_digest(
            root / "previous-ibus-engine",
            kind="previous-engine state",
            private=True,
        ),
        "engine_unit": _regular_digest(engine_unit, kind="engine unit"),
        "voice_unit": _regular_digest(voice_unit, kind="voice unit"),
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ManifestError(
            "installation manifest could not be opened safely"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > MAX_MANIFEST_BYTES
        ):
            raise ManifestError("installation manifest metadata is unsafe")
        chunks: list[bytes] = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ManifestError("installation manifest is too large")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ManifestError("installation manifest is invalid") from error
    if not isinstance(document, dict):
        raise ManifestError("installation manifest is invalid")
    return document


def _validate_document(document: dict[str, Any]) -> tuple[str, dict[str, str]]:
    if set(document) != {"format", "version", "install_id", "digests"}:
        raise ManifestError("installation manifest schema is invalid")
    if (
        document.get("format") != MANIFEST_FORMAT
        or document.get("version") != MANIFEST_VERSION
    ):
        raise ManifestError("installation manifest version is unsupported")
    install_id = document.get("install_id")
    if not isinstance(install_id, str) or _INSTALL_ID_RE.fullmatch(install_id) is None:
        raise ManifestError("installation manifest install id is invalid")
    digests = document.get("digests")
    if not isinstance(digests, dict) or set(digests) != _EXPECTED_DIGESTS:
        raise ManifestError("installation manifest digest set is invalid")
    if any(
        not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None
        for value in digests.values()
    ):
        raise ManifestError("installation manifest contains an invalid digest")
    return install_id, digests


def create_manifest(
    root: Path,
    engine_unit: Path,
    voice_unit: Path,
    output: Path,
    install_id: str,
) -> None:
    if _INSTALL_ID_RE.fullmatch(install_id) is None:
        raise ManifestError("install id is invalid")
    if output != root / MANIFEST_NAME:
        raise ManifestError("manifest must use the fixed installation path")
    if output.exists() or output.is_symlink():
        raise ManifestError("refusing to replace an existing installation manifest")
    document = {
        "format": MANIFEST_FORMAT,
        "version": MANIFEST_VERSION,
        "install_id": install_id,
        "digests": _critical_digests(
            root, engine_unit, voice_unit, install_id=install_id
        ),
    }
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(output, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short manifest write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ManifestError("installation manifest could not be written") from error


def verify_manifest(root: Path, engine_unit: Path, voice_unit: Path) -> str:
    secure_directory(root, kind="installation root")
    entries = {path.name for path in root.iterdir()}
    if entries != _EXPECTED_ROOT_ENTRIES:
        raise ManifestError("installation root contains unowned top-level entries")
    document = _read_manifest(root / MANIFEST_NAME)
    install_id, expected = _validate_document(document)
    actual = _critical_digests(root, engine_unit, voice_unit, install_id=install_id)
    if actual != expected:
        raise ManifestError("installed files do not match the ownership manifest")
    return install_id


def socket_state(runtime_root: Path, path: Path) -> str:
    secure_directory(runtime_root, kind="runtime root", private=True)
    expected_parent = runtime_root / "murmur-ime"
    if path != expected_parent / "voice.sock":
        raise ManifestError("control socket must use the fixed runtime path")
    try:
        secure_directory(expected_parent, kind="control socket", private=True)
    except ManifestError:
        if not expected_parent.exists() and not expected_parent.is_symlink():
            return "absent"
        raise
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "absent"
    except OSError as error:
        raise ManifestError("control socket metadata is unavailable") from error
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ManifestError("control socket path is unsafe")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return "stale"
    except TimeoutError:
        return "live"
    except OSError as error:
        raise ManifestError("control socket could not be probed safely") from error
    finally:
        probe.close()
    return "live"


def require_disjoint(left: Path, right: Path) -> None:
    """Reject equal or nested data/config destinations after safe resolution."""

    _require_absolute(left, "first destination")
    _require_absolute(right, "second destination")
    resolved_left = left.resolve(strict=False)
    resolved_right = right.resolve(strict=False)
    if resolved_left == resolved_right:
        raise ManifestError("data and config destinations must be disjoint")
    try:
        resolved_left.relative_to(resolved_right)
    except ValueError:
        pass
    else:
        raise ManifestError("data and config destinations must not be nested")
    try:
        resolved_right.relative_to(resolved_left)
    except ValueError:
        return
    raise ManifestError("data and config destinations must not be nested")


def managed_voice_process_count(root: Path) -> int:
    """Count exact installed-wrapper daemon argv owned by the current uid."""

    secure_directory(root, kind="installation root")
    expected_python = os.fsencode(root / "voice-venv" / "bin" / "python")
    expected_prefix = (expected_python, b"-s", b"-m", b"murmur_voice")
    count = 0
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError as error:
        raise ManifestError("process table is unavailable") from error
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        except OSError as error:
            raise ManifestError(
                "managed daemon process could not be inspected"
            ) from error
        if len(raw) > 128 * 1024:
            raise ManifestError("managed daemon command line is unexpectedly large")
        arguments = tuple(item for item in raw.split(b"\0") if item)
        if arguments[:4] == expected_prefix:
            count += 1
    return count


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    secure = subparsers.add_parser("secure-dir")
    secure.add_argument("--path", required=True, type=Path)
    secure.add_argument("--kind", required=True)
    secure.add_argument("--create", action="store_true")
    secure.add_argument("--private", action="store_true")

    create = subparsers.add_parser("create")
    create.add_argument("--root", required=True, type=Path)
    create.add_argument("--engine-unit", required=True, type=Path)
    create.add_argument("--voice-unit", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--install-id", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", required=True, type=Path)
    verify.add_argument("--engine-unit", required=True, type=Path)
    verify.add_argument("--voice-unit", required=True, type=Path)

    probe = subparsers.add_parser("socket-state")
    probe.add_argument("--runtime-root", required=True, type=Path)
    probe.add_argument("--path", required=True, type=Path)
    disjoint = subparsers.add_parser("require-disjoint")
    disjoint.add_argument("--left", required=True, type=Path)
    disjoint.add_argument("--right", required=True, type=Path)
    processes = subparsers.add_parser("voice-process-count")
    processes.add_argument("--root", required=True, type=Path)
    subparsers.add_parser("new-id")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    options = parser.parse_args(arguments)
    try:
        if options.command == "secure-dir":
            secure_directory(
                options.path,
                kind=options.kind,
                create=options.create,
                private=options.private,
            )
        elif options.command == "create":
            create_manifest(
                options.root,
                options.engine_unit,
                options.voice_unit,
                options.output,
                options.install_id,
            )
        elif options.command == "verify":
            print(
                verify_manifest(options.root, options.engine_unit, options.voice_unit)
            )
        elif options.command == "socket-state":
            print(socket_state(options.runtime_root, options.path))
        elif options.command == "require-disjoint":
            require_disjoint(options.left, options.right)
        elif options.command == "voice-process-count":
            print(managed_voice_process_count(options.root))
        else:
            print(secrets.token_hex(16))
    except ManifestError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
