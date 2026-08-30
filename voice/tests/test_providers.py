from __future__ import annotations

import pytest

from murmur_voice.config import VoiceConfig
from murmur_voice.openai_transcription import OpenAITranscriptionClient
from murmur_voice.providers import (
    PROVIDER_DESCRIPTORS,
    create_asr_client,
    provider_descriptor,
)
from murmur_voice.qwen import QwenASRClient
from murmur_voice.volcengine import VolcengineASRClient


def test_provider_registry_has_stable_unique_capabilities():
    assert [item.provider_id for item in PROVIDER_DESCRIPTORS] == [
        "volcengine",
        "qwen",
        "openai",
        "minimax",
    ]
    assert len({item.provider_id for item in PROVIDER_DESCRIPTORS}) == 4
    assert provider_descriptor("volcengine").availability == "ready"
    assert provider_descriptor("minimax").availability == "planned"
    with pytest.raises(ValueError, match="unsupported"):
        provider_descriptor("other")


@pytest.mark.parametrize(
    ("config", "expected_type"),
    (
        (VoiceConfig("key"), VolcengineASRClient),
        (VoiceConfig("key", provider="qwen"), QwenASRClient),
        (VoiceConfig("key", provider="openai"), OpenAITranscriptionClient),
    ),
)
def test_provider_factory_creates_selected_backend(config, expected_type):
    client = create_asr_client(config)
    assert isinstance(client, expected_type)
    client.disconnect()


def test_provider_factory_never_fakes_unverified_minimax_asr():
    with pytest.raises(ValueError, match="not available"):
        create_asr_client(VoiceConfig("key", provider="minimax"))
