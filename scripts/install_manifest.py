#!/usr/bin/env python3
"""Validate and record one per-user Open Voice Input installation.

This helper never imports code from an existing installation.  It treats the
manifest as an ownership claim only after all fixed paths, metadata, and
content digests have been checked independently.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
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
MANIFEST_VERSION = 2
MANIFEST_NAME = "install-manifest.json"
MAX_MANIFEST_BYTES = 64 * 1024
DESKTOP_ENTRY_NAME = "io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop"
ICON_NAME = "io.github.SidUParis.OpenVoiceInputLinux.Settings.svg"

_INSTALL_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_RE = re.compile(r"^[0-9]+:[0-9]+$")
_V1_DIGESTS = frozenset(
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
_V2_DIGESTS = _V1_DIGESTS | {"desktop_entry", "settings_icon"}
_SUPPORTED_VERSIONS = {1, MANIFEST_VERSION}
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


def secure_directory_descriptor(path: Path, descriptor: int) -> str:
    """Require an inherited descriptor for the current secure directory path."""

    _require_absolute(path, "lock directory")
    try:
        path_metadata = path.lstat()
        descriptor_metadata = os.fstat(descriptor)
    except OSError as error:
        raise ManifestError("lock directory descriptor is unavailable") from error
    if (
        not stat.S_ISDIR(path_metadata.st_mode)
        or not stat.S_ISDIR(descriptor_metadata.st_mode)
        or path_metadata.st_uid != os.getuid()
        or descriptor_metadata.st_uid != os.getuid()
        or stat.S_IMODE(path_metadata.st_mode) & 0o022
        or stat.S_IMODE(descriptor_metadata.st_mode) & 0o022
        or (path_metadata.st_dev, path_metadata.st_ino)
        != (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
    ):
        raise ManifestError(
            "lock descriptor must reference the current user-owned directory"
        )
    return f"{descriptor_metadata.st_dev}:{descriptor_metadata.st_ino}"


def require_absent(paths: Sequence[Path]) -> None:
    """Recheck that fixed destinations are absent immediately before commit."""

    for path in paths:
        _require_absolute(path, "asset destination")
        secure_directory(path.parent, kind="asset destination")
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ManifestError("asset destination metadata is unavailable") from error
        raise ManifestError("refusing to replace an existing destination")


def move_no_clobber(source: Path, destination: Path) -> str:
    """Atomically rename one owned staged path without replacing a path."""

    _require_absolute(source, "staged asset")
    _require_absolute(destination, "asset destination")
    secure_directory(source.parent, kind="staged path")
    secure_directory(destination.parent, kind="destination")
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        source_parent = os.open(source.parent, directory_flags)
        destination_parent = os.open(destination.parent, directory_flags)
    except OSError as error:
        raise ManifestError("asset directory could not be opened safely") from error
    try:
        source_parent_metadata = os.fstat(source_parent)
        destination_parent_metadata = os.fstat(destination_parent)
        if (
            not stat.S_ISDIR(source_parent_metadata.st_mode)
            or not stat.S_ISDIR(destination_parent_metadata.st_mode)
            or source_parent_metadata.st_uid != os.getuid()
            or destination_parent_metadata.st_uid != os.getuid()
            or stat.S_IMODE(source_parent_metadata.st_mode) & 0o022
            or stat.S_IMODE(destination_parent_metadata.st_mode) & 0o022
        ):
            raise ManifestError("move directory metadata is unsafe")
        source_metadata = os.stat(
            source.name, dir_fd=source_parent, follow_symlinks=False
        )
        if (
            not (
                stat.S_ISREG(source_metadata.st_mode)
                or stat.S_ISDIR(source_metadata.st_mode)
            )
            or source_metadata.st_uid != os.getuid()
            or stat.S_IMODE(source_metadata.st_mode) & 0o022
        ):
            raise ManifestError("staged path must be owned and not linked")
        rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if rename is None:
            raise ManifestError("atomic no-clobber rename is unavailable")
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        if (
            rename(
                source_parent,
                os.fsencode(source.name),
                destination_parent,
                os.fsencode(destination.name),
                1,
            )
            != 0
        ):
            error_number = ctypes.get_errno()
            if error_number in (errno.EEXIST, errno.ENOTEMPTY):
                raise ManifestError("refusing to replace an existing destination")
            if error_number == errno.EXDEV:
                raise ManifestError(
                    "staged path and destination must use one filesystem"
                )
            raise ManifestError(
                "staged path could not be committed safely"
            ) from OSError(error_number, os.strerror(error_number))
        # A successful renameat2 is the commit point.  Do not introduce a
        # fallible post-rename check that could hide the completed move from
        # the caller's rollback flag; the fixed-path manifest verification is
        # responsible for validating committed content before services start.
        return f"{source_metadata.st_dev}:{source_metadata.st_ino}"
    except OSError as error:
        raise ManifestError("staged path metadata is unavailable") from error
    finally:
        for descriptor in (destination_parent, source_parent):
            try:
                os.close(descriptor)
            except OSError:
                pass


def quarantine_committed(source: Path, quarantine: Path, identity: str) -> None:
    """Isolate only the exact inode committed by a completed transaction."""

    if _IDENTITY_RE.fullmatch(identity) is None:
        raise ManifestError("committed path identity is invalid")
    actual_identity = move_no_clobber(source, quarantine)
    if actual_identity == identity:
        return
    try:
        move_no_clobber(quarantine, source)
    except ManifestError as error:
        raise ManifestError(
            "changed destination was retained in the rollback quarantine"
        ) from error
    raise ManifestError("committed destination identity changed")


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
    desktop_entry: Path | None = None,
    settings_icon: Path | None = None,
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
    digests = {
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
    if desktop_entry is not None or settings_icon is not None:
        if desktop_entry is None or settings_icon is None:
            raise ManifestError("both desktop assets are required")
        secure_directory(desktop_entry.parent, kind="desktop entry")
        secure_directory(settings_icon.parent, kind="settings icon")
        digests.update(
            {
                "desktop_entry": _regular_digest(desktop_entry, kind="desktop entry"),
                "settings_icon": _regular_digest(settings_icon, kind="settings icon"),
            }
        )
    return digests


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


def _validate_document(document: dict[str, Any]) -> tuple[int, str, dict[str, str]]:
    if set(document) != {"format", "version", "install_id", "digests"}:
        raise ManifestError("installation manifest schema is invalid")
    version = document.get("version")
    if (
        document.get("format") != MANIFEST_FORMAT
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version not in _SUPPORTED_VERSIONS
    ):
        raise ManifestError("installation manifest version is unsupported")
    install_id = document.get("install_id")
    if not isinstance(install_id, str) or _INSTALL_ID_RE.fullmatch(install_id) is None:
        raise ManifestError("installation manifest install id is invalid")
    digests = document.get("digests")
    expected_digests = _V1_DIGESTS if version == 1 else _V2_DIGESTS
    if not isinstance(digests, dict) or set(digests) != expected_digests:
        raise ManifestError("installation manifest digest set is invalid")
    if any(
        not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None
        for value in digests.values()
    ):
        raise ManifestError("installation manifest contains an invalid digest")
    return version, install_id, digests


def create_manifest(
    root: Path,
    engine_unit: Path,
    voice_unit: Path,
    desktop_entry: Path,
    settings_icon: Path,
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
            root,
            engine_unit,
            voice_unit,
            install_id=install_id,
            desktop_entry=desktop_entry,
            settings_icon=settings_icon,
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


def _expected_desktop_paths(root: Path) -> tuple[Path, Path]:
    data_home = root.parent
    desktop_entry = data_home / "applications" / DESKTOP_ENTRY_NAME
    settings_icon = data_home / "icons" / "hicolor" / "scalable" / "apps" / ICON_NAME
    return desktop_entry, settings_icon


def _secure_desktop_directories(data_home: Path) -> None:
    for relative, kind in (
        (Path(), "XDG data"),
        (Path("applications"), "desktop entry"),
        (Path("icons"), "icon theme"),
        (Path("icons/hicolor"), "hicolor icon theme"),
        (Path("icons/hicolor/scalable"), "scalable icon theme"),
        (Path("icons/hicolor/scalable/apps"), "application icon"),
    ):
        secure_directory(data_home / relative, kind=kind)


def verify_manifest(
    root: Path,
    engine_unit: Path,
    voice_unit: Path,
    desktop_entry: Path | None = None,
    settings_icon: Path | None = None,
    *,
    staged: bool = False,
) -> tuple[str, int]:
    secure_directory(root, kind="installation root")
    entries = {path.name for path in root.iterdir()}
    if entries != _EXPECTED_ROOT_ENTRIES:
        raise ManifestError("installation root contains unowned top-level entries")
    document = _read_manifest(root / MANIFEST_NAME)
    version, install_id, expected = _validate_document(document)
    if version == 1:
        actual = _critical_digests(root, engine_unit, voice_unit, install_id=install_id)
    else:
        if not staged:
            fixed_desktop_entry, fixed_settings_icon = _expected_desktop_paths(root)
            if (
                desktop_entry != fixed_desktop_entry
                or settings_icon != fixed_settings_icon
            ):
                raise ManifestError(
                    "desktop assets must use the fixed installation paths"
                )
            _secure_desktop_directories(root.parent)
        actual = _critical_digests(
            root,
            engine_unit,
            voice_unit,
            install_id=install_id,
            desktop_entry=desktop_entry,
            settings_icon=settings_icon,
        )
    if actual != expected:
        raise ManifestError("installed files do not match the ownership manifest")
    return install_id, version


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


def managed_voice_process_count(root: Path, argv_root: Path | None = None) -> int:
    """Count exact managed daemon argv using a trusted installation tree.

    ``root`` remains available and trusted even after it has been quarantined.
    ``argv_root`` may name the now-absent publication path recorded by an
    already-running process.  Process executable identity prevents a spoofed
    argv string from being treated as one of our Python workers.
    """

    secure_directory(root, kind="installation root")
    if argv_root is None:
        argv_root = root
    _require_absolute(argv_root, "process argv root")
    trusted_python = root / "voice-venv" / "bin" / "python"
    expected_argv_path = argv_root / "voice-venv" / "bin" / "python"
    expected_argv_pythons = {os.fsencode(expected_argv_path)}
    # The published root may already have been renamed, but its parent still
    # exists.  Resolve only that parent so a canonical argv spelling through a
    # symlinked XDG ancestor remains recognizable without following the now
    # absent root or the venv's final interpreter symlink.
    try:
        canonical_argv_parent = argv_root.parent.resolve(strict=True)
    except OSError as error:
        raise ManifestError("process argv root parent is unavailable") from error
    expected_argv_pythons.add(
        os.fsencode(
            canonical_argv_parent / argv_root.name / "voice-venv" / "bin" / "python"
        )
    )
    try:
        trusted_python_metadata = trusted_python.stat()
    except OSError as error:
        raise ManifestError("managed Python interpreter is unavailable") from error
    trusted_python_mode = stat.S_IMODE(trusted_python_metadata.st_mode)
    if (
        not stat.S_ISREG(trusted_python_metadata.st_mode)
        or trusted_python_mode & 0o022
        or not trusted_python_mode & 0o111
    ):
        raise ManifestError("managed Python interpreter metadata is unsafe")
    trusted_python_identity = (
        trusted_python_metadata.st_dev,
        trusted_python_metadata.st_ino,
    )
    expected_shapes = {
        # Current launcher.  -I also implies -s, but its argv spelling matters
        # here because this check deliberately recognizes only our wrappers.
        (b"-I", b"-m", b"murmur_voice"),
        # Compatibility with installations produced before the launcher moved
        # from -s to isolated mode, so a live old daemon cannot escape an
        # upgrade or uninstall guard.
        (b"-s", b"-m", b"murmur_voice"),
    }
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
        if len(arguments) < 4 or arguments[1:4] not in expected_shapes:
            continue
        try:
            process_executable_metadata = (entry / "exe").stat()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        except OSError as error:
            raise ManifestError(
                "managed daemon executable could not be inspected"
            ) from error
        process_executable_identity = (
            process_executable_metadata.st_dev,
            process_executable_metadata.st_ino,
        )
        if process_executable_identity != trusted_python_identity:
            continue
        if arguments[0] not in expected_argv_pythons:
            try:
                argv_python_metadata = os.stat(arguments[0])
            except OSError:
                continue
            if (
                argv_python_metadata.st_dev,
                argv_python_metadata.st_ino,
            ) != trusted_python_identity:
                continue
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

    secure_fd = subparsers.add_parser("secure-dir-fd")
    secure_fd.add_argument("--path", required=True, type=Path)
    secure_fd.add_argument("--fd", required=True, type=int)

    absent = subparsers.add_parser("require-absent")
    absent.add_argument("--path", required=True, action="append", type=Path)

    move_path = subparsers.add_parser("move-no-clobber")
    move_path.add_argument("--source", required=True, type=Path)
    move_path.add_argument("--destination", required=True, type=Path)

    quarantine_path = subparsers.add_parser("quarantine-committed")
    quarantine_path.add_argument("--source", required=True, type=Path)
    quarantine_path.add_argument("--quarantine", required=True, type=Path)
    quarantine_path.add_argument("--identity", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--root", required=True, type=Path)
    create.add_argument("--engine-unit", required=True, type=Path)
    create.add_argument("--voice-unit", required=True, type=Path)
    create.add_argument("--desktop-entry", required=True, type=Path)
    create.add_argument("--settings-icon", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--install-id", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", required=True, type=Path)
    verify.add_argument("--engine-unit", required=True, type=Path)
    verify.add_argument("--voice-unit", required=True, type=Path)
    verify.add_argument("--desktop-entry", type=Path)
    verify.add_argument("--settings-icon", type=Path)
    verify.add_argument("--print-version", action="store_true")
    verify.add_argument("--staged", action="store_true")

    probe = subparsers.add_parser("socket-state")
    probe.add_argument("--runtime-root", required=True, type=Path)
    probe.add_argument("--path", required=True, type=Path)
    disjoint = subparsers.add_parser("require-disjoint")
    disjoint.add_argument("--left", required=True, type=Path)
    disjoint.add_argument("--right", required=True, type=Path)
    processes = subparsers.add_parser("voice-process-count")
    processes.add_argument("--root", required=True, type=Path)
    processes.add_argument("--argv-root", type=Path)
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
        elif options.command == "secure-dir-fd":
            print(secure_directory_descriptor(options.path, options.fd))
        elif options.command == "require-absent":
            require_absent(options.path)
        elif options.command == "move-no-clobber":
            print(move_no_clobber(options.source, options.destination))
        elif options.command == "quarantine-committed":
            quarantine_committed(options.source, options.quarantine, options.identity)
        elif options.command == "create":
            create_manifest(
                options.root,
                options.engine_unit,
                options.voice_unit,
                options.desktop_entry,
                options.settings_icon,
                options.output,
                options.install_id,
            )
        elif options.command == "verify":
            install_id, version = verify_manifest(
                options.root,
                options.engine_unit,
                options.voice_unit,
                options.desktop_entry,
                options.settings_icon,
                staged=options.staged,
            )
            if options.print_version:
                print(install_id, version)
            else:
                print(install_id)
        elif options.command == "socket-state":
            print(socket_state(options.runtime_root, options.path))
        elif options.command == "require-disjoint":
            require_disjoint(options.left, options.right)
        elif options.command == "voice-process-count":
            print(managed_voice_process_count(options.root, options.argv_root))
        else:
            print(secrets.token_hex(16))
    except ManifestError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
