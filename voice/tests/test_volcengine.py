from __future__ import annotations

import gzip
import json
import logging
import struct
import threading

import pytest

from murmur_voice.config import CorrectionPair, VoiceConfig
from murmur_voice.volcengine import (
    AudioBackpressureError,
    VolcengineASRClient,
    VolcengineProtocolError,
    _COMPRESSION_GZIP,
    _LAST_WITH_SEQUENCE,
    _SERIALIZATION_JSON,
    _SERVER_FULL_RESPONSE,
    _build_header,
    _decode_payload,
    _encode_audio_request,
    _encode_full_request,
    _extract_text,
)


def _server_frame(payload, *, sequence=1, final=False):
    raw = gzip.compress(
        json.dumps(payload, ensure_ascii=False).encode("utf-8"), mtime=0
    )
    flags = _LAST_WITH_SEQUENCE if final else 0b0001
    return b"".join(
        (
            _build_header(
                _SERVER_FULL_RESPONSE,
                flags,
                _SERIALIZATION_JSON,
                _COMPRESSION_GZIP,
            ),
            struct.pack(">iI", -abs(sequence) if final else sequence, len(raw)),
            raw,
        )
    )


def test_default_payload_and_api_key_headers():
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())

    assert client._chunk_bytes == 6400
    assert client._build_headers("id") == {
        "X-Api-Key": "test-key",
        "X-Api-Resource-Id": "volc.seedasr.sauc.duration",
        "X-Api-Connect-Id": "id",
        "X-Api-Sequence": "-1",
    }
    payload = client._build_payload()
    assert payload["audio"] == {
        "format": "pcm",
        "codec": "raw",
        "rate": 16000,
        "bits": 16,
        "channel": 1,
    }
    assert payload["request"]["enable_nonstream"] is True
    assert payload["request"]["enable_ddc"] is True
    assert "context" not in payload["request"]


def test_personal_vocabulary_uses_official_request_context_json(caplog):
    client = VolcengineASRClient(
        VoiceConfig("test-key", ("PrivateName", "专业词")).provider_settings()
    )

    with caplog.at_level(logging.DEBUG):
        payload = client._build_payload()

    context = payload["request"]["context"]
    assert isinstance(context, str)
    assert json.loads(context) == {
        "hotwords": [{"word": "PrivateName"}, {"word": "专业词"}]
    }
    assert "PrivateName" not in caplog.text
    assert "专业词" not in caplog.text


def test_explicit_corrections_use_official_correct_words_context_json(caplog):
    private_wrong = "deep seek"
    private_canonical = "DeepSeek"
    client = VolcengineASRClient(
        VoiceConfig(
            "test-key",
            corrections=(CorrectionPair(private_wrong, private_canonical),),
        ).provider_settings()
    )

    with caplog.at_level(logging.DEBUG):
        payload = client._build_payload()

    context = payload["request"]["context"]
    assert isinstance(context, str)
    assert context == '{"correct_words":{"deep seek":"DeepSeek"}}'
    assert json.loads(context) == {"correct_words": {private_wrong: private_canonical}}
    assert private_wrong not in caplog.text
    assert private_canonical not in caplog.text


def test_hotwords_and_corrections_share_one_compact_context_string(caplog):
    private_hotword = "PrivateName"
    private_wrong = "欧盆爱"
    private_canonical = "OpenAI"
    client = VolcengineASRClient(
        VoiceConfig(
            "test-key",
            (private_hotword,),
            (CorrectionPair(private_wrong, private_canonical),),
        ).provider_settings()
    )

    with caplog.at_level(logging.DEBUG):
        payload = client._build_payload()

    context = payload["request"]["context"]
    assert context == (
        '{"hotwords":[{"word":"PrivateName"}],"correct_words":{"欧盆爱":"OpenAI"}}'
    )
    assert json.loads(context) == {
        "hotwords": [{"word": private_hotword}],
        "correct_words": {private_wrong: private_canonical},
    }
    assert private_hotword not in caplog.text
    assert private_wrong not in caplog.text
    assert private_canonical not in caplog.text


def test_client_never_applies_a_local_correction_to_provider_results():
    client = VolcengineASRClient(
        VoiceConfig(
            "test-key",
            corrections=(CorrectionPair("wrong form", "canonical form"),),
        ).provider_settings()
    )
    with client._lock:
        client._generation = 9
        client._active = True
    events = []
    client.on_result = events.append

    assert client._handle_message(
        _server_frame(
            {"result": {"text": "wrong form remains"}},
            final=True,
        ),
        9,
    )
    assert events == ["wrong form remains"]


def test_request_and_audio_binary_envelopes():
    request = _encode_full_request({"audio": {"format": "pcm"}})
    audio = _encode_audio_request(b"pcm")
    final = _encode_audio_request(b"last", final=True)

    assert request[:4] == bytes((0x11, 0x10, 0x10, 0x00))
    assert audio[:4] == bytes((0x11, 0x20, 0x00, 0x00))
    assert final[:4] == bytes((0x11, 0x22, 0x00, 0x00))


def test_final_frame_delivers_rewrite_before_one_finish():
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())
    with client._lock:
        client._generation = 7
        client._active = True
    events = []
    client.on_result = lambda text: events.append(("result", text))
    client.on_finish = lambda: events.append(("finish", None))

    assert client._handle_message(
        _server_frame({"result": {"text": "最终文本"}}, final=True), 7
    )
    assert events == [("result", "最终文本"), ("finish", None)]


def test_result_extraction_supports_cumulative_and_utterance_forms():
    assert _extract_text({"result": {"text": "完整"}}) == "完整"
    assert (
        _extract_text({"result": {"utterances": [{"text": "一"}, {"text": "二"}]}})
        == "一二"
    )


def test_pending_audio_is_bounded_and_reports_backpressure():
    settings = VoiceConfig("test-key").provider_settings()
    settings["max_pending_audio_seconds"] = 1
    client = VolcengineASRClient(settings)
    client._max_pending_audio_bytes = 3
    notified = threading.Event()
    errors = []
    client.on_error = lambda error: (errors.append(error), notified.set())
    with client._lock:
        client._generation = 9
        client._active = True

    client.send_audio(b"four")

    assert notified.wait(1)
    assert len(errors) == 1
    assert isinstance(errors[0], AudioBackpressureError)
    assert client.pending_audio_bytes == 0


def test_gzip_payload_expansion_is_bounded():
    compressed = gzip.compress(b"x" * (8 * 1024 * 1024 + 1), mtime=0)

    with pytest.raises(ValueError, match="size limit"):
        _decode_payload(_SERIALIZATION_JSON, _COMPRESSION_GZIP, compressed)


def test_remote_payload_is_not_logged(caplog):
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())
    with client._lock:
        client._generation = 4
        client._active = True
    client.on_error = lambda error: None

    with caplog.at_level(logging.ERROR):
        client._handle_message("not-json-TOP-SECRET-TEXT", 4)

    assert "TOP-SECRET-TEXT" not in caplog.text


def test_shared_error_callback_still_receives_the_correct_signature():
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())
    with client._lock:
        client._generation = 5
        client._active = True
    calls = []

    def callback(*arguments):
        calls.append(arguments)

    client.on_error = callback
    client.on_auth_error = callback

    network_error = RuntimeError("connection failed")
    client._notify_error(network_error, 5)
    client._notify_error(VolcengineProtocolError(401), 5)

    assert calls == [(network_error,), ()]
