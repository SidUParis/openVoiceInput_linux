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
    REVIEW_SUBMIT_TIMEOUT_SECONDS,
    ControlError,
    ControlServer,
    LastReview,
    ReviewSubmitReply,
    request_command,
    request_last_review,
    review_socket_path,
    runtime_socket_path,
    submit_last_review,
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
        self.review = None
        self.review_submissions = []
        self.review_submit_reply = ReviewSubmitReply(
            True,
            "review-submitted",
            "explicit-feedback-activated",
            "feedback-disabled",
        )

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

    def review_last(self):
        return self.review

    def submit_last_review(self, utterance_id, spoken_verbatim):
        self.review_submissions.append((utterance_id, spoken_verbatim))
        return self.review_submit_reply


class FakeInteraction:
    def __init__(self, session):
        self.session = session
        self.calls = []

    def press(self, *, event_time=None):
        self.calls.append(("press", event_time))
        self.session.state = VoiceState.STARTING
        return CommandReply(True, "started", self.session.state)

    def release(self, *, event_time=None):
        self.calls.append(("release", event_time))
        self.session.state = VoiceState.STOPPING
        return CommandReply(True, "stopping", self.session.state)

    def cancel(self):
        self.calls.append("cancel")
        self.session.state = VoiceState.IDLE
        return CommandReply(True, "cancelled", self.session.state)

    def reset_for_explicit_command(self):
        self.calls.append("reset")

    def close(self):
        self.calls.append("close")


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
    while (
        not server.path.exists() or not server.review_path.exists()
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.path.exists()
    assert server.review_path.exists()
    return session, server, thread


def test_socket_stays_inside_private_runtime_directory(runtime_dir):
    path = runtime_socket_path()
    assert path.is_relative_to(runtime_dir)

    with pytest.raises(ControlError, match="inside"):
        runtime_socket_path(runtime_dir.parent / "outside.sock")

    private_path = review_socket_path()
    assert private_path == runtime_dir / "murmur-ime-private" / "review.sock"
    assert private_path.parent != path.parent
    with pytest.raises(ControlError, match="inside"):
        review_socket_path(runtime_dir.parent / "outside-review.sock")
    with pytest.raises(ControlError, match="host-only"):
        review_socket_path(runtime_dir / "murmur-ime" / "review.sock")


def test_cli_commands_and_toggle_over_private_socket(runtime_dir):
    session, server, thread = _start_server(runtime_dir)

    assert stat.S_IMODE(server.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(server.review_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(server.review_path.parent.stat().st_mode) == 0o700
    assert request_command("toggle")["state"] == "starting"
    assert request_command("toggle")["state"] == "stopping"
    assert request_command("cancel")["state"] == "idle"
    assert request_command("shutdown")["code"] == "shutting-down"

    thread.join(2)
    assert not thread.is_alive()
    assert session.closed
    assert not server.path.exists()
    assert not server.review_path.exists()


def test_review_socket_returns_only_bounded_last_result_on_sibling_path(runtime_dir):
    session, server, thread = _start_server(runtime_dir)
    private_text = "只在主机设置中显示的识别结果"
    session.review = LastReview("utterance-1", private_text)

    review = request_last_review()

    assert review is not None
    assert review.utterance_id == "utterance-1"
    assert review.provider_text == private_text
    assert review.delivered_text == private_text
    assert private_text not in repr(review)
    assert request_command("status").keys() == {"ok", "code", "state"}

    submission = submit_last_review("utterance-1", "实际逐字内容")
    assert submission == session.review_submit_reply
    assert session.review_submissions == [("utterance-1", "实际逐字内容")]

    session.review = None
    assert request_last_review() is None
    request_command("shutdown")
    thread.join(2)


def test_review_socket_round_trips_maximum_raw_and_delivered_text(runtime_dir):
    session, _server, thread = _start_server(runtime_dir)
    maximum_text = "𐀀" * 4096
    session.review = LastReview("utterance-max", maximum_text, maximum_text)

    review = request_last_review()

    assert review is not None
    assert review.provider_text == maximum_text
    assert review.delivered_text == maximum_text
    assert maximum_text not in repr(review)
    request_command("shutdown")
    thread.join(2)


@pytest.mark.parametrize(
    "maximum_text",
    (
        "\x01" * 4096,
        ('"\\' * 2048),
    ),
)
def test_review_socket_round_trips_maximum_json_escaping(runtime_dir, maximum_text):
    session, _server, thread = _start_server(runtime_dir)
    maximum_id = "u" * 128
    session.review = LastReview(maximum_id, maximum_text, maximum_text)

    review = request_last_review()

    assert review is not None
    assert review.utterance_id == maximum_id
    assert review.provider_text == maximum_text
    assert review.delivered_text == maximum_text
    assert maximum_text not in repr(review)
    request_command("shutdown")
    thread.join(2)


def test_review_submission_failure_is_content_free_and_never_echoes_text(runtime_dir):
    session, _server, thread = _start_server(runtime_dir)
    session.review_submit_reply = ReviewSubmitReply(False, "stale-review")
    private_text = "must-not-return-in-review-response"

    with pytest.raises(ControlError, match="stale-review") as raised:
        submit_last_review("utterance-1", private_text)

    assert private_text not in str(raised.value)
    request_command("shutdown")
    thread.join(2)


def test_press_release_and_cancel_route_through_interaction_controller(runtime_dir):
    session = FakeSession()
    interaction = FakeInteraction(session)
    server = ControlServer(session, interaction=interaction)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not server.path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert request_command("press")["state"] == "starting"
    assert request_command("release")["state"] == "stopping"
    assert request_command("cancel")["state"] == "idle"
    assert request_command("toggle")["state"] == "starting"
    assert request_command("shutdown")["code"] == "shutting-down"
    thread.join(2)

    assert [
        call[0] if isinstance(call, tuple) else call for call in interaction.calls[:4]
    ] == ["press", "release", "cancel", "reset"]
    press_time = interaction.calls[0][1]
    release_time = interaction.calls[1][1]
    assert isinstance(press_time, float)
    assert isinstance(release_time, float)
    assert release_time >= press_time
    assert interaction.calls.count("close") >= 1


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


def test_review_socket_refuses_symlink_escape_from_private_sibling(
    runtime_dir, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (runtime_dir / "murmur-ime-private").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ControlError, match="inside"):
        review_socket_path()


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


def test_private_submit_client_waits_longer_without_changing_public_timeout(
    runtime_dir, monkeypatch
):
    class FakeClientSocket:
        def __init__(self):
            self.timeout = None
            self.closed = False

        def settimeout(self, value):
            self.timeout = value

        def connect(self, path):
            assert path == str(runtime_dir / "murmur-ime-private" / "review.sock")

        def sendall(self, data):
            document = json.loads(data.decode("utf-8"))
            assert document["command"] == "submit-review"
            assert document["utterance_id"] == "utterance-1"

        def recv(self, maximum):
            del maximum
            return (
                b'{"ok":true,"code":"review-submitted",'
                b'"reason_code":"explicit-feedback-activated",'
                b'"feedback_code":"feedback-disabled"}\n'
            )

        def close(self):
            self.closed = True

    client = FakeClientSocket()
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: client)

    reply = submit_last_review("utterance-1", "actual speech")

    assert reply.ok
    assert client.timeout == REVIEW_SUBMIT_TIMEOUT_SECONDS
    assert REVIEW_SUBMIT_TIMEOUT_SECONDS < CONTROL_RESPONSE_TIMEOUT_SECONDS
    assert client.closed


def test_private_server_read_timeout_remains_short(runtime_dir):
    session = FakeSession()
    server = ControlServer(session)
    server_side, client_side = socket.socketpair()
    client_side.sendall(b"review-last\n")

    with server_side, client_side:
        server._serve_review_connection(server_side)
        assert server_side.gettimeout() == 2.0
