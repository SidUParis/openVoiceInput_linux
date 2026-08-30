from __future__ import annotations

import asyncio
import json
import threading

import pytest

from murmur_voice.config import VoiceConfig
from murmur_voice.qwen import (
    QwenASRClient,
    QwenProtocolError,
    build_finish_task,
    build_run_task,
)
from murmur_voice.volcengine import AudioBackpressureError


def test_qwen_run_and_finish_events_match_reviewed_protocol():
    task_id = "synthetic-task-id-1"
    run = build_run_task(
        task_id,
        model="qwen-audio-3.0-asr-flash-streaming",
        sample_rate=16_000,
        language_hints=("zh", "en", "fr"),
        vocabulary=("Austral", "benchmark"),
    )

    assert run["header"] == {
        "action": "run-task",
        "task_id": task_id,
        "streaming": "duplex",
    }
    assert run["payload"]["task_group"] == "audio"
    assert run["payload"]["task"] == "asr"
    assert run["payload"]["function"] == "recognition"
    assert run["payload"]["parameters"] == {
        "format": "pcm",
        "sample_rate": 16_000,
        "language_hints": ["zh", "en", "fr"],
        "vocabulary": {"Austral": 5, "benchmark": 5},
    }
    assert run["payload"]["input"] == {}
    assert build_finish_task(task_id)["payload"] == {"input": {}}


def test_qwen_server_events_assemble_final_and_partial_text():
    task_id = "synthetic-task-id-2"
    events = [
        {
            "header": {"task_id": task_id, "event": "task-started"},
            "payload": {},
        },
        {
            "header": {"task_id": task_id, "event": "result-generated"},
            "payload": {
                "output": {
                    "sentence": {
                        "sentence_id": 1,
                        "sentence_end": True,
                        "text": "你好，",
                    }
                }
            },
        },
        {
            "header": {"task_id": task_id, "event": "result-generated"},
            "payload": {
                "output": {
                    "sentence": {
                        "sentence_id": 2,
                        "sentence_end": False,
                        "text": "bonjour",
                    }
                }
            },
        },
        {
            "header": {"task_id": task_id, "event": "task-finished"},
            "payload": {},
        },
    ]

    class FakeWebSocket:
        async def recv(self):
            import json

            return json.dumps(events.pop(0))

    client = QwenASRClient(VoiceConfig("key", provider="qwen").provider_settings())
    client._active = True
    client._generation = 7
    callbacks = []
    client.on_open = lambda: callbacks.append("open")
    client.on_result = lambda text: callbacks.append(text)
    client.on_finish = lambda: callbacks.append("finish")
    finish_sent = asyncio.Event()
    finish_sent.set()

    asyncio.run(
        client._receive(FakeWebSocket(), task_id, 7, asyncio.Event(), finish_sent)
    )

    assert callbacks == ["open", "你好，", "你好，bonjour", "你好，", "finish"]


def test_qwen_sender_waits_for_start_then_drains_audio_before_finish():
    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            if isinstance(payload, str) and "finish-task" in payload:
                assert finish_sent.is_set()
            self.sent.append(payload)

    client = QwenASRClient(VoiceConfig("key", provider="qwen").provider_settings())
    client._active = True
    client._generation = 9
    client._pending_audio.extend(b"\x01\x00" * 4000)
    client._finish_requested = True
    client._wake_event = asyncio.Event()
    started = asyncio.Event()
    started.set()
    finish_sent = asyncio.Event()
    websocket = FakeWebSocket()

    asyncio.run(client._send_audio(websocket, "task-id", 9, started, finish_sent))

    assert b"".join(item for item in websocket.sent if isinstance(item, bytes)) == (
        b"\x01\x00" * 4000
    )
    finish = json.loads(websocket.sent[-1])
    assert finish["header"]["action"] == "finish-task"
    assert finish["header"]["task_id"] == "task-id"
    assert finish_sent.is_set()


@pytest.mark.parametrize(
    ("events", "mark_finish_sent"),
    (
        (
            [
                {
                    "header": {"task_id": "task-id", "event": "task-finished"},
                    "payload": {},
                }
            ],
            True,
        ),
        (
            [
                {
                    "header": {"task_id": "task-id", "event": "task-started"},
                    "payload": {},
                },
                {
                    "header": {"task_id": "task-id", "event": "task-finished"},
                    "payload": {},
                },
            ],
            False,
        ),
        (
            [
                {
                    "header": {
                        "task_id": "task-id",
                        "event": "result-generated",
                    },
                    "payload": {
                        "output": {
                            "sentence": {
                                "sentence_id": 1,
                                "sentence_end": True,
                                "text": "too early",
                            }
                        }
                    },
                }
            ],
            True,
        ),
    ),
)
def test_qwen_rejects_events_outside_the_task_lifecycle(events, mark_finish_sent):
    class FakeWebSocket:
        async def recv(self):
            return json.dumps(events.pop(0))

    client = QwenASRClient(VoiceConfig("key", provider="qwen").provider_settings())
    client._active = True
    client._generation = 11
    finish_sent = asyncio.Event()
    if mark_finish_sent:
        finish_sent.set()

    with pytest.raises(QwenProtocolError):
        asyncio.run(
            client._receive(
                FakeWebSocket(),
                "task-id",
                11,
                asyncio.Event(),
                finish_sent,
            )
        )


def test_qwen_only_partial_result_is_cleared_before_finish():
    task_id = "task-id"
    events = [
        {
            "header": {"task_id": task_id, "event": "task-started"},
            "payload": {},
        },
        {
            "header": {"task_id": task_id, "event": "result-generated"},
            "payload": {
                "output": {
                    "sentence": {
                        "sentence_id": 1,
                        "sentence_end": False,
                        "text": "intermediate only",
                    }
                }
            },
        },
        {
            "header": {"task_id": task_id, "event": "task-finished"},
            "payload": {},
        },
    ]

    class FakeWebSocket:
        async def recv(self):
            return json.dumps(events.pop(0))

    client = QwenASRClient(VoiceConfig("key", provider="qwen").provider_settings())
    client._active = True
    client._generation = 12
    callbacks = []
    client.on_result = callbacks.append
    client.on_finish = lambda: callbacks.append("finish")
    finish_sent = asyncio.Event()
    finish_sent.set()

    asyncio.run(
        client._receive(FakeWebSocket(), task_id, 12, asyncio.Event(), finish_sent)
    )

    assert callbacks == ["intermediate only", "", "finish"]


def test_qwen_audio_overflow_reports_from_a_non_capture_thread():
    notified = threading.Event()
    errors = []
    callback_threads = []
    capture_thread = threading.get_ident()
    client = QwenASRClient(VoiceConfig("key", provider="qwen").provider_settings())
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


def test_qwen_missing_key_reports_auth_without_starting_network_thread():
    client = QwenASRClient(
        {**VoiceConfig("key", provider="qwen").provider_settings(), "api_key": ""}
    )
    called = []
    client.on_auth_error = lambda: called.append("auth")

    client.connect()

    assert called == ["auth"]
    assert client._loop is None
    assert client.is_connected is False
