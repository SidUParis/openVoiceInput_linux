"""Threaded Gemini Live transcription WebSocket client.

The client speaks the Gemini Live API (``BidiGenerateContent``) with the
dedicated ``gemini-3.5-transcribe-live`` model. Each utterance is one manual
activity: ``activityStart``, 16 kHz PCM chunks, ``activityEnd``. The server
streams cumulative ``interimInputTranscription`` text for responsive preedit,
then an authoritative ``inputTranscription`` and ``generationComplete``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
from collections.abc import Callable
from typing import Any

from .config import (
    GEMINI_DEFAULT_MODEL,
    GEMINI_ENDPOINT,
    GEMINI_LANGUAGE_CODES,
    GEMINI_MAX_VOCABULARY_TERMS,
    GEMINI_MODELS,
    GEMINI_TRANSCRIPTION_MODES,
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
# The Live API documents ~100 ms chunks for real-time input.
_CHUNK_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS // 10
_AUDIO_MIME_TYPE = f"audio/pcm;rate={SAMPLE_RATE}"

# Interim hypotheses sometimes space out every CJK character ("我 今 天").
# Collapse whitespace only when both neighbours are CJK text or punctuation,
# so the spacing Gemini puts between Chinese and Latin words is kept.
_CJK = "⺀-⿟　-〿぀-ヿ㄀-ㄯ㐀-䶿一-鿿豈-﫿︰-﹏＀-￯"
_CJK_GAP = re.compile(rf"(?<=[{_CJK}])[ \t]+(?=[{_CJK}])")
_AUTH_CLOSE_MARKERS = ("api key", "api_key", "permission", "unauthenticated")


class GeminiProtocolError(RuntimeError):
    """A safe protocol failure that does not retain remote error text."""


def normalize_transcript_spacing(text: str) -> str:
    """Remove spurious spaces between adjacent CJK characters."""

    return _CJK_GAP.sub("", text)


def build_setup_message(
    *,
    model: str,
    language_codes: tuple[str, ...],
    vocabulary: tuple[str, ...],
    mode: str,
) -> dict[str, Any]:
    transcription: dict[str, Any] = {
        "languageCodes": list(language_codes),
        "mode": mode,
    }
    if vocabulary:
        transcription["customVocabulary"] = list(vocabulary)
    return {
        "setup": {
            "model": f"models/{model}",
            "generationConfig": {"responseModalities": ["TEXT"]},
            "inputAudioTranscription": transcription,
            # One push-to-talk utterance is one manual activity, so a pause
            # while thinking never splits or ends the dictation early.
            "realtimeInputConfig": {"automaticActivityDetection": {"disabled": True}},
        }
    }


def build_activity_start() -> dict[str, Any]:
    return {"realtimeInput": {"activityStart": {}}}


def build_activity_end() -> dict[str, Any]:
    return {"realtimeInput": {"activityEnd": {}}}


def build_audio_message(chunk: bytes) -> dict[str, Any]:
    return {
        "realtimeInput": {
            "audio": {
                "data": base64.b64encode(chunk).decode("ascii"),
                "mimeType": _AUDIO_MIME_TYPE,
            }
        }
    }


def _dumps(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def _is_auth_close(error: BaseException) -> bool:
    received = getattr(error, "rcvd", None)
    reason = str(getattr(received, "reason", "") or "").lower()
    return any(marker in reason for marker in _AUTH_CLOSE_MARKERS)


class GeminiASRClient:
    """Gemini Live transcription isolated on a private asyncio thread."""

    is_streaming = True
    waits_for_final_event = True
    provider_name = "gemini"
    provider_resource_id = None

    def __init__(self, settings: dict[str, Any]) -> None:
        settings = settings if isinstance(settings, dict) else {}
        self._api_key = str(settings.get("api_key") or "").strip()
        self._endpoint = str(settings.get("endpoint") or GEMINI_ENDPOINT)
        self._model = str(settings.get("model") or GEMINI_DEFAULT_MODEL)
        self.provider_model = self._model
        if self._endpoint != GEMINI_ENDPOINT:
            raise ValueError("only the reviewed Gemini endpoint is supported")
        if self._model not in GEMINI_MODELS:
            raise ValueError("Gemini recognition model is unsupported")
        sample_rate = int(settings.get("sample_rate") or SAMPLE_RATE)
        if sample_rate != SAMPLE_RATE:
            raise ValueError("Gemini input must be 16 kHz PCM")
        codes = settings.get("language_codes", GEMINI_LANGUAGE_CODES)
        if not isinstance(codes, (list, tuple)):
            raise ValueError("Gemini language codes are invalid")
        self._language_codes = tuple(str(item) for item in codes[:4])
        if any(item not in GEMINI_LANGUAGE_CODES for item in self._language_codes):
            raise ValueError("Gemini language code is unsupported")
        self._mode = str(settings.get("mode") or "VERBATIM")
        if self._mode not in GEMINI_TRANSCRIPTION_MODES:
            raise ValueError("Gemini transcription mode is unsupported")
        vocabulary = normalize_vocabulary_terms(settings.get("vocabulary", ()))
        self._vocabulary = vocabulary[:GEMINI_MAX_VOCABULARY_TERMS]
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
                name="murmur-gemini-asr",
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
                    name="murmur-gemini-overflow",
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
            # The key travels in a header, never in the URL, so it cannot
            # leak through proxies or connection error messages.
            headers = {
                "x-goog-api-key": self._api_key,
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
                with self._lock:
                    self._connected = True
                    self._wake_event = asyncio.Event()
                    self._started_event = asyncio.Event()
                    started = self._started_event
                    finish_sent = asyncio.Event()
                await websocket.send(
                    _dumps(
                        build_setup_message(
                            model=self._model,
                            language_codes=self._language_codes,
                            vocabulary=self._vocabulary,
                            mode=self._mode,
                        )
                    )
                )
                sender = asyncio.create_task(
                    self._send_audio(websocket, generation, started, finish_sent)
                )
                receiver = asyncio.create_task(
                    self._receive(websocket, generation, started, finish_sent)
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
            if status in (401, 403) or _is_auth_close(error):
                self._invoke(self.on_auth_error, generation)
            else:
                self._invoke(
                    self.on_error,
                    generation,
                    GeminiProtocolError("Gemini transcription failed"),
                )

    async def _send_audio(
        self,
        websocket: Any,
        generation: int,
        started: asyncio.Event,
        finish_sent: asyncio.Event,
    ) -> None:
        await started.wait()
        if not self._is_current(generation):
            return
        await websocket.send(_dumps(build_activity_start()))
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
                await websocket.send(_dumps(build_audio_message(chunk)))
            if finish:
                # Publish the local lifecycle edge before the await: the server
                # may finalize and complete while this coroutine yields.
                finish_sent.set()
                await websocket.send(_dumps(build_activity_end()))
                return
            if not chunk and wake is not None:
                await wake.wait()

    async def _receive(
        self,
        websocket: Any,
        generation: int,
        started: asyncio.Event,
        finish_sent: asyncio.Event,
    ) -> None:
        finals: list[str] = []
        setup_complete = False
        last_emitted = ""
        while self._is_current(generation):
            raw = await websocket.recv()
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise GeminiProtocolError(
                        "Gemini returned non-UTF-8 data"
                    ) from error
            if not isinstance(raw, str):
                raise GeminiProtocolError("Gemini returned a non-JSON event")
            try:
                document = json.loads(raw)
            except json.JSONDecodeError as error:
                raise GeminiProtocolError("Gemini returned invalid JSON") from error
            if not isinstance(document, dict):
                raise GeminiProtocolError("Gemini event is invalid")
            if "setupComplete" in document:
                if setup_complete:
                    raise GeminiProtocolError("Gemini completed setup more than once")
                setup_complete = True
                started.set()
                self._invoke(self.on_open, generation)
                continue
            if not setup_complete:
                raise GeminiProtocolError("Gemini returned an event before setup")
            content = document.get("serverContent")
            if content is None:
                # voiceActivity, usageMetadata, goAway, ... carry no transcript.
                continue
            if not isinstance(content, dict):
                raise GeminiProtocolError("Gemini server content is invalid")
            partial = ""
            changed = False
            interim = content.get("interimInputTranscription")
            if interim is not None:
                text = interim.get("text") if isinstance(interim, dict) else None
                if not isinstance(text, str):
                    raise GeminiProtocolError("Gemini interim transcript is invalid")
                partial = normalize_transcript_spacing(text)
                changed = True
            final = content.get("inputTranscription")
            if final is not None:
                text = final.get("text") if isinstance(final, dict) else None
                if not isinstance(text, str):
                    raise GeminiProtocolError("Gemini final transcript is invalid")
                finals.append(normalize_transcript_spacing(text))
                partial = ""
                changed = True
            if changed:
                combined = "".join(finals) + partial
                if combined and combined != last_emitted:
                    last_emitted = combined
                    self._invoke(self.on_result, generation, combined)
            if content.get("generationComplete") is True or (
                content.get("turnComplete") is True
            ):
                if not finish_sent.is_set():
                    # Manual activity: only our activityEnd ends the utterance.
                    continue
                final_text = "".join(finals)
                if final_text != last_emitted:
                    self._invoke(self.on_result, generation, final_text)
                self._invoke(self.on_finish, generation)
                return

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
