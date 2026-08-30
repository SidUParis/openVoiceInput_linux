"""Bounded stop-to-final client for OpenAI's audio transcription endpoint."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
import uuid
import wave
from collections.abc import Callable
from typing import Any

from .config import OPENAI_ENDPOINT, OPENAI_MODELS, normalize_vocabulary_terms
from .volcengine import AudioBackpressureError

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_UPLOAD_GATE = threading.BoundedSemaphore(1)


class OpenAITranscriptionError(RuntimeError):
    """A safe provider failure that never embeds the response or API key."""


class OpenAITranscriptionClient:
    """Collect one bounded PCM utterance and transcribe it after stop.

    The network call runs on a private daemon thread.  This backend therefore
    has no partial results, but it implements the same callback surface as the
    streaming clients consumed by :class:`VoiceSession`.
    """

    is_streaming = False
    waits_for_final_event = True
    provider_name = "openai"
    provider_resource_id = None

    def __init__(
        self,
        settings: dict[str, Any],
        *,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        settings = settings if isinstance(settings, dict) else {}
        self._api_key = str(settings.get("api_key") or "").strip()
        self._endpoint = str(settings.get("endpoint") or OPENAI_ENDPOINT)
        self._model = str(settings.get("model") or "")
        self.provider_model = self._model
        if self._endpoint != OPENAI_ENDPOINT:
            raise ValueError(
                "only the reviewed OpenAI transcription endpoint is supported"
            )
        if self._model not in OPENAI_MODELS:
            raise ValueError("OpenAI transcription model is unsupported")
        self._prompt_terms = normalize_vocabulary_terms(
            settings.get("prompt_terms", ())
        )
        self.final_result_timeout = max(
            5.0, min(120.0, float(settings.get("final_result_timeout") or 60.0))
        )
        seconds = max(
            1.0, min(600.0, float(settings.get("max_audio_seconds") or 600.0))
        )
        self._max_audio_bytes = int(SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH * seconds)
        self._urlopen = urlopen or urllib.request.urlopen

        self._lock = threading.RLock()
        self._audio = bytearray()
        self._active = False
        self._finish_requested = False
        self._generation = 0

        self.on_open: Callable[[], None] | None = None
        self.on_result: Callable[[str], None] | None = None
        self.on_finish: Callable[[], None] | None = None
        self.on_error: Callable[[BaseException], None] | None = None
        self.on_auth_error: Callable[[], None] | None = None

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._active

    @property
    def pending_audio_bytes(self) -> int:
        with self._lock:
            return len(self._audio)

    def connect(self) -> None:
        self.disconnect()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._audio.clear()
            self._active = True
            self._finish_requested = False
        if not self._api_key:
            self._notify_auth(generation)
            with self._lock:
                if generation == self._generation:
                    self._active = False
            return
        threading.Thread(
            target=self._invoke,
            args=(self.on_open, generation),
            name="murmur-openai-open",
            daemon=True,
        ).start()

    def send_audio(self, data: bytes) -> None:
        if not data:
            return
        overflow = False
        with self._lock:
            if not self._active or self._finish_requested:
                return
            if len(self._audio) + len(data) > self._max_audio_bytes:
                self._finish_requested = True
                overflow = True
                generation = self._generation
            else:
                self._audio.extend(data)
                return
        if overflow:
            threading.Thread(
                target=self._invoke,
                args=(self.on_error, generation, AudioBackpressureError()),
                name="murmur-openai-overflow",
                daemon=True,
            ).start()

    def finish_sending(self) -> None:
        with self._lock:
            if not self._active or self._finish_requested:
                return
            self._finish_requested = True
            generation = self._generation
            pcm = bytes(self._audio)
            self._audio.clear()
        if not _UPLOAD_GATE.acquire(blocking=False):
            threading.Thread(
                target=self._notify_error,
                args=(
                    OpenAITranscriptionError(
                        "a previous transcription is still active"
                    ),
                    generation,
                ),
                name="murmur-openai-busy",
                daemon=True,
            ).start()
            return
        try:
            threading.Thread(
                target=self._transcribe,
                args=(generation, pcm),
                name="murmur-openai-transcribe",
                daemon=True,
            ).start()
        except Exception:
            _UPLOAD_GATE.release()
            raise

    def disconnect(self) -> None:
        with self._lock:
            self._generation += 1
            self._active = False
            self._finish_requested = False
            self._audio.clear()

    def _transcribe(self, generation: int, pcm: bytes) -> None:
        try:
            if not self._is_current(generation):
                return
            if not pcm:
                raise OpenAITranscriptionError("recording is empty")
            request = self._build_request(pcm)
            if not self._is_current(generation):
                return
            with self._urlopen(request, timeout=self.final_result_timeout) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise OpenAITranscriptionError("provider response is too large")
            document = json.loads(body.decode("utf-8"))
            if not isinstance(document, dict) or set(document) - {
                "text",
                "usage",
                "logprobs",
            }:
                raise OpenAITranscriptionError(
                    "provider response has an unsupported shape"
                )
            text = document.get("text")
            if not isinstance(text, str) or not text.strip():
                raise OpenAITranscriptionError(
                    "provider response contains no transcript"
                )
            self._invoke(self.on_result, generation, text.strip())
            self._invoke(self.on_finish, generation)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                self._notify_auth(generation)
            else:
                self._notify_error(
                    OpenAITranscriptionError("provider request failed"), generation
                )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            OSError,
            ValueError,
            OpenAITranscriptionError,
        ) as error:
            if isinstance(error, OpenAITranscriptionError):
                safe_error = error
            else:
                safe_error = OpenAITranscriptionError("provider request failed")
            self._notify_error(safe_error, generation)
        finally:
            _UPLOAD_GATE.release()
            with self._lock:
                if generation == self._generation:
                    self._active = False

    def _build_request(self, pcm: bytes) -> urllib.request.Request:
        boundary = f"murmur-{uuid.uuid4().hex}"
        wav = _pcm_to_wav(pcm)
        fields = [("model", self._model)]
        if self._prompt_terms:
            fields.append(("prompt", "，".join(self._prompt_terms)))
        parts: list[bytes] = []
        for name, value in fields:
            parts.extend(
                (
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                )
            )
        parts.extend(
            (
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="file"; filename="utterance.wav"\r\n',
                b"Content-Type: audio/wav\r\n\r\n",
                wav,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            )
        )
        return urllib.request.Request(
            self._endpoint,
            data=b"".join(parts),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
            method="POST",
        )

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and self._active

    def _notify_auth(self, generation: int) -> None:
        self._invoke(self.on_auth_error, generation)

    def _notify_error(self, error: BaseException, generation: int) -> None:
        self._invoke(self.on_error, generation, error)

    def _invoke(
        self, callback: Callable[..., None] | None, generation: int, *args: Any
    ) -> None:
        if callback is not None and self._is_current(generation):
            callback(*args)


def _pcm_to_wav(pcm: bytes) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(CHANNELS)
        output.setsampwidth(SAMPLE_WIDTH)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)
    return stream.getvalue()
