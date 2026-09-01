"""Permission-scoped local command channel for the foreground daemon."""

from __future__ import annotations

import json
import os
import queue
import re
import select
import socket
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .state import CommandReply

if TYPE_CHECKING:
    from .interaction import InteractionController
    from .session import VoiceSession

MAX_COMMAND_BYTES = 256
MAX_RESPONSE_BYTES = 4096
MAX_REVIEW_REQUEST_BYTES = 20 * 1024
MAX_REVIEW_TEXT_CODEPOINTS = 4096
MAX_REVIEW_TEXT_UTF8_BYTES = 16 * 1024
# With ensure_ascii=False, one allowed codepoint needs at most six JSON bytes:
# U+0000..U+001F use ``\u00xx`` while a raw Unicode scalar uses at most four
# UTF-8 bytes.  The response carries two independently maximum-sized texts;
# 1 KiB safely bounds fixed keys, punctuation, booleans, and the 128-byte ID.
_MAX_REVIEW_JSON_TEXT_BYTES = max(
    MAX_REVIEW_TEXT_UTF8_BYTES,
    MAX_REVIEW_TEXT_CODEPOINTS * 6,
)
MAX_REVIEW_RESPONSE_BYTES = (2 * _MAX_REVIEW_JSON_TEXT_BYTES) + 1024
_UTTERANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
# Production Preedit acquisition is bounded to 29 s, failed microphone recovery
# (including conservative rollback) to 10 s, and Preedit cleanup to 8 s. Keep the
# client beyond their 47 s sum so it receives the real failure reply instead of
# reporting a false daemon outage. VoiceSession separately gates provider/capture
# opening with a 35 s total-start deadline.
CONTROL_RESPONSE_TIMEOUT_SECONDS = 50.0
REVIEW_SUBMIT_TIMEOUT_SECONDS = 15.0
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


@dataclass(frozen=True, slots=True, repr=False)
class LastReview:
    """One bounded raw final plus read-only delivery returned to the host UI."""

    utterance_id: str
    provider_text: str
    delivered_text: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class ReviewSubmission:
    """One bounded verbatim submission, hidden from debug representations."""

    utterance_id: str
    spoken_verbatim: str


@dataclass(frozen=True, slots=True)
class ReviewSubmitReply:
    """Content-free outcome from the daemon-owned review transaction."""

    ok: bool
    code: str
    reason_code: str | None = None
    feedback_code: str | None = None


def runtime_socket_path(
    requested: str | os.PathLike[str] | None = None,
) -> Path:
    return _runtime_socket_path(
        requested,
        default_relative=Path("murmur-ime") / "voice.sock",
        label="control",
    )


def review_socket_path(
    requested: str | os.PathLike[str] | None = None,
) -> Path:
    """Return a host-only socket outside the Flatpak-exposed runtime subtree."""

    resolved = _runtime_socket_path(
        requested,
        default_relative=Path("murmur-ime-private") / "review.sock",
        label="review",
    )
    runtime_root = Path(os.environ["XDG_RUNTIME_DIR"]).resolve()
    private_root = (runtime_root / "murmur-ime-private").resolve(strict=False)
    try:
        resolved.relative_to(private_root)
    except ValueError as error:
        raise ControlError(
            "review socket must stay inside the host-only private runtime directory"
        ) from error
    return resolved


def _runtime_socket_path(
    requested: str | os.PathLike[str] | None,
    *,
    default_relative: Path,
    label: str,
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

    path = Path(requested) if requested is not None else runtime_root / default_relative
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(runtime_root)
    except ValueError as error:
        raise ControlError(f"{label} socket must be inside XDG_RUNTIME_DIR") from error
    return resolved


class ControlServer:
    """Serve bounded commands on a mode-0600 AF_UNIX socket."""

    def __init__(
        self,
        session: VoiceSession,
        socket_path: str | os.PathLike[str] | None = None,
        *,
        interaction: InteractionController | None = None,
        private_review_socket_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._session = session
        self._interaction = interaction
        self.path = runtime_socket_path(socket_path)
        self.review_path = review_socket_path(private_review_socket_path)
        if self.path == self.review_path:
            raise ControlError("control and review sockets must be separate")
        self._socket: socket.socket | None = None
        self._review_socket: socket.socket | None = None
        self._running = False

    def serve_forever(
        self, signal_commands: queue.SimpleQueue[str] | None = None
    ) -> None:
        self._prepare_parent(self.path)
        self._prepare_parent(self.review_path)
        self._remove_stale_socket(self.path)
        self._remove_stale_socket(self.review_path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        review_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket = server
        self._review_socket = review_server
        try:
            server.bind(str(self.path))
            os.chmod(self.path, 0o600)
            server.listen(4)
            review_server.bind(str(self.review_path))
            os.chmod(self.review_path, 0o600)
            review_server.listen(2)
            self._running = True
            while self._running:
                self._drain_signal_commands(signal_commands)
                if not self._running:
                    break
                readable, _writable, _exceptional = select.select(
                    (server, review_server), (), (), 0.2
                )
                for listener in readable:
                    if not self._running:
                        break
                    connection, _ = listener.accept()
                    with connection:
                        if listener is review_server:
                            self._serve_review_connection(connection)
                        else:
                            self._serve_connection(connection)
        finally:
            self._running = False
            if self._interaction is not None:
                self._interaction.close()
            self._session.close()
            server.close()
            review_server.close()
            self._socket = None
            self._review_socket = None
            self._unlink_own_socket(self.path)
            self._unlink_own_socket(self.review_path)

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

    def _serve_review_connection(self, connection: socket.socket) -> None:
        """Return one bounded final on a socket unavailable to the Flatpak."""

        connection.settimeout(2.0)
        is_submission = False
        try:
            request = _receive_bounded(connection, MAX_REVIEW_REQUEST_BYTES)
            if request != b"review-last":
                is_submission = True
                submission = _decode_review_submission(request)
                submitter = getattr(self._session, "submit_last_review", None)
                if not callable(submitter):
                    reply = ReviewSubmitReply(False, "review-unavailable")
                else:
                    reply = submitter(
                        submission.utterance_id,
                        submission.spoken_verbatim,
                    )
                document = _review_submit_document(reply)
            else:
                reader = getattr(self._session, "review_last", None)
                review = reader() if callable(reader) else None
                document = _review_document(review)
        except (ControlError, OSError):
            document = (
                {"ok": False, "code": "invalid-request"}
                if is_submission
                else {"available": False, "code": "invalid-request"}
            )
        except Exception:
            # Session/runtime failures must neither terminate the daemon nor
            # reflect submitted text into a response.
            document = (
                {"ok": False, "code": "review-failed"}
                if is_submission
                else {"available": False, "code": "review-failed"}
            )
        response = (
            json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(response) > MAX_REVIEW_RESPONSE_BYTES:
            response = b'{"available":false,"code":"invalid-response"}\n'
        try:
            connection.sendall(response)
        except OSError:
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

    @staticmethod
    def _prepare_parent(path: Path) -> None:
        parent = path.parent
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

    @staticmethod
    def _remove_stale_socket(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ControlError("refusing to replace an unsafe control path")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            path.unlink(missing_ok=True)
        else:
            raise ControlError("voice daemon is already running")
        finally:
            probe.close()

    @staticmethod
    def _unlink_own_socket(path: Path) -> None:
        try:
            metadata = path.lstat()
            if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.getuid():
                path.unlink()
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


def request_last_review(
    socket_path: str | os.PathLike[str] | None = None,
) -> LastReview | None:
    """Read the latest final through the separate host-only private socket."""

    path = review_socket_path(socket_path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(2.0)
        client.connect(str(path))
        client.sendall(b"review-last\n")
        response = _receive_bounded(client, MAX_REVIEW_RESPONSE_BYTES)
    except OSError as error:
        raise ControlError("review service is unavailable") from error
    finally:
        client.close()
    try:
        document = json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlError("review service returned an invalid response") from error
    if not isinstance(document, dict) or type(document.get("available")) is not bool:
        raise ControlError("review service returned an invalid response")
    if document["available"] is False:
        if set(document) not in ({"available"}, {"available", "code"}):
            raise ControlError("review service returned an invalid response")
        return None
    response_fields = set(document)
    legacy_fields = {"available", "utterance_id", "provider_text"}
    current_fields = legacy_fields | {"delivered_text"}
    if response_fields not in (legacy_fields, current_fields):
        raise ControlError("review service returned an invalid response")
    return _validated_review(
        document.get("utterance_id"),
        document.get("provider_text"),
        document.get("delivered_text"),
    )


def submit_last_review(
    utterance_id: str,
    spoken_verbatim: str,
    socket_path: str | os.PathLike[str] | None = None,
) -> ReviewSubmitReply:
    """Submit one explicit verbatim review to the daemon-owned transaction."""

    review = _validated_review(utterance_id, spoken_verbatim)
    submission = ReviewSubmission(review.utterance_id, review.provider_text)
    request = (
        json.dumps(
            {
                "command": "submit-review",
                "utterance_id": submission.utterance_id,
                "spoken_verbatim": submission.spoken_verbatim,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(request) > MAX_REVIEW_REQUEST_BYTES:
        raise ControlError("review submission is too large")
    path = review_socket_path(socket_path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(REVIEW_SUBMIT_TIMEOUT_SECONDS)
        client.connect(str(path))
        client.sendall(request)
        response = _receive_bounded(client, MAX_RESPONSE_BYTES)
    except OSError as error:
        raise ControlError("review service is unavailable") from error
    finally:
        client.close()
    try:
        document = json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlError("review service returned an invalid response") from error
    if not isinstance(document, dict) or type(document.get("ok")) is not bool:
        raise ControlError("review service returned an invalid response")
    if document["ok"] is False:
        if set(document) != {"ok", "code"} or not _valid_status_code(
            document.get("code")
        ):
            raise ControlError("review service returned an invalid response")
        raise ControlError(str(document["code"]))
    if (
        set(document) != {"ok", "code", "reason_code", "feedback_code"}
        or document.get("code") != "review-submitted"
        or not _valid_status_code(document.get("reason_code"))
        or document.get("feedback_code")
        not in {"feedback-disabled", "feedback-failed", "feedback-queued"}
    ):
        raise ControlError("review service returned an invalid response")
    return ReviewSubmitReply(
        True,
        "review-submitted",
        str(document["reason_code"]),
        str(document["feedback_code"]),
    )


def _review_document(value: object) -> dict[str, Any]:
    if value is None:
        return {"available": False}
    review = _validated_review(
        getattr(value, "utterance_id", None),
        getattr(value, "provider_text", None),
        getattr(value, "delivered_text", None),
    )
    return {
        "available": True,
        "utterance_id": review.utterance_id,
        "provider_text": review.provider_text,
        "delivered_text": review.delivered_text,
    }


def _decode_review_submission(payload: bytes) -> ReviewSubmission:
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ControlError) as error:
        raise ControlError("invalid review submission") from error
    if not isinstance(document, dict) or set(document) != {
        "command",
        "utterance_id",
        "spoken_verbatim",
    }:
        raise ControlError("invalid review submission")
    if document.get("command") != "submit-review":
        raise ControlError("invalid review submission")
    review = _validated_review(
        document.get("utterance_id"),
        document.get("spoken_verbatim"),
    )
    return ReviewSubmission(review.utterance_id, review.provider_text)


def _review_submit_document(value: object) -> dict[str, Any]:
    if not isinstance(value, ReviewSubmitReply) or not _valid_status_code(value.code):
        return {"ok": False, "code": "review-failed"}
    if not value.ok:
        return {"ok": False, "code": value.code}
    if (
        value.code != "review-submitted"
        or not _valid_status_code(value.reason_code)
        or value.feedback_code
        not in {"feedback-disabled", "feedback-failed", "feedback-queued"}
    ):
        return {"ok": False, "code": "review-failed"}
    return {
        "ok": True,
        "code": value.code,
        "reason_code": value.reason_code,
        "feedback_code": value.feedback_code,
    }


def _reject_duplicate_json_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for name, value in pairs:
        if name in document:
            raise ControlError("duplicate review field")
        document[name] = value
    return document


def _valid_status_code(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= 96
        and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value)
    )


def _validated_review(
    utterance_id: object,
    provider_text: object,
    delivered_text: object | None = None,
) -> LastReview:
    if not isinstance(utterance_id, str) or not _UTTERANCE_ID_RE.fullmatch(
        utterance_id
    ):
        raise ControlError("review service returned an invalid response")
    if (
        not isinstance(provider_text, str)
        or not provider_text
        or len(provider_text) > MAX_REVIEW_TEXT_CODEPOINTS
        or len(provider_text.encode("utf-8")) > MAX_REVIEW_TEXT_UTF8_BYTES
        or "\x00" in provider_text
    ):
        raise ControlError("review service returned an invalid response")
    if delivered_text is None:
        delivered_text = provider_text
    if (
        not isinstance(delivered_text, str)
        or not delivered_text
        or len(delivered_text) > MAX_REVIEW_TEXT_CODEPOINTS
        or len(delivered_text.encode("utf-8")) > MAX_REVIEW_TEXT_UTF8_BYTES
        or "\x00" in delivered_text
    ):
        raise ControlError("review service returned an invalid response")
    return LastReview(utterance_id, provider_text, delivered_text)


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
