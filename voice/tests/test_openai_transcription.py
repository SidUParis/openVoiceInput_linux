from __future__ import annotations

import json
import threading
import wave

import pytest

from murmur_voice.config import VoiceConfig
from murmur_voice.openai_transcription import (
    OpenAITranscriptionClient,
    OpenAITranscriptionError,
)
from murmur_voice.volcengine import AudioBackpressureError


class _Response:
    def __init__(self, document=None, *, payload: bytes | None = None):
        self._payload = (
            payload if payload is not None else json.dumps(document).encode()
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        assert len(self._payload) < limit
        return self._payload


def test_openai_batch_client_posts_bounded_wav_and_returns_final(tmp_path):
    requests = []

    def urlopen(request, *, timeout):
        requests.append((request, timeout))
        return _Response({"text": "中英 French result"})

    settings = VoiceConfig(
        "private-key",
        hotwords=("Austral",),
        provider="openai",
    ).provider_settings()
    client = OpenAITranscriptionClient(settings, urlopen=urlopen)
    opened = threading.Event()
    finished = threading.Event()
    results = []
    errors = []
    client.on_open = opened.set
    client.on_result = results.append
    client.on_finish = finished.set
    client.on_error = errors.append

    client.connect()
    assert opened.wait(1)
    client.send_audio(b"\x01\x00" * 1600)
    client.finish_sending()

    assert finished.wait(2)
    assert results == ["中英 French result"]
    assert errors == []
    request, timeout = requests[0]
    assert timeout == settings["final_result_timeout"]
    assert request.full_url.endswith("/v1/audio/transcriptions")
    assert request.get_header("Authorization") == "Bearer private-key"
    assert b"gpt-4o-mini-transcribe" in request.data
    assert "private-key" not in repr(client)
    wav_start = request.data.index(b"RIFF")
    wav_end = request.data.index(b"\r\n--murmur-", wav_start)
    wav_path = tmp_path / "captured.wav"
    wav_path.write_bytes(request.data[wav_start:wav_end])
    with wave.open(str(wav_path), "rb") as audio:
        assert audio.getframerate() == 16_000
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getnframes() == 1600


def test_openai_client_disconnect_drops_late_response():
    release = threading.Event()
    callbacks = []

    def urlopen(_request, *, timeout):
        assert timeout > 0
        release.wait(1)
        return _Response({"text": "late"})

    client = OpenAITranscriptionClient(
        VoiceConfig("key", provider="openai").provider_settings(),
        urlopen=urlopen,
    )
    client.on_open = lambda: None
    client.on_result = callbacks.append
    client.on_finish = lambda: callbacks.append("finish")
    client.connect()
    client.send_audio(b"\x00\x00" * 100)
    client.finish_sending()
    client.disconnect()
    release.set()

    assert callbacks == []


@pytest.mark.parametrize(
    "payload",
    (
        b"not-json",
        b"{}",
        b'{"text":""}',
        b'{"text":"valid","unexpected":"remote detail"}',
    ),
)
def test_openai_malformed_or_empty_response_reports_error(payload):
    notified = threading.Event()
    finished = threading.Event()
    errors = []

    def urlopen(_request, *, timeout):
        assert timeout > 0
        return _Response(payload=payload)

    client = OpenAITranscriptionClient(
        VoiceConfig("key", provider="openai").provider_settings(),
        urlopen=urlopen,
    )
    client.on_error = lambda error: (errors.append(error), notified.set())
    client.on_finish = finished.set

    client.connect()
    client.send_audio(b"\x00\x00" * 100)
    client.finish_sending()

    assert notified.wait(1)
    assert not finished.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], OpenAITranscriptionError)
    assert "remote detail" not in str(errors[0])


def test_openai_audio_overflow_reports_from_a_non_capture_thread():
    notified = threading.Event()
    errors = []
    callback_threads = []
    capture_thread = threading.get_ident()
    client = OpenAITranscriptionClient(
        VoiceConfig("key", provider="openai").provider_settings()
    )
    client._max_audio_bytes = 3
    client.on_error = lambda error: (
        errors.append(error),
        callback_threads.append(threading.get_ident()),
        notified.set(),
    )

    client.connect()
    client.send_audio(b"four")

    assert notified.wait(1)
    assert len(errors) == 1
    assert isinstance(errors[0], AudioBackpressureError)
    assert callback_threads != [capture_thread]
    assert client.pending_audio_bytes == 0
    client.disconnect()


def test_openai_allows_only_one_in_flight_upload():
    first_entered = threading.Event()
    release_first = threading.Event()
    first_finished = threading.Event()
    second_error = threading.Event()
    first_requests = []
    second_requests = []
    second_errors = []

    def first_urlopen(request, *, timeout):
        assert timeout > 0
        first_requests.append(request)
        first_entered.set()
        assert release_first.wait(1)
        return _Response({"text": "first"})

    def second_urlopen(request, *, timeout):
        second_requests.append((request, timeout))
        return _Response({"text": "second"})

    first = OpenAITranscriptionClient(
        VoiceConfig("key", provider="openai").provider_settings(),
        urlopen=first_urlopen,
    )
    second = OpenAITranscriptionClient(
        VoiceConfig("key", provider="openai").provider_settings(),
        urlopen=second_urlopen,
    )
    first.on_finish = first_finished.set
    second.on_error = lambda error: (second_errors.append(error), second_error.set())

    first.connect()
    first.send_audio(b"\x00\x00" * 100)
    first.finish_sending()
    assert first_entered.wait(1)

    second.connect()
    second.send_audio(b"\x00\x00" * 100)
    second.finish_sending()

    assert second_error.wait(1)
    assert second_requests == []
    assert len(second_errors) == 1
    assert isinstance(second_errors[0], OpenAITranscriptionError)
    assert "previous transcription" in str(second_errors[0])

    release_first.set()
    assert first_finished.wait(1)
    assert len(first_requests) == 1
    second.disconnect()


def test_openai_disconnect_before_network_upload_prevents_request():
    build_entered = threading.Event()
    release_build = threading.Event()
    worker_done = threading.Event()
    requests = []
    callbacks = []

    def urlopen(request, *, timeout):
        requests.append((request, timeout))
        return _Response({"text": "must not be delivered"})

    client = OpenAITranscriptionClient(
        VoiceConfig("key", provider="openai").provider_settings(),
        urlopen=urlopen,
    )
    original_build = client._build_request
    original_transcribe = client._transcribe

    def blocked_build(pcm):
        build_entered.set()
        assert release_build.wait(1)
        return original_build(pcm)

    def observed_transcribe(generation, pcm):
        try:
            original_transcribe(generation, pcm)
        finally:
            worker_done.set()

    client._build_request = blocked_build
    client._transcribe = observed_transcribe
    client.on_result = callbacks.append
    client.on_finish = lambda: callbacks.append("finish")
    client.on_error = callbacks.append
    client.connect()
    client.send_audio(b"\x00\x00" * 100)
    client.finish_sending()
    assert build_entered.wait(1)

    client.disconnect()
    release_build.set()

    assert worker_done.wait(1)
    assert requests == []
    assert callbacks == []
