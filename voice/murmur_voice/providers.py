"""Provider-neutral ASR client registry and factory.

The daemon depends on one small callback-oriented protocol.  Individual
providers remain isolated modules so adding a cloud or local backend does not
change session, IBus, microphone, or retention semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .config import CorrectionPair, VoiceConfig

ProviderAvailability = Literal["ready", "planned"]


class ASRClient(Protocol):
    """The callback surface consumed by :class:`VoiceSession`."""

    final_result_timeout: float
    on_open: Any
    on_result: Any
    on_finish: Any
    on_error: Any
    on_auth_error: Any
    terminal_corrections: tuple[CorrectionPair, ...]

    def connect(self) -> None: ...

    def send_audio(self, data: bytes) -> None: ...

    def finish_sending(self) -> None: ...

    def disconnect(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Public, secret-free provider capability metadata for settings UI."""

    provider_id: str
    display_name: str
    description: str
    availability: ProviderAvailability
    streaming: bool
    supports_hotwords: bool
    supports_corrections: bool
    requires_api_key: bool = True


PROVIDER_DESCRIPTORS: tuple[ProviderDescriptor, ...] = (
    ProviderDescriptor(
        provider_id="volcengine",
        display_name="火山引擎",
        description="当前默认；中英实时转写与 provider 纠错提示。",
        availability="ready",
        streaming=True,
        supports_hotwords=True,
        supports_corrections=True,
    ),
    ProviderDescriptor(
        provider_id="qwen",
        display_name="阿里云千问 ASR",
        description="Qwen Audio 实时转写；支持中、英、法语言提示和热词。",
        availability="ready",
        streaming=True,
        supports_hotwords=True,
        supports_corrections=False,
    ),
    ProviderDescriptor(
        provider_id="openai",
        display_name="OpenAI Transcribe",
        description="停录后批量转写；支持提示词，但不提供实时 partial。",
        availability="ready",
        streaming=False,
        supports_hotwords=True,
        supports_corrections=False,
    ),
    ProviderDescriptor(
        provider_id="minimax",
        display_name="MiniMax",
        description="等待官方提供可独立验证的语音转文字接口。",
        availability="planned",
        streaming=False,
        supports_hotwords=False,
        supports_corrections=False,
    ),
)

_DESCRIPTORS_BY_ID = {
    descriptor.provider_id: descriptor for descriptor in PROVIDER_DESCRIPTORS
}


def provider_descriptor(provider_id: str) -> ProviderDescriptor:
    """Return one known descriptor without accepting arbitrary provider IDs."""

    try:
        return _DESCRIPTORS_BY_ID[provider_id]
    except KeyError as error:
        raise ValueError("recognition provider is unsupported") from error


def create_asr_client(config: VoiceConfig) -> ASRClient:
    """Create one reviewed backend from an immutable private config snapshot."""

    provider = config.provider
    if provider == "minimax":
        raise ValueError("MiniMax speech-to-text is not available in the public API")
    settings = config.provider_settings()
    if provider == "volcengine":
        from .volcengine import VolcengineASRClient

        return VolcengineASRClient(settings)
    if provider == "qwen":
        from .qwen import QwenASRClient

        return QwenASRClient(settings)
    if provider == "openai":
        from .openai_transcription import OpenAITranscriptionClient

        return OpenAITranscriptionClient(settings)
    raise ValueError("recognition provider is unsupported")
