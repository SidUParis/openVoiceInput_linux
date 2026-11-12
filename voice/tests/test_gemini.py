from __future__ import annotations

import asyncio
import base64
import json
import threading

import pytest

from murmur_voice.config import (
    GEMINI_MAX_VOCABULARY_TERMS,
    CorrectionPair,
    VoiceConfig,
)
from murmur_voice.gemini import (
    GeminiASRClient,
    GeminiProtocolError,
    _is_auth_close,
    build_activity_end,
    build_activity_start,
    build_audio_message,
    build_setup_message,
    normalize_transcript_spacing,
)
from murmur_voice.volcengine import AudioBackpressureError


def _settings(**overrides):
    settings = VoiceConfig("key", provider="gemini").provider_settings()
    settings.update(overrides)
    return settings


def _active_client(generation: int, **overrides) -> GeminiASRClient:
    client = GeminiASRClient(_settings(**overrides))
    client._active = True
    client._generation = generation
    return client


class _ScriptedWebSocket:
    def __init__(self, events):
        self._events = list(events)

    async def recv(self):
        event = self._events.pop(0)
        return event if isinstance(event, (str, bytes)) else json.dumps(event)


def test_gemini_setup_message_matches_reviewed_live_protocol():
    setup = build_setup_message(
        model="gemini-3.5-transcribe-live",
        language_codes=("cmn-Hans-CN", "en-US"),
        vocabulary=("devbox", "chezmoi"),
        mode="VERBATIM",
    )

    assert setup == {
        "setup": {
            "model": "models/gemini-3.5-transcribe-live",
            "generationConfig": {"responseModalities": ["TEXT"]},
            "inputAudioTranscription": {
                "languageCodes": ["cmn-Hans-CN", "en-US"],
                "mode": "VERBATIM",
                "customVocabulary": ["devbox", "chezmoi"],
            },
            "realtimeInputConfig": {"automaticActivityDetection": {"disabled": True}},
        }
    }
    assert (
        "customVocabulary"
        not in build_setup_message(
            model="gemini-3.5-transcribe-live",
            language_codes=(),
            vocabulary=(),
            mode="SMART",
        )["setup"]["inputAudioTranscription"]
    )
    assert build_activity_start() == {"realtimeInput": {"activityStart": {}}}
    assert build_activity_end() == {"realtimeInput": {"activityEnd": {}}}
    audio = build_audio_message(b"\x01\x00\x02\x00")["realtimeInput"]["audio"]
    assert audio["mimeType"] == "audio/pcm;rate=16000"
    assert base64.b64decode(audio["data"]) == b"\x01\x00\x02\x00"


def test_gemini_settings_merge_terms_cap_vocabulary_and_hint_simplified_chinese():
    correction = CorrectionPair("奔驰 mark", "benchmark")
    settings = VoiceConfig(
        "key",
        hotwords=("devbox", "benchmark"),
        corrections=(correction,),
        provider="gemini",
    ).provider_settings()

    assert settings["model"] == "gemini-3.5-transcribe-live"
    assert settings["vocabulary"] == ("devbox", "benchmark")
    assert settings["language_codes"] == ("cmn-Hans-CN", "en-US")
    assert settings["mode"] == "VERBATIM"

    many = tuple(f"term{index}" for index in range(150))
    capped = VoiceConfig("key", hotwords=many, provider="gemini").provider_settings()
    assert len(capped["vocabulary"]) == GEMINI_MAX_VOCABULARY_TERMS
    assert capped["vocabulary"][0] == "term0"


@pytest.mark.parametrize(
    "overrides",
    (
        {"endpoint": "wss://example.invalid/ws"},
        {"model": "gemini-other-live"},
        {"sample_rate": 24_000},
        {"language_codes": ("fr-FR",)},
        {"language_codes": "cmn-Hans-CN"},
        {"mode": "FREESTYLE"},
    ),
)
def test_gemini_client_rejects_unreviewed_settings(overrides):
    with pytest.raises(ValueError):
        GeminiASRClient(_settings(**overrides))


def test_gemini_spacing_normalization_only_joins_cjk_neighbours():
    assert (
        normalize_transcript_spacing("我 今 天 在 devbox 上 用")
        == "我今天在 devbox 上用"
    )
    assert normalize_transcript_spacing("配 置 ， 然 后") == "配置，然后"
    assert normalize_transcript_spacing("push 到 GitHub") == "push 到 GitHub"
    assert normalize_transcript_spacing("unit test cover") == "unit test cover"


def test_gemini_receiver_streams_cumulative_interim_then_final():
    events = [
        {"setupComplete": {}},
        {"serverContent": {}, "voiceActivity": {"type": "ACTIVITY_START"}},
        {"serverContent": {"interimInputTranscription": {"text": "我 今 天"}}},
        {"serverContent": {"interimInputTranscription": {"text": "我今天在 devbox"}}},
        {"serverContent": {"interimInputTranscription": {"text": "我今天在 devbox"}}},
        {"serverContent": {"inputTranscription": {"text": "我今天在 devbox 上。"}}},
        {"serverContent": {"generationComplete": True}},
    ]
    client = _active_client(7)
    callbacks = []
    client.on_open = lambda: callbacks.append("open")
    client.on_result = callbacks.append
    client.on_finish = lambda: callbacks.append("finish")
    started = asyncio.Event()
    finish_sent = asyncio.Event()
    finish_sent.set()

    asyncio.run(client._receive(_ScriptedWebSocket(events), 7, started, finish_sent))

    assert started.is_set()
    assert callbacks == [
        "open",
        "我今天",
        "我今天在 devbox",
        "我今天在 devbox 上。",
        "finish",
    ]


def test_gemini_receiver_joins_several_finals_and_ignores_early_completion():
    events = [
        {"setupComplete": {}},
        {"serverContent": {"inputTranscription": {"text": "第一句。"}}},
        # Before our activityEnd, a completion must not end the utterance.
        {"serverContent": {"generationComplete": True}},
        {"serverContent": {"interimInputTranscription": {"text": " Second"}}},
        {"serverContent": {"inputTranscription": {"text": " Second part."}}},
        {"serverContent": {"turnComplete": True}},
    ]
    client = _active_client(8)
    callbacks = []
    client.on_result = callbacks.append
    client.on_finish = lambda: callbacks.append("finish")
    finish_sent = asyncio.Event()

    class FinishMidwayWebSocket(_ScriptedWebSocket):
        async def recv(self):
            # Our activityEnd goes out after the early completion arrived.
            if len(self._events) == 3:
                finish_sent.set()
            return await super().recv()

    asyncio.run(
        client._receive(FinishMidwayWebSocket(events), 8, asyncio.Event(), finish_sent)
    )

    assert callbacks == [
        "第一句。",
        "第一句。 Second",
        "第一句。 Second part.",
        "finish",
    ]


def test_gemini_completion_without_a_final_keeps_the_shown_interim():
    events = [
        {"setupComplete": {}},
        {"serverContent": {"interimInputTranscription": {"text": "intermediate"}}},
        {"serverContent": {"generationComplete": True}},
    ]
    client = _active_client(9)
    callbacks = []
    client.on_result = callbacks.append
    client.on_finish = lambda: callbacks.append("finish")
    finish_sent = asyncio.Event()
    finish_sent.set()

    asyncio.run(
        client._receive(_ScriptedWebSocket(events), 9, asyncio.Event(), finish_sent)
    )

    assert callbacks == ["intermediate", "finish"]


@pytest.mark.parametrize(
    "events",
    (
        [{"serverContent": {"interimInputTranscription": {"text": "too early"}}}],
        [{"setupComplete": {}}, {"setupComplete": {}}],
        [{"setupComplete": {}}, "not json"],
        [{"setupComplete": {}}, json.dumps([1, 2])],
        [{"setupComplete": {}}, {"serverContent": "invalid"}],
        [{"setupComplete": {}}, {"serverContent": {"inputTranscription": []}}],
        [
            {"setupComplete": {}},
            {"serverContent": {"interimInputTranscription": {"text": 5}}},
        ],
    ),
)
def test_gemini_rejects_events_outside_the_reviewed_protocol(events):
    client = _active_client(10)
    finish_sent = asyncio.Event()
    finish_sent.set()

    with pytest.raises(GeminiProtocolError):
        asyncio.run(
            client._receive(
                _ScriptedWebSocket(events), 10, asyncio.Event(), finish_sent
            )
        )


def test_gemini_sender_waits_for_setup_then_brackets_audio_in_one_activity():
    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            assert isinstance(payload, str)
            document = json.loads(payload)
            if "activityEnd" in document["realtimeInput"]:
                assert finish_sent.is_set()
            self.sent.append(document["realtimeInput"])

    client = _active_client(11)
    client._pending_audio.extend(b"\x01\x00" * 4000)
    client._finish_requested = True
    client._wake_event = asyncio.Event()
    started = asyncio.Event()
    started.set()
    finish_sent = asyncio.Event()
    websocket = FakeWebSocket()

    asyncio.run(client._send_audio(websocket, 11, started, finish_sent))

    assert websocket.sent[0] == {"activityStart": {}}
    assert websocket.sent[-1] == {"activityEnd": {}}
    chunks = [base64.b64decode(item["audio"]["data"]) for item in websocket.sent[1:-1]]
    assert b"".join(chunks) == b"\x01\x00" * 4000
    assert max(len(chunk) for chunk in chunks) == 3200
    assert finish_sent.is_set()


def test_gemini_auth_close_is_recognized_without_keeping_remote_text():
    class Received:
        def __init__(self, reason):
            self.code = 1007
            self.reason = reason

    class Closed(Exception):
        def __init__(self, reason):
            super().__init__("closed")
            self.rcvd = Received(reason)

    assert _is_auth_close(Closed("API key not valid. Please pass a valid API key."))
    assert not _is_auth_close(Closed("models/x is not found for API version v1beta"))
    assert not _is_auth_close(Exception("no close frame"))


def test_gemini_audio_overflow_reports_from_a_non_capture_thread():
    notified = threading.Event()
    errors = []
    callback_threads = []
    capture_thread = threading.get_ident()
    client = GeminiASRClient(_settings())
    client._max_pending_audio_bytes = 3
    client.on_error = lambda error: (
        errors.append(error),
        callback_threads.append(threading.get_ident()),
        notified.set(),
    )
    with client._lock:
        client._generation = 13
        client._active = True

    client.send_audio(b"four")

    assert notified.wait(1)
    assert len(errors) == 1
    assert isinstance(errors[0], AudioBackpressureError)
    assert callback_threads != [capture_thread]
    assert client.pending_audio_bytes == 0


def test_gemini_missing_key_reports_auth_without_starting_network_thread():
    client = GeminiASRClient(_settings(api_key=""))
    called = []
    client.on_auth_error = lambda: called.append("auth")

    client.connect()

    assert called == ["auth"]
    assert client._loop is None
    assert client.is_connected is False


def test_gemini_empty_or_finished_transcription_messages_are_not_errors():
    events = [
        {"setupComplete": {}},
        {"serverContent": {"interimInputTranscription": {}}},
        {"serverContent": {"interimInputTranscription": {"text": "你好"}}},
        {"serverContent": {"inputTranscription": {"text": "你好。"}}},
        {"serverContent": {"inputTranscription": {"finished": True}}},
        {"serverContent": {"generationComplete": True}},
    ]
    client = _active_client(20)
    callbacks = []
    client.on_result = callbacks.append
    client.on_finish = lambda: callbacks.append("finish")
    finish_sent = asyncio.Event()
    finish_sent.set()

    asyncio.run(
        client._receive(_ScriptedWebSocket(events), 20, asyncio.Event(), finish_sent)
    )

    assert callbacks == ["你好", "你好。", "finish"]


def test_gemini_cumulative_interim_after_a_final_is_not_doubled():
    events = [
        {"setupComplete": {}},
        {"serverContent": {"inputTranscription": {"text": "第一句。"}}},
        {"serverContent": {"interimInputTranscription": {"text": "第一句。第 二"}}},
        {"serverContent": {"inputTranscription": {"text": "第二句。"}}},
        {"serverContent": {"generationComplete": True}},
    ]
    client = _active_client(21)
    callbacks = []
    client.on_result = callbacks.append
    client.on_finish = lambda: callbacks.append("finish")
    finish_sent = asyncio.Event()
    finish_sent.set()

    asyncio.run(
        client._receive(_ScriptedWebSocket(events), 21, asyncio.Event(), finish_sent)
    )

    assert callbacks == ["第一句。", "第一句。第二", "第一句。第二句。", "finish"]


def test_gemini_transcript_beyond_the_preedit_limit_fails_closed():
    events = [
        {"setupComplete": {}},
        {"serverContent": {"interimInputTranscription": {"text": "字" * 4097}}},
    ]
    client = _active_client(22)
    finish_sent = asyncio.Event()

    with pytest.raises(GeminiProtocolError):
        asyncio.run(
            client._receive(
                _ScriptedWebSocket(events), 22, asyncio.Event(), finish_sent
            )
        )


def test_gemini_vocabulary_dedupes_case_insensitively_before_the_cap():
    hotwords = ("GitHub", "github", *(f"term{index}" for index in range(120)))
    corrections = (CorrectionPair("奔驰 mark", "benchmark"),)
    settings = VoiceConfig(
        "key", hotwords=hotwords, corrections=corrections, provider="gemini"
    ).provider_settings()

    assert settings["vocabulary"][:2] == ("GitHub", "term0")
    assert len(settings["vocabulary"]) == GEMINI_MAX_VOCABULARY_TERMS
    assert sum(term.casefold() == "github" for term in settings["vocabulary"]) == 1


def test_gemini_rejects_empty_language_codes():
    with pytest.raises(ValueError):
        GeminiASRClient(_settings(language_codes=()))
