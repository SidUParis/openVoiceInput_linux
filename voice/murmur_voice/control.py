"""Permission-scoped local command channel for the foreground daemon."""

from __future__ import annotations

import json
import os
import queue
import socket
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .state import CommandReply

if TYPE_CHECKING:
    from .interaction import InteractionController
    from .session import VoiceSession

MAX_COMMAND_BYTES = 256
MAX_RESPONSE_BYTES = 4096
# Production Preedit acquisition is bounded to 29 s, failed microphone recovery
# (including conservative rollback) to 10 s, and Preedit cleanup to 8 s. Keep the
# client beyond their 47 s sum so it receives the real failure reply instead of
# reporting a false daemon outage. VoiceSession separately gates provider/capture
# opening with a 35 s total-start deadline.
CONTROL_RESPONSE_TIMEOUT_SECONDS = 50.0
KNOWN_COMMANDS = frozenset(
    {
        "start",
        "stop",
        "toggle",
        "press",
        "release",
        "cancel",
        "status",
        "shutdown",
    }
)


class ControlError(RuntimeError):
    """A safe control-channel error."""


def runtime_socket_path(
    requested: str | os.PathLike[str] | None = None,
) -> Path:
    runtime_value = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_value:
        raise ControlError("XDG_RUNTIME_DIR is required")
    runtime_root = Path(runtime_value).resolve()
    try:
        metadata = runtime_root.stat()
    except OSError as error:
        raise ControlError("XDG_RUNTIME_DIR is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ControlError("XDG_RUNTIME_DIR must be private and user-owned")

    path = (
        Path(requested)
        if requested is not None
        else runtime_root / "murmur-ime" / "voice.sock"
    )
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(runtime_root)
    except ValueError as error:
        raise ControlError("control socket must be inside XDG_RUNTIME_DIR") from error
    return resolved


class ControlServer:
    """Serve bounded commands on a mode-0600 AF_UNIX socket."""

    def __init__(
        self,
        session: VoiceSession,
        socket_path: str | os.PathLike[str] | None = None,
        *,
        interaction: InteractionController | None = None,
    ) -> None:
        self._session = session
        self._interaction = interaction
        self.path = runtime_socket_path(socket_path)
        self._socket: socket.socket | None = None
        self._running = False

    def serve_forever(
        self, signal_commands: queue.SimpleQueue[str] | None = None
    ) -> None:
        self._prepare_parent()
        self._remove_stale_socket()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket = server
        try:
            server.bind(str(self.path))
            os.chmod(self.path, 0o600)
            server.listen(4)
            server.settimeout(0.2)
            self._running = True
            while self._running:
                self._drain_signal_commands(signal_commands)
                if not self._running:
                    break
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    self._serve_connection(connection)
        finally:
            self._running = False
            if self._interaction is not None:
                self._interaction.close()
            self._session.close()
            server.close()
            self._socket = None
            self._unlink_own_socket()

    def handle_command(
        self,
        command: str,
        *,
        event_time: float | None = None,
    ) -> CommandReply:
        if command == "start":
            self._reset_interaction()
            return self._session.start()
        if command == "stop":
            self._reset_interaction()
            return self._session.stop()
        if command == "toggle":
            self._reset_interaction()
            return self._session.toggle()
        if command == "press":
            if self._interaction is None:
                return CommandReply(
                    False, "interaction-unavailable", self._session.state
                )
            return self._interaction.press(event_time=event_time)
        if command == "release":
            if self._interaction is None:
                return CommandReply(
                    False, "interaction-unavailable", self._session.state
                )
            return self._interaction.release(event_time=event_time)
        if command == "cancel":
            if self._interaction is not None:
                return self._interaction.cancel()
            return self._session.cancel()
        if command == "status":
            return self._session.status()
        if command == "shutdown":
            state = self._session.state
            if self._interaction is not None:
                self._interaction.close()
            self._running = False
            return CommandReply(True, "shutting-down", state)
        return CommandReply(False, "unknown-command", self._session.state)

    def _reset_interaction(self) -> None:
        if self._interaction is not None:
            self._interaction.reset_for_explicit_command()

    def _serve_connection(self, connection: socket.socket) -> None:
        connection.settimeout(2.0)
        try:
            command, event_time = _receive_command(connection)
            reply = self.handle_command(command, event_time=event_time)
        except ControlError as error:
            code = (
                "request-too-large" if "too large" in str(error) else "invalid-request"
            )
            reply = CommandReply(False, code, self._session.state)
        response = (json.dumps(reply.as_dict(), separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        try:
            connection.sendall(response)
        except OSError:
            # A shortcut client may disappear after issuing its command. Its
            # disconnect must not terminate the foreground daemon.
            pass

    def _drain_signal_commands(
        self, signal_commands: queue.SimpleQueue[str] | None
    ) -> None:
        if signal_commands is None:
            return
        while True:
            try:
                command = signal_commands.get_nowait()
            except queue.Empty:
                return
            self.handle_command(command)
            if not self._running:
                return

    def _prepare_parent(self) -> None:
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = parent.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or parent.is_symlink()
        ):
            raise ControlError(
                "control socket directory must be user-owned and private"
            )
        parent.chmod(0o700)

    def _remove_stale_socket(self) -> None:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ControlError("refusing to replace an unsafe control path")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(self.path))
        except (ConnectionRefusedError, FileNotFoundError):
            self.path.unlink(missing_ok=True)
        else:
            raise ControlError("voice daemon is already running")
        finally:
            probe.close()

    def _unlink_own_socket(self) -> None:
        try:
            metadata = self.path.lstat()
            if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.getuid():
                self.path.unlink()
        except FileNotFoundError:
            pass


def request_command(
    command: str,
    socket_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    if command not in KNOWN_COMMANDS:
        raise ControlError("unknown control command")
    event_nanoseconds = time.monotonic_ns() if command in {"press", "release"} else None
    path = runtime_socket_path(socket_path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(CONTROL_RESPONSE_TIMEOUT_SECONDS)
        client.connect(str(path))
        wire_command = (
            f"{command} {event_nanoseconds}"
            if event_nanoseconds is not None
            else command
        )
        client.sendall((wire_command + "\n").encode("ascii"))
        response = _receive_bounded(client, MAX_RESPONSE_BYTES)
    except OSError as error:
        raise ControlError("voice daemon is unavailable") from error
    finally:
        client.close()
    try:
        document = json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlError("voice daemon returned an invalid response") from error
    if not isinstance(document, dict):
        raise ControlError("voice daemon returned an invalid response")
    return document


def _receive_command(connection: socket.socket) -> tuple[str, float | None]:
    raw = _receive_bounded(connection, MAX_COMMAND_BYTES)
    try:
        request = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ControlError("invalid command encoding") from error
    fields = request.split(" ")
    command = fields[0] if fields else ""
    if not command or command not in KNOWN_COMMANDS:
        raise ControlError("unknown command")
    if len(fields) == 1:
        return command, None
    if (
        len(fields) != 2
        or command not in {"press", "release"}
        or not fields[1].isdigit()
        or len(fields[1]) > 20
    ):
        raise ControlError("invalid command")
    event_nanoseconds = int(fields[1])
    if event_nanoseconds <= 0:
        raise ControlError("invalid command")
    now_nanoseconds = time.monotonic_ns()
    maximum_queue_age = int((CONTROL_RESPONSE_TIMEOUT_SECONDS * 2.0) * 1_000_000_000)
    if (
        event_nanoseconds > now_nanoseconds + 1_000_000_000
        or now_nanoseconds - event_nanoseconds > maximum_queue_age
    ):
        raise ControlError("invalid command")
    return command, event_nanoseconds / 1_000_000_000.0


def _receive_bounded(connection: socket.socket, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(min(256, maximum + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > maximum:
            raise ControlError("request is too large")
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0]
