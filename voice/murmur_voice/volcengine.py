# SPDX-License-Identifier: GPL-3.0-only
# Portions adapted from Doubao Murmur, Copyright (c) 2026 lilong7676,
# supplied under MIT. Modifications Copyright (c) 2026 Open Voice Input Linux
# contributors. See NOTICE.md for source paths and the preserved MIT terms.
"""Threaded client for Volcengine v3 optimized bidirectional ASR."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import struct
import threading
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import DEFAULT_ENDPOINT, DEFAULT_RESOURCE_ID

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
SAMPLE_BITS = 16
CHANNELS = 1
BYTES_PER_SAMPLE = SAMPLE_BITS // 8

_VERSION = 0b0001
_HEADER_WORDS = 0b0001
_CLIENT_FULL_REQUEST = 0b0001
_CLIENT_AUDIO_REQUEST = 0b0010
_SERVER_FULL_RESPONSE = 0b1001
_SERVER_ERROR_RESPONSE = 0b1111

_NO_SEQUENCE = 0b0000
_POSITIVE_SEQUENCE = 0b0001
_LAST_NO_SEQUENCE = 0b0010
_LAST_WITH_SEQUENCE = 0b0011

_SERIALIZATION_NONE = 0b0000
_SERIALIZATION_JSON = 0b0001
_COMPRESSION_NONE = 0b0000
_COMPRESSION_GZIP = 0b0001

_AUTH_ERROR_CODES = {401, 403, 45000010, 45000011, 45000012}
_AUTH_WORDS = (
    "auth",
    "unauthor",
    "forbidden",
    "api key",
    "access key",
    "credential",
    "permission",
)

# WebSocket frames are already capped at 4 MiB. Gzip can expand a tiny frame
# into an arbitrarily large allocation, so bound the decoded JSON separately.
_MAX_DECOMPRESSED_PAYLOAD_BYTES = 8 * 1024 * 1024


class VolcengineProtocolError(RuntimeError):
    """A server error whose printable form excludes the remote payload."""

    def __init__(self, code: int, detail: Any = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"Volcengine ASR protocol error {code}")


class AudioBackpressureError(RuntimeError):
    """The network sender did not drain PCM within the bounded queue."""

    def __init__(self) -> None:
        super().__init__("Volcengine ASR audio queue exceeded its safe limit")


@dataclass(frozen=True, slots=True)
class ParsedFrame:
    message_type: int
    flags: int
    serialization: int
    compression: int
    sequence: int | None
    is_last: bool
    payload: Any = None
    error_code: int | None = None


def _build_header(
    message_type: int,
    flags: int,
    serialization: int,
    compression: int,
) -> bytes:
    return bytes(
        (
            (_VERSION << 4) | _HEADER_WORDS,
            (message_type << 4) | flags,
            (serialization << 4) | compression,
            0,
        )
    )


def _encode_full_request(payload: dict[str, Any], sequence: int | None = None) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    flags = _POSITIVE_SEQUENCE if sequence is not None else _NO_SEQUENCE
    parts = [
        _build_header(
            _CLIENT_FULL_REQUEST,
            flags,
            _SERIALIZATION_JSON,
            _COMPRESSION_NONE,
        )
    ]
    if sequence is not None:
        if sequence == 0:
            raise ValueError("sequence must be non-zero")
        parts.append(struct.pack(">i", abs(sequence)))
    parts.extend((struct.pack(">I", len(body)), body))
    return b"".join(parts)


def _encode_audio_request(
    audio: bytes, sequence: int | None = None, *, final: bool = False
) -> bytes:
    if sequence == 0:
        raise ValueError("sequence must be non-zero")
    if sequence is None:
        flags = _LAST_NO_SEQUENCE if final else _NO_SEQUENCE
    else:
        flags = _LAST_WITH_SEQUENCE if final else _POSITIVE_SEQUENCE
    parts = [
        _build_header(
            _CLIENT_AUDIO_REQUEST,
            flags,
            _SERIALIZATION_NONE,
            _COMPRESSION_NONE,
        )
    ]
    if sequence is not None:
        parts.append(struct.pack(">i", -abs(sequence) if final else abs(sequence)))
    body = bytes(audio)
    parts.extend((struct.pack(">I", len(body)), body))
    return b"".join(parts)


def _take(frame: bytes, offset: int, size: int, label: str) -> tuple[bytes, int]:
    end = offset + size
    if offset < 0 or end > len(frame):
        raise ValueError(f"truncated Volcengine frame ({label})")
    return frame[offset:end], end


def _decode_payload(serialization: int, compression: int, payload: bytes) -> Any:
    if compression == _COMPRESSION_GZIP:
        payload = _decompress_gzip_bounded(payload)
    elif compression != _COMPRESSION_NONE:
        raise ValueError(f"unsupported payload compression {compression}")
    if serialization == _SERIALIZATION_JSON:
        return {} if not payload else json.loads(payload.decode("utf-8"))
    if serialization == _SERIALIZATION_NONE:
        return payload
    raise ValueError(f"unsupported payload serialization {serialization}")


def _decompress_gzip_bounded(payload: bytes) -> bytes:
    """Decode one gzip member without allowing expansion past the hard cap."""
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        decoded = decoder.decompress(payload, _MAX_DECOMPRESSED_PAYLOAD_BYTES + 1)
        if len(decoded) > _MAX_DECOMPRESSED_PAYLOAD_BYTES or decoder.unconsumed_tail:
            raise ValueError("gzip payload exceeds the decoded size limit")
        decoded += decoder.flush(_MAX_DECOMPRESSED_PAYLOAD_BYTES + 1 - len(decoded))
    except zlib.error as error:
        raise ValueError("invalid gzip payload") from error
    if (
        len(decoded) > _MAX_DECOMPRESSED_PAYLOAD_BYTES
        or not decoder.eof
        or decoder.unused_data
    ):
        raise ValueError("invalid or oversized gzip payload")
    return decoded


def _parse_server_frame(message: bytes | bytearray | memoryview) -> ParsedFrame:
    frame = bytes(message)
    if len(frame) < 4:
        raise ValueError("truncated Volcengine frame header")
    version = frame[0] >> 4
    header_words = frame[0] & 0x0F
    if version != _VERSION:
        raise ValueError(f"unsupported Volcengine protocol version {version}")
    if header_words < 1:
        raise ValueError("invalid Volcengine header size")
    header_size = header_words * 4
    if len(frame) < header_size:
        raise ValueError("truncated Volcengine extended header")

    message_type = frame[1] >> 4
    flags = frame[1] & 0x0F
    serialization = frame[2] >> 4
    compression = frame[2] & 0x0F
    offset = header_size
    sequence = None
    if flags & _POSITIVE_SEQUENCE:
        raw, offset = _take(frame, offset, 4, "sequence")
        sequence = struct.unpack(">i", raw)[0]
    is_last = bool(flags & _LAST_NO_SEQUENCE)

    if message_type == _SERVER_FULL_RESPONSE:
        raw, offset = _take(frame, offset, 4, "payload size")
        payload_size = struct.unpack(">I", raw)[0]
        raw_payload, offset = _take(frame, offset, payload_size, "payload")
        if offset != len(frame):
            raise ValueError("unexpected bytes after Volcengine payload")
        return ParsedFrame(
            message_type,
            flags,
            serialization,
            compression,
            sequence,
            is_last,
            _decode_payload(serialization, compression, raw_payload),
        )

    if message_type == _SERVER_ERROR_RESPONSE:
        raw, offset = _take(frame, offset, 4, "error code")
        error_code = struct.unpack(">I", raw)[0]
        raw, offset = _take(frame, offset, 4, "error payload size")
        payload_size = struct.unpack(">I", raw)[0]
        raw_payload, offset = _take(frame, offset, payload_size, "error payload")
        if offset != len(frame):
            raise ValueError("unexpected bytes after Volcengine error payload")
        try:
            payload = _decode_payload(serialization, compression, raw_payload)
        except (ValueError, UnicodeDecodeError):
            payload = raw_payload.decode("utf-8", "replace")
        return ParsedFrame(
            message_type,
            flags,
            serialization,
            compression,
            sequence,
            True,
            payload,
            error_code,
        )
    raise ValueError(f"unexpected Volcengine server message type {message_type}")


def _extract_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str) and text:
            return text
        utterances = result.get("utterances")
    elif isinstance(result, list):
        direct = [
            item.get("text", "")
            for item in result
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if any(direct):
            return "".join(direct)
        utterances = [
            utterance
            for item in result
            if isinstance(item, dict)
            for utterance in (item.get("utterances") or [])
        ]
    else:
        utterances = payload.get("utterances")
    if not isinstance(utterances, list):
        return ""
    return "".join(
        str(item.get("text") or "") for item in utterances if isinstance(item, dict)
    )


def _payload_error(payload: Any) -> VolcengineProtocolError | None:
    if not isinstance(payload, dict):
        return None
    raw_code = payload.get("code", payload.get("status_code"))
    if raw_code in (None, 0, "0", 20000000, "20000000"):
        return None
    try:
        code = int(raw_code)
    except (TypeError, ValueError):
        code = -1
    return VolcengineProtocolError(
        code, payload.get("message", payload.get("status_text"))
    )


def _looks_like_auth_error(error: BaseException) -> bool:
    code = getattr(error, "code", None)
    if code in _AUTH_ERROR_CODES:
        return True
    status = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    status = (
        status
        or getattr(response, "status_code", None)
        or getattr(response, "status", None)
    )
    if status in (401, 403):
        return True
    detail = getattr(error, "detail", "")
    lowered = f"{error} {detail}".lower()
    return any(word in lowered for word in _AUTH_WORDS)


def _load_websockets():
    try:
        import websockets
    except Exception as error:
        raise RuntimeError(
            "websockets is unavailable; install the voice dependencies"
        ) from error
    return websockets


def _websocket_header_kwargs(
    connect_func: Callable[..., Any], headers: dict[str, str]
) -> dict[str, Any]:
    try:
        parameters = inspect.signature(connect_func).parameters
    except (TypeError, ValueError):
        parameters = {}
    key = (
        "additional_headers" if "additional_headers" in parameters else "extra_headers"
    )
    kwargs: dict[str, Any] = {key: headers}
    if "ping_timeout" in parameters:
        # Keep sending protocol pings, but let the provider decide whether a
        # quiet connection is still valid.
        kwargs["ping_timeout"] = None
    return kwargs


class VolcengineASRClient:
    """Streaming provider isolated on a private asyncio thread."""

    is_streaming = True
    waits_for_final_event = True

    def __init__(self, settings: dict[str, Any]) -> None:
        settings = settings if isinstance(settings, dict) else {}
        self._api_key = str(settings.get("api_key") or "").strip()
        self._endpoint = str(settings.get("endpoint") or DEFAULT_ENDPOINT)
        self._resource_id = str(settings.get("resource_id") or DEFAULT_RESOURCE_ID)
        if self._endpoint != DEFAULT_ENDPOINT:
            raise ValueError("only the reviewed Volcengine endpoint is supported")
        if self._resource_id != DEFAULT_RESOURCE_ID:
            raise ValueError("only the reviewed Volcengine resource is supported")

        self._uid = str(settings.get("uid") or "murmur-ime-voice")
        self._chunk_ms = max(100, min(1000, int(settings.get("chunk_ms") or 200)))
        self._chunk_bytes = (
            SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * self._chunk_ms // 1000
        )
        self.final_result_timeout = max(
            1.0, min(60.0, float(settings.get("final_result_timeout") or 20.0))
        )
        pending_seconds = max(
            1.0,
            min(30.0, float(settings.get("max_pending_audio_seconds") or 10.0)),
        )
        self._max_pending_audio_bytes = int(
            SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * pending_seconds
        )
        self._request_options = {
            "model_name": "bigmodel",
            "enable_itn": bool(settings.get("enable_itn", True)),
            "enable_punc": bool(settings.get("enable_punc", True)),
            "enable_ddc": bool(settings.get("enable_ddc", True)),
            "enable_nonstream": bool(settings.get("enable_nonstream", True)),
            "show_utterances": bool(settings.get("show_utterances", True)),
            "result_type": str(settings.get("result_type") or "full"),
            "end_window_size": max(
                300, min(5000, int(settings.get("end_window_size") or 800))
            ),
        }

        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task | None = None
        self._wake_event: asyncio.Event | None = None
        self._ws: Any | None = None
        self._pending_audio = bytearray()
        self._connected = False
        self._active = False
        self._finish_requested = False
        self._buffer_failed = False
        self._generation = 0
        self._last_text = ""

        self.on_open: Callable[[], None] | None = None
        self.on_result: Callable[[str], None] | None = None
        self.on_finish: Callable[[], None] | None = None
        self.on_error: Callable[[BaseException], None] | None = None
        self.on_auth_error: Callable[[], None] | None = None

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def pending_audio_bytes(self) -> int:
        with self._lock:
            return len(self._pending_audio)

    def _build_headers(self, connect_id: str | None = None) -> dict[str, str]:
        return {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Connect-Id": connect_id or str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        }

    def _build_payload(self) -> dict[str, Any]:
        return {
            "user": {"uid": self._uid, "platform": "Linux"},
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": SAMPLE_RATE,
                "bits": SAMPLE_BITS,
                "channel": CHANNELS,
            },
            "request": dict(self._request_options),
        }

    def connect(self) -> None:
        if not self._api_key:
            self._notify_error(
                RuntimeError("Volcengine API key is not configured"), None
            )
            return
        self.disconnect()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._pending_audio.clear()
            self._connected = False
            self._active = True
            self._finish_requested = False
            self._buffer_failed = False
            self._last_text = ""
            loop = asyncio.new_event_loop()
            self._loop = loop
            thread = threading.Thread(
                target=self._run_loop,
                args=(loop, generation),
                name="murmur-volcengine-asr",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def send_audio(self, data: bytes) -> None:
        if not data:
            return
        overflow = False
        with self._lock:
            if not self._active or self._finish_requested or self._buffer_failed:
                return
            if len(self._pending_audio) + len(data) > self._max_pending_audio_bytes:
                self._buffer_failed = True
                overflow = True
            else:
                self._pending_audio.extend(data)
            generation = self._generation
            loop = self._loop
            event = self._wake_event
        if overflow:

            def callback() -> None:
                self._notify_error(AudioBackpressureError(), generation)

            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(callback)
            else:
                threading.Thread(target=callback, daemon=True).start()
            return
        if loop is not None and event is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)

    def finish_sending(self) -> None:
        with self._lock:
            if not self._active or self._finish_requested:
                return
            self._finish_requested = True
            loop = self._loop
            event = self._wake_event
        if loop is not None and event is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)

    def disconnect(self) -> None:
        with self._lock:
            self._generation += 1
            self._active = False
            self._connected = False
            self._finish_requested = False
            self._buffer_failed = False
            self._pending_audio.clear()
            loop = self._loop
            task = self._task
            event = self._wake_event
        if loop is not None and loop.is_running():
            if event is not None:
                loop.call_soon_threadsafe(event.set)
            if task is not None and not task.done():
                loop.call_soon_threadsafe(task.cancel)

    def _run_loop(self, loop: asyncio.AbstractEventLoop, generation: int) -> None:
        asyncio.set_event_loop(loop)
        task = loop.create_task(self._connect_and_listen(generation))
        with self._lock:
            if generation == self._generation:
                self._task = task
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        finally:
            pending = asyncio.all_tasks(loop)
            for item in pending:
                item.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            with self._lock:
                if self._loop is loop:
                    self._loop = None
                    self._task = None
                    self._wake_event = None
                    self._ws = None
                    self._connected = False
                    self._active = False

    async def _connect_and_listen(self, generation: int) -> None:
        if not self._is_current(generation):
            return
        logger.info("Connecting to Volcengine ASR")
        try:
            websockets = _load_websockets()
            kwargs = _websocket_header_kwargs(websockets.connect, self._build_headers())
            async with websockets.connect(
                self._endpoint,
                open_timeout=8,
                close_timeout=3,
                max_size=2**22,
                **kwargs,
            ) as websocket:
                if not self._is_current(generation):
                    return
                with self._lock:
                    self._ws = websocket
                    self._connected = True
                    self._wake_event = asyncio.Event()
                await websocket.send(_encode_full_request(self._build_payload()))
                logger.info("Connected to Volcengine ASR")
                self._invoke(self.on_open, generation)

                sender = asyncio.create_task(
                    self._send_audio_stream(websocket, generation)
                )
                receiver = asyncio.create_task(
                    self._receive_messages(websocket, generation)
                )
                try:
                    done, _ = await asyncio.wait(
                        (sender, receiver),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if receiver in done:
                        await receiver
                        if not sender.done():
                            sender.cancel()
                    else:
                        await sender
                        await receiver
                finally:
                    for task in (sender, receiver):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(sender, receiver, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self._is_current(generation):
                logger.error(
                    "Volcengine ASR connection failed (%s)",
                    error.__class__.__name__,
                )
                if _looks_like_auth_error(error):
                    self._notify_error(error, generation)
                else:
                    self._notify_error(
                        RuntimeError(
                            "Volcengine ASR connection failed "
                            f"({error.__class__.__name__})"
                        ),
                        generation,
                    )
        finally:
            with self._lock:
                if generation == self._generation:
                    self._connected = False
                    self._ws = None

    async def _send_audio_stream(self, websocket: Any, generation: int) -> None:
        while self._is_current(generation):
            with self._lock:
                event = self._wake_event
                chunks: list[tuple[bytes, bool]] = []
                while len(self._pending_audio) > self._chunk_bytes:
                    chunk = bytes(self._pending_audio[: self._chunk_bytes])
                    del self._pending_audio[: self._chunk_bytes]
                    chunks.append((chunk, False))
                if self._finish_requested:
                    chunks.append((bytes(self._pending_audio), True))
                    self._pending_audio.clear()
                if event is not None:
                    event.clear()
            for chunk, final in chunks:
                if not self._is_current(generation):
                    return
                await websocket.send(_encode_audio_request(chunk, final=final))
                if final:
                    return
            if event is None:
                await asyncio.sleep(0)
            else:
                await event.wait()

    async def _receive_messages(self, websocket: Any, generation: int) -> None:
        async for message in websocket:
            if not self._is_current(generation):
                return
            if self._handle_message(message, generation):
                return
        if self._is_current(generation):
            raise ConnectionError("Volcengine ASR closed before final response")

    def _handle_message(self, message: Any, generation: int | None = None) -> bool:
        if generation is None:
            with self._lock:
                generation = self._generation
        if not self._is_current(generation):
            return True
        try:
            if isinstance(message, str):
                payload = json.loads(message)
                parsed = ParsedFrame(
                    _SERVER_FULL_RESPONSE,
                    _NO_SEQUENCE,
                    _SERIALIZATION_JSON,
                    _COMPRESSION_NONE,
                    None,
                    bool(payload.get("final") or payload.get("is_final")),
                    payload,
                )
            else:
                parsed = _parse_server_frame(message)
            if parsed.message_type == _SERVER_ERROR_RESPONSE:
                raise VolcengineProtocolError(parsed.error_code or -1, parsed.payload)
            payload_error = _payload_error(parsed.payload)
            if payload_error is not None:
                raise payload_error
        except Exception as error:
            logger.error(
                "Volcengine ASR response rejected (%s)",
                error.__class__.__name__,
            )
            self._notify_error(error, generation)
            return True

        text = _extract_text(parsed.payload)
        if text:
            with self._lock:
                changed = generation == self._generation and text != self._last_text
                if changed:
                    self._last_text = text
            if changed:
                self._invoke(self.on_result, generation, text)
        if parsed.is_last:
            self._invoke(self.on_finish, generation)
            return True
        return False

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return self._active and generation == self._generation

    def _invoke(
        self,
        callback: Callable[..., Any] | None,
        generation: int,
        *arguments: Any,
    ) -> None:
        if callback is None or not self._is_current(generation):
            return
        try:
            callback(*arguments)
        except Exception as error:
            logger.error("ASR callback failed (%s)", error.__class__.__name__)

    def _notify_error(self, error: BaseException, generation: int | None) -> None:
        if generation is not None and not self._is_current(generation):
            return
        callback: Callable[..., Any] | None
        is_auth_error = _looks_like_auth_error(error)
        callback = self.on_auth_error if is_auth_error else self.on_error
        if callback is None:
            return
        try:
            callback() if is_auth_error else callback(error)
        except Exception as callback_error:
            logger.error("ASR callback failed (%s)", callback_error.__class__.__name__)
