from __future__ import annotations

import json
import stat

import pytest

from murmur_voice.config import ConfigError
from murmur_voice.interaction import (
    InteractionConfig,
    InteractionController,
    load_interaction_config,
    save_interaction_config,
)
from murmur_voice.state import CommandReply, VoiceState


class FakeTimer:
    def __init__(self, seconds, callback):
        self.seconds = seconds
        self.callback = callback
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class FakeSession:
    def __init__(self, state=VoiceState.IDLE):
        self.state = state
        self.calls = []
        self.start_reply = None

    def start(self):
        self.calls.append("start")
        if self.start_reply is not None:
            return self.start_reply
        self.state = VoiceState.STARTING
        return CommandReply(True, "started", self.state)

    def stop(self):
        self.calls.append("stop")
        if self.state not in (VoiceState.STARTING, VoiceState.RECORDING):
            return CommandReply(False, "no-active-session", self.state)
        self.state = VoiceState.STOPPING
        return CommandReply(True, "stopping", self.state)

    def toggle(self):
        self.calls.append("toggle")
        if self.state is VoiceState.OBSERVING:
            self.state = VoiceState.IDLE
        if self.state is VoiceState.IDLE:
            self.state = VoiceState.STARTING
            return CommandReply(True, "started", self.state)
        if self.state in (VoiceState.STARTING, VoiceState.RECORDING):
            self.state = VoiceState.STOPPING
            return CommandReply(True, "stopping", self.state)
        return CommandReply(True, "already-stopping", self.state)

    def cancel(self):
        self.calls.append("cancel")
        if self.state is VoiceState.IDLE:
            return CommandReply(False, "no-active-session", self.state)
        self.state = VoiceState.IDLE
        return CommandReply(True, "cancelled", self.state)


def _controller(session, config, *, now=None, timers=None):
    clock = now if now is not None else [0.0]
    created = timers if timers is not None else []

    def timer_factory(seconds, callback):
        timer = FakeTimer(seconds, callback)
        created.append(timer)
        return timer

    controller = InteractionController(
        session,
        config_reader=lambda: config,
        monotonic=lambda: clock[0],
        timer_factory=timer_factory,
    )
    return controller, clock, created


def test_interaction_config_defaults_and_private_round_trip(tmp_path):
    path = tmp_path / "private" / "interaction.json"

    assert load_interaction_config(path) == InteractionConfig()

    save_interaction_config("push_to_talk", 240, 90, path)

    assert load_interaction_config(path) == InteractionConfig("push_to_talk", 240, 90)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document == {
        "version": 1,
        "interaction_mode": "push_to_talk",
        "minimum_hold_milliseconds": 240,
        "release_timeout_seconds": 90,
    }


@pytest.mark.parametrize(
    "values",
    (
        ("unknown", 180, 120),
        ("toggle", -1, 120),
        ("toggle", True, 120),
        ("push_to_talk", 180, 4),
        ("push_to_talk", 180, 601),
    ),
)
def test_interaction_config_rejects_unbounded_or_ambiguous_values(values):
    with pytest.raises(ConfigError):
        InteractionConfig(*values)


def test_toggle_mode_acts_on_press_only_and_ignores_key_repeat():
    session = FakeSession()
    controller, _, timers = _controller(session, InteractionConfig())

    assert controller.press().code == "started"
    assert controller.press().code == "repeat-press-ignored"
    assert session.calls == ["toggle"]
    assert controller.release().code == "released"
    assert timers[0].cancelled

    assert controller.press().code == "stopping"
    assert session.calls == ["toggle", "toggle"]


def test_toggle_lost_release_rearms_without_stopping_recording():
    session = FakeSession()
    controller, _, timers = _controller(session, InteractionConfig())

    controller.press()
    timers[0].fire()

    assert session.calls == ["toggle"]
    assert session.state is VoiceState.STARTING
    assert controller.press().code == "stopping"


def test_push_to_talk_starts_on_press_and_stops_on_release():
    session = FakeSession()
    controller, now, timers = _controller(
        session, InteractionConfig("push_to_talk", 180, 120)
    )

    assert controller.press().code == "started"
    now[0] = 0.5
    assert controller.release().code == "stopping"

    assert session.calls == ["start", "stop"]
    assert timers[0].seconds == 120.0
    assert timers[0].cancelled


def test_short_push_to_talk_cancels_instead_of_committing():
    session = FakeSession()
    controller, now, _ = _controller(
        session, InteractionConfig("push_to_talk", 200, 120)
    )

    controller.press()
    now[0] = 0.199
    reply = controller.release()

    assert reply.code == "short-press-cancelled"
    assert reply.state is VoiceState.IDLE
    assert session.calls == ["start", "cancel"]


def test_lost_release_watchdog_stops_owned_session_and_rearms():
    session = FakeSession()
    controller, _, timers = _controller(
        session, InteractionConfig("push_to_talk", 0, 30)
    )

    controller.press()
    timers[0].fire()

    assert session.calls == ["start", "stop"]
    assert session.state is VoiceState.STOPPING
    session.state = VoiceState.IDLE
    assert controller.press().code == "started"


def test_stray_release_never_stops_an_external_recording():
    session = FakeSession(VoiceState.RECORDING)
    controller, _, _ = _controller(session, InteractionConfig("push_to_talk", 0, 30))

    assert controller.release().code == "stray-release-ignored"
    assert session.calls == []
    assert session.state is VoiceState.RECORDING


def test_out_of_order_repeat_arriving_after_release_cannot_start_again():
    session = FakeSession()
    controller, _, _ = _controller(session, InteractionConfig("push_to_talk", 0, 30))

    assert controller.press(event_time=10.0).code == "started"
    assert controller.release(event_time=12.0).code == "stopping"
    session.state = VoiceState.IDLE

    assert controller.press(event_time=11.0).code == "stale-edge-ignored"
    assert session.calls == ["start", "stop"]


def test_escape_clears_held_state_and_cancels_once():
    session = FakeSession()
    controller, _, timers = _controller(
        session, InteractionConfig("push_to_talk", 0, 30)
    )
    controller.press()

    assert controller.cancel().code == "cancelled"
    assert controller.release().code == "stray-release-ignored"
    timers[0].fire()

    assert session.calls == ["start", "cancel"]


def test_push_to_talk_press_during_observation_uses_atomic_toggle():
    session = FakeSession(VoiceState.OBSERVING)
    controller, now, _ = _controller(session, InteractionConfig("push_to_talk", 0, 30))

    assert controller.press().code == "started"
    now[0] = 1.0
    assert controller.release().code == "stopping"
    assert session.calls == ["toggle", "stop"]


def test_start_failure_and_invalid_config_do_not_latch_pressed_state():
    session = FakeSession()
    session.start_reply = CommandReply(False, "provider-error", VoiceState.IDLE)
    controller, _, _ = _controller(session, InteractionConfig("push_to_talk", 0, 30))

    assert controller.press().code == "provider-error"
    assert controller.release().code == "stray-release-ignored"

    invalid = InteractionController(
        session,
        config_reader=lambda: (_ for _ in ()).throw(ConfigError("private")),
    )
    assert invalid.press().code == "interaction-config-invalid"
    assert invalid.release().code == "stray-release-ignored"


def test_watchdog_creation_failure_prevents_session_start():
    session = FakeSession()
    controller = InteractionController(
        session,
        config_reader=lambda: InteractionConfig("push_to_talk", 0, 30),
        timer_factory=lambda seconds, callback: (_ for _ in ()).throw(
            RuntimeError("no timer")
        ),
    )

    assert controller.press().code == "interaction-safety-unavailable"
    assert session.calls == []
