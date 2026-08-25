from __future__ import annotations

import json
import socket
import stat
import threading
import time

import pytest

from murmur_voice.audio import MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS
from murmur_voice.control import (
    CONTROL_RESPONSE_TIMEOUT_SECONDS,
    MAX_COMMAND_BYTES,
    ControlError,
    ControlServer,
    request_command,
    runtime_socket_path,
)
from murmur_voice.session import (
    PREEDIT_ACQUIRE_TIMEOUT_UPPER_BOUND_SECONDS,
    VOICE_START_CLEANUP_TIMEOUT_SECONDS,
    VOICE_START_TIMEOUT_SECONDS,
)
from murmur_voice.state import CommandReply, VoiceState


class FakeSession:
    def __init__(self):
        self.state = VoiceState.IDLE
        self.calls = []
        self.closed = False

    def _reply(self, command, state=None):
        self.calls.append(command)
        if state is not None:
            self.state = state
        return CommandReply(True, command, self.state)

    def start(self):
        return self._reply("started", VoiceState.STARTING)

    def stop(self):
        return self._reply("stopping", VoiceState.STOPPING)

    def toggle(self):
        if self.state is VoiceState.IDLE:
            return self.start()
        return self.stop()

    def cancel(self):
        return self._reply("cancelled", VoiceState.IDLE)

    def status(self):
        return self._reply("status")

    def close(self):
        self.closed = True
        self.state = VoiceState.IDLE


@pytest.fixture
def runtime_dir(tmp_path, monkeypatch):
    path = tmp_path / "runtime"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(path))
    return path


def _start_server(runtime_dir):
    session = FakeSession()
    server = ControlServer(session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not server.path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.path.exists()
    return session, server, thread


def test_socket_stays_inside_private_runtime_directory(runtime_dir):
    path = runtime_socket_path()
    assert path.is_relative_to(runtime_dir)

    with pytest.raises(ControlError, match="inside"):
        runtime_socket_path(runtime_dir.parent / "outside.sock")


def test_cli_commands_and_toggle_over_private_socket(runtime_dir):
    session, server, thread = _start_server(runtime_dir)

    assert stat.S_IMODE(server.path.stat().st_mode) == 0o600
    assert request_command("toggle")["state"] == "starting"
    assert request_command("toggle")["state"] == "stopping"
    assert request_command("cancel")["state"] == "idle"
    assert request_command("shutdown")["code"] == "shutting-down"

    thread.join(2)
    assert not thread.is_alive()
    assert session.closed
    assert not server.path.exists()


def test_oversize_and_unknown_commands_are_rejected(runtime_dir):
    session, server, thread = _start_server(runtime_dir)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(server.path))
    client.sendall(b"x" * (MAX_COMMAND_BYTES + 1) + b"\n")
    response = b""
    while b"\n" not in response:
        response += client.recv(1024)
    client.close()
    assert json.loads(response)["code"] == "request-too-large"

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(server.path))
    client.sendall(b"delete-everything\n")
    response = b""
    while b"\n" not in response:
        response += client.recv(1024)
    client.close()
    assert json.loads(response)["code"] == "invalid-request"

    request_command("shutdown")
    thread.join(2)


def test_insecure_runtime_directory_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "runtime"
    path.mkdir(mode=0o755)
    path.chmod(0o755)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(path))

    with pytest.raises(ControlError, match="private"):
        runtime_socket_path()


def test_client_disconnect_after_command_does_not_break_server(runtime_dir):
    session = FakeSession()
    server = ControlServer(session)
    server_side, client_side = socket.socketpair()
    client_side.sendall(b"status\n")
    client_side.close()

    with server_side:
        server._serve_connection(server_side)

    assert session.calls == ["status"]


def test_client_response_timeout_exceeds_hard_microphone_preflight_bound(
    runtime_dir, monkeypatch
):
    class FakeClientSocket:
        def __init__(self):
            self.timeout = None
            self.closed = False

        def settimeout(self, value):
            self.timeout = value

        def connect(self, path):
            assert path == str(runtime_dir / "murmur-ime" / "voice.sock")

        def sendall(self, data):
            assert data == b"status\n"

        def recv(self, maximum):
            del maximum
            return b'{"ok":true,"code":"status","state":"idle"}\n'

        def close(self):
            self.closed = True

    client = FakeClientSocket()
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: client)

    assert request_command("status")["state"] == "idle"
    assert client.timeout == CONTROL_RESPONSE_TIMEOUT_SECONDS
    assert CONTROL_RESPONSE_TIMEOUT_SECONDS > (
        PREEDIT_ACQUIRE_TIMEOUT_UPPER_BOUND_SECONDS
        + MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS
        + VOICE_START_CLEANUP_TIMEOUT_SECONDS
    )
    assert VOICE_START_TIMEOUT_SECONDS > PREEDIT_ACQUIRE_TIMEOUT_UPPER_BOUND_SECONDS
    assert client.closed
