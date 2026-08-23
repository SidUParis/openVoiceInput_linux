from __future__ import annotations

from murmur_voice.audio import AudioCapture, BLOCK_SIZE, SAMPLE_RATE


class FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.active = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False
        self.stopped = True

    def close(self):
        self.closed = True


def test_audio_capture_is_fully_injectable_without_a_microphone():
    streams = []

    def factory(**kwargs):
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream

    chunks = []
    capture = AudioCapture(stream_factory=factory)
    capture.start(chunks.append)
    stream = streams[0]

    assert capture.is_capturing
    assert stream.kwargs["samplerate"] == SAMPLE_RATE
    assert stream.kwargs["channels"] == 1
    assert stream.kwargs["dtype"] == "int16"
    assert stream.kwargs["blocksize"] == BLOCK_SIZE

    stream.kwargs["callback"](b"\x01\x02", 1, None, None)
    assert chunks == [b"\x01\x02"]

    capture.stop()
    assert stream.stopped and stream.closed
    assert not capture.is_capturing


def test_audio_capture_rejects_nested_start():
    capture = AudioCapture(stream_factory=FakeStream)
    capture.start(lambda data: None)
    try:
        try:
            capture.start(lambda data: None)
        except RuntimeError as error:
            assert "already active" in str(error)
        else:
            raise AssertionError("nested capture should fail")
    finally:
        capture.stop()
