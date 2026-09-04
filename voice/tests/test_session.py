from __future__ import annotations

import pytest

from murmur_voice.adaptive_runtime import (
    AdaptiveObservationResult,
    AdaptiveObservedCandidate,
)
from murmur_voice.audio import AudioDeviceError, MicrophonePolicyError
from murmur_voice.confirmed_correction import ConfirmedCorrectionEdit
from murmur_voice.config import ConfigError, CorrectionPair, VoiceConfig
from murmur_voice.preedit import AcquireResult, ObservationSnapshot
from murmur_voice.output_style import OutputDelivery, OutputStyleConfig
from murmur_voice.output_target import OutputTargetConfig
from murmur_voice.session import (
    ADAPTIVE_OBSERVATION_FINISH_MARGIN_SECONDS,
    ADAPTIVE_OBSERVATION_SECONDS,
    LAST_REVIEW_TTL_SECONDS,
    VOICE_START_TIMEOUT_SECONDS,
    VoiceSession,
)
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
        self.metadata_callback = None

    def set_source_metadata_callback(self, callback):
        self.metadata_callback = callback

    def start(self, callback):
        self.order.append("audio-start")
        self.callback = callback
        self.started += 1

    def stop(self):
        self.order.append("audio-stop")
        self.callback = None
        self.stopped += 1


class FakeDataRecord:
    def __init__(self, *, commit_error=None, stop_result=True):
        self.audio = []
        self.stop_calls = 0
        self.commits = []
        self.deliveries = []
        self.targets = []
        self.discards = 0
        self.commit_error = commit_error
        self.stop_result = stop_result
        self.microphone_metadata = []

    def add_audio(self, data):
        self.audio.append(data)

    def stop_audio(self):
        self.stop_calls += 1
        return self.stop_result

    def commit(self, provider_final, delivery, target="caret"):
        self.commits.append(provider_final)
        self.deliveries.append(delivery)
        self.targets.append(target)
        if self.commit_error is not None:
            raise self.commit_error

    def discard(self):
        self.discards += 1

    def set_microphone_metadata(self, metadata):
        self.microphone_metadata.append(metadata)


class FakePreedit:
    def __init__(self, order, acquisition=AcquireResult.ACQUIRED):
        self.order = order
        self.acquisition = acquisition
        self.partial_result = True
        self.final_result = True
        self.calls = []
        self.closed = 0
        self.acquire_hook = None
        self.final_hook = None
        self.observation_result = None
        self.observation_supported = None

    def acquire_result(self, utterance_id):
        self.order.append("preedit-acquire")
        self.calls.append(("acquire", utterance_id))
        if self.acquire_hook is not None:
            self.acquire_hook()
        return self.acquisition

    def partial(self, utterance_id, revision, text):
        self.calls.append(("partial", utterance_id, revision, text))
        return self.partial_result

    def final(self, utterance_id, revision, text):
        self.calls.append(("final", utterance_id, revision, text))
        if self.final_hook is not None:
            self.final_hook()
        return self.final_result

    def cancel(self, utterance_id):
        self.calls.append(("cancel", utterance_id))
        return True

    def finish_observation(self, utterance_id):
        self.calls.append(("finish-observation", utterance_id))
        return self.observation_result

    def close(self):
        self.closed += 1


class FakeClipboardWriter:
    def __init__(self, order, *, preflight_error=None, write_error=None):
        self.order = order
        self.preflight_error = preflight_error
        self.write_error = write_error
        self.preflight_calls = 0
        self.writes = []

    def preflight(self):
        self.order.append("clipboard-preflight")
        self.preflight_calls += 1
        if self.preflight_error is not None:
            raise self.preflight_error

    def write(self, text):
        self.order.append("clipboard-write")
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(text)


class MaliciousProviderText(str):
    def __len__(self):
        raise AssertionError("untrusted str subclass method must not run")


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


def _session(acquisition=AcquireResult.ACQUIRED, **session_options):
    order = []
    asr = FakeASR(order)
    audio = FakeAudio(order)
    preedit = FakePreedit(order, acquisition)
    timers = []

    def timer_factory(seconds, callback):
        timer = FakeTimer(seconds, callback)
        timers.append(timer)
        return timer

    last_review_timer_factory = session_options.pop(
        "last_review_timer_factory",
        lambda seconds, callback: FakeTimer(seconds, callback),
    )

    utterance_factory = session_options.pop("utterance_factory", lambda: "utterance-1")
    session = VoiceSession(
        VoiceConfig("test-key"),
        asr_client=asr,
        audio_capture=audio,
        preedit_client=preedit,
        timer_factory=timer_factory,
        utterance_factory=utterance_factory,
        last_review_timer_factory=last_review_timer_factory,
        **session_options,
    )
    return session, asr, audio, preedit, timers, order


def test_recent_accepted_final_is_memory_only_bounded_and_cleared_on_close():
    now = [10.0]
    review_timers = []

    def review_timer_factory(seconds, callback):
        timer = FakeTimer(seconds, callback)
        review_timers.append(timer)
        return timer

    session, asr, _audio, preedit, _timers, _order = _session(
        monotonic=lambda: now[0],
        last_review_ttl_seconds=LAST_REVIEW_TTL_SECONDS,
        last_review_timer_factory=review_timer_factory,
    )
    preedit.observation_supported = False
    session.start()
    asr.on_result("private provider final")
    asr.on_finish()

    review = session.review_last()
    assert review is not None
    assert review.utterance_id == "utterance-1"
    assert review.provider_text == "private provider final"
    assert "private provider final" not in repr(review)
    assert len(review_timers) == 1
    assert review_timers[0].seconds == LAST_REVIEW_TTL_SECONDS
    assert review_timers[0].started

    review_timers[0].fire()
    assert session.review_last() is None
    assert session.submit_last_review("utterance-1", "late text").code == (
        "stale-review"
    )

    session.close()
    assert session.review_last() is None


def test_new_accepted_final_overwrites_the_previous_review():
    utterances = iter(("utterance-1", "utterance-2"))
    review_timers = []

    def review_timer_factory(seconds, callback):
        timer = FakeTimer(seconds, callback)
        review_timers.append(timer)
        return timer

    session, asr, _audio, preedit, _timers, _order = _session(
        utterance_factory=lambda: next(utterances),
        last_review_timer_factory=review_timer_factory,
    )
    preedit.observation_supported = False
    session.start()
    asr.on_result("first private final")
    asr.on_finish()

    session.start()
    asr.on_result("second private final")
    asr.on_finish()

    review = session.review_last()
    assert review is not None
    assert review.utterance_id == "utterance-2"
    assert review.provider_text == "second private final"
    assert "first private final" not in repr(review)
    assert session.submit_last_review("utterance-1", "old correction").code == (
        "stale-review"
    )
    assert session.review_last() == review
    assert len(review_timers) == 2
    assert review_timers[0].cancelled
    assert review_timers[1].started
    review_timers[0].fire()
    assert session.review_last() == review
    assert [call[0] for call in preedit.calls].count("final") == 2


def test_daemon_close_cancels_review_ttl_and_clears_text_immediately():
    review_timers = []

    def review_timer_factory(seconds, callback):
        timer = FakeTimer(seconds, callback)
        review_timers.append(timer)
        return timer

    session, asr, _audio, _preedit, _timers, _order = _session(
        last_review_timer_factory=review_timer_factory,
    )
    session.start()
    asr.on_result("private final cleared by shutdown")
    asr.on_finish()
    assert session.review_last() is not None

    session.close()

    assert session.review_last() is None
    assert len(review_timers) == 1
    assert review_timers[0].cancelled


def test_review_ttl_timer_failure_drops_text_without_failing_dictation():
    class BrokenTimer(FakeTimer):
        def start(self):
            raise RuntimeError("no timer thread")

    session, asr, _audio, _preedit, _timers, _order = _session(
        last_review_timer_factory=BrokenTimer,
    )
    session.start()
    asr.on_result("private final must not remain unbounded")

    asr.on_finish()

    assert session.state is VoiceState.OBSERVING
    assert session.review_last() is None


def test_review_submission_is_id_bound_writes_feedback_and_consumes_once():
    handler_calls = []
    feedback = []
    result = AdaptiveObservationResult(
        "explicit-feedback-activated",
        captured_count=1,
        activated_count=1,
        candidates=(
            AdaptiveObservedCandidate(
                "Ostro", "Austral", "recognition", "explicit", "active"
            ),
        ),
    )

    def handle(provider_text, spoken_verbatim):
        handler_calls.append((provider_text, spoken_verbatim))
        return result

    def write_feedback(utterance_id, document):
        feedback.append((utterance_id, document))
        return True

    session, asr, _audio, preedit, _timers, _order = _session(
        explicit_feedback_handler=handle,
        data_collection_feedback_writer=write_feedback,
    )
    preedit.observation_supported = False
    session.start()
    asr.on_result("Ostro")
    asr.on_finish()

    reply = session.submit_last_review("utterance-1", "Austral")

    assert reply.ok
    assert reply.reason_code == "explicit-feedback-activated"
    assert reply.feedback_code == "feedback-queued"
    assert handler_calls == [("Ostro", "Austral")]
    assert feedback[0][0] == "utterance-1"
    assert feedback[0][1] == result.as_feedback_document()
    assert session.review_last() is None
    assert session.state is VoiceState.IDLE
    assert [call[0] for call in preedit.calls].count("finish-observation") == 0

    duplicate = session.submit_last_review("utterance-1", "Austral")
    assert not duplicate.ok
    assert duplicate.code == "stale-review"
    assert handler_calls == [("Ostro", "Austral")]
    assert len(feedback) == 1


def test_stale_review_id_is_rejected_without_consuming_current_result():
    handler_calls = []
    session, asr, _audio, preedit, _timers, _order = _session(
        explicit_feedback_handler=lambda provider, spoken: handler_calls.append(
            (provider, spoken)
        )
    )
    preedit.observation_supported = False
    session.start()
    asr.on_result("current provider final")
    asr.on_finish()

    reply = session.submit_last_review("older-utterance", "actual speech")

    assert not reply.ok
    assert reply.code == "stale-review"
    assert handler_calls == []
    assert session.review_last() is not None


@pytest.mark.parametrize(
    "active_state",
    (
        VoiceState.STARTING,
        VoiceState.RECORDING,
        VoiceState.STOPPING,
        VoiceState.OBSERVING,
    ),
)
def test_review_submission_rejects_every_active_state_without_side_effects(
    active_state,
):
    handler_calls = []
    writer_calls = []
    session, asr, _audio, preedit, _timers, _order = _session(
        explicit_feedback_handler=lambda provider, spoken: handler_calls.append(
            (provider, spoken)
        ),
        data_collection_feedback_writer=lambda utterance_id, document: (
            writer_calls.append((utterance_id, document))
        ),
    )
    preedit.observation_supported = False
    session.start()
    asr.on_result("provider final")
    asr.on_finish()
    review = session.review_last()
    assert review is not None
    with session._lock:
        session._state = active_state

    reply = session.submit_last_review(review.utterance_id, "spoken verbatim")

    assert not reply.ok
    assert reply.code == "session-active"
    assert handler_calls == []
    assert writer_calls == []
    assert session.review_last() == review


def test_review_submission_with_collection_disabled_still_learns_and_consumes():
    result = AdaptiveObservationResult("explicit-feedback-no-change")
    session, asr, _audio, preedit, _timers, _order = _session(
        explicit_feedback_handler=lambda provider, spoken: result,
    )
    preedit.observation_supported = False
    session.start()
    asr.on_result("same words")
    asr.on_finish()

    reply = session.submit_last_review("utterance-1", "same words")

    assert reply.ok
    assert reply.feedback_code == "feedback-disabled"
    assert session.review_last() is None


def test_review_feedback_enqueue_failure_is_distinct_after_ledger_success():
    result = AdaptiveObservationResult("explicit-feedback-activated")

    def fail_feedback(_utterance_id, _document):
        raise OSError("simulated sidecar enqueue failure")

    session, asr, _audio, preedit, _timers, _order = _session(
        explicit_feedback_handler=lambda provider, spoken: result,
        data_collection_feedback_writer=fail_feedback,
    )
    preedit.observation_supported = False
    session.start()
    asr.on_result("provider final")
    asr.on_finish()

    reply = session.submit_last_review("utterance-1", "spoken verbatim")

    assert reply.ok
    assert reply.reason_code == "explicit-feedback-activated"
    assert reply.feedback_code == "feedback-failed"
    assert session.status().code == "data-collection-failed"
    assert session.review_last() is None


def test_start_acquires_focus_before_provider_and_capture():
    session, asr, audio, preedit, timers, order = _session()

    reply = session.start()

    assert reply.ok
    assert reply.state is VoiceState.STARTING
    assert order[:3] == ["preedit-acquire", "asr-connect", "audio-start"]
    assert asr.connected == 1 and audio.started == 1
    assert preedit.calls == [
        ("acquire", "utterance-1"),
        ("partial", "utterance-1", 1, ""),
    ]
    assert timers[0].seconds == 600.0 and timers[0].started
    assert timers[1].seconds == 540.0 and timers[1].started


def test_opt_in_data_record_receives_exact_audio_and_authoritative_final():
    record = FakeDataRecord()
    factory_calls = []
    session, asr, audio, preedit, timers, order = _session(
        data_collection_factory=lambda utterance_id: (
            factory_calls.append(utterance_id) or record
        )
    )
    session.start()

    audio.callback(b"\x01\x02")
    asr.on_result("teacher partial")
    asr.on_result("teacher final")
    session.stop()
    asr.on_finish()

    assert factory_calls == ["utterance-1"]
    assert asr.audio == [b"\x01\x02"]
    assert record.audio == [b"\x01\x02"]
    assert record.commits == ["teacher final"]
    assert record.discards == 0
    assert record.stop_calls >= 1


def test_opt_in_record_receives_late_microphone_route_metadata():
    record = FakeDataRecord()
    session, asr, audio, preedit, timers, order = _session(
        data_collection_factory=lambda _utterance_id: record
    )

    session.start()
    assert callable(audio.metadata_callback)
    metadata = object()
    audio.metadata_callback(metadata)

    assert record.microphone_metadata == [metadata]


def test_optional_collection_start_failure_never_blocks_dictation():
    def fail_collection(_utterance_id):
        raise OSError("private path must not be logged")

    session, asr, audio, preedit, timers, order = _session(
        data_collection_factory=fail_collection
    )

    reply = session.start()

    assert reply.ok is True
    assert asr.connected == 1
    assert audio.started == 1
    assert session.status().code == "data-collection-unavailable"


def test_collection_write_failure_keeps_final_commit_and_reports_warning():
    record = FakeDataRecord(commit_error=OSError("simulated storage loss"))
    session, asr, audio, preedit, timers, order = _session(
        data_collection_factory=lambda _utterance_id: record
    )
    session.start()
    asr.on_result("authoritative")

    asr.on_finish()

    assert session.state is VoiceState.OBSERVING
    assert next(call for call in preedit.calls if call[0] == "final")[-1] == (
        "authoritative"
    )
    assert session.status().code == "data-collection-failed"


def test_cancel_discards_optional_record_without_publishing_a_label():
    record = FakeDataRecord()
    session, asr, audio, preedit, timers, order = _session(
        data_collection_factory=lambda _utterance_id: record
    )
    session.start()
    audio.callback(b"\x01\x02")

    session.cancel()

    assert record.commits == []
    assert record.discards == 1


def test_rejected_authoritative_final_discards_optional_record():
    record = FakeDataRecord()
    session, asr, audio, preedit, timers, order = _session(
        data_collection_factory=lambda _utterance_id: record
    )
    preedit.final_result = False
    session.start()
    audio.callback(b"\x01\x02")
    asr.on_result("authoritative")

    asr.on_finish()

    assert record.commits == []
    assert record.discards == 1
    assert session.status().code == "preedit-final-rejected"


def test_background_collection_failure_is_visible_without_blocking_status():
    session, asr, audio, preedit, timers, order = _session(
        data_collection_status_reader=lambda: "data-collection-failed"
    )

    assert session.status().code == "data-collection-failed"

    reply = session.start()
    assert reply.ok is True
    assert asr.connected == 1
    assert audio.started == 1


def test_broken_collection_status_reader_is_reduced_to_fixed_warning():
    def fail_status():
        raise RuntimeError("path must not escape")

    session, *_ = _session(data_collection_status_reader=fail_status)

    assert session.status().code == "data-collection-failed"


def test_rejected_preedit_never_starts_microphone_or_network():
    session, asr, audio, preedit, timers, order = _session(AcquireResult.REJECTED)

    reply = session.start()

    assert not reply.ok
    assert reply.code == "preedit-rejected"
    assert asr.connected == 0 and audio.started == 0
    assert order == ["preedit-acquire"]


def test_invalid_microphone_policy_fails_before_preedit_provider_or_audio():
    order = []
    audio = FakeAudio(order)
    preedit = FakePreedit(order)

    def invalid_policy():
        order.append("policy-validate")
        raise MicrophonePolicyError("private details must not appear")

    def provider_factory():
        raise AssertionError("provider factory must not run")

    session = VoiceSession(
        VoiceConfig("test-key"),
        asr_client_factory=provider_factory,
        audio_capture=audio,
        preedit_client=preedit,
        microphone_policy_validator=invalid_policy,
    )

    reply = session.start()

    assert reply.ok is False
    assert reply.code == "microphone-policy-invalid"
    assert reply.state is VoiceState.IDLE
    assert order == ["policy-validate"]
    assert preedit.calls == []
    assert audio.started == 0 and audio.stopped == 0
    assert session.status().code == "microphone-policy-invalid"


def test_policy_failure_during_audio_prepare_keeps_fixed_status_code():
    session, asr, audio, preedit, timers, order = _session()

    def fail_prepare():
        order.append("audio-prepare")
        raise MicrophonePolicyError("simulated invalid policy")

    audio.prepare = fail_prepare
    reply = session.start()

    assert not reply.ok
    assert reply.code == "microphone-policy-invalid"
    assert asr.connected == 0 and audio.started == 0
    assert order == [
        "preedit-acquire",
        "audio-prepare",
        "audio-stop",
        "asr-disconnect",
    ]
    assert session.status().code == "microphone-policy-invalid"


def test_microphone_preflight_failure_never_connects_provider():
    session, asr, audio, preedit, timers, order = _session()

    def fail_prepare():
        order.append("audio-prepare")
        raise AudioDeviceError("simulated device failure")

    audio.prepare = fail_prepare
    reply = session.start()

    assert not reply.ok
    assert reply.code == "microphone-unavailable"
    assert reply.state is VoiceState.IDLE
    assert asr.connected == 0 and audio.started == 0
    assert order == [
        "preedit-acquire",
        "audio-prepare",
        "audio-stop",
        "asr-disconnect",
    ]
    assert session.status().code == "microphone-unavailable"


def test_stream_open_failure_disconnects_provider_and_aborts_session():
    session, asr, audio, preedit, timers, order = _session()

    def fail_start(callback):
        del callback
        order.append("audio-start")
        raise AudioDeviceError("simulated stream-open failure")

    audio.start = fail_start
    reply = session.start()

    assert not reply.ok
    assert reply.code == "microphone-unavailable"
    assert reply.state is VoiceState.IDLE
    assert asr.connected == 1
    assert asr.disconnected == 1
    assert order == [
        "preedit-acquire",
        "asr-connect",
        "audio-start",
        "audio-stop",
        "asr-disconnect",
    ]
    assert [call[0] for call in preedit.calls] == ["acquire", "partial", "cancel"]


def test_focus_lost_during_preflight_never_connects_or_opens_microphone():
    session, asr, audio, preedit, timers, order = _session()

    def lose_focus_during_prepare():
        order.append("audio-prepare")
        preedit.partial_result = False

    audio.prepare = lose_focus_during_prepare

    reply = session.start()

    assert not reply.ok
    assert reply.code == "preedit-lost"
    assert reply.state is VoiceState.IDLE
    assert asr.connected == 0 and audio.started == 0
    assert preedit.calls == [
        ("acquire", "utterance-1"),
        ("partial", "utterance-1", 1, ""),
        ("cancel", "utterance-1"),
    ]
    assert session.status().code == "preedit-lost"


def test_expired_start_deadline_after_acquire_never_connects_or_opens():
    now = [0.0]
    session, asr, audio, preedit, timers, order = _session(
        monotonic=lambda: now[0],
        start_timeout_seconds=1.0,
    )
    preedit.acquire_hook = lambda: now.__setitem__(0, 1.0)

    reply = session.start()

    assert not reply.ok
    assert reply.code == "start-timeout"
    assert reply.state is VoiceState.IDLE
    assert asr.connected == 0 and audio.started == 0
    assert preedit.calls == [
        ("acquire", "utterance-1"),
        ("cancel", "utterance-1"),
    ]
    assert session.status().code == "start-timeout"


def test_preflight_that_consumes_whole_start_budget_never_contacts_provider():
    now = [0.0]
    session, asr, audio, preedit, timers, order = _session(
        monotonic=lambda: now[0],
        start_timeout_seconds=VOICE_START_TIMEOUT_SECONDS,
    )

    def exhaust_deadline():
        order.append("audio-prepare")
        now[0] = VOICE_START_TIMEOUT_SECONDS

    audio.prepare = exhaust_deadline

    reply = session.start()

    assert reply.code == "start-timeout"
    assert asr.connected == 0 and audio.started == 0
    assert not any(call[0] == "partial" for call in preedit.calls)


def test_partials_and_authoritative_final_use_strict_revisions_once():
    session, asr, audio, preedit, timers, order = _session()
    session.start()
    asr.on_open()
    asr.on_result("第一版")
    asr.on_result("第二版")

    asr.on_finish()
    asr.on_finish()

    assert session.state is VoiceState.OBSERVING
    assert asr.disconnected == 1
    assert timers[2].seconds == pytest.approx(
        ADAPTIVE_OBSERVATION_SECONDS - ADAPTIVE_OBSERVATION_FINISH_MARGIN_SECONDS,
        abs=0.01,
    )
    timers[2].fire()

    assert session.state is VoiceState.IDLE
    assert [call[0] for call in preedit.calls] == [
        "acquire",
        "partial",
        "partial",
        "partial",
        "final",
        "finish-observation",
    ]
    assert preedit.calls[1][2:] == (1, "")
    assert preedit.calls[2][2] == 2
    assert preedit.calls[3][2] == 3
    assert preedit.calls[4][2:] == (4, "第二版")


def test_clean_mode_keeps_partials_raw_and_cleans_only_terminal_delivery():
    reasons = []
    learned = []
    session, asr, _audio, preedit, timers, _order = _session(
        output_style_reader=lambda: OutputStyleConfig("clean"),
        observation_handler=lambda snapshot: learned.append(snapshot),
        observation_result_handler=lambda reason: (
            reasons.append(reason) or AdaptiveObservationResult(reason)
        ),
    )
    session.start()

    asr.on_result("我我觉得，呃，可以。")
    partial = [call for call in preedit.calls if call[0] == "partial"][-1]
    assert partial[-1] == "我我觉得，呃，可以。"
    asr.on_finish()

    final = next(call for call in preedit.calls if call[0] == "final")
    assert final[-1] == "我觉得，可以。"
    assert session.state is VoiceState.IDLE
    assert reasons == ["postprocessed-output-not-safe-for-asr-learning"]
    assert learned == []
    assert [call[0] for call in preedit.calls].count("finish-observation") == 1
    assert len(timers) == 2
    review = session.review_last()
    assert review is not None
    assert review.provider_text == "我我觉得，呃，可以。"
    assert review.delivered_text == "我觉得，可以。"


def test_confirmed_correction_is_frozen_and_only_changes_terminal_delivery():
    reasons = []
    session, asr, _audio, preedit, timers, _order = _session(
        observation_result_handler=lambda reason: (
            reasons.append(reason) or AdaptiveObservationResult(reason)
        ),
    )
    asr.terminal_corrections = (CorrectionPair("Elas", "ILaaS"),)
    session.start()
    asr.terminal_corrections = (CorrectionPair("Elas", "mutated-too-late"),)

    asr.on_result("Elas is ready")
    partial = [call for call in preedit.calls if call[0] == "partial"][-1]
    assert partial[-1] == "Elas is ready"
    asr.on_finish()

    final = next(call for call in preedit.calls if call[0] == "final")
    assert final[-1] == "ILaaS is ready"
    assert session.state is VoiceState.IDLE
    assert reasons == ["postprocessed-output-not-safe-for-asr-learning"]
    assert [call[0] for call in preedit.calls].count("finish-observation") == 1
    assert len(timers) == 2
    review = session.review_last()
    assert review is not None
    assert review.provider_text == "Elas is ready"
    assert review.delivered_text == "ILaaS is ready"


def test_clean_mode_is_frozen_at_start_and_a_save_applies_next_utterance():
    current = [OutputStyleConfig("clean")]
    utterances = iter(("utterance-1", "utterance-2"))
    session, asr, _audio, preedit, _timers, _order = _session(
        utterance_factory=lambda: next(utterances),
        output_style_reader=lambda: current[0],
    )
    session.start()
    current[0] = OutputStyleConfig("faithful")
    asr.on_result("我我继续。")
    asr.on_finish()

    first_final = [call for call in preedit.calls if call[0] == "final"][-1]
    assert first_final[-1] == "我继续。"
    assert session.state is VoiceState.IDLE

    session.start()
    asr.on_result("我我继续。")
    asr.on_finish()
    second_final = [call for call in preedit.calls if call[0] == "final"][-1]
    assert second_final[-1] == "我我继续。"
    assert session.state is VoiceState.OBSERVING


def test_invalid_output_style_fails_before_focus_provider_or_microphone():
    def invalid():
        raise ConfigError("private mode must not escape")

    session, asr, audio, preedit, timers, order = _session(
        output_style_reader=invalid,
    )

    reply = session.start()

    assert reply.code == "output-style-invalid"
    assert reply.state is VoiceState.IDLE
    assert asr.connected == 0
    assert audio.started == 0
    assert preedit.calls == []
    assert timers == []
    assert order == []


def test_invalid_output_target_fails_before_focus_provider_or_microphone():
    def invalid():
        raise ConfigError("private target must not escape")

    order = []
    writer = FakeClipboardWriter(order)
    session, asr, audio, preedit, timers, _ = _session(
        output_target_reader=invalid,
        clipboard_writer=writer,
    )

    reply = session.start()

    assert reply.code == "output-target-invalid"
    assert reply.state is VoiceState.IDLE
    assert asr.connected == 0
    assert audio.started == 0
    assert preedit.calls == []
    assert writer.preflight_calls == 0
    assert timers == []
    assert order == []


def test_clipboard_preflight_fails_before_ibus_provider_or_microphone():
    order = []
    policy_calls = []
    writer = FakeClipboardWriter(
        order,
        preflight_error=RuntimeError("private helper detail"),
    )
    session, asr, audio, preedit, timers, _ = _session(
        output_target_reader=lambda: OutputTargetConfig("clipboard"),
        clipboard_writer=writer,
        microphone_policy_validator=lambda: policy_calls.append(True),
    )

    reply = session.start()

    assert reply.code == "clipboard-unavailable"
    assert reply.state is VoiceState.IDLE
    assert writer.preflight_calls == 1
    assert policy_calls == []
    assert asr.connected == 0
    assert audio.started == 0
    assert preedit.calls == []
    assert timers == []
    assert order == ["clipboard-preflight"]


def test_clipboard_target_skips_ibus_and_writes_only_clean_authoritative_terminal():
    order = []
    outcomes = []
    feedback = []
    record = FakeDataRecord()
    writer = FakeClipboardWriter(order)
    session, asr, audio, preedit, _timers, session_order = _session(
        output_target_reader=lambda: OutputTargetConfig("clipboard"),
        clipboard_writer=writer,
        output_style_reader=lambda: OutputStyleConfig("clean"),
        data_collection_factory=lambda _utterance_id: record,
        observation_result_handler=lambda reason: (
            outcomes.append(reason) or AdaptiveObservationResult(reason)
        ),
        data_collection_feedback_writer=lambda utterance_id, document: (
            feedback.append((utterance_id, document)) or True
        ),
    )

    reply = session.start()
    asr.on_result("我我觉得，呃，可以。")

    assert reply.ok
    assert order == ["clipboard-preflight"]
    assert session_order[:2] == ["asr-connect", "audio-start"]
    assert writer.writes == []
    assert preedit.calls == []

    asr.on_finish()

    assert writer.writes == ["我觉得，可以。"]
    assert [event for event in order if event == "clipboard-write"] == [
        "clipboard-write"
    ]
    assert preedit.calls == []
    assert session.state is VoiceState.IDLE
    assert session.status().code == "clipboard-ready"
    review = session.review_last()
    assert review is not None
    assert review.provider_text == "我我觉得，呃，可以。"
    assert review.delivered_text == "我觉得，可以。"
    assert record.commits == ["我我觉得，呃，可以。"]
    assert record.targets == ["clipboard"]
    assert outcomes == ["clipboard-output-no-surrounding-text"]
    assert feedback[0][0] == "utterance-1"


def test_clipboard_copy_failure_discards_record_and_review_with_content_free_status(
    caplog,
):
    order = []
    private_text = "private provider transcript"
    record = FakeDataRecord()
    writer = FakeClipboardWriter(
        order,
        write_error=RuntimeError(private_text),
    )
    session, asr, _audio, preedit, _timers, _ = _session(
        output_target_reader=lambda: OutputTargetConfig("clipboard"),
        clipboard_writer=writer,
        data_collection_factory=lambda _utterance_id: record,
    )
    session.start()
    asr.on_result(private_text)

    asr.on_finish()

    assert session.state is VoiceState.IDLE
    assert session.status().code == "clipboard-copy-failed"
    assert record.commits == []
    assert record.discards == 1
    assert session.review_last() is None
    assert writer.writes == []
    assert preedit.calls == []
    assert private_text not in caplog.text


def test_clipboard_copy_failure_clears_an_older_review():
    targets = iter((OutputTargetConfig("caret"), OutputTargetConfig("clipboard")))
    order = []
    writer = FakeClipboardWriter(order, write_error=RuntimeError("private"))
    session, asr, _audio, preedit, _timers, _ = _session(
        output_target_reader=lambda: next(targets),
        clipboard_writer=writer,
    )
    preedit.observation_supported = False
    session.start()
    asr.on_result("older accepted final")
    asr.on_finish()
    assert session.review_last() is not None

    session.start()
    asr.on_result("failed clipboard final")
    asr.on_finish()

    assert session.review_last() is None
    assert session.status().code == "clipboard-copy-failed"


def test_clipboard_target_is_frozen_for_one_utterance_then_hot_reloaded():
    current = [OutputTargetConfig("clipboard")]
    order = []
    writer = FakeClipboardWriter(order)
    session, asr, _audio, preedit, _timers, _ = _session(
        output_target_reader=lambda: current[0],
        clipboard_writer=writer,
        observation_result_handler=lambda reason: AdaptiveObservationResult(reason),
    )
    session.start()
    current[0] = OutputTargetConfig("caret")
    asr.on_result("first final")
    asr.on_finish()

    assert writer.writes == ["first final"]
    assert preedit.calls == []

    preedit.observation_supported = False
    session.start()
    asr.on_result("second final")
    asr.on_finish()

    assert writer.preflight_calls == 1
    assert writer.writes == ["first final"]
    assert [call[0] for call in preedit.calls] == [
        "acquire",
        "partial",
        "partial",
        "final",
    ]


def test_clipboard_cancel_and_empty_final_never_touch_ibus_or_clipboard():
    order = []
    writer = FakeClipboardWriter(order)
    session, asr, _audio, preedit, _timers, _ = _session(
        output_target_reader=lambda: OutputTargetConfig("clipboard"),
        clipboard_writer=writer,
    )
    session.start()
    session.cancel()

    assert preedit.calls == []
    assert writer.writes == []

    session.start()
    asr.on_finish()

    assert preedit.calls == []
    assert writer.writes == []
    assert session.review_last() is None
    assert session.status().code == "clipboard-armed"


def test_clipboard_data_collection_failure_takes_priority_over_ready_status():
    order = []
    writer = FakeClipboardWriter(order)
    record = FakeDataRecord(commit_error=RuntimeError("storage failed"))
    session, asr, _audio, preedit, _timers, _ = _session(
        output_target_reader=lambda: OutputTargetConfig("clipboard"),
        clipboard_writer=writer,
        data_collection_factory=lambda _utterance_id: record,
        observation_result_handler=lambda reason: AdaptiveObservationResult(reason),
    )
    session.start()
    asr.on_result("provider final")

    asr.on_finish()

    assert writer.writes == ["provider final"]
    assert preedit.calls == []
    assert session.status().code == "data-collection-failed"


@pytest.mark.parametrize(
    "exact_text",
    (
        "x" * 4096,
        "😀" * 4096,
    ),
)
def test_clipboard_result_accepts_exact_codepoint_and_utf8_boundaries(exact_text):
    order = []
    writer = FakeClipboardWriter(order)
    session, asr, _audio, preedit, _timers, _ = _session(
        output_target_reader=lambda: OutputTargetConfig("clipboard"),
        clipboard_writer=writer,
        observation_result_handler=lambda reason: AdaptiveObservationResult(reason),
    )
    session.start()

    asr.on_result(exact_text)
    asr.on_finish()

    assert writer.writes == [exact_text]
    assert preedit.calls == []
    assert session.status().code == "clipboard-ready"


@pytest.mark.parametrize(
    "invalid_text",
    (
        "x" * 4097,
        "😀" * 4097,
        "private\x00provider-result",
        "\ud800",
        MaliciousProviderText("private-subclass-result"),
        b"private-provider-result",
    ),
)
def test_invalid_or_oversized_clipboard_result_aborts_without_any_delivery_or_record(
    invalid_text,
    caplog,
):
    order = []
    writer = FakeClipboardWriter(order)
    record = FakeDataRecord()
    session, asr, _audio, preedit, _timers, _ = _session(
        output_target_reader=lambda: OutputTargetConfig("clipboard"),
        clipboard_writer=writer,
        data_collection_factory=lambda _utterance_id: record,
    )
    session.start()

    asr.on_result(invalid_text)
    asr.on_finish()

    assert session.state is VoiceState.IDLE
    assert session.status().code == "provider-error"
    assert asr.disconnected == 1
    assert writer.writes == []
    assert record.commits == []
    assert record.discards == 1
    assert session.review_last() is None
    assert preedit.calls == []
    if isinstance(invalid_text, str):
        assert invalid_text not in caplog.text


def test_idle_clipboard_target_reports_armed_without_preflight_or_clipboard_access():
    order = []
    writer = FakeClipboardWriter(order)
    current = [OutputTargetConfig("clipboard")]
    session, _asr, _audio, preedit, _timers, _ = _session(
        output_target_reader=lambda: current[0],
        clipboard_writer=writer,
    )

    assert session.status().code == "clipboard-armed"
    assert writer.preflight_calls == 0
    assert writer.writes == []
    assert preedit.calls == []

    current[0] = OutputTargetConfig("caret")
    assert session.status().code == "status"


def test_clipboard_armed_never_overrides_a_higher_priority_last_outcome():
    order = []
    session, _asr, _audio, _preedit, _timers, _ = _session(
        output_target_reader=lambda: OutputTargetConfig("clipboard"),
        clipboard_writer=FakeClipboardWriter(order),
    )
    with session._lock:
        session._last_error_code = "clipboard-ready"

    assert session.status().code == "clipboard-ready"

    with session._lock:
        session._last_error_code = "provider-error"
    assert session.status().code == "provider-error"


def test_clipboard_ready_is_a_historical_success_outcome_not_live_contents():
    current = [OutputTargetConfig("clipboard")]
    order = []
    writer = FakeClipboardWriter(order)
    session, asr, _audio, _preedit, _timers, _ = _session(
        output_target_reader=lambda: current[0],
        clipboard_writer=writer,
        observation_result_handler=lambda reason: AdaptiveObservationResult(reason),
    )
    session.start()
    asr.on_result("copied once")
    asr.on_finish()
    assert writer.writes == ["copied once"]

    current[0] = OutputTargetConfig("caret")

    # The code reports the last successful delivery. It does not re-read or
    # claim ownership of whatever another application may now have copied.
    assert session.status().code == "clipboard-ready"


def test_clean_processor_failure_delivers_raw_and_preserves_observation():
    def fail(_provider_final, _mode):
        raise RuntimeError("private transcript must not escape")

    session, asr, _audio, preedit, timers, _order = _session(
        output_style_reader=lambda: OutputStyleConfig("clean"),
        output_delivery_factory=fail,
    )
    session.start()
    asr.on_result("我我继续。")

    asr.on_finish()

    final = next(call for call in preedit.calls if call[0] == "final")
    assert final[-1] == "我我继续。"
    assert session.state is VoiceState.OBSERVING
    assert len(timers) == 3


def test_malformed_delivery_factory_can_never_commit_arbitrary_terminal_text():
    session, asr, _audio, preedit, _timers, _order = _session(
        output_style_reader=lambda: OutputStyleConfig("clean"),
        output_delivery_factory=lambda _raw, _mode: OutputDelivery(
            mode="clean",
            text="arbitrary replacement",
            processor="openvoice-clean-expression",
            processor_version=1,
            outcome="cleaned",
        ),
    )
    session.start()
    asr.on_result("raw provider final")

    asr.on_finish()

    final = next(call for call in preedit.calls if call[0] == "final")
    assert final[-1] == "raw provider final"
    assert "arbitrary replacement" not in repr(session.review_last())
    assert session.state is VoiceState.OBSERVING


def test_delivery_factory_cannot_inject_a_correction_outside_frozen_rules():
    unauthorized_edit = ConfirmedCorrectionEdit(
        start=0,
        end=4,
        source="Elas",
        replacement="UNAUTHORIZED",
    )

    def malicious(_raw, _mode, *, corrections):
        del corrections
        return OutputDelivery(
            mode="faithful",
            text="UNAUTHORIZED is ready",
            processor="identity",
            processor_version=1,
            outcome="faithful",
            correction_outcome="corrected",
            correction_edits=(unauthorized_edit,),
        )

    session, asr, _audio, preedit, _timers, _order = _session(
        output_delivery_factory=malicious,
    )
    asr.terminal_corrections = (CorrectionPair("Elas", "ILaaS"),)
    session.start()
    asr.on_result("Elas is ready")

    asr.on_finish()

    final = next(call for call in preedit.calls if call[0] == "final")
    assert final[-1] == "Elas is ready"


def test_explicit_review_learns_from_raw_provider_not_cleaned_delivery():
    submissions = []
    session, asr, _audio, _preedit, _timers, _order = _session(
        output_style_reader=lambda: OutputStyleConfig("clean"),
        observation_result_handler=lambda reason: AdaptiveObservationResult(reason),
        explicit_feedback_handler=lambda raw, spoken: (
            submissions.append((raw, spoken))
            or AdaptiveObservationResult("explicit-feedback-activated")
        ),
    )
    session.start()
    asr.on_result("我我觉得，呃，可以。")
    asr.on_finish()

    reply = session.submit_last_review("utterance-1", "我觉得可以。")

    assert reply.ok is True
    assert submissions == [("我我觉得，呃，可以。", "我觉得可以。")]


@pytest.mark.parametrize("failure", ("factory", "start"))
def test_observation_timer_failure_cancels_and_restores_immediately(failure):
    order = []
    asr = FakeASR(order)
    audio = FakeAudio(order)
    preedit = FakePreedit(order)
    timers = []

    def timer_factory(seconds, callback):
        if len(timers) == 2 and failure == "factory":
            raise RuntimeError("no timer")
        timer = FakeTimer(seconds, callback)
        timers.append(timer)
        if len(timers) == 3 and failure == "start":
            timer.start = lambda: (_ for _ in ()).throw(RuntimeError("no thread"))
        return timer

    session = VoiceSession(
        VoiceConfig("test-key"),
        asr_client=asr,
        audio_capture=audio,
        preedit_client=preedit,
        timer_factory=timer_factory,
        utterance_factory=lambda: "utterance-1",
    )
    session.start()
    asr.on_result("final")

    asr.on_finish()

    assert session.state is VoiceState.IDLE
    assert session.status().code == "adaptive-correction-failed"
    assert [call[0] for call in preedit.calls][-2:] == ["final", "cancel"]


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
    assert session.state is VoiceState.OBSERVING
    timers[3].fire()
    assert session.state is VoiceState.IDLE
    assert [call[0] for call in preedit.calls].count("final") == 1
    final_call = next(call for call in preedit.calls if call[0] == "final")
    assert final_call[-1] == "authoritative"


def test_observation_learns_once_after_provider_and_capture_are_closed():
    learned = []
    session, asr, audio, preedit, timers, order = _session(
        observation_handler=lambda snapshot: learned.append(snapshot) or True,
    )
    preedit.observation_result = ObservationSnapshot(
        baseline_text="奔驰 mark",
        committed_start=0,
        committed_end=7,
        current_text="bench mark",
        cursor=10,
        anchor=10,
    )
    session.start()
    asr.on_result("奔驰 mark")

    asr.on_finish()

    assert session.state is VoiceState.OBSERVING
    assert audio.callback is None
    assert asr.disconnected == 1
    assert timers[2].seconds == pytest.approx(4.5, abs=0.01)
    assert learned == []

    timers[2].fire()
    timers[2].fire()

    assert session.state is VoiceState.IDLE
    assert learned == [preedit.observation_result]
    assert session.status().code == "adaptive-correction-learned"
    assert [call[0] for call in preedit.calls].count("finish-observation") == 1


def test_unsupported_surrounding_restores_without_waiting_and_keeps_review():
    outcomes = []

    def report(reason):
        outcomes.append(reason)
        return AdaptiveObservationResult(reason_code=reason)

    session, asr, _audio, preedit, timers, _order = _session(
        observation_result_handler=report,
    )
    preedit.observation_supported = False
    session.start()
    asr.on_result("provider final for explicit review")

    asr.on_finish()

    assert session.state is VoiceState.IDLE
    assert outcomes == ["surrounding-text-unavailable"]
    assert session.status().code == "adaptive-correction-skipped"
    assert [call[0] for call in preedit.calls].count("finish-observation") == 0
    assert len(timers) == 2
    review = session.review_last()
    assert review is not None
    assert review.provider_text == "provider final for explicit review"


def test_observation_timer_accounts_for_the_final_dbus_round_trip():
    now = [10.0]
    session, asr, audio, preedit, timers, order = _session(
        monotonic=lambda: now[0],
        observation_handler=lambda snapshot: False,
    )
    preedit.final_hook = lambda: now.__setitem__(0, 10.25)
    session.start()
    asr.on_result("Ostro")

    asr.on_finish()

    assert session.state is VoiceState.OBSERVING
    assert timers[2].seconds == (
        ADAPTIVE_OBSERVATION_SECONDS - ADAPTIVE_OBSERVATION_FINISH_MARGIN_SECONDS - 0.25
    )


def test_cancel_during_observation_discards_and_restores_without_learning():
    learned = []
    session, asr, audio, preedit, timers, order = _session(
        observation_handler=lambda snapshot: learned.append(snapshot) or True,
    )
    session.start()
    asr.on_result("private final")
    asr.on_finish()

    reply = session.cancel()
    timers[2].fire()

    assert reply.code == "cancelled"
    assert session.state is VoiceState.IDLE
    assert learned == []
    assert [call[0] for call in preedit.calls].count("cancel") == 1
    assert [call[0] for call in preedit.calls].count("finish-observation") == 0


def test_delayed_observation_timer_restores_but_does_not_learn_after_deadline():
    now = [0.0]
    learned = []
    session, asr, audio, preedit, timers, order = _session(
        monotonic=lambda: now[0],
        observation_handler=lambda snapshot: learned.append(snapshot) or True,
    )
    preedit.observation_result = ObservationSnapshot(
        baseline_text="Ostro",
        committed_start=0,
        committed_end=5,
        current_text="Austral",
        cursor=7,
        anchor=7,
    )
    session.start()
    asr.on_result("Ostro")
    asr.on_finish()
    now[0] = ADAPTIVE_OBSERVATION_SECONDS + 0.001

    timers[2].fire()

    assert session.state is VoiceState.IDLE
    assert learned == []
    assert [call[0] for call in preedit.calls].count("finish-observation") == 1


def test_toggle_during_observation_finishes_then_starts_next_utterance():
    session, asr, audio, preedit, timers, order = _session()
    session.start()
    asr.on_result("first")
    asr.on_finish()

    reply = session.toggle()

    assert reply.code == "started"
    assert session.state is VoiceState.STARTING
    assert [call[0] for call in preedit.calls].count("finish-observation") == 1
    assert [call[0] for call in preedit.calls].count("acquire") == 2


def test_candidate_result_is_observable_and_offered_as_feedback_sidecar():
    feedback = []
    result = AdaptiveObservationResult(
        "candidates-saved",
        captured_count=1,
        candidate_count=1,
        replacement_hunks=2,
        candidates=(
            AdaptiveObservedCandidate(
                "Ostro", "Austral", "recognition", "medium", "candidate"
            ),
        ),
    )
    session, asr, audio, preedit, timers, order = _session(
        observation_handler=lambda snapshot: result,
        data_collection_feedback_writer=lambda utterance_id, document: feedback.append(
            (utterance_id, document)
        ),
    )
    preedit.observation_result = ObservationSnapshot("Ostro", 0, 5, "Austral", 7, 7)
    session.start()
    asr.on_result("Ostro")
    asr.on_finish()

    timers[2].fire()

    assert session.status().code == "adaptive-correction-candidate"
    assert feedback[0][0] == "utterance-1"
    assert feedback[0][1]["reason_code"] == "candidates-saved"
    assert set(feedback[0][1]) == {
        "reason_code",
        "captured_count",
        "activated_count",
        "candidate_count",
        "conflicted_count",
        "replacement_hunks",
        "corrections",
    }


def test_missing_surrounding_text_persists_reason_and_feedback():
    reasons = []
    feedback = []

    def report(reason):
        reasons.append(reason)
        return AdaptiveObservationResult(reason)

    session, asr, audio, preedit, timers, order = _session(
        observation_handler=lambda snapshot: pytest.fail("unexpected snapshot"),
        observation_result_handler=report,
        data_collection_feedback_writer=lambda utterance_id, document: feedback.append(
            (utterance_id, document)
        ),
    )
    session.start()
    asr.on_result("provider final")
    asr.on_finish()

    timers[2].fire()

    assert reasons == ["surrounding-text-unavailable"]
    assert feedback[0][0] == "utterance-1"
    assert feedback[0][1]["reason_code"] == "surrounding-text-unavailable"
    assert session.status().code == "adaptive-correction-skipped"


def test_feedback_sidecar_failure_is_not_hidden_by_learning_status():
    result = AdaptiveObservationResult(
        "active-learned",
        captured_count=1,
        activated_count=1,
        candidates=(
            AdaptiveObservedCandidate(
                "Ostro", "Austral", "recognition", "strong", "active"
            ),
        ),
    )

    def fail_feedback(_utterance_id, _document):
        raise OSError("simulated storage failure")

    session, asr, audio, preedit, timers, order = _session(
        observation_handler=lambda snapshot: result,
        data_collection_feedback_writer=fail_feedback,
    )
    preedit.observation_result = ObservationSnapshot("Ostro", 0, 5, "Austral", 7, 7)
    session.start()
    asr.on_result("Ostro")
    asr.on_finish()

    timers[2].fire()

    assert session.status().code == "data-collection-failed"


def test_invalid_hot_reload_context_never_opens_microphone_or_network():
    order = []
    audio = FakeAudio(order)
    preedit = FakePreedit(order)

    def fail_factory():
        raise ConfigError("private value must not be logged")

    session = VoiceSession(
        VoiceConfig("test-key"),
        asr_client_factory=fail_factory,
        audio_capture=audio,
        preedit_client=preedit,
        utterance_factory=lambda: "utterance-1",
    )

    reply = session.start()

    assert reply.code == "recognition-context-invalid"
    assert reply.state is VoiceState.IDLE
    assert audio.started == 0
    assert order == ["preedit-acquire", "audio-stop"]
    assert [call[0] for call in preedit.calls] == ["acquire", "cancel"]


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
