# SPDX-License-Identifier: GPL-3.0-only
# Portions adapted from Doubao Murmur, Copyright (c) 2026 lilong7676,
# supplied under MIT. Modifications Copyright (c) 2026 Open Voice Input Linux
# contributors. See NOTICE.md for source paths and the preserved MIT terms.
"""Capture 16 kHz mono Int16 PCM through sounddevice/PortAudio."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_DTYPE = "int16"
BLOCK_SIZE = 4096

logger = logging.getLogger(__name__)


class AudioCapture:
    """Small microphone boundary with an injectable stream for offline tests."""

    def __init__(self, stream_factory: Callable[..., Any] | None = None) -> None:
        self._stream_factory = stream_factory or _default_stream_factory
        self._stream: Any | None = None
        self._on_audio_data: Callable[[bytes], None] | None = None
        self._lock = threading.RLock()

    @property
    def is_capturing(self) -> bool:
        with self._lock:
            stream = self._stream
        return bool(stream is not None and getattr(stream, "active", False))

    def start(self, on_audio_data: Callable[[bytes], None]) -> None:
        """Start capture; the callback runs on PortAudio's audio thread."""

        if not callable(on_audio_data):
            raise TypeError("on_audio_data must be callable")
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("audio capture is already active")
            self._on_audio_data = on_audio_data
            try:
                stream = self._stream_factory(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype=SAMPLE_DTYPE,
                    blocksize=BLOCK_SIZE,
                    callback=self._audio_callback,
                    latency="low",
                )
                self._stream = stream
                stream.start()
            except Exception:
                self._stream = None
                self._on_audio_data = None
                raise
        logger.info("Audio capture started")

    def stop(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
            self._on_audio_data = None
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            stream.close()
        logger.info("Audio capture stopped")

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        del frames, time_info
        if status:
            logger.warning("Audio callback reported a capture status")
        with self._lock:
            callback = self._on_audio_data
        if callback is not None:
            callback(bytes(indata))


def _default_stream_factory(**kwargs):
    return _load_sounddevice().RawInputStream(**kwargs)


def _load_sounddevice():
    try:
        import sounddevice as sounddevice_module
    except Exception as error:
        raise RuntimeError(
            "sounddevice/PortAudio is unavailable; install the voice dependencies"
        ) from error
    return sounddevice_module
