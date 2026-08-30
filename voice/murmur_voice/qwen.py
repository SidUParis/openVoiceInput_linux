"""Threaded Qwen real-time ASR WebSocket client."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Callable
from typing import Any

from .config import (
    QWEN_DEFAULT_MODEL,
    QWEN_ENDPOINT,
    QWEN_MODELS,
    normalize_vocabulary_terms,
)
from .volcengine import (
    AudioBackpressureError,
    _load_websockets,
    _websocket_header_kwargs,
)

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1
_CHUNK_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS // 5


class QwenProtocolError(RuntimeError):
    """A safe protocol failure that does not retain remote error text."""


def build_run_task(
    task_id: str,
    *,
    model: str,
    sample_rate: int,
    language_hints: tuple[str, ...],
    vocabulary: tuple[str, ...],
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "format": "pcm",
        "sample_rate": sample_rate,
        "language_hints": list(language_hints),
    }
    if vocabulary:
        parameters["vocabulary"] = {term: 5 for term in vocabulary}
    return {
        "header": {
            "action": "run-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": model,
            "parameters": parameters,
            "input": {},
        },
    }


def build_finish_task(task_id: str) -> dict[str, Any]:
    return {
        "header": {
            "action": "finish-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {"input": {}},
    }


class QwenASRClient:
    """Qwen duplex streaming isolated on a private asyncio thread."""

    is_streaming = True
    waits_for_final_event = True
    provider_name = "qwen"
    provider_resource_id = None

    def __init__(self, settings: dict[str, Any]) -> None:
        settings = settings if isinstance(settings, dict) else {}
        self._api_key = str(settings.get("api_key") or "").strip()
        self._endpoint = str(settings.get("endpoint") or QWEN_ENDPOINT)
        self._model = str(settings.get("model") or QWEN_DEFAULT_MODEL)
        self.provider_model = self._model
        if self._endpoint != QWEN_ENDPOINT:
            raise ValueError("only the reviewed Qwen endpoint is supported")
        if self._model not in QWEN_MODELS:
            raise ValueError("Qwen recognition model is unsupported")
        sample_rate = int(settings.get("sample_rate") or SAMPLE_RATE)
        if sample_rate != SAMPLE_RATE:
            raise ValueError("Qwen input must be 16 kHz PCM")
        hints = settings.get("language_hints", ("zh", "en", "fr"))
        if not isinstance(hints, (list, tuple)) or not hints:
            raise ValueError("Qwen language hints are invalid")
        self._language_hints = tuple(str(item) for item in hints[:4])
        if any(item not in {"zh", "en", "fr"} for item in self._language_hints):
            raise ValueError("Qwen language hint is unsupported")
        self._vocabulary = normalize_vocabulary_terms(settings.get("vocabulary", ()))
        self.final_result_timeout = max(
            1.0, min(60.0, float(settings.get("final_result_timeout") or 20.0))
        )
        pending_seconds = max(
            1.0,
            min(30.0, float(settings.get("max_pending_audio_seconds") or 10.0)),
        )
        self._max_pending_audio_bytes = int(
            SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS * pending_seconds
        )

        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._wake_event: asyncio.Event | None = None
        self._started_event: asyncio.Event | None = None
        self._pending_audio = bytearray()
        self._active = False
        self._connected = False
        self._finish_requested = False
        self._buffer_failed = False
        self._generation = 0

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

    def connect(self) -> None:
        self.disconnect()
        if not self._api_key:
            with self._lock:
                self._generation += 1
                generation = self._generation
                self._active = True
            self._invoke(self.on_auth_error, generation)
            with self._lock:
                if generation == self._generation:
                    self._active = False
            return
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._active = True
            self._connected = False
            self._finish_requested = False
            self._buffer_failed = False
            self._pending_audio.clear()
            loop = asyncio.new_event_loop()
            self._loop = loop
            thread = threading.Thread(
                target=self._run_loop,
                args=(loop, generation),
                name="murmur-qwen-asr",
                daemon=True,
            )
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
                self._invoke(self.on_error, generation, AudioBackpressureError())

            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(callback)
            else:
                threading.Thread(
                    target=callback,
                    name="murmur-qwen-overflow",
                    daemon=True,
                ).start()
        elif loop is not None and event is not None and loop.is_running():
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
            wake = self._wake_event
        if loop is not None and loop.is_running():
            if wake is not None:
                loop.call_soon_threadsafe(wake.set)
            if task is not None and not task.done():
                loop.call_soon_threadsafe(task.cancel)

    def _run_loop(self, loop: asyncio.AbstractEventLoop, generation: int) -> None:
        asyncio.set_event_loop(loop)
        task = loop.create_task(self._run(generation))
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
                    self._started_event = None
                    self._active = False
                    self._connected = False

    async def _run(self, generation: int) -> None:
        try:
            if not self._is_current(generation):
                return
            websockets = _load_websockets()
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": "open-voice-input-linux",
            }
            kwargs = _websocket_header_kwargs(websockets.connect, headers)
            if not self._is_current(generation):
                return
            async with websockets.connect(
                self._endpoint,
                open_timeout=8,
                close_timeout=3,
                max_size=2**21,
                **kwargs,
            ) as websocket:
                if not self._is_current(generation):
                    return
                task_id = str(uuid.uuid4())
                with self._lock:
                    self._connected = True
                    self._wake_event = asyncio.Event()
                    self._started_event = asyncio.Event()
                    started = self._started_event
                    finish_sent = asyncio.Event()
                await websocket.send(
                    json.dumps(
                        build_run_task(
                            task_id,
                            model=self._model,
                            sample_rate=SAMPLE_RATE,
                            language_hints=self._language_hints,
                            vocabulary=self._vocabulary,
                        ),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                sender = asyncio.create_task(
                    self._send_audio(
                        websocket,
                        task_id,
                        generation,
                        started,
                        finish_sent,
                    )
                )
                receiver = asyncio.create_task(
                    self._receive(
                        websocket,
                        task_id,
                        generation,
                        started,
                        finish_sent,
                    )
                )
                done, _pending = await asyncio.wait(
                    (sender, receiver), return_when=asyncio.FIRST_COMPLETED
                )
                if receiver in done:
                    await receiver
                    if not sender.done():
                        sender.cancel()
                else:
                    await sender
                    await receiver
        except asyncio.CancelledError:
            raise
        except Exception as error:
            status = getattr(error, "status_code", None)
            response = getattr(error, "response", None)
            status = (
                status
                or getattr(response, "status_code", None)
                or getattr(response, "status", None)
            )
            if status in (401, 403):
                self._invoke(self.on_auth_error, generation)
            else:
                self._invoke(
                    self.on_error, generation, QwenProtocolError("Qwen ASR failed")
                )

    async def _send_audio(
        self,
        websocket: Any,
        task_id: str,
        generation: int,
        started: asyncio.Event,
        finish_sent: asyncio.Event,
    ) -> None:
        await started.wait()
        while self._is_current(generation):
            chunk = b""
            finish = False
            with self._lock:
                if self._pending_audio:
                    size = min(_CHUNK_BYTES, len(self._pending_audio))
                    chunk = bytes(self._pending_audio[:size])
                    del self._pending_audio[:size]
                finish = self._finish_requested and not self._pending_audio
                wake = self._wake_event
                if wake is not None:
                    wake.clear()
            if chunk:
                await websocket.send(chunk)
            if finish:
                # Publish the local lifecycle edge before the await. A valid
                # server may process the frame and deliver task-finished while
                # the transport coroutine is still yielding back to us.
                finish_sent.set()
                await websocket.send(
                    json.dumps(build_finish_task(task_id), separators=(",", ":"))
                )
                return
            if not chunk and wake is not None:
                await wake.wait()

    async def _receive(
        self,
        websocket: Any,
        task_id: str,
        generation: int,
        started: asyncio.Event,
        finish_sent: asyncio.Event,
    ) -> None:
        final_sentences: dict[int, str] = {}
        task_started = False
        last_emitted = ""
        while self._is_current(generation):
            raw = await websocket.recv()
            if not isinstance(raw, str):
                raise QwenProtocolError("Qwen returned a non-JSON event")
            try:
                document = json.loads(raw)
            except json.JSONDecodeError as error:
                raise QwenProtocolError("Qwen returned invalid JSON") from error
            header = document.get("header") if isinstance(document, dict) else None
            if not isinstance(header, dict) or header.get("task_id") != task_id:
                raise QwenProtocolError("Qwen event does not match the active task")
            event = header.get("event")
            if event == "task-started":
                if task_started:
                    raise QwenProtocolError("Qwen started the task more than once")
                task_started = True
                started.set()
                self._invoke(self.on_open, generation)
                continue
            if event == "result-generated":
                if not task_started:
                    raise QwenProtocolError("Qwen returned a result before task start")
                sentence = (
                    document.get("payload", {}).get("output", {}).get("sentence")
                    if isinstance(document.get("payload"), dict)
                    else None
                )
                if not isinstance(sentence, dict) or sentence.get("heartbeat") is True:
                    continue
                sentence_id = sentence.get("sentence_id")
                text = sentence.get("text")
                if type(sentence_id) is not int or not isinstance(text, str):
                    raise QwenProtocolError("Qwen sentence event is invalid")
                if sentence.get("sentence_end") is True:
                    final_sentences[sentence_id] = text
                    partial = ""
                else:
                    partial = text
                combined = "".join(
                    final_sentences[key] for key in sorted(final_sentences)
                )
                combined += partial
                if combined:
                    last_emitted = combined
                    self._invoke(self.on_result, generation, combined)
                continue
            if event == "task-finished":
                if not task_started or not finish_sent.is_set():
                    raise QwenProtocolError("Qwen finished outside the task lifecycle")
                final_text = "".join(
                    final_sentences[key] for key in sorted(final_sentences)
                )
                if final_text != last_emitted:
                    self._invoke(self.on_result, generation, final_text)
                self._invoke(self.on_finish, generation)
                return
            if event == "task-failed":
                error_code = str(header.get("error_code") or "")
                if "AUTH" in error_code.upper() or "UNAUTHORIZED" in error_code.upper():
                    self._invoke(self.on_auth_error, generation)
                else:
                    self._invoke(
                        self.on_error, generation, QwenProtocolError("Qwen task failed")
                    )
                return
            raise QwenProtocolError("Qwen event type is unsupported")

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and self._active

    def _invoke(
        self,
        callback: Callable[..., None] | None,
        generation: int,
        *args: Any,
    ) -> None:
        if callback is not None and self._is_current(generation):
            callback(*args)
