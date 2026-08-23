from __future__ import annotations

from murmur_voice.config import VoiceConfig
from murmur_voice.preedit import AcquireResult
from murmur_voice.session import VoiceSession
from murmur_voice.state import VoiceState


class FakeASR:
    final_result_timeout = 7.0

    def __init__(self, order):
        self.order = order
        self.connected = 0
        self.finished = 0
        self.disconnected = 0
        self.audio = []
        self.on_open = None
        self.on_result = None
        self.on_finish = None
        self.on_error = None
        self.on_auth_error = None

    def connect(self):
        self.order.append("asr-connect")
        self.connected += 1

    def send_audio(self, data):
        self.audio.append(data)

    def finish_sending(self):
        self.order.append("asr-finish")
        self.finished += 1

    def disconnect(self):
        self.order.append("asr-disconnect")
        self.disconnected += 1


class FakeAudio:
    def __init__(self, order):
        self.order = order
        self.callback = None
        self.started = 0
        self.stopped = 0

    def start(self, callback):
        self.order.append("audio-start")
        self.callback = callback
        self.started += 1

    def stop(self):
        self.order.append("audio-stop")
        self.callback = None
        self.stopped += 1


class FakePreedit:
    def __init__(self, order, acquisition=AcquireResult.ACQUIRED):
        self.order = order
        self.acquisition = acquisition
        self.partial_result = True
        self.final_result = True
        self.calls = []
        self.closed = 0

    def acquire_result(self, utterance_id):
        self.order.append("preedit-acquire")
        self.calls.append(("acquire", utterance_id))
        return self.acquisition

    def partial(self, utterance_id, revision, text):
        self.calls.append(("partial", utterance_id, revision, text))
        return self.partial_result

    def final(self, utterance_id, revision, text):
        self.calls.append(("final", utterance_id, revision, text))
        return self.final_result

    def cancel(self, utterance_id):
        self.calls.append(("cancel", utterance_id))
        return True

    def close(self):
        self.closed += 1


class FakeTimer:
    def __init__(self, seconds, callback):
        self.seconds = seconds
        self.callback = callback
        self.started = False
        self.cancelled = False
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


def _session(acquisition=AcquireResult.ACQUIRED):
    order = []
    asr = FakeASR(order)
    audio = FakeAudio(order)
    preedit = FakePreedit(order, acquisition)
    timers = []

    def timer_factory(seconds, callback):
        timer = FakeTimer(seconds, callback)
        timers.append(timer)
        return timer

    session = VoiceSession(
        VoiceConfig("test-key"),
        asr_client=asr,
        audio_capture=audio,
        preedit_client=preedit,
        timer_factory=timer_factory,
        utterance_factory=lambda: "utterance-1",
    )
    return session, asr, audio, preedit, timers, order


def test_start_acquires_focus_before_provider_and_capture():
    session, asr, audio, preedit, timers, order = _session()

    reply = session.start()

    assert reply.ok
    assert reply.state is VoiceState.STARTING
    assert order[:3] == ["preedit-acquire", "asr-connect", "audio-start"]
    assert asr.connected == 1 and audio.started == 1
    assert timers[0].seconds == 600.0 and timers[0].started
    assert timers[1].seconds == 540.0 and timers[1].started


def test_rejected_preedit_never_starts_microphone_or_network():
    session, asr, audio, preedit, timers, order = _session(AcquireResult.REJECTED)

    reply = session.start()

    assert not reply.ok
    assert reply.code == "preedit-rejected"
    assert asr.connected == 0 and audio.started == 0
    assert order == ["preedit-acquire"]


def test_partials_and_authoritative_final_use_strict_revisions_once():
    session, asr, audio, preedit, timers, order = _session()
    session.start()
    asr.on_open()
    asr.on_result("第一版")
    asr.on_result("第二版")

    asr.on_finish()
    asr.on_finish()

    assert session.state is VoiceState.IDLE
    assert [call[0] for call in preedit.calls] == [
        "acquire",
        "partial",
        "partial",
        "final",
    ]
    assert preedit.calls[1][2] == 1
    assert preedit.calls[2][2] == 2
    assert preedit.calls[3][2:] == (3, "第二版")


def test_stop_waits_for_final_but_timeout_cancels_without_commit():
    session, asr, audio, preedit, timers, order = _session()
    session.start()
    asr.on_result("不是权威最终稿")

    reply = session.stop()
    assert reply.state is VoiceState.STOPPING
    assert asr.finished == 1
    assert timers[0].cancelled
    assert timers[2].seconds == 7.0 and timers[2].started

    timers[2].fire()

    assert session.state is VoiceState.IDLE
    assert [call[0] for call in preedit.calls].count("final") == 0
    assert [call[0] for call in preedit.calls].count("cancel") == 1
    assert session.status().code == "final-timeout"


def test_cancel_and_partial_rejection_never_fallback_or_double_commit():
    session, asr, audio, preedit, timers, order = _session()
    session.start()
    preedit.partial_result = False

    asr.on_result("private transcript")

    assert session.state is VoiceState.IDLE
    assert [call[0] for call in preedit.calls] == [
        "acquire",
        "partial",
        "cancel",
    ]
    assert asr.disconnected == 1


def test_toggle_starts_then_stops_and_close_restores():
    session, asr, audio, preedit, timers, order = _session()

    assert session.toggle().code == "started"
    assert session.toggle().code == "stopping"
    assert session.toggle().code == "already-stopping"
    session.close()

    assert session.state is VoiceState.IDLE
    assert preedit.closed == 1
    assert [call[0] for call in preedit.calls].count("cancel") == 1


def test_ten_minute_limit_auto_stops_then_accepts_only_provider_final():
    session, asr, audio, preedit, timers, order = _session()
    session.start()
    asr.on_result("live")

    timers[0].fire()

    assert session.state is VoiceState.STOPPING
    assert asr.finished == 1
    assert timers[2].seconds == 7.0
    asr.on_result("authoritative")
    asr.on_finish()
    assert session.state is VoiceState.IDLE
    assert [call[0] for call in preedit.calls].count("final") == 1
    assert preedit.calls[-1][-1] == "authoritative"


def test_cancelled_old_duration_timer_cannot_stop_a_new_session():
    session, asr, audio, preedit, timers, order = _session()
    session.start()
    old_timer = timers[0]
    session.cancel()
    session.start()

    old_timer.fire()

    assert session.state is VoiceState.STARTING
    assert asr.finished == 0


def test_late_callbacks_from_cancelled_session_cannot_touch_new_session():
    session, asr, audio, preedit, timers, order = _session()
    session.start()
    late_partial = asr.on_result
    late_final = asr.on_finish
    late_timer = timers[0]
    session.cancel()
    session.start()
    calls_before = list(preedit.calls)

    late_partial("late A")
    late_final()
    late_timer.fire()

    assert session.state is VoiceState.STARTING
    assert preedit.calls == calls_before
    assert asr.finished == 0


def test_one_minute_warning_is_observable_through_status():
    session, asr, audio, preedit, timers, order = _session()
    session.start()

    timers[1].fire()

    assert session.status().code == "recording-limit-warning"
    assert session.state is VoiceState.STARTING
