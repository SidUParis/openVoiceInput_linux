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
    _UtteranceFrameState,
    _build_header,
    _decode_payload,
    _encode_audio_request,
    _encode_full_request,
    _extract_text,
    _timed_utterances,
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


@pytest.mark.parametrize("result_type", ("incremental", "FULL", "", 1))
def test_unsupported_result_type_is_rejected_at_initialization(result_type):
    settings = VoiceConfig("test-key").provider_settings()
    settings["result_type"] = result_type

    with pytest.raises(ValueError, match="result_type"):
        VolcengineASRClient(settings)


def test_missing_result_type_keeps_legacy_full_default():
    settings = VoiceConfig("test-key").provider_settings()
    del settings["result_type"]

    client = VolcengineASRClient(settings)

    assert client._result_type == "full"
    assert client._build_payload()["request"]["result_type"] == "full"


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


def test_mixed_two_pass_utterances_prefer_definite_and_keep_completed_sentences():
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())
    with client._lock:
        client._generation = 7
        client._active = True
    results = []
    client.on_result = results.append

    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "嗯第一句还没好",
                    "utterances": [
                        {
                            "definite": False,
                            "start_time": 0,
                            "end_time": 800,
                            "text": "嗯第一句还没好",
                        }
                    ],
                }
            },
            sequence=1,
        ),
        7,
    )
    client._handle_message(
        _server_frame(
            {
                "result": {
                    # A connection-level cumulative hypothesis can lag the
                    # two-pass utterance selected by ``definite``.
                    "text": "嗯第一句还没好第二句草稿",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 900,
                            "text": "第一句好了。",
                        },
                        {
                            "definite": False,
                            "start_time": 1000,
                            "end_time": 1500,
                            "text": "第二句草稿",
                        },
                    ],
                }
            },
            sequence=2,
        ),
        7,
    )
    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第一句好了。第二句改好了。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 900,
                            "text": "第一句好了。",
                        },
                        {
                            "definite": True,
                            "start_time": 1000,
                            "end_time": 1600,
                            "text": "第二句改好了。",
                        },
                    ],
                }
            },
            sequence=3,
        ),
        7,
    )

    assert results == [
        "嗯第一句还没好",
        "第一句好了。第二句草稿",
        "第一句好了。第二句改好了。",
    ]


def test_single_result_mode_accumulates_definite_sentences():
    settings = VoiceConfig("test-key").provider_settings()
    settings["result_type"] = "single"
    client = VolcengineASRClient(settings)
    with client._lock:
        client._generation = 7
        client._active = True
    results = []
    client.on_result = results.append

    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第一句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 600,
                            "text": "第一句。",
                        }
                    ],
                }
            },
            sequence=1,
        ),
        7,
    )
    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第二句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 800,
                            "end_time": 1300,
                            "text": "第二句。",
                        }
                    ],
                }
            },
            sequence=2,
        ),
        7,
    )

    assert client._build_payload()["request"]["result_type"] == "single"
    assert results == ["第一句。", "第一句。第二句。"]


def test_empty_terminal_frame_keeps_last_definite_rewrite_before_finish():
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())
    with client._lock:
        client._generation = 7
        client._active = True
    events = []
    client.on_result = lambda text: events.append(("result", text))
    client.on_finish = lambda: events.append(("finish", None))

    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "嗯我们开会",
                    "utterances": [
                        {
                            "definite": False,
                            "start_time": 0,
                            "end_time": 700,
                            "text": "嗯我们开会",
                        }
                    ],
                }
            },
            sequence=1,
        ),
        7,
    )
    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "嗯我们开会",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 700,
                            "text": "我们开会。",
                        }
                    ],
                }
            },
            sequence=2,
        ),
        7,
    )
    terminal = client._handle_message(
        _server_frame({}, sequence=3, final=True),
        7,
    )

    assert terminal
    assert events == [
        ("result", "嗯我们开会"),
        ("result", "我们开会。"),
        ("finish", None),
    ]


def test_repeated_full_utterance_frames_never_duplicate_definite_sentences():
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())
    with client._lock:
        client._generation = 7
        client._active = True
    results = []
    client.on_result = results.append

    first = {
        "definite": True,
        "start_time": 0,
        "end_time": 600,
        "text": "第一句。",
    }
    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第一句。第二句草稿",
                    "utterances": [
                        first,
                        {
                            "definite": False,
                            "start_time": 800,
                            "end_time": 1200,
                            "text": "第二句草稿",
                        },
                    ],
                }
            },
            sequence=1,
        ),
        7,
    )
    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第一句。第二句。",
                    "utterances": [
                        first,
                        {
                            "definite": True,
                            "start_time": 800,
                            "end_time": 1300,
                            "text": "第二句。",
                        },
                    ],
                }
            },
            sequence=2,
        ),
        7,
    )

    assert results == ["第一句。第二句草稿", "第一句。第二句。"]


def test_new_definite_replaces_all_overlapping_previous_time_slots():
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())
    with client._lock:
        client._generation = 7
        client._active = True
    results = []
    client.on_result = results.append

    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第一句。第二句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 600,
                            "text": "第一句。",
                        },
                        {
                            "definite": True,
                            "start_time": 700,
                            "end_time": 1300,
                            "text": "第二句。",
                        },
                    ],
                }
            },
            sequence=1,
        ),
        7,
    )
    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "合并后的二遍句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 50,
                            "end_time": 1250,
                            "text": "合并后的二遍句。",
                        }
                    ],
                }
            },
            sequence=2,
        ),
        7,
    )

    assert results == ["第一句。第二句。", "合并后的二遍句。"]


def test_multiple_new_definites_can_split_one_overlapping_previous_time_slot():
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())
    with client._lock:
        client._generation = 7
        client._active = True
    results = []
    client.on_result = results.append

    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "旧的合并句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 50,
                            "end_time": 1250,
                            "text": "旧的合并句。",
                        }
                    ],
                }
            },
            sequence=1,
        ),
        7,
    )
    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "新的第一句。新的第二句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 750,
                            "text": "新的第一句。",
                        },
                        {
                            "definite": True,
                            "start_time": 700,
                            "end_time": 1300,
                            "text": "新的第二句。",
                        },
                    ],
                }
            },
            sequence=2,
        ),
        7,
    )

    assert results == ["旧的合并句。", "新的第一句。新的第二句。"]


def test_empty_terminal_frame_preserves_last_nondefinite_trailing_sentence():
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())
    with client._lock:
        client._generation = 7
        client._active = True
    events = []
    client.on_result = lambda text: events.append(("result", text))
    client.on_finish = lambda: events.append(("finish", None))

    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第一句。尾句草稿",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 600,
                            "text": "第一句。",
                        },
                        {
                            "definite": False,
                            "start_time": 800,
                            "end_time": 1200,
                            "text": "尾句草稿",
                        },
                    ],
                }
            },
            sequence=1,
        ),
        7,
    )
    terminal = client._handle_message(
        _server_frame({}, sequence=2, final=True),
        7,
    )

    assert terminal
    assert events == [("result", "第一句。尾句草稿"), ("finish", None)]


def test_malformed_mixed_utterance_frame_uses_complete_result_text_fallback():
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())
    with client._lock:
        client._generation = 7
        client._active = True
    results = []
    client.on_result = results.append

    first = {
        "definite": True,
        "start_time": 0,
        "end_time": 600,
        "text": "第一句。",
    }
    client._handle_message(
        _server_frame(
            {"result": {"text": "第一句。", "utterances": [first]}},
            sequence=1,
        ),
        7,
    )
    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第一句。尾句完整回退。",
                    "utterances": [
                        first,
                        {
                            "definite": False,
                            "text": "尾句缺少时间字段。",
                        },
                    ],
                }
            },
            sequence=2,
        ),
        7,
    )
    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "二遍尾句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 800,
                            "end_time": 1300,
                            "text": "二遍尾句。",
                        }
                    ],
                }
            },
            sequence=3,
        ),
        7,
    )

    assert results == [
        "第一句。",
        "第一句。尾句完整回退。",
        "第一句。二遍尾句。",
    ]


def test_malformed_frame_without_full_text_keeps_last_safe_state_unchanged():
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())
    with client._lock:
        client._generation = 7
        client._active = True
    results = []
    client.on_result = results.append

    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "旧第一句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 600,
                            "text": "旧第一句。",
                        }
                    ],
                }
            },
            sequence=1,
        ),
        7,
    )
    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 600,
                            "text": "不应吸收的新第一句。",
                        },
                        {
                            "definite": False,
                            "start_time": "invalid",
                            "end_time": 1200,
                            "text": "坏尾句",
                        },
                    ],
                }
            },
            sequence=2,
        ),
        7,
    )
    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第二句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 800,
                            "end_time": 1300,
                            "text": "第二句。",
                        }
                    ],
                }
            },
            sequence=3,
        ),
        7,
    )

    assert results == ["旧第一句。", "旧第一句。第二句。"]


def test_full_malformed_terminal_frame_uses_cumulative_fallback_then_finishes():
    settings = VoiceConfig("test-key").provider_settings()
    settings["result_type"] = "full"
    client = VolcengineASRClient(settings)
    with client._lock:
        client._generation = 7
        client._active = True
    events = []
    client.on_result = lambda text: events.append(("result", text))
    client.on_finish = lambda: events.append(("finish", None))

    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第一句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 600,
                            "text": "第一句。",
                        }
                    ],
                }
            },
            sequence=1,
        ),
        7,
    )
    terminal = client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第一句。完整尾句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 600,
                            "text": "第一句。",
                        },
                        {"definite": False, "text": "缺时间字段"},
                    ],
                }
            },
            sequence=2,
            final=True,
        ),
        7,
    )

    assert terminal
    assert events == [
        ("result", "第一句。"),
        ("result", "第一句。完整尾句。"),
        ("finish", None),
    ]


def test_single_malformed_terminal_before_definite_errors_without_finish(caplog):
    settings = VoiceConfig("test-key").provider_settings()
    settings["result_type"] = "single"
    client = VolcengineASRClient(settings)
    with client._lock:
        client._generation = 7
        client._active = True
    results = []
    errors = []
    finishes = []
    client.on_result = results.append
    client.on_error = errors.append
    client.on_finish = lambda: finishes.append(True)

    with caplog.at_level(logging.ERROR):
        terminal = client._handle_message(
            _server_frame(
                {
                    "result": {
                        "text": "PRIVATE-SINGLE-FALLBACK",
                        "utterances": [{"definite": False, "text": "缺时间字段"}],
                    }
                },
                sequence=1,
                final=True,
            ),
            7,
        )

    assert terminal
    assert results == []
    assert finishes == []
    assert len(errors) == 1
    assert isinstance(errors[0], VolcengineProtocolError)
    assert "PRIVATE-SINGLE-FALLBACK" not in str(errors[0])
    assert "PRIVATE-SINGLE-FALLBACK" not in caplog.text


def test_single_malformed_terminal_after_definite_preserves_assembly_state():
    settings = VoiceConfig("test-key").provider_settings()
    settings["result_type"] = "single"
    client = VolcengineASRClient(settings)
    with client._lock:
        client._generation = 7
        client._active = True
    results = []
    errors = []
    finishes = []
    client.on_result = results.append
    client.on_error = errors.append
    client.on_finish = lambda: finishes.append(True)

    client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第一句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 600,
                            "text": "第一句。",
                        }
                    ],
                }
            },
            sequence=1,
        ),
        7,
    )
    terminal = client._handle_message(
        _server_frame(
            {
                "result": {
                    "text": "第二句但不可安全拼接。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": "invalid",
                            "end_time": 1300,
                            "text": "第二句但不可安全拼接。",
                        }
                    ],
                }
            },
            sequence=2,
            final=True,
        ),
        7,
    )

    assert terminal
    assert results == ["第一句。"]
    assert finishes == []
    assert len(errors) == 1
    assert isinstance(errors[0], VolcengineProtocolError)

    # The malformed terminal ended the real receive loop. Calling the pure
    # assembler here only proves that its prior definite state was not mutated.
    assert (
        client._result_assembler.update(
            {
                "result": {
                    "text": "第三句。",
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 1400,
                            "end_time": 1900,
                            "text": "第三句。",
                        }
                    ],
                }
            }
        )
        == "第一句。第三句。"
    )


def test_result_text_only_frames_remain_backward_compatible():
    client = VolcengineASRClient(VoiceConfig("test-key").provider_settings())
    with client._lock:
        client._generation = 7
        client._active = True
    events = []
    client.on_result = lambda text: events.append(("result", text))
    client.on_finish = lambda: events.append(("finish", None))

    client._handle_message(
        _server_frame({"result": {"text": "legacy partial"}}, sequence=1),
        7,
    )
    client._handle_message(
        _server_frame({"result": {"text": "legacy final"}}, sequence=2, final=True),
        7,
    )

    assert events == [
        ("result", "legacy partial"),
        ("result", "legacy final"),
        ("finish", None),
    ]


def test_incremental_definite_assembly_is_bounded_across_frames(caplog):
    settings = VoiceConfig("test-key").provider_settings()
    settings["result_type"] = "single"
    client = VolcengineASRClient(settings)
    with client._lock:
        client._generation = 7
        client._active = True
    results = []
    errors = []
    client.on_result = results.append
    client.on_error = errors.append

    client._handle_message(
        _server_frame(
            {
                "result": {
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 1,
                            "text": "x" * 4096,
                        }
                    ]
                }
            },
            sequence=1,
        ),
        7,
    )
    with caplog.at_level(logging.ERROR):
        rejected = client._handle_message(
            _server_frame(
                {
                    "result": {
                        "utterances": [
                            {
                                "definite": True,
                                "start_time": 2,
                                "end_time": 3,
                                "text": "PRIVATE-OVERFLOW-TAIL",
                            }
                        ]
                    }
                },
                sequence=2,
            ),
            7,
        )

    assert rejected
    assert len(results) == 1
    assert len(results[0]) == 4096
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "safe limit" in str(errors[0])
    assert "PRIVATE-OVERFLOW-TAIL" not in caplog.text


def test_result_extraction_supports_cumulative_and_utterance_forms():
    assert _extract_text({"result": {"text": "完整"}}) == "完整"
    assert (
        _extract_text({"result": {"utterances": [{"text": "一"}, {"text": "二"}]}})
        == "一二"
    )


@pytest.mark.parametrize(
    ("payload", "expected_state"),
    (
        ({"result": {"text": "legacy"}}, _UtteranceFrameState.ABSENT),
        ({"result": {"utterances": []}}, _UtteranceFrameState.VALID),
        (
            {
                "result": {
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 1,
                            "text": "valid",
                        }
                    ]
                }
            },
            _UtteranceFrameState.VALID,
        ),
        (
            {
                "result": {
                    "utterances": [
                        {
                            "definite": True,
                            "start_time": 0,
                            "end_time": 1,
                            "text": "valid",
                        },
                        {"definite": False, "text": "missing times"},
                    ]
                }
            },
            _UtteranceFrameState.MALFORMED,
        ),
        (
            {"result": {"utterances": "not-a-list"}},
            _UtteranceFrameState.MALFORMED,
        ),
    ),
)
def test_utterance_parser_distinguishes_absent_valid_and_malformed(
    payload, expected_state
):
    assert _timed_utterances(payload).state is expected_state


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
