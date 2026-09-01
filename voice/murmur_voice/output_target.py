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

_WL_COPY = "/usr/bin/wl-copy"
_XCLIP = "/usr/bin/xclip"


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


class ClipboardWriter:
    """Write one bounded UTF-8 terminal result through a reviewed helper.

    ``preflight`` performs no process or clipboard mutation.  It resolves only
    one root-owned, non-writable regular executable under ``/usr/bin`` and
    freezes its fixed argument vector for the subsequent ``write``.  The
    injectable runner/metadata/environment seams exist for offline tests; the
    candidate paths themselves are intentionally not injectable.
    """

    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        metadata_reader: MetadataReader = os.stat,
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
        self._environment = dict(os.environ if environment is None else environment)
        self._timeout_seconds = min(timeout, MAX_CLIPBOARD_TIMEOUT_SECONDS)
        self._command: tuple[str, ...] | None = None
        self._backend: str | None = None

    @property
    def backend(self) -> str | None:
        """Return only the content-free backend name selected by preflight."""

        return self._backend

    def preflight(self) -> None:
        """Freeze a trusted local helper without executing or writing to it."""

        self._command = None
        self._backend = None
        candidates: list[tuple[str, str, Sequence[str]]] = []
        if self._environment.get("WAYLAND_DISPLAY"):
            candidates.append(
                (
                    "wl-copy",
                    _WL_COPY,
                    (_WL_COPY, "--type", "text/plain;charset=utf-8"),
                )
            )
        if self._environment.get("DISPLAY"):
            candidates.append(
                (
                    "xclip",
                    _XCLIP,
                    (_XCLIP, "-selection", "clipboard", "-in"),
                )
            )
        for backend, path, command in candidates:
            try:
                metadata = self._metadata_reader(path)
                mode = metadata.st_mode
                trusted = (
                    path in {_WL_COPY, _XCLIP}
                    and Path(path).is_absolute()
                    and stat.S_ISREG(mode)
                    and metadata.st_uid == 0
                    and not mode & (stat.S_IWGRP | stat.S_IWOTH)
                    and bool(mode & stat.S_IXUSR)
                )
            except Exception:
                trusted = False
            if trusted:
                self._command = tuple(command)
                self._backend = backend
                return
        raise ClipboardError("clipboard helper is unavailable")

    def write(self, text: str) -> None:
        """Replace the local clipboard once, with transcript bytes on stdin only."""

        if not isinstance(text, str):
            raise TypeError("clipboard text must be a string")
        if not text or "\x00" in text:
            raise ClipboardError("clipboard text is empty or invalid")
        if len(text) > MAX_CLIPBOARD_TEXT_CODEPOINTS:
            raise ClipboardError("clipboard text is too large")
        payload = text.encode("utf-8")
        if len(payload) > MAX_CLIPBOARD_TEXT_UTF8_BYTES:
            raise ClipboardError("clipboard text is too large")
        command = self._command
        if command is None:
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
            )
            if type(getattr(result, "returncode", None)) is not int:
                raise ClipboardError("clipboard write failed")
            if result.returncode != 0:
                raise ClipboardError("clipboard write failed")
        except ClipboardError:
            raise
        except Exception:
            raise ClipboardError("clipboard write failed") from None
