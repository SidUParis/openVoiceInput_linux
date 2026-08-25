from __future__ import annotations

import json
import os
import threading
import time

import pytest

from murmur_voice.audio import (
    AudioCapture,
    AudioDeviceError,
    BLOCK_SIZE,
    MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS,
    SAMPLE_RATE,
    _PreflightBudget,
    _PulseInputSelection,
    _resolve_pulse_portaudio_device,
    resolve_input_device as _resolve_input_device,
)


class FakePulseSoundDevice:
    def __init__(self, devices=None, rejected=()):
        self.devices = (
            [{"name": "pulse", "max_input_channels": 32}]
            if devices is None
            else devices
        )
        self.rejected = set(rejected)
        self.checked = []

    def query_devices(self, kind=None):
        assert kind is None
        return self.devices

    def check_input_settings(self, **kwargs):
        self.checked.append(kwargs)
        if kwargs.get("device") in self.rejected:
            raise RuntimeError("unsupported")


def resolve_input_device(**kwargs):
    """Use an explicit fake Pulse endpoint for pactl-focused unit tests."""

    if kwargs.get("pactl_runner") is not None and "sounddevice_module" not in kwargs:
        kwargs["sounddevice_module"] = FakePulseSoundDevice()
    return _resolve_input_device(**kwargs)


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


def test_failed_stream_start_is_closed_and_reported_as_device_error():
    stream = FakeStream()

    def fail_start():
        raise RuntimeError("simulated PortAudio failure")

    stream.start = fail_start
    capture = AudioCapture(stream_factory=lambda **kwargs: stream)

    with pytest.raises(AudioDeviceError, match="could not be opened"):
        capture.start(lambda data: None)

    assert stream.closed
    assert not capture.is_capturing


def test_audio_capture_resolves_again_for_every_recording():
    streams = []
    devices = iter((3, 7))

    def factory(**kwargs):
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream

    capture = AudioCapture(stream_factory=factory, input_resolver=lambda: next(devices))
    capture.start(lambda data: None)
    capture.stop()
    capture.start(lambda data: None)
    capture.stop()

    assert [stream.kwargs["device"] for stream in streams] == [3, 7]


def test_pulse_route_is_present_through_factory_and_start_then_restored(monkeypatch):
    source = "alsa_input.pci-test.analog-stereo"
    monkeypatch.setenv("PULSE_SOURCE", "preexisting.source")
    observed = []

    class InspectingStream(FakeStream):
        def start(self):
            observed.append(("start", os.environ.get("PULSE_SOURCE")))
            super().start()

    def factory(**kwargs):
        observed.append(("factory", os.environ.get("PULSE_SOURCE")))
        return InspectingStream(**kwargs)

    capture = AudioCapture(
        stream_factory=factory,
        input_resolver=lambda: _PulseInputSelection(source, 17),
    )
    capture.start(lambda data: None)

    assert observed == [("factory", source), ("start", source)]
    assert capture._stream.kwargs["device"] == 17
    assert os.environ["PULSE_SOURCE"] == "preexisting.source"
    assert capture.is_capturing
    capture.stop()


@pytest.mark.parametrize("failure_phase", ["factory", "start"])
def test_pulse_route_environment_is_restored_after_open_failure(
    monkeypatch, failure_phase
):
    source = "alsa_input.pci-test.analog-stereo"
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    stream = FakeStream()

    if failure_phase == "start":

        def fail_start():
            assert os.environ.get("PULSE_SOURCE") == source
            raise RuntimeError("simulated failure")

        stream.start = fail_start

    def factory(**kwargs):
        assert os.environ.get("PULSE_SOURCE") == source
        if failure_phase == "factory":
            raise RuntimeError("simulated failure")
        stream.kwargs = kwargs
        return stream

    capture = AudioCapture(
        stream_factory=factory,
        input_resolver=lambda: _PulseInputSelection(source, 17),
    )

    with pytest.raises(AudioDeviceError, match="could not be opened"):
        capture.start(lambda data: None)

    assert "PULSE_SOURCE" not in os.environ
    if failure_phase == "start":
        assert stream.closed


def test_nonpulse_open_waits_for_concurrent_pulse_environment(monkeypatch):
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    first_entered = threading.Event()
    release_first = threading.Event()
    observations = []

    class BlockingStream(FakeStream):
        def __init__(self, route, **kwargs):
            super().__init__(**kwargs)
            self.route = route

        def start(self):
            observations.append(("start", self.route, os.environ.get("PULSE_SOURCE")))
            if self.route == "alsa_input.first":
                first_entered.set()
                assert release_first.wait(timeout=1)
            super().start()

    def factory(**kwargs):
        source = os.environ.get("PULSE_SOURCE")
        route = source or f"device:{kwargs.get('device')}"
        observations.append(("factory", route, source))
        return BlockingStream(route, **kwargs)

    first = AudioCapture(
        stream_factory=factory,
        input_resolver=lambda: _PulseInputSelection("alsa_input.first", 17),
    )
    second = AudioCapture(
        stream_factory=factory,
        input_resolver=lambda: 7,
    )
    threads = [
        threading.Thread(target=capture.start, args=(lambda data: None,))
        for capture in (first, second)
    ]
    threads[0].start()
    assert first_entered.wait(timeout=1)
    threads[1].start()
    time.sleep(0.05)
    assert [item[1] for item in observations] == [
        "alsa_input.first",
        "alsa_input.first",
    ]
    release_first.set()
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()

    assert observations == [
        ("factory", "alsa_input.first", "alsa_input.first"),
        ("start", "alsa_input.first", "alsa_input.first"),
        ("factory", "device:7", None),
        ("start", "device:7", None),
    ]
    assert "PULSE_SOURCE" not in os.environ
    first.stop()
    second.stop()


class FakePactl:
    def __init__(self, *, sources: str, default: str, cards=None):
        self.sources = sources
        self.initial_sources = sources
        self.default = default
        self.cards = [] if cards is None else cards
        self.calls = []
        self.after_profile_sources = None
        self.after_profile_json_sources = []
        self.fail = set()
        self.concurrent_default_on_source_identity_read = None
        self.concurrent_default_on_cards_read = None
        self.concurrent_profile_on_cards_read = None
        self._cards_reads = 0

    def __call__(self, arguments):
        command = tuple(arguments)
        self.calls.append(command)
        if command in self.fail:
            raise RuntimeError("simulated pactl failure")
        if command == ("list", "short", "sources"):
            return self.sources
        if command == ("get-default-source",):
            return self.default + "\n" if self.default else ""
        if command == ("--format=json", "list", "cards"):
            self._cards_reads += 1
            if (
                self.concurrent_default_on_cards_read is not None
                and self._cards_reads == self.concurrent_default_on_cards_read[0]
            ):
                self.default = self.concurrent_default_on_cards_read[1]
            if (
                self.concurrent_profile_on_cards_read is not None
                and self._cards_reads == self.concurrent_profile_on_cards_read[0]
            ):
                for card in self.cards if isinstance(self.cards, list) else ():
                    if card.get("name") == "alsa_card.pci-test":
                        card["active_profile"] = self.concurrent_profile_on_cards_read[
                            1
                        ]
            if isinstance(self.cards, str):
                return self.cards
            return json.dumps(self.cards)
        if command == ("--format=json", "list", "sources"):
            if self.concurrent_default_on_source_identity_read is not None:
                self.default = self.concurrent_default_on_source_identity_read
                self.concurrent_default_on_source_identity_read = None
            return json.dumps(self.after_profile_json_sources)
        if command[:1] == ("set-card-profile",):
            for card in self.cards if isinstance(self.cards, list) else ():
                if card.get("name") == command[1]:
                    card["active_profile"] = command[2]
            if "+input:" in command[2] and self.after_profile_sources is not None:
                self.sources = self.after_profile_sources
            elif "+input:" not in command[2]:
                self.sources = self.initial_sources
            return ""
        if command == ("info",):
            return f"Default Source: {self.default}\n"
        raise AssertionError(f"unexpected pactl call: {command!r}")


def _short_source(index, name, state="SUSPENDED"):
    return f"{index}\t{name}\tPipeWire\ts32le 2ch 48000Hz\t{state}\n"


def _assert_pulse_selection(selection, source, portaudio_device=0):
    assert isinstance(selection, _PulseInputSelection)
    assert selection.source == source
    assert selection.portaudio_device == portaudio_device


def _json_source(
    name,
    card_index,
    state="SUSPENDED",
    device_class="sound",
    *,
    device_name=None,
    alsa_card=None,
    bus_path=None,
):
    properties = {
        "device.class": device_class,
        "media.class": "Audio/Source",
    }
    if device_name is not None:
        properties["device.name"] = device_name
    if alsa_card is not None:
        properties["alsa.card"] = alsa_card
    if bus_path is not None:
        properties["device.bus_path"] = bus_path
    return {
        "name": name,
        "card": card_index,
        "state": state,
        "properties": properties,
    }


def _card(
    name="alsa_card.pci-test",
    *,
    index=2,
    active="output:analog-stereo",
    candidate_priority=6565,
    candidate_available=True,
    candidate_sources=1,
    alsa_card=None,
    bus_path=None,
):
    card = {
        "name": name,
        "index": index,
        "active_profile": active,
        "profiles": {
            "output:analog-stereo": {
                "sinks": 1,
                "sources": 0,
                "priority": 6500,
                "available": True,
            },
            "output:analog-stereo+input:analog-stereo": {
                "sinks": 1,
                "sources": candidate_sources,
                "priority": candidate_priority,
                "available": candidate_available,
            },
            # Higher source count is not enough: changing the active output is
            # intentionally outside automatic recovery.
            "output:hdmi-stereo+input:analog-stereo": {
                "sinks": 1,
                "sources": 1,
                "priority": 9000,
                "available": True,
            },
        },
    }
    properties = {}
    if alsa_card is not None:
        properties["alsa.card"] = alsa_card
    if bus_path is not None:
        properties["device.bus_path"] = bus_path
    if properties:
        card["properties"] = properties
    return card


def test_valid_real_pulse_default_is_kept_without_mutation():
    real = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(8, real) + _short_source(9, "sink.monitor"),
        default=real,
    )

    _assert_pulse_selection(resolve_input_device(pactl_runner=pactl), real)
    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_monitor_default_is_left_unchanged_and_real_source_is_bound_per_stream():
    real = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(8, real) + _short_source(9, "sink.monitor"),
        default="sink.monitor",
    )

    _assert_pulse_selection(resolve_input_device(pactl_runner=pactl), real)
    assert pactl.default == "sink.monitor"
    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_unobservable_initial_default_fails_before_any_global_mutation():
    real = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(8, real) + _short_source(9, "sink.monitor"),
        default="sink.monitor",
    )
    pactl.fail.update({("get-default-source",), ("info",)})

    with pytest.raises(AudioDeviceError, match="could not be determined"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_output_only_profile_is_safely_extended_after_disconnect():
    real = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="bluez_input.disconnected",
        cards=[_card()],
    )
    pactl.after_profile_sources = _short_source(9, "sink.monitor") + _short_source(
        10, real
    )
    pactl.after_profile_json_sources = [_json_source(real, 2)]

    selection = resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)
    _assert_pulse_selection(selection, real)
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo+input:analog-stereo",
    ) in pactl.calls
    assert pactl.default == "bluez_input.disconnected"
    assert not any(call[0] == "set-default-source" for call in pactl.calls)
    assert not any(
        call[0] in {"set-source-mute", "set-source-volume"} for call in pactl.calls
    )


def test_pipewire_null_card_uses_matching_device_name_identity():
    real = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    pactl.after_profile_sources = _short_source(10, real)
    pactl.after_profile_json_sources = [
        _json_source(real, None, device_name="alsa_card.pci-test")
    ]

    selection = resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    _assert_pulse_selection(selection, real)
    assert pactl.default == "sink.monitor"


def test_pulseaudio_15_null_card_uses_alsa_and_bus_path_identity():
    real = "alsa_input.pci-test.analog-stereo"
    bus_path = "pci-0000:00:1f.3"
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card(index=1, alsa_card="0", bus_path=bus_path)],
    )
    pactl.after_profile_sources = _short_source(10, real)
    pactl.after_profile_json_sources = [
        _json_source(real, None, alsa_card="0", bus_path=bus_path)
    ]

    selection = resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    _assert_pulse_selection(selection, real)
    assert pactl.default == "sink.monitor"


@pytest.mark.parametrize(
    ("source_alsa_card", "source_bus_path"),
    [
        ("0", None),
        (None, "pci-0000:00:1f.3"),
        ("1", "pci-0000:00:1f.3"),
        ("0", "pci-0000:00:1e.0"),
    ],
)
def test_pulseaudio_15_identity_missing_or_conflicting_fails_closed(
    source_alsa_card, source_bus_path
):
    real = "alsa_input.pci-test.analog-stereo"
    bus_path = "pci-0000:00:1f.3"
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card(index=1, alsa_card="0", bus_path=bus_path)],
    )
    pactl.after_profile_sources = _short_source(10, real)
    pactl.after_profile_json_sources = [
        _json_source(
            real,
            None,
            alsa_card=source_alsa_card,
            bus_path=source_bus_path,
        )
    ]

    with pytest.raises(AudioDeviceError, match="did not appear"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    assert pactl.cards[0]["active_profile"] == "output:analog-stereo"
    assert ("set-default-source", real) not in pactl.calls


def test_conflicting_numeric_and_named_card_identity_is_rejected():
    real = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    pactl.after_profile_sources = _short_source(10, real)
    pactl.after_profile_json_sources = [
        _json_source(real, 2, device_name="alsa_card.someone-else")
    ]

    with pytest.raises(AudioDeviceError, match="did not appear"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    assert pactl.cards[0]["active_profile"] == "output:analog-stereo"
    assert ("set-default-source", real) not in pactl.calls


def test_two_sources_on_recovered_card_are_never_guessed():
    first = "alsa_input.pci-test.analog-stereo"
    second = "alsa_input.pci-test.alt"
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    pactl.after_profile_sources = _short_source(10, first) + _short_source(11, second)
    pactl.after_profile_json_sources = [
        _json_source(first, 2),
        _json_source(second, 2),
    ]

    with pytest.raises(AudioDeviceError, match="ambiguous"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    assert pactl.cards[0]["active_profile"] == "output:analog-stereo"
    assert not any(call[0] == "set-default-source" for call in pactl.calls)


def test_recovered_source_is_bound_to_changed_card_amid_concurrent_sources():
    real = "alsa_input.pci-test.analog-stereo"
    concurrent = "bluez_input.concurrent"
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    pactl.after_profile_sources = (
        _short_source(9, "sink.monitor")
        + _short_source(10, concurrent)
        + _short_source(11, real)
    )
    pactl.after_profile_json_sources = [
        _json_source(concurrent, 17),
        _json_source(real, 2),
    ]

    selection = resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    _assert_pulse_selection(selection, real)
    assert pactl.default == "sink.monitor"
    assert not any(call[0] == "set-default-source" for call in pactl.calls)


def test_unrelated_source_does_not_justify_leaving_recovered_profile():
    concurrent = "xrdp_input.concurrent"
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    pactl.after_profile_sources = _short_source(10, concurrent)
    pactl.after_profile_json_sources = [_json_source(concurrent, 17)]

    with pytest.raises(AudioDeviceError, match="did not appear"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    assert pactl.cards[0]["active_profile"] == "output:analog-stereo"
    assert ("set-default-source", concurrent) not in pactl.calls


def test_bad_card_json_fails_without_opening_a_silent_default():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards="not-json",
    )

    with pytest.raises(AudioDeviceError, match="invalid data"):
        resolve_input_device(pactl_runner=pactl)
    assert not any(call[0] == "set-card-profile" for call in pactl.calls)


def test_multiple_recoverable_cards_fail_instead_of_comparing_priorities():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[
            _card("alsa_card.pci-test", index=2, candidate_priority=6565),
            _card("alsa_card.usb-test", index=3, candidate_priority=9000),
        ],
    )
    with pytest.raises(AudioDeviceError, match="ambiguous"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)
    assert not any(call[0] == "set-card-profile" for call in pactl.calls)


def test_tied_card_profiles_fail_instead_of_guessing():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card("alsa_card.a", index=2), _card("alsa_card.b", index=3)],
    )

    with pytest.raises(AudioDeviceError, match="ambiguous"):
        resolve_input_device(pactl_runner=pactl)
    assert not any(call[0] == "set-card-profile" for call in pactl.calls)


def test_profile_command_failure_is_reported_and_does_not_continue():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    command = (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo+input:analog-stereo",
    )
    pactl.fail.add(command)

    with pytest.raises(AudioDeviceError, match="profile recovery failed"):
        resolve_input_device(pactl_runner=pactl)
    assert not any(call[0] == "set-default-source" for call in pactl.calls)


def test_pactl_disappearing_after_first_probe_never_uses_generic_fallback():
    real = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(sources=_short_source(8, real), default=real)
    sounddevice = FakePulseSoundDevice()

    def disappear_after_probe(arguments):
        if tuple(arguments) == ("get-default-source",):
            raise FileNotFoundError
        return pactl(arguments)

    with pytest.raises(AudioDeviceError, match="disappeared"):
        _resolve_input_device(
            pactl_runner=disappear_after_probe,
            sounddevice_module=sounddevice,
        )

    assert len(sounddevice.checked) == 1
    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_applied_profile_then_pactl_disappearance_rolls_back_and_fails_closed():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    new_profile = (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo+input:analog-stereo",
    )

    def apply_then_disappear(arguments):
        result = pactl(arguments)
        if tuple(arguments) == new_profile:
            raise FileNotFoundError
        return result

    with pytest.raises(AudioDeviceError, match="disappeared"):
        _resolve_input_device(
            pactl_runner=apply_then_disappear,
            sounddevice_module=FakePulseSoundDevice(),
            sleep=lambda seconds: None,
        )

    assert pactl.cards[0]["active_profile"] == "output:analog-stereo"
    assert pactl.calls[-1] == (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    )


def test_profile_declaring_multiple_sources_fails_before_mutation():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card(candidate_sources=2)],
    )

    with pytest.raises(AudioDeviceError, match="no safe"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_profile_applied_at_forward_deadline_is_rolled_back():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    clock = FakeClock()

    def apply_then_expire(arguments):
        result = pactl(arguments)
        if tuple(arguments) == (
            "set-card-profile",
            "alsa_card.pci-test",
            "output:analog-stereo+input:analog-stereo",
        ):
            clock.now = 3.0
        return result

    with pytest.raises(AudioDeviceError, match="timed out"):
        resolve_input_device(
            pactl_runner=apply_then_expire,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert pactl.cards[0]["active_profile"] == "output:analog-stereo"
    assert pactl.calls[-1] == (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    )


def test_profile_is_rolled_back_if_input_source_never_appears():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )

    with pytest.raises(AudioDeviceError, match="did not appear"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    assert pactl.calls[-1] == (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    )
    assert ("set-default-source", "sink.monitor") not in pactl.calls


def test_no_default_attempt_preserves_profile_after_concurrent_default_change():
    concurrent_default = "bluez_input.user-choice"
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    pactl.concurrent_default_on_source_identity_read = concurrent_default

    with pytest.raises(AudioDeviceError, match="did not appear"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    assert pactl.default == concurrent_default
    assert pactl.cards[0]["active_profile"] == (
        "output:analog-stereo+input:analog-stereo"
    )
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    ) not in pactl.calls


def test_failed_recovery_preserves_concurrent_profile_change():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    pactl.concurrent_profile_on_cards_read = (2, "output:hdmi-stereo")

    with pytest.raises(AudioDeviceError, match="did not appear"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    assert pactl.cards[0]["active_profile"] == "output:hdmi-stereo"
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    ) not in pactl.calls


def test_failed_recovery_rechecks_default_immediately_before_profile_rollback():
    concurrent_default = "bluez_input.late-user-choice"
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    pactl.concurrent_default_on_cards_read = (3, concurrent_default)

    with pytest.raises(AudioDeviceError, match="did not appear"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    assert pactl.default == concurrent_default
    assert pactl.cards[0]["active_profile"] == (
        "output:analog-stereo+input:analog-stereo"
    )
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    ) not in pactl.calls


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_preflight_budget_reserves_rollback_time_and_has_hard_bound():
    clock = FakeClock()
    budget = _PreflightBudget(lambda arguments: "", clock.sleep, clock.monotonic)

    clock.now = 3.0
    with pytest.raises(AudioDeviceError, match="timed out"):
        budget.forward(("info",))
    assert budget.rollback(("info",)) == ""

    clock.now = MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS
    with pytest.raises(AudioDeviceError, match="timed out"):
        budget.rollback(("info",))


def test_full_profile_rollback_fits_reserved_budget_with_info_fallback():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    clock = FakeClock()

    def timed_runner(arguments):
        command = tuple(arguments)
        try:
            result = pactl(command)
            if (
                command == ("get-default-source",)
                and pactl.cards[0]["active_profile"]
                == "output:analog-stereo+input:analog-stereo"
            ):
                raise RuntimeError("force info fallback during rollback")
            return result
        finally:
            clock.now += 0.49

    with pytest.raises(AudioDeviceError, match="timed out"):
        resolve_input_device(
            pactl_runner=timed_runner,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert clock.now < MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS
    assert pactl.cards[0]["active_profile"] == "output:analog-stereo"
    assert pactl.calls.count(("info",)) == 2


def test_exhausted_rollback_budget_preserves_duplex_profile():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    clock = FakeClock()

    def slow_after_profile(arguments):
        command = tuple(arguments)
        duplex = (
            pactl.cards[0]["active_profile"]
            == "output:analog-stereo+input:analog-stereo"
        )
        try:
            return pactl(command)
        finally:
            clock.now += 2.0 if duplex else 0.49

    with pytest.raises(AudioDeviceError, match="timed out"):
        resolve_input_device(
            pactl_runner=slow_after_profile,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert clock.now >= MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS
    assert pactl.cards[0]["active_profile"] == (
        "output:analog-stereo+input:analog-stereo"
    )
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    ) not in pactl.calls


def test_exact_unique_pulse_portaudio_device_is_returned_by_index():
    sounddevice = FakePulseSoundDevice(
        [
            {"name": "Built-in Audio", "max_input_channels": 2},
            {"name": "pulse", "max_input_channels": 32},
        ]
    )

    assert _resolve_pulse_portaudio_device(sounddevice) == 1
    assert sounddevice.checked == [
        {
            "device": 1,
            "channels": 1,
            "dtype": "int16",
            "samplerate": 16_000,
        }
    ]


@pytest.mark.parametrize(
    "sounddevice",
    [
        FakePulseSoundDevice([{"name": "Built-in Audio", "max_input_channels": 2}]),
        FakePulseSoundDevice(
            [
                {"name": "pulse", "max_input_channels": 32},
                {"name": "pulse", "max_input_channels": 32},
            ]
        ),
        FakePulseSoundDevice(
            [{"name": "pulse", "max_input_channels": 32}], rejected={0}
        ),
    ],
)
def test_missing_ambiguous_or_unsupported_pulse_endpoint_fails_closed(
    sounddevice,
):
    with pytest.raises(AudioDeviceError, match="no unique usable PulseAudio"):
        _resolve_pulse_portaudio_device(sounddevice)


@pytest.mark.parametrize(
    "sounddevice",
    [
        FakePulseSoundDevice([]),
        FakePulseSoundDevice(
            [
                {"name": "pulse", "max_input_channels": 32},
                {"name": "pulse", "max_input_channels": 32},
            ]
        ),
    ],
)
def test_pulse_endpoint_is_verified_before_profile_mutation(sounddevice):
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )

    with pytest.raises(AudioDeviceError, match="no unique usable PulseAudio"):
        _resolve_input_device(
            pactl_runner=pactl,
            sounddevice_module=sounddevice,
            sleep=lambda seconds: None,
        )

    assert not any(call[0].startswith("set-") for call in pactl.calls)


class FakeSoundDevice:
    def __init__(self):
        self.default_index = 4

    def query_devices(self, kind=None):
        if kind == "input":
            return {
                "name": "Built-in physical microphone",
                "max_input_channels": 2,
                "index": self.default_index,
            }
        return []

    def check_input_settings(self, **kwargs):
        assert kwargs == {
            "device": self.default_index,
            "channels": 1,
            "dtype": "int16",
            "samplerate": 16_000,
        }


class FlexibleSoundDevice:
    def __init__(self, default, devices):
        self.default = default
        self.devices = devices

    def query_devices(self, kind=None):
        return self.default if kind == "input" else self.devices

    def check_input_settings(self, **kwargs):
        del kwargs


def _missing_pactl(arguments):
    del arguments
    raise FileNotFoundError


def test_missing_pactl_uses_inspectable_portaudio_default():
    assert (
        resolve_input_device(
            pactl_runner=_missing_pactl,
            sounddevice_module=FakeSoundDevice(),
        )
        == 4
    )


def test_unindexed_physical_default_is_frozen_only_after_unique_enumeration():
    sounddevice = FlexibleSoundDevice(
        {"name": "Built-in microphone", "max_input_channels": 2},
        [{"name": "Built-in microphone", "max_input_channels": 2}],
    )

    assert (
        _resolve_input_device(
            pactl_runner=_missing_pactl,
            sounddevice_module=sounddevice,
        )
        == 0
    )


@pytest.mark.parametrize(
    ("default", "devices"),
    [
        (
            {"name": "default", "max_input_channels": 32},
            [
                {"name": "Built-in microphone", "max_input_channels": 2},
                {"name": "USB microphone", "max_input_channels": 2},
            ],
        ),
        (
            {"name": "sink.monitor", "max_input_channels": 2, "index": 4},
            [{"name": "sink.monitor", "max_input_channels": 2}],
        ),
        (
            {
                "name": "Built-in microphone",
                "max_input_channels": 2,
                "index": "4",
            },
            [],
        ),
    ],
)
def test_no_pactl_generic_ambiguous_monitor_or_bad_index_fails_closed(default, devices):
    with pytest.raises(AudioDeviceError, match="no unique usable microphone"):
        _resolve_input_device(
            pactl_runner=_missing_pactl,
            sounddevice_module=FlexibleSoundDevice(default, devices),
        )


def test_no_pactl_default_index_is_frozen_during_prepare():
    sounddevice = FakeSoundDevice()
    streams = []

    capture = AudioCapture(
        stream_factory=lambda **kwargs: (
            streams.append(FakeStream(**kwargs)) or streams[-1]
        ),
        input_resolver=lambda: _resolve_input_device(
            pactl_runner=_missing_pactl,
            sounddevice_module=sounddevice,
        ),
    )
    capture.prepare()
    sounddevice.default_index = 9
    capture.start(lambda data: None)

    assert streams[0].kwargs["device"] == 4
    capture.stop()
