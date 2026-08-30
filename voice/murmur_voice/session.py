"""Thread-safe lifecycle joining capture, ASR, and the Preedit1 client."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from .audio import AudioCapture, AudioDeviceError, MicrophonePolicyError
from .config import ConfigError, VoiceConfig
from .data_collection import DataCollectionError
from .preedit import AcquireResult, PreeditClient
from .state import CommandReply, VoiceState
from .volcengine import AudioBackpressureError

logger = logging.getLogger(__name__)

# Production PreeditClient uses at most one direct/Flatpak-host ibus command.
# Its pessimistic acquisition path is bounded by roughly 28.25 seconds:
# saved-engine recovery (10 s), current-engine lookup (3 s), temporary switch
# (7 s), D-Bus retry (1.25 s), and failure restoration (7 s).  Each switch
# includes a 3 s command plus a 1 s verify window whose last 3 s query may
# cross the window.  The larger
# whole-start bound also contains the 3 s forward microphone preflight and scheduling
# margin.  Checkpoints prevent a timed-out control request from later opening
# the microphone.
PREEDIT_ACQUIRE_TIMEOUT_UPPER_BOUND_SECONDS = 29.0
VOICE_START_TIMEOUT_SECONDS = 35.0
# A timed-out acquired session can spend one 250 ms D-Bus Cancel plus one
# pessimistic 7 s IBus restore before replying to the control client.
VOICE_START_CLEANUP_TIMEOUT_SECONDS = 8.0
ADAPTIVE_OBSERVATION_SECONDS = 5.0
ADAPTIVE_OBSERVATION_FINISH_MARGIN_SECONDS = 0.5


class _VoiceStartTimeout(RuntimeError):
    pass


class _PreeditHeartbeatRejected(RuntimeError):
    pass


class VoiceSession:
    """Own exactly one voice utterance and never log transcript content."""

    def __init__(
        self,
        config: VoiceConfig,
        *,
        asr_client: Any | None = None,
        asr_client_factory: Any | None = None,
        audio_capture: Any | None = None,
        preedit_client: Any | None = None,
        timer_factory: Any | None = None,
        utterance_factory: Any | None = None,
        max_recording_seconds: float = 600.0,
        monotonic: Any | None = None,
        start_timeout_seconds: float = VOICE_START_TIMEOUT_SECONDS,
        observation_handler: Any | None = None,
        observation_result_handler: Any | None = None,
        observation_seconds: float = ADAPTIVE_OBSERVATION_SECONDS,
        data_collection_factory: Any | None = None,
        data_collection_status_reader: Any | None = None,
        data_collection_feedback_writer: Any | None = None,
        microphone_policy_validator: Any | None = None,
    ) -> None:
        if asr_client_factory is not None:
            self._asr_factory = asr_client_factory
        elif asr_client is not None:
            self._asr_factory = lambda: asr_client
        else:
            from .providers import create_asr_client

            self._asr_factory = lambda: create_asr_client(config)
        self._asr: Any | None = None
        self._audio = audio_capture or AudioCapture()
        self._preedit = preedit_client or PreeditClient()
        self._timer_factory = timer_factory or threading.Timer
        self._utterance_factory = utterance_factory or (lambda: uuid.uuid4().hex)
        self._max_recording_seconds = max(1.0, min(600.0, float(max_recording_seconds)))
        self._monotonic = monotonic or time.monotonic
        self._start_timeout_seconds = max(1.0, float(start_timeout_seconds))
        self._observation_handler = observation_handler
        self._observation_result_handler = observation_result_handler
        self._observation_seconds = max(0.1, min(30.0, float(observation_seconds)))
        self._data_collection_factory = data_collection_factory
        self._data_collection_status_reader = data_collection_status_reader
        self._data_collection_feedback_writer = data_collection_feedback_writer
        self._microphone_policy_validator = microphone_policy_validator

        self._lock = threading.RLock()
        self._state = VoiceState.IDLE
        self._utterance_id: str | None = None
        self._revision = 0
        self._latest_text = ""
        self._recording_timer: Any | None = None
        self._warning_timer: Any | None = None
        self._duration_warning = False
        self._final_timer: Any | None = None
        self._observation_timer: Any | None = None
        self._observation_deadline: float | None = None
        self._last_error_code = "none"
        self._closed = False
        self._session_serial = 0
        self._data_record: Any | None = None

    @property
    def state(self) -> VoiceState:
        with self._lock:
            return self._state

    def status(self) -> CommandReply:
        with self._lock:
            collection_code = "none"
            if self._data_collection_status_reader is not None:
                try:
                    collection_code = self._data_collection_status_reader()
                except Exception:
                    collection_code = "data-collection-failed"
            if self._duration_warning and self._state in (
                VoiceState.STARTING,
                VoiceState.RECORDING,
            ):
                code = "recording-limit-warning"
            elif collection_code == "data-collection-failed":
                code = collection_code
            elif self._last_error_code != "none":
                code = self._last_error_code
            else:
                code = "status"
            return CommandReply(True, code, self._state)

    def start(self) -> CommandReply:
        with self._lock:
            if self._closed:
                return CommandReply(False, "daemon-closed", self._state)
            if self._state is not VoiceState.IDLE:
                return CommandReply(False, "session-active", self._state)

            if self._microphone_policy_validator is not None:
                try:
                    self._microphone_policy_validator()
                except Exception:
                    # The dedicated validator never forwards private file
                    # content. Fail before acquiring preedit focus, probing or
                    # repairing audio profiles, or constructing a provider.
                    logger.error("Microphone priority could not be loaded safely")
                    self._last_error_code = "microphone-policy-invalid"
                    return CommandReply(
                        False,
                        "microphone-policy-invalid",
                        self._state,
                    )

            utterance_id = str(self._utterance_factory())
            start_deadline = self._monotonic() + self._start_timeout_seconds
            self._session_serial += 1
            session_serial = self._session_serial
            self._state = VoiceState.STARTING
            self._last_error_code = "none"
            acquisition = self._preedit.acquire_result(utterance_id)
            if acquisition is not AcquireResult.ACQUIRED:
                self._state = VoiceState.IDLE
                code = (
                    "preedit-unavailable"
                    if acquisition is AcquireResult.UNAVAILABLE
                    else "preedit-rejected"
                )
                self._last_error_code = code
                return CommandReply(False, code, self._state)

            self._utterance_id = utterance_id
            self._revision = 0
            self._latest_text = ""
            try:
                self._require_start_time(start_deadline)
                asr = self._asr_factory()
                self._asr = asr
                asr.on_open = lambda: self._on_asr_open(session_serial, asr)
                asr.on_result = lambda text: self._on_asr_result(
                    session_serial, asr, text
                )
                asr.on_finish = lambda: self._on_asr_finish(session_serial, asr)
                asr.on_error = lambda error: self._on_asr_error(
                    session_serial, asr, error
                )
                asr.on_auth_error = lambda: self._on_asr_auth_error(session_serial, asr)
                self._require_start_time(start_deadline)
                prepare_audio = getattr(self._audio, "prepare", None)
                if callable(prepare_audio):
                    # Resolve/repair the input before the provider thread can
                    # connect. This preflight opens no microphone and uploads
                    # no audio.
                    prepare_audio()
                self._require_start_time(start_deadline)
                # Acquire can be invalidated by a focus change while the
                # bounded microphone preflight runs.  An empty Partial uses the
                # existing ABI as a session/focus heartbeat without displaying
                # text; only a still-focused engine can accept it.
                if not self._preedit.partial(utterance_id, 1, ""):
                    raise _PreeditHeartbeatRejected
                self._revision = 1
                self._require_start_time(start_deadline)
                self._begin_data_record_locked(utterance_id, asr)
                # The provider immediately returns after creating its private
                # event-loop thread. Network work never runs in the IBus engine.
                asr.connect()
                self._require_start_time(start_deadline)
                data_record = self._data_record
                self._audio.start(
                    lambda data: self._send_audio_chunk(asr, data_record, data)
                )
                self._require_start_time(start_deadline)
                timer = self._timer_factory(
                    self._max_recording_seconds,
                    lambda: self._on_recording_timeout(session_serial),
                )
                if hasattr(timer, "daemon"):
                    timer.daemon = True
                self._recording_timer = timer
                timer.start()
                warning_delay = self._max_recording_seconds - 60.0
                if warning_delay > 0:
                    warning_timer = self._timer_factory(
                        warning_delay,
                        lambda: self._on_recording_warning(session_serial),
                    )
                    if hasattr(warning_timer, "daemon"):
                        warning_timer.daemon = True
                    self._warning_timer = warning_timer
                    warning_timer.start()
            except _VoiceStartTimeout:
                logger.error("Voice session start exceeded its safe deadline")
                self._abort_locked("start-timeout")
                return CommandReply(False, "start-timeout", self._state)
            except _PreeditHeartbeatRejected:
                logger.warning("Focused preedit was lost during microphone preflight")
                self._abort_locked("preedit-lost")
                return CommandReply(False, "preedit-lost", self._state)
            except ConfigError:
                logger.error("Recognition context could not be loaded safely")
                self._abort_locked("recognition-context-invalid")
                return CommandReply(False, "recognition-context-invalid", self._state)
            except MicrophonePolicyError:
                logger.error("Microphone priority could not be loaded safely")
                self._abort_locked("microphone-policy-invalid")
                return CommandReply(False, "microphone-policy-invalid", self._state)
            except AudioDeviceError:
                logger.error("No usable microphone is available")
                self._abort_locked("microphone-unavailable")
                return CommandReply(False, "microphone-unavailable", self._state)
            except Exception:
                logger.error("Voice session failed to start")
                self._abort_locked("capture-start-failed")
                return CommandReply(False, "capture-start-failed", self._state)
            return CommandReply(True, "started", self._state)

    def _require_start_time(self, deadline: float) -> None:
        if self._monotonic() >= deadline:
            raise _VoiceStartTimeout

    def stop(self) -> CommandReply:
        with self._lock:
            if self._state is VoiceState.STOPPING:
                return CommandReply(True, "already-stopping", self._state)
            if self._state not in (VoiceState.STARTING, VoiceState.RECORDING):
                return CommandReply(False, "no-active-session", self._state)
            asr = self._asr
            if asr is None:
                self._abort_locked("provider-error")
                return CommandReply(False, "provider-error", self._state)
            self._cancel_warning_timer_locked()
            self._cancel_recording_timer_locked()
            self._state = VoiceState.STOPPING
            try:
                self._audio.stop()
            except Exception:
                logger.error("Audio capture stop failed")
            self._stop_data_record_audio_locked()
            asr.finish_sending()
            session_serial = self._session_serial
            timer = self._timer_factory(
                float(getattr(asr, "final_result_timeout", 20.0)),
                lambda: self._on_final_timeout(session_serial),
            )
            if hasattr(timer, "daemon"):
                timer.daemon = True
            self._final_timer = timer
            timer.start()
            return CommandReply(True, "stopping", self._state)

    def toggle(self) -> CommandReply:
        with self._lock:
            state = self._state
            if state is VoiceState.OBSERVING:
                self._finish_observation_locked(self._session_serial)
                state = self._state
        if state is VoiceState.IDLE:
            return self.start()
        if state in (VoiceState.STARTING, VoiceState.RECORDING):
            return self.stop()
        return CommandReply(True, "already-stopping", state)

    def cancel(self) -> CommandReply:
        with self._lock:
            if self._state is VoiceState.IDLE:
                return CommandReply(False, "no-active-session", self._state)
            self._abort_locked("cancelled")
            return CommandReply(True, "cancelled", self._state)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._state is not VoiceState.IDLE:
                self._abort_locked("daemon-shutdown")
            self._closed = True
            self._preedit.close()

    def _on_asr_open(self, session_serial: int, source: Any) -> None:
        with self._lock:
            if not self._is_current_source_locked(session_serial, source):
                return
            if self._state is VoiceState.STARTING:
                self._state = VoiceState.RECORDING

    def _on_asr_result(self, session_serial: int, source: Any, text: str) -> None:
        with self._lock:
            if not self._is_current_source_locked(session_serial, source):
                return
            utterance_id = self._utterance_id
            if utterance_id is None or self._state not in (
                VoiceState.STARTING,
                VoiceState.RECORDING,
                VoiceState.STOPPING,
            ):
                return
            revision = self._revision + 1
            if not self._preedit.partial(utterance_id, revision, text):
                logger.warning("Focused preedit rejected an ASR partial")
                self._abort_locked("preedit-lost")
                return
            self._revision = revision
            self._latest_text = text
            if self._state is VoiceState.STARTING:
                self._state = VoiceState.RECORDING

    def _on_asr_finish(self, session_serial: int, source: Any) -> None:
        with self._lock:
            if not self._is_current_source_locked(session_serial, source):
                return
            if self._state is VoiceState.IDLE or self._utterance_id is None:
                return
            self._cancel_final_timer_locked()
            utterance_id = self._utterance_id
            provider_final = self._latest_text
            # Stop capture before final delivery. Optional retention is only
            # offered after the same authoritative final was accepted by the
            # focused IBus client.
            self._disconnect_provider_locked()
            accepted = False
            observation_deadline = None
            if provider_final:
                # Start the window before the synchronous Final D-Bus call.
                # The engine starts its own bound while handling that call, so
                # this side will finish first even after round-trip latency.
                observation_deadline = self._monotonic() + self._observation_seconds
                accepted = self._preedit.final(
                    utterance_id,
                    self._revision + 1,
                    provider_final,
                )
            else:
                self._preedit.cancel(utterance_id)

            if provider_final and not accepted:
                # final() may reject before making its D-Bus call. cancel() is
                # harmless if final() already cleared the client session.
                self._preedit.cancel(utterance_id)
                self._last_error_code = "preedit-final-rejected"
                self._discard_data_record_locked()
                self._clear_sensitive_state_locked()
                return
            if not provider_final:
                self._discard_data_record_locked()
                self._last_error_code = "none"
                self._clear_sensitive_state_locked()
                return

            collection_failed = not self._commit_data_record_locked(provider_final)
            self._last_error_code = (
                "data-collection-failed" if collection_failed else "none"
            )
            self._revision = 0
            self._latest_text = ""
            self._duration_warning = False
            self._state = VoiceState.OBSERVING
            assert observation_deadline is not None
            self._observation_deadline = observation_deadline
            remaining = max(
                0.0,
                observation_deadline
                - self._monotonic()
                - ADAPTIVE_OBSERVATION_FINISH_MARGIN_SECONDS,
            )
            try:
                timer = self._timer_factory(
                    remaining,
                    lambda: self._on_observation_timeout(session_serial),
                )
                if hasattr(timer, "daemon"):
                    timer.daemon = True
                self._observation_timer = timer
                timer.start()
            except Exception:
                # If a thread/timer cannot be armed, immediately cancel the
                # just-committed observation so murmur-voice cannot remain the
                # selected IBus engine indefinitely.
                self._observation_timer = None
                logger.error("Adaptive observation timer could not be armed")
                self._abort_locked("adaptive-correction-failed")

    def _on_asr_error(
        self, session_serial: int, source: Any, error: BaseException
    ) -> None:
        code = (
            "audio-backpressure"
            if isinstance(error, AudioBackpressureError)
            else "provider-error"
        )
        with self._lock:
            if not self._is_current_source_locked(session_serial, source):
                return
            if self._state is VoiceState.IDLE:
                return
            logger.error("Voice provider session failed (%s)", code)
            self._abort_locked(code)

    def _on_asr_auth_error(self, session_serial: int, source: Any) -> None:
        with self._lock:
            if not self._is_current_source_locked(session_serial, source):
                return
            if self._state is VoiceState.IDLE:
                return
            logger.error("Voice provider authentication failed")
            self._abort_locked("provider-auth")

    def _on_final_timeout(self, session_serial: int) -> None:
        with self._lock:
            if (
                session_serial != self._session_serial
                or self._state is not VoiceState.STOPPING
            ):
                return
            # A live hypothesis is not authoritative. Never commit it after a
            # missing connection-level final because focus may also have moved.
            logger.error("Voice provider final result timed out")
            self._final_timer = None
            self._abort_locked("final-timeout")

    def _on_recording_timeout(self, session_serial: int) -> None:
        with self._lock:
            if session_serial != self._session_serial:
                return
            self._recording_timer = None
            if self._state not in (
                VoiceState.STARTING,
                VoiceState.RECORDING,
            ):
                return
            logger.info("Voice recording reached the local duration limit")
            # Auto-stop still requests the provider's authoritative two-pass
            # final. If that final never arrives, the existing final timeout
            # cancels preedit instead of committing a live hypothesis.
            self.stop()

    def _on_recording_warning(self, session_serial: int) -> None:
        with self._lock:
            if session_serial != self._session_serial or self._state not in (
                VoiceState.STARTING,
                VoiceState.RECORDING,
            ):
                return
            self._warning_timer = None
            self._duration_warning = True
            logger.info("Voice recording will stop in 60 seconds")

    def _on_observation_timeout(self, session_serial: int) -> None:
        with self._lock:
            if (
                session_serial != self._session_serial
                or self._state is not VoiceState.OBSERVING
            ):
                return
            self._observation_timer = None
            self._finish_observation_locked(session_serial)

    def _finish_observation_locked(self, session_serial: int) -> None:
        if (
            session_serial != self._session_serial
            or self._state is not VoiceState.OBSERVING
        ):
            return
        self._cancel_observation_timer_locked()
        utterance_id = self._utterance_id
        deadline = self._observation_deadline
        within_deadline = deadline is not None and self._monotonic() <= deadline
        snapshot = None
        if utterance_id is not None:
            snapshot = self._preedit.finish_observation(utterance_id)
        learned = False
        observation_result = None
        if (
            within_deadline
            and snapshot is not None
            and self._observation_handler is not None
        ):
            try:
                observation_result = self._observation_handler(snapshot)
                learned = observation_result is True or bool(
                    getattr(observation_result, "learned", False)
                )
            except Exception:
                # Never include a transcript or pair in the diagnostic.
                logger.error("Adaptive correction update failed")
                self._last_error_code = "adaptive-correction-failed"
        elif self._observation_result_handler is not None:
            reason_code = (
                "observation-timeout"
                if not within_deadline
                else (
                    "surrounding-text-unavailable"
                    if snapshot is None
                    else "observation-handler-unavailable"
                )
            )
            try:
                observation_result = self._observation_result_handler(reason_code)
            except Exception:
                logger.error("Adaptive correction outcome could not be saved")
                self._last_error_code = "adaptive-correction-failed"
        if utterance_id is not None and observation_result is not None:
            self._write_data_feedback_locked(utterance_id, observation_result)
        if self._last_error_code != "data-collection-failed":
            if learned:
                self._last_error_code = "adaptive-correction-learned"
            elif getattr(observation_result, "conflicted_count", 0):
                self._last_error_code = "adaptive-correction-conflicted"
            elif getattr(observation_result, "candidate_count", 0):
                self._last_error_code = "adaptive-correction-candidate"
            elif (
                observation_result is not None
                and self._last_error_code != "adaptive-correction-failed"
            ):
                self._last_error_code = "adaptive-correction-skipped"
        self._clear_sensitive_state_locked()

    def _write_data_feedback_locked(
        self,
        utterance_id: str,
        observation_result: Any,
    ) -> None:
        writer = self._data_collection_feedback_writer
        if writer is None:
            return
        serializer = getattr(observation_result, "as_feedback_document", None)
        if not callable(serializer):
            return
        try:
            writer(utterance_id, serializer())
        except Exception:
            # Feedback is optional and must not break dictation or expose text.
            logger.error("Optional local correction feedback could not be saved")
            self._last_error_code = "data-collection-failed"

    def _abort_locked(self, code: str) -> None:
        self._cancel_warning_timer_locked()
        self._cancel_recording_timer_locked()
        self._cancel_final_timer_locked()
        self._cancel_observation_timer_locked()
        utterance_id = self._utterance_id
        try:
            self._audio.stop()
        except Exception:
            logger.error("Audio capture cleanup failed")
        self._discard_data_record_locked()
        asr = self._asr
        self._asr = None
        if asr is not None:
            asr.disconnect()
        if utterance_id is not None:
            self._preedit.cancel(utterance_id)
        self._clear_sensitive_state_locked()
        self._last_error_code = code

    def _reset_provider_locked(self) -> None:
        self._disconnect_provider_locked()
        self._discard_data_record_locked()
        self._clear_sensitive_state_locked()

    def _disconnect_provider_locked(self) -> None:
        self._cancel_warning_timer_locked()
        self._cancel_recording_timer_locked()
        self._cancel_final_timer_locked()
        try:
            self._audio.stop()
        except Exception:
            logger.error("Audio capture cleanup failed")
        self._stop_data_record_audio_locked()
        asr = self._asr
        self._asr = None
        if asr is not None:
            asr.disconnect()

    def _clear_sensitive_state_locked(self) -> None:
        self._state = VoiceState.IDLE
        self._utterance_id = None
        self._revision = 0
        self._latest_text = ""
        self._duration_warning = False
        self._observation_deadline = None

    def _begin_data_record_locked(self, utterance_id: str, asr: Any) -> None:
        self._data_record = None
        factory = self._data_collection_factory
        if factory is None:
            return
        try:
            record = factory(utterance_id)
            if record is not None:
                setter = getattr(record, "set_provider_identity", None)
                if callable(setter):
                    setter(
                        str(getattr(asr, "provider_name", "unknown")),
                        str(getattr(asr, "provider_model", "unknown")),
                        getattr(asr, "provider_resource_id", None),
                    )
            self._data_record = record
        except (ConfigError, DataCollectionError, OSError):
            # Optional retention must never prevent dictation or disclose the
            # selected path through logs.
            logger.error("Optional local data collection is unavailable")
            self._last_error_code = "data-collection-unavailable"
        except Exception:
            logger.error("Optional local data collection could not start")
            self._last_error_code = "data-collection-unavailable"

    @staticmethod
    def _send_audio_chunk(asr: Any, data_record: Any | None, data: bytes) -> None:
        asr.send_audio(data)
        if data_record is not None:
            data_record.add_audio(data)

    def _stop_data_record_audio_locked(self) -> None:
        record = self._data_record
        if record is None:
            return
        try:
            complete = record.stop_audio()
        except Exception:
            complete = False
        if not complete:
            logger.error("Optional local audio record is incomplete")
            self._last_error_code = "data-collection-failed"

    def _commit_data_record_locked(self, provider_final: str) -> bool:
        record = self._data_record
        self._data_record = None
        if record is None:
            return True
        if not provider_final:
            record.discard()
            return True
        try:
            record.commit(provider_final)
        except Exception:
            logger.error("Optional local data record could not be completed")
            return False
        return True

    def _discard_data_record_locked(self) -> None:
        record = self._data_record
        self._data_record = None
        if record is None:
            return
        try:
            record.discard()
        except Exception:
            logger.error("Optional local data record cleanup failed")

    def _cancel_final_timer_locked(self) -> None:
        timer = self._final_timer
        self._final_timer = None
        if timer is not None:
            timer.cancel()

    def _cancel_observation_timer_locked(self) -> None:
        timer = self._observation_timer
        self._observation_timer = None
        if timer is not None:
            timer.cancel()

    def _cancel_recording_timer_locked(self) -> None:
        timer = self._recording_timer
        self._recording_timer = None
        if timer is not None:
            timer.cancel()

    def _cancel_warning_timer_locked(self) -> None:
        timer = self._warning_timer
        self._warning_timer = None
        if timer is not None:
            timer.cancel()

    def _is_current_source_locked(self, session_serial: int, source: Any) -> bool:
        return (
            session_serial == self._session_serial
            and source is self._asr
            and self._utterance_id is not None
        )
