# SPDX-License-Identifier: GPL-3.0-only
"""Crash-safe restoration of the user's temporarily replaced IBus engine."""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PREEDIT_ENGINE = "murmur-voice"

_IBUS_TIMEOUT_SECONDS = 3
_ENGINE_SWITCH_VERIFY_SECONDS = 1.0
_ENGINE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+:-]{0,255}$")
_STATE_FILE_NAME = "previous-ibus-engine"
_MAX_STATE_BYTES = 257

logger = logging.getLogger(__name__)


class RestoreError(RuntimeError):
    """A safe, non-content-bearing restore-state error."""


def valid_engine_name(value: Any, *, allow_preedit: bool = True) -> bool:
    """Accept only the bounded ASCII token syntax used by IBus engines."""

    return (
        isinstance(value, str)
        and _ENGINE_NAME_RE.fullmatch(value) is not None
        and (allow_preedit or value != PREEDIT_ENGINE)
    )


def parse_engine_name(output: Any, *, allow_preedit: bool = True) -> str | None:
    """Parse one unambiguous, strictly validated engine name."""

    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    if not isinstance(output, str):
        return None
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    engine = lines[0]
    return engine if valid_engine_name(engine, allow_preedit=allow_preedit) else None


def command_candidates(tool: str) -> list[list[str]]:
    """Return direct or Flatpak-host command prefixes without a shell."""

    commands: list[list[str]] = []
    if shutil.which(tool):
        commands.append([tool])
    if os.path.exists("/.flatpak-info") and shutil.which("flatpak-spawn"):
        commands.append(["flatpak-spawn", "--host", tool])
    return commands


def default_restore_state_path() -> Path:
    """Return a state path inside the private per-user runtime directory."""

    runtime_value = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_value:
        raise RestoreError("private runtime state is unavailable")
    runtime_root = Path(runtime_value)
    if not runtime_root.is_absolute():
        raise RestoreError("private runtime state is unavailable")
    try:
        metadata = runtime_root.lstat()
    except OSError as error:
        raise RestoreError("private runtime state is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RestoreError("private runtime state is unsafe")
    return runtime_root / "murmur-ime" / _STATE_FILE_NAME


class EngineRestoreState:
    """Own one private, atomically published original-engine record."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_restore_state_path()
        if not self.path.is_absolute() or self.path.name in ("", ".", ".."):
            raise RestoreError("restore-state path must be absolute")

    def record(self, engine: str) -> None:
        """Atomically record the actual engine before any temporary switch."""

        if not valid_engine_name(engine, allow_preedit=False):
            raise RestoreError("refusing to record an invalid IBus engine")
        directory_fd = self._open_private_parent(create=True)
        assert directory_fd is not None
        temporary_name = f".{_STATE_FILE_NAME}.{secrets.token_hex(12)}.tmp"
        temporary_fd: int | None = None
        try:
            self._refuse_existing_target(directory_fd)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            flags |= getattr(os, "O_NOFOLLOW", 0)
            temporary_fd = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=directory_fd,
            )
            os.fchmod(temporary_fd, 0o600)
            payload = f"{engine}\n".encode("ascii")
            view = memoryview(payload)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:
                    raise RestoreError("could not write private restore state")
                view = view[written:]
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None

            # link(2) publishes a fully written inode without overwriting a
            # state record that appeared concurrently.
            os.link(
                temporary_name,
                self.path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.fsync(directory_fd)
        except FileExistsError as error:
            raise RestoreError("private restore state already exists") from error
        except RestoreError:
            raise
        except OSError as error:
            raise RestoreError("could not record private restore state") from error
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory_fd)

    def load(self) -> str | None:
        """Load a secure state record, returning ``None`` when it is absent."""

        directory_fd = self._open_private_parent(create=False)
        if directory_fd is None:
            return None
        try:
            engine, _ = self._read_target(directory_fd)
            return engine
        finally:
            os.close(directory_fd)

    def clear(self, expected_engine: str) -> None:
        """Remove exactly the secure state record that was restored."""

        if not valid_engine_name(expected_engine, allow_preedit=False):
            raise RestoreError("refusing to clear invalid restore state")
        directory_fd = self._open_private_parent(create=False)
        if directory_fd is None:
            return
        try:
            engine, opened_metadata = self._read_target(directory_fd)
            if engine is None:
                return
            if engine != expected_engine:
                raise RestoreError("restore state changed unexpectedly")
            try:
                current_metadata = os.stat(
                    self.path.name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise RestoreError("restore state changed unexpectedly") from error
            if (
                opened_metadata is None
                or current_metadata.st_dev != opened_metadata.st_dev
                or current_metadata.st_ino != opened_metadata.st_ino
            ):
                raise RestoreError("restore state changed unexpectedly")
            os.unlink(self.path.name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except RestoreError:
            raise
        except OSError as error:
            raise RestoreError("could not clear private restore state") from error
        finally:
            os.close(directory_fd)

    def _open_private_parent(self, *, create: bool) -> int | None:
        parent = self.path.parent
        try:
            metadata = parent.lstat()
        except FileNotFoundError:
            if not create:
                return None
            self._create_private_parent(parent)
            try:
                metadata = parent.lstat()
            except OSError as error:
                raise RestoreError(
                    "private restore directory is unavailable"
                ) from error
        except OSError as error:
            raise RestoreError("private restore directory is unavailable") from error
        if not self._private_directory(metadata):
            raise RestoreError("private restore directory is unsafe")
        flags = os.O_RDONLY | os.O_CLOEXEC
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(parent, flags)
            opened_metadata = os.fstat(directory_fd)
        except OSError as error:
            raise RestoreError("private restore directory is unavailable") from error
        if (
            not self._private_directory(opened_metadata)
            or opened_metadata.st_dev != metadata.st_dev
            or opened_metadata.st_ino != metadata.st_ino
        ):
            os.close(directory_fd)
            raise RestoreError("private restore directory changed unexpectedly")
        return directory_fd

    @staticmethod
    def _create_private_parent(parent: Path) -> None:
        try:
            ancestor = parent.parent.lstat()
        except OSError as error:
            raise RestoreError("private restore directory is unavailable") from error
        if (
            not stat.S_ISDIR(ancestor.st_mode)
            or stat.S_ISLNK(ancestor.st_mode)
            or ancestor.st_uid != os.getuid()
            or stat.S_IMODE(ancestor.st_mode) & 0o077
        ):
            raise RestoreError("private restore directory parent is unsafe")
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise RestoreError("private restore directory is unavailable") from error

    @staticmethod
    def _private_directory(metadata: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and not stat.S_IMODE(metadata.st_mode) & 0o077
        )

    def _refuse_existing_target(self, directory_fd: int) -> None:
        try:
            os.stat(
                self.path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        except OSError as error:
            raise RestoreError("private restore state is unavailable") from error
        raise RestoreError("private restore state already exists")

    def _read_target(
        self, directory_fd: int
    ) -> tuple[str | None, os.stat_result | None]:
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_fd = os.open(self.path.name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return None, None
        except OSError as error:
            raise RestoreError("private restore state is unsafe") from error
        try:
            metadata = os.fstat(file_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > _MAX_STATE_BYTES
            ):
                raise RestoreError("private restore state is unsafe")
            payload = os.read(file_fd, _MAX_STATE_BYTES + 1)
        except RestoreError:
            raise
        except OSError as error:
            raise RestoreError("private restore state is unavailable") from error
        finally:
            os.close(file_fd)
        if len(payload) != metadata.st_size or not payload.endswith(b"\n"):
            raise RestoreError("private restore state is invalid")
        try:
            engine = payload[:-1].decode("ascii")
        except UnicodeDecodeError as error:
            raise RestoreError("private restore state is invalid") from error
        if not valid_engine_name(engine, allow_preedit=False):
            raise RestoreError("private restore state is invalid")
        return engine, metadata


class IBusEngineCommands:
    """Strict, shell-free IBus engine inspection and switching."""

    def __init__(
        self,
        *,
        command_provider: Callable[[str], list[list[str]]] | None = None,
        command_runner: Callable[..., Any] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        retry_interval: float = 0.05,
    ) -> None:
        self._command_provider = command_provider or command_candidates
        self._command_runner = command_runner or subprocess.run
        self._monotonic = monotonic or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._retry_interval = max(0.001, float(retry_interval))

    def current_engine(self) -> str | None:
        for prefix in self._ibus_command_candidates():
            try:
                result = self._command_runner(
                    prefix + ["engine"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=_IBUS_TIMEOUT_SECONDS,
                )
            except Exception:
                continue
            engine = parse_engine_name(getattr(result, "stdout", ""))
            if engine is not None:
                return engine
        return None

    def set_engine(self, engine: str) -> bool:
        if not valid_engine_name(engine):
            return False
        for prefix in self._ibus_command_candidates():
            try:
                self._command_runner(
                    prefix + ["engine", engine],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_IBUS_TIMEOUT_SECONDS,
                )
            except Exception:
                continue
            deadline = self._monotonic() + _ENGINE_SWITCH_VERIFY_SECONDS
            while True:
                if self.current_engine() == engine:
                    return True
                if self._monotonic() >= deadline:
                    break
                self._sleeper(self._retry_interval)
        return False

    def _ibus_command_candidates(self) -> list[list[str]]:
        commands = [list(item) for item in self._command_provider("ibus")]
        host_commands = [
            command
            for command in commands
            if command[:2] == ["flatpak-spawn", "--host"]
        ]
        return host_commands or commands


def restore_saved_engine(
    state: EngineRestoreState | None = None,
    *,
    current_engine: Callable[[], str | None] | None = None,
    set_engine: Callable[[str], bool] | None = None,
) -> bool:
    """Restore one crash record without ever logging its engine name."""

    try:
        restore_state = state or EngineRestoreState()
        saved_engine = restore_state.load()
    except RestoreError:
        logger.warning("Could not read the private IBus restore state")
        return False
    if saved_engine is None:
        return True

    commands = None
    if current_engine is None or set_engine is None:
        commands = IBusEngineCommands()
    current = current_engine if current_engine is not None else commands.current_engine
    switch = set_engine if set_engine is not None else commands.set_engine
    active_engine = current()
    if active_engine is None:
        logger.warning("Could not determine the current IBus engine for restoration")
        return False

    # If the user has already selected another real engine, preserve that newer
    # choice and retire the stale record. Only murmur-voice is ever replaced.
    restored = active_engine == saved_engine
    if active_engine not in (PREEDIT_ENGINE, saved_engine):
        restored = True
    elif not restored:
        restored = switch(saved_engine)
    if not restored:
        logger.warning("Could not restore the saved IBus engine")
        return False
    try:
        restore_state.clear(saved_engine)
    except RestoreError:
        logger.warning("Could not clear the private IBus restore state")
        return False
    return True
