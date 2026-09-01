# SPDX-License-Identifier: GPL-3.0-only
"""Private terminal-output routing and a bounded local clipboard sink.

``caret`` preserves the native IBus path.  ``clipboard`` is an explicit local
compatibility target for applications (notably remote-desktop canvases) which
cannot accept an IBus commit.  The clipboard writer never receives transcript
text through an argument, environment variable, diagnostic, or shell string:
the authoritative terminal delivery is sent once on the selected helper's
standard input.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import (
    ConfigError,
    _load_private_bytes,
    _reject_duplicate_json_fields,
    _write_private_json,
)

OUTPUT_TARGET_CONFIG_VERSION = 1
OUTPUT_TARGETS = ("caret", "clipboard")
DEFAULT_OUTPUT_TARGET = "caret"
MAX_OUTPUT_TARGET_CONFIG_BYTES = 8 * 1024

# A ten-minute utterance should remain far below these limits.  Keeping both a
# character and encoded-byte bound avoids unbounded allocations at the process
# boundary while allowing multilingual UTF-8 text.
MAX_CLIPBOARD_TEXT_CODEPOINTS = 65_536
MAX_CLIPBOARD_TEXT_UTF8_BYTES = 256 * 1024
DEFAULT_CLIPBOARD_TIMEOUT_SECONDS = 2.0
MAX_CLIPBOARD_TIMEOUT_SECONDS = 5.0
DISPLAY_SOCKET_PROBE_TIMEOUT_SECONDS = 0.25

_WL_COPY = "/usr/bin/wl-copy"
_XCLIP = "/usr/bin/xclip"
_X11_SOCKET_DIRECTORY = "/tmp/.X11-unix"
_WAYLAND_DISPLAY_RE = re.compile(r"^wayland-(?:0|[1-9][0-9]{0,4})$")
_X11_DISPLAY_RE = re.compile(
    r"^:(?P<display>0|[1-9][0-9]{0,4})(?:\.(?:0|[1-9][0-9]{0,2}))?$"
)


class ClipboardError(RuntimeError):
    """A content-free failure at the local clipboard process boundary."""


@dataclass(frozen=True, slots=True)
class OutputTargetConfig:
    """One user-selected target, frozen by ``VoiceSession.start``."""

    target: str = DEFAULT_OUTPUT_TARGET

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or self.target not in OUTPUT_TARGETS:
            raise ConfigError("output target is unsupported")


def default_output_target_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "murmur-ime" / "output-target.json"


def load_output_target_config(
    path: str | os.PathLike[str] | None = None,
) -> OutputTargetConfig:
    """Load a private target policy; an absent file keeps native caret output."""

    config_path = (
        Path(path) if path is not None else default_output_target_config_path()
    )
    raw = _load_private_bytes(
        config_path,
        kind="output target configuration",
        limit=MAX_OUTPUT_TARGET_CONFIG_BYTES,
        missing_ok=True,
    )
    if raw is None:
        return OutputTargetConfig()
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ConfigError(
            "output target configuration is not valid UTF-8 JSON"
        ) from error
    if not isinstance(document, dict) or set(document) != {"version", "target"}:
        raise ConfigError("output target configuration has unsupported fields")
    if (
        type(document.get("version")) is not int
        or document["version"] != OUTPUT_TARGET_CONFIG_VERSION
    ):
        raise ConfigError("output target configuration uses an unsupported version")
    return OutputTargetConfig(target=document.get("target"))


def save_output_target_config(
    target: str,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Atomically persist one private target without touching the daemon."""

    config = OutputTargetConfig(target=target)
    config_path = (
        Path(path) if path is not None else default_output_target_config_path()
    )
    return _write_private_json(
        config_path,
        {"version": OUTPUT_TARGET_CONFIG_VERSION, "target": config.target},
        kind="output target configuration",
        temporary_prefix=".output-target.json.",
    )


Runner = Callable[..., Any]
MetadataReader = Callable[[str], Any]
SocketProbe = Callable[[str, float], None]
UidReader = Callable[[], int]
UidMapReader = Callable[[], str]
OverflowUidReader = Callable[[], int]


def _read_uid_map() -> str:
    value = Path("/proc/self/uid_map").read_text(encoding="ascii")
    if len(value) > 4096:
        raise ValueError("uid map is too large")
    return value


def _read_overflow_uid() -> int:
    value = Path("/proc/sys/kernel/overflowuid").read_text(encoding="ascii")
    if len(value) > 32:
        raise ValueError("overflow uid is invalid")
    return int(value.strip())


def _probe_unix_socket(path: str, timeout_seconds: float) -> None:
    """Open and immediately close one bounded local Unix connection."""

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout_seconds)
        client.connect(path)
    finally:
        client.close()


class ClipboardWriter:
    """Write one bounded UTF-8 terminal result through a reviewed helper.

    ``preflight`` performs no helper process or clipboard mutation. It resolves
    one root-owned, non-writable regular executable under ``/usr/bin``, proves
    the matching local display Unix socket is live with a bounded connect/close,
    and freezes the fixed argument vector for the subsequent ``write``. The
    injectable process/filesystem/socket seams exist for offline tests; helper
    and display socket locations themselves are intentionally not injectable.
    """

    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        metadata_reader: MetadataReader = os.stat,
        socket_metadata_reader: MetadataReader = os.lstat,
        socket_probe: SocketProbe = _probe_unix_socket,
        uid_reader: UidReader = os.getuid,
        uid_map_reader: UidMapReader = _read_uid_map,
        overflow_uid_reader: OverflowUidReader = _read_overflow_uid,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = DEFAULT_CLIPBOARD_TIMEOUT_SECONDS,
    ) -> None:
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("clipboard timeout is invalid") from error
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("clipboard timeout is invalid")
        self._runner = runner
        self._metadata_reader = metadata_reader
        self._socket_metadata_reader = socket_metadata_reader
        self._socket_probe = socket_probe
        self._uid_reader = uid_reader
        self._uid_map_reader = uid_map_reader
        self._overflow_uid_reader = overflow_uid_reader
        self._environment = dict(os.environ if environment is None else environment)
        self._timeout_seconds = min(timeout, MAX_CLIPBOARD_TIMEOUT_SECONDS)
        self._command: tuple[str, ...] | None = None
        self._command_environment: dict[str, str] | None = None
        self._backend: str | None = None

    @property
    def backend(self) -> str | None:
        """Return only the content-free backend name selected by preflight."""

        return self._backend

    def preflight(self) -> None:
        """Freeze a trusted local helper without executing or writing to it."""

        self._command = None
        self._command_environment = None
        self._backend = None
        try:
            uid = self._uid_reader()
            if type(uid) is not int or uid < 0:
                raise ValueError
        except Exception:
            raise ClipboardError("clipboard helper is unavailable") from None
        trusted_system_uids = self._trusted_system_uids()

        candidates: list[tuple[str, str, Sequence[str], str, frozenset[int]]] = []
        wayland_socket = self._wayland_socket_path(uid)
        if wayland_socket is not None:
            candidates.append(
                (
                    "wl-copy",
                    _WL_COPY,
                    (_WL_COPY, "--type", "text/plain;charset=utf-8"),
                    wayland_socket,
                    frozenset({uid}),
                )
            )
        x11_socket = self._x11_socket_path(trusted_system_uids)
        if x11_socket is not None:
            candidates.append(
                (
                    "xclip",
                    _XCLIP,
                    (_XCLIP, "-selection", "clipboard", "-in"),
                    x11_socket,
                    frozenset({uid}) | trusted_system_uids,
                )
            )
        for backend, path, command, display_socket, socket_owners in candidates:
            if self._helper_is_trusted(
                path,
                trusted_system_uids,
            ) and self._display_socket_is_live(display_socket, socket_owners):
                self._command = tuple(command)
                self._command_environment = self._helper_environment(backend)
                self._backend = backend
                return
        raise ClipboardError("clipboard helper is unavailable")

    def _trusted_system_uids(self) -> frozenset[int]:
        """Map host root into this process's user-namespace UID view."""

        trusted = {0}
        try:
            mappings = []
            for line in self._uid_map_reader().splitlines():
                fields = line.split()
                if len(fields) != 3 or any(not field.isdecimal() for field in fields):
                    raise ValueError
                inside, outside, count = (int(field) for field in fields)
                if count < 1:
                    raise ValueError
                mappings.append((inside, outside, count))
            if not mappings or len(mappings) > 64:
                raise ValueError
            for inside, outside, count in mappings:
                if outside == 0 or outside < 0 < outside + count:
                    trusted.add(inside - outside)
                    break
            else:
                overflow_uid = self._overflow_uid_reader()
                if type(overflow_uid) is not int or not 1 <= overflow_uid < 2**32:
                    raise ValueError
                trusted.add(overflow_uid)
        except Exception:
            # Fail closed to the ordinary host-root view when proc metadata is
            # unavailable or malformed. The start then reports unavailable.
            return frozenset({0})
        return frozenset(trusted)

    def _helper_is_trusted(
        self,
        path: str,
        trusted_system_uids: frozenset[int],
    ) -> bool:
        try:
            metadata = self._metadata_reader(path)
            mode = metadata.st_mode
            return (
                path in {_WL_COPY, _XCLIP}
                and Path(path).is_absolute()
                and stat.S_ISREG(mode)
                and metadata.st_uid in trusted_system_uids
                and not mode & (stat.S_IWGRP | stat.S_IWOTH)
                # The daemon is never root. Requiring other-execute proves the
                # selected root-owned helper can actually be invoked by this
                # ordinary desktop user before capture or provider startup.
                and bool(mode & stat.S_IXOTH)
            )
        except Exception:
            return False

    def _helper_environment(self, backend: str) -> dict[str, str]:
        """Freeze only the display credentials required by the chosen helper."""

        keys = (
            ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
            if backend == "wl-copy"
            else ("DISPLAY", "XAUTHORITY", "HOME")
        )
        environment: dict[str, str] = {}
        for key in keys:
            value = self._environment.get(key)
            if (
                isinstance(value, str)
                and value
                and len(value) <= 4096
                and not any(character in value for character in "\x00\r\n")
            ):
                environment[key] = value
        return environment

    def _wayland_socket_path(self, uid: int) -> str | None:
        display = self._environment.get("WAYLAND_DISPLAY")
        runtime = self._environment.get("XDG_RUNTIME_DIR")
        if (
            type(display) is not str
            or _WAYLAND_DISPLAY_RE.fullmatch(display) is None
            or type(runtime) is not str
            or not runtime
            or len(runtime) > 4096
            or any(character in runtime for character in "\x00\r\n")
        ):
            return None
        normalized = os.path.normpath(runtime)
        runtime_path = Path(runtime)
        if not runtime_path.is_absolute() or normalized != runtime:
            return None
        try:
            metadata = self._socket_metadata_reader(runtime)
            mode = metadata.st_mode
            if (
                not stat.S_ISDIR(mode)
                or metadata.st_uid != uid
                or stat.S_IMODE(mode) & 0o077
            ):
                return None
        except Exception:
            return None
        return os.fspath(runtime_path / display)

    def _x11_socket_path(
        self,
        trusted_system_uids: frozenset[int],
    ) -> str | None:
        display = self._environment.get("DISPLAY")
        if type(display) is not str:
            return None
        match = _X11_DISPLAY_RE.fullmatch(display)
        if match is None:
            return None
        try:
            metadata = self._socket_metadata_reader(_X11_SOCKET_DIRECTORY)
            mode = metadata.st_mode
            if (
                not stat.S_ISDIR(mode)
                or metadata.st_uid not in trusted_system_uids
                or not mode & stat.S_ISVTX
            ):
                return None
        except Exception:
            return None
        display_number = int(match.group("display"))
        return f"{_X11_SOCKET_DIRECTORY}/X{display_number}"

    def _display_socket_is_live(
        self,
        path: str,
        allowed_owners: frozenset[int],
    ) -> bool:
        try:
            metadata = self._socket_metadata_reader(path)
            if (
                not stat.S_ISSOCK(metadata.st_mode)
                or metadata.st_uid not in allowed_owners
            ):
                return False
            self._socket_probe(path, DISPLAY_SOCKET_PROBE_TIMEOUT_SECONDS)
            return True
        except Exception:
            return False

    def write(self, text: str) -> None:
        """Replace the local clipboard once, with transcript bytes on stdin only."""

        if not isinstance(text, str):
            raise TypeError("clipboard text must be a string")
        if not text or "\x00" in text:
            raise ClipboardError("clipboard text is empty or invalid")
        if len(text) > MAX_CLIPBOARD_TEXT_CODEPOINTS:
            raise ClipboardError("clipboard text is too large")
        try:
            payload = text.encode("utf-8")
        except UnicodeError:
            raise ClipboardError("clipboard text is invalid") from None
        if len(payload) > MAX_CLIPBOARD_TEXT_UTF8_BYTES:
            raise ClipboardError("clipboard text is too large")
        command = self._command
        command_environment = self._command_environment
        if command is None or command_environment is None:
            raise ClipboardError("clipboard preflight is required")
        try:
            result = self._runner(
                list(command),
                input=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self._timeout_seconds,
                check=False,
                close_fds=True,
                env=dict(command_environment),
            )
            if type(getattr(result, "returncode", None)) is not int:
                raise ClipboardError("clipboard write failed")
            if result.returncode != 0:
                raise ClipboardError("clipboard write failed")
        except ClipboardError:
            raise
        except Exception:
            raise ClipboardError("clipboard write failed") from None
