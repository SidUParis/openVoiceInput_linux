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
    MicrophonePolicyError,
    SAMPLE_RATE,
    _PreflightBudget,
    _PulseInputSelection,
    _resolve_pulse_portaudio_device,
    resolve_input_device as _resolve_input_device,
)
from murmur_voice.microphone_policy import (
    MicrophonePolicyConfig,
    MicrophoneSourcePreference,
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
    def __init__(self, *, sources: str, default: str, cards=None, json_sources=None):
        self.sources = sources
        self.initial_sources = sources
        self.default = default
        self.cards = [] if cards is None else cards
        self.json_sources = (
            self._json_sources_from_short(sources)
            if json_sources is None
            else json_sources
        )
        self.calls = []
        self.after_profile_sources = None
        self.after_profile_json_sources = []
        self._profile_applied = False
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
            if (
                self._profile_applied
                and self.concurrent_default_on_source_identity_read is not None
            ):
                self.default = self.concurrent_default_on_source_identity_read
                self.concurrent_default_on_source_identity_read = None
            sources = (
                self.after_profile_json_sources
                if self._profile_applied
                else self.json_sources
            )
            return json.dumps(sources)
        if command[:1] == ("set-card-profile",):
            for card in self.cards if isinstance(self.cards, list) else ():
                if card.get("name") == command[1]:
                    card["active_profile"] = command[2]
            if "+input:" in command[2]:
                self._profile_applied = True
                if self.after_profile_sources is not None:
                    self.sources = self.after_profile_sources
            elif "+input:" not in command[2]:
                self.sources = self.initial_sources
                self._profile_applied = False
            return ""
        if command == ("info",):
            return f"Default Source: {self.default}\n"
        raise AssertionError(f"unexpected pactl call: {command!r}")

    @staticmethod
    def _json_sources_from_short(sources):
        result = []
        for line in sources.splitlines():
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            result.append(
                {
                    "name": fields[1],
                    "card": None,
                    "state": fields[-1] if len(fields) >= 5 else "UNKNOWN",
                    "properties": {
                        "device.class": "sound",
                        "media.class": "Audio/Source",
                    },
                }
            )
        return result


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
    extra_properties=None,
    active_port=None,
    ports=None,
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
    if extra_properties is not None:
        properties.update(extra_properties)
    source = {
        "name": name,
        "card": card_index,
        "state": state,
        "properties": properties,
    }
    if active_port is not None:
        source["active_port"] = active_port
    if ports is not None:
        source["ports"] = ports
    return source


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


def test_linked_dji_is_bound_for_this_stream_without_changing_system_default():
    built_in = "alsa_input.pci-test.analog-stereo"
    dji = "alsa_input.usb-DJI_Technology_Co.__Ltd._Wireless_Mic_Rx.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(8, built_in) + _short_source(9, dji),
        default=built_in,
    )

    selection = resolve_input_device(
        pactl_runner=pactl,
        dji_link_probe=lambda: True,
    )

    _assert_pulse_selection(selection, dji)
    assert pactl.default == built_in
    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_offline_dji_falls_back_to_unique_built_in_for_this_stream():
    built_in = "alsa_input.pci-test.analog-stereo"
    dji = "alsa_input.usb-DJI_Technology_Co.__Ltd._Wireless_Mic_Rx.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(8, built_in) + _short_source(9, dji),
        default=dji,
    )

    selection = resolve_input_device(
        pactl_runner=pactl,
        dji_link_probe=lambda: False,
    )

    _assert_pulse_selection(selection, built_in)
    assert pactl.default == dji
    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_offline_dji_recovers_unique_hidden_output_only_built_in_once():
    built_in = "alsa_input.pci-test.analog-stereo"
    dji = "alsa_input.usb-DJI_Technology_Co.__Ltd._Wireless_Mic_Rx.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(8, dji) + _short_source(9, "sink.monitor"),
        default=dji,
        cards=[_card()],
    )
    pactl.after_profile_sources = _short_source(8, dji) + _short_source(10, built_in)
    pactl.after_profile_json_sources = [_json_source(built_in, 2)]
    probe_calls = []

    def probe():
        probe_calls.append("probe")
        return False

    selection = resolve_input_device(
        pactl_runner=pactl,
        sleep=lambda seconds: None,
        dji_link_probe=probe,
    )

    _assert_pulse_selection(selection, built_in)
    assert probe_calls == ["probe"]
    assert pactl.default == dji
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo+input:analog-stereo",
    ) in pactl.calls
    assert not any(call[0] == "set-default-source" for call in pactl.calls)
    assert not any("sink" in call[0] for call in pactl.calls)


def test_offline_dji_with_multiple_hidden_cards_fails_without_mutation():
    dji = "alsa_input.usb-DJI_Technology_Co.__Ltd._Wireless_Mic_Rx.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(8, dji) + _short_source(9, "sink.monitor"),
        default=dji,
        cards=[
            _card("alsa_card.pci-first", index=2),
            _card("alsa_card.pci-second", index=3),
        ],
    )
    probe_calls = []

    def probe():
        probe_calls.append("probe")
        return False

    with pytest.raises(AudioDeviceError, match="profile selection is ambiguous"):
        resolve_input_device(
            pactl_runner=pactl,
            sleep=lambda seconds: None,
            dji_link_probe=probe,
        )

    assert probe_calls == ["probe"]
    assert pactl.default == dji
    assert ("--format=json", "list", "cards") in pactl.calls
    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_offline_dji_without_safe_hidden_card_fails_without_mutation():
    dji = "alsa_input.usb-DJI_Technology_Co.__Ltd._Wireless_Mic_Rx.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(8, dji) + _short_source(9, "sink.monitor"),
        default=dji,
        cards=[],
    )
    probe_calls = []

    def probe():
        probe_calls.append("probe")
        return False

    with pytest.raises(AudioDeviceError, match="no safe input-capable"):
        resolve_input_device(
            pactl_runner=pactl,
            sleep=lambda seconds: None,
            dji_link_probe=probe,
        )

    assert probe_calls == ["probe"]
    assert pactl.default == dji
    assert ("--format=json", "list", "cards") in pactl.calls
    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_unknown_dji_link_state_uses_known_built_in_instead_of_promoting_dji():
    built_in = "alsa_input.pci-test.analog-stereo"
    dji = "alsa_input.usb-DJI_Technology_Co.__Ltd._Wireless_Mic_Rx.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(8, built_in) + _short_source(9, dji),
        default=dji,
    )

    selection = resolve_input_device(
        pactl_runner=pactl,
        dji_link_probe=lambda: None,
    )

    _assert_pulse_selection(selection, built_in)
    assert pactl.default == dji


def test_offline_dji_uses_category_order_across_non_dji_fallbacks():
    first = "bluez_input.first"
    second = "xrdp_input.second"
    dji = "alsa_input.usb-DJI_Technology_Co.__Ltd._Wireless_Mic_Rx.analog-stereo"
    pactl = FakePactl(
        sources=(
            _short_source(7, first) + _short_source(8, second) + _short_source(9, dji)
        ),
        default=dji,
    )

    selection = resolve_input_device(
        pactl_runner=pactl,
        dji_link_probe=lambda: False,
    )

    _assert_pulse_selection(selection, first)
    assert pactl.default == dji
    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_user_category_order_can_put_headset_before_online_dji():
    built_in = "alsa_input.pci-test.analog-stereo"
    headset = "bluez_input.poly.headset-head-unit"
    dji = "alsa_input.usb-DJI_Wireless_Mic_Rx.analog-stereo"
    pactl = FakePactl(
        sources=(
            _short_source(7, built_in)
            + _short_source(8, headset)
            + _short_source(9, dji)
        ),
        default=built_in,
    )
    policy = MicrophonePolicyConfig(priority=("headset", "dji", "external", "built-in"))

    selection = resolve_input_device(
        pactl_runner=pactl,
        dji_link_probe=lambda: True,
        microphone_policy=policy,
    )

    _assert_pulse_selection(selection, headset)
    assert pactl.default == built_in
    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_default_order_uses_headset_when_dji_is_offline_then_built_in_when_absent():
    built_in = "alsa_input.pci-test.analog-stereo"
    headset = "bluez_input.poly.headset-head-unit"
    dji = "alsa_input.usb-DJI_Wireless_Mic_Rx.analog-stereo"
    with_headset = FakePactl(
        sources=(
            _short_source(7, built_in)
            + _short_source(8, headset)
            + _short_source(9, dji)
        ),
        default=dji,
    )
    without_headset = FakePactl(
        sources=_short_source(7, built_in) + _short_source(9, dji),
        default=dji,
    )

    first = resolve_input_device(
        pactl_runner=with_headset,
        dji_link_probe=lambda: False,
    )
    second = resolve_input_device(
        pactl_runner=without_headset,
        dji_link_probe=lambda: False,
    )

    _assert_pulse_selection(first, headset)
    _assert_pulse_selection(second, built_in)


def test_exact_source_preference_disambiguates_two_headsets():
    first = "bluez_input.first.headset-head-unit"
    second = "bluez_input.second.headset-head-unit"
    built_in = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(
        sources=(
            _short_source(7, first)
            + _short_source(8, second)
            + _short_source(9, built_in)
        ),
        default=built_in,
    )
    policy = MicrophonePolicyConfig(
        preferred_sources=(MicrophoneSourcePreference("headset", second),)
    )

    selection = resolve_input_device(
        pactl_runner=pactl,
        microphone_policy=policy,
    )

    _assert_pulse_selection(selection, second)


def test_live_default_disambiguates_two_sources_within_same_category():
    first = "bluez_input.first.headset-head-unit"
    second = "bluez_input.second.headset-head-unit"
    pactl = FakePactl(
        sources=_short_source(7, first) + _short_source(8, second),
        default=first,
    )

    selection = resolve_input_device(pactl_runner=pactl)

    _assert_pulse_selection(selection, first)


def test_ambiguous_higher_category_is_skipped_for_unique_lower_category():
    first = "bluez_input.first.headset-head-unit"
    second = "bluez_input.second.headset-head-unit"
    external = "alsa_input.usb-studio-mic.analog-stereo"
    built_in = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(
        sources=(
            _short_source(6, first)
            + _short_source(7, second)
            + _short_source(8, external)
            + _short_source(9, built_in)
        ),
        default=built_in,
    )

    selection = resolve_input_device(pactl_runner=pactl)

    _assert_pulse_selection(selection, external)


def test_unavailable_active_headset_port_is_skipped_for_built_in():
    headset = "alsa_input.usb-headset.analog-stereo"
    built_in = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(7, headset) + _short_source(8, built_in),
        default=headset,
        json_sources=[
            _json_source(
                headset,
                4,
                extra_properties={"device.bus": "usb"},
                active_port="analog-input-headset-mic",
                ports=[
                    {
                        "name": "analog-input-headset-mic",
                        "type": "Headset",
                        "availability": "not available",
                    }
                ],
            ),
            _json_source(
                built_in,
                2,
                extra_properties={
                    "device.bus": "pci",
                    "device.form_factor": "internal",
                },
            ),
        ],
    )

    selection = resolve_input_device(pactl_runner=pactl)

    _assert_pulse_selection(selection, built_in)


def test_active_headset_port_overrides_pci_card_builtin_classification():
    combo_source = "alsa_input.pci-combo.analog-stereo"
    built_in = "alsa_input.pci-internal.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(7, combo_source) + _short_source(8, built_in),
        default=built_in,
        json_sources=[
            _json_source(
                combo_source,
                2,
                extra_properties={"device.bus": "pci"},
                active_port="analog-input-headset-mic",
                ports=[
                    {
                        "name": "analog-input-headset-mic",
                        "type": "Headset",
                        "availability": "yes",
                    }
                ],
            ),
            _json_source(
                built_in,
                3,
                extra_properties={
                    "device.bus": "pci",
                    "device.form_factor": "internal",
                },
                active_port="analog-input-internal-mic",
            ),
        ],
    )

    selection = resolve_input_device(pactl_runner=pactl)

    _assert_pulse_selection(selection, combo_source)


def test_non_dji_generic_wireless_receiver_uses_explicit_usb_ids():
    generic = "alsa_input.usb-Wireless_Microphone_Rx.analog-stereo"
    built_in = "alsa_input.pci-test.analog-stereo"
    probe_calls = []
    pactl = FakePactl(
        sources=_short_source(7, generic) + _short_source(8, built_in),
        default=built_in,
        json_sources=[
            _json_source(
                generic,
                4,
                extra_properties={
                    "device.bus": "usb",
                    "device.vendor.id": "0x1234",
                    "device.product.id": "0x4011",
                },
            ),
            _json_source(
                built_in,
                2,
                extra_properties={
                    "device.bus": "pci",
                    "device.form_factor": "internal",
                },
            ),
        ],
    )

    selection = resolve_input_device(
        pactl_runner=pactl,
        dji_link_probe=lambda: probe_calls.append("probe") or True,
    )

    _assert_pulse_selection(selection, generic)
    assert probe_calls == []


def test_multiple_dji_sources_are_not_mapped_from_one_boolean_probe():
    first = "alsa_input.usb-DJI_first.analog-stereo"
    second = "alsa_input.usb-DJI_second.analog-stereo"
    built_in = "alsa_input.pci-test.analog-stereo"
    probe_calls = []
    pactl = FakePactl(
        sources=(
            _short_source(6, first)
            + _short_source(7, second)
            + _short_source(8, built_in)
        ),
        default=first,
        json_sources=[
            _json_source(
                first,
                4,
                extra_properties={
                    "device.bus": "usb",
                    "device.vendor.id": "0x2ca3",
                    "device.product.id": "0x4011",
                },
            ),
            _json_source(
                second,
                5,
                extra_properties={
                    "device.bus": "usb",
                    "device.vendor.id": "0x2ca3",
                    "device.product.id": "0x4011",
                },
            ),
            _json_source(
                built_in,
                2,
                extra_properties={
                    "device.bus": "pci",
                    "device.form_factor": "internal",
                },
            ),
        ],
    )

    selection = resolve_input_device(
        pactl_runner=pactl,
        dji_link_probe=lambda: probe_calls.append("probe") or True,
    )

    _assert_pulse_selection(selection, built_in)
    assert probe_calls == []


def test_unknown_single_dji_current_default_is_last_resort_without_fallback():
    dji = "alsa_input.usb-DJI_Wireless_Mic_Rx.analog-stereo"
    pactl = FakePactl(sources=_short_source(9, dji), default=dji)

    selection = resolve_input_device(
        pactl_runner=pactl,
        dji_link_probe=lambda: None,
    )

    _assert_pulse_selection(selection, dji)
    assert ("--format=json", "list", "cards") in pactl.calls
    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_unknown_dji_default_yields_to_known_recoverable_hidden_builtin():
    dji = "alsa_input.usb-DJI_Wireless_Mic_Rx.analog-stereo"
    built_in = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(9, dji),
        default=dji,
        cards=[_card()],
    )
    pactl.after_profile_sources = _short_source(10, built_in)
    pactl.after_profile_json_sources = [_json_source(built_in, 2)]

    selection = resolve_input_device(
        pactl_runner=pactl,
        sleep=lambda seconds: None,
        dji_link_probe=lambda: None,
    )

    _assert_pulse_selection(selection, built_in)


def test_invalid_policy_value_fails_before_any_audio_system_probe():
    calls = []

    with pytest.raises(MicrophonePolicyError, match="configuration is invalid"):
        resolve_input_device(
            pactl_runner=lambda arguments: calls.append(tuple(arguments)) or "",
            microphone_policy=object(),
        )

    assert calls == []


def test_nonempty_malformed_source_json_fails_without_profile_mutation():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
        json_sources=[{"state": "SUSPENDED", "properties": {}}],
    )

    with pytest.raises(AudioDeviceError, match="invalid data"):
        resolve_input_device(pactl_runner=pactl)

    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_nonstandard_monitor_metadata_is_never_selected():
    monitor = "capture_of_output_without_monitor_suffix"
    built_in = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(7, monitor) + _short_source(8, built_in),
        default=monitor,
        json_sources=[
            _json_source(
                monitor,
                4,
                device_class="monitor",
            ),
            _json_source(
                built_in,
                2,
                extra_properties={
                    "device.bus": "pci",
                    "device.form_factor": "internal",
                },
            ),
        ],
    )

    selection = resolve_input_device(pactl_runner=pactl)

    _assert_pulse_selection(selection, built_in)


def test_output_only_usb_card_is_not_automatically_recovered():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card("alsa_card.usb-external", index=4)],
    )

    with pytest.raises(AudioDeviceError, match="no safe input-capable"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_recovery_rejects_nonstandard_media_sink_source_and_rolls_back():
    nonstandard_monitor = "captured_output_without_monitor_suffix"
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[_card()],
    )
    pactl.after_profile_sources = _short_source(10, nonstandard_monitor)
    pactl.after_profile_json_sources = [
        _json_source(
            nonstandard_monitor,
            2,
            extra_properties={"media.class": "Audio/Sink"},
        )
    ]

    with pytest.raises(AudioDeviceError, match="did not appear"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)

    assert pactl.cards[0]["active_profile"] == "output:analog-stereo"


def test_ambiguous_headsets_fall_through_to_hidden_recoverable_builtin():
    first = "bluez_input.first.headset-head-unit"
    second = "bluez_input.second.headset-head-unit"
    built_in = "alsa_input.pci-test.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(7, first) + _short_source(8, second),
        default="sink.monitor",
        cards=[_card()],
    )
    pactl.after_profile_sources = _short_source(10, built_in)
    pactl.after_profile_json_sources = [_json_source(built_in, 2)]

    selection = resolve_input_device(
        pactl_runner=pactl,
        sleep=lambda seconds: None,
    )

    _assert_pulse_selection(selection, built_in)
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo+input:analog-stereo",
    ) in pactl.calls


def test_custom_builtin_before_external_recovers_hidden_builtin_first():
    external = "alsa_input.usb-studio-mic.analog-stereo"
    built_in = "alsa_input.pci-test.analog-stereo"
    policy = MicrophonePolicyConfig(priority=("built-in", "dji", "headset", "external"))
    pactl = FakePactl(
        sources=_short_source(8, external),
        default=external,
        cards=[_card()],
    )
    pactl.after_profile_sources = _short_source(10, built_in)
    pactl.after_profile_json_sources = [_json_source(built_in, 2)]

    selection = resolve_input_device(
        pactl_runner=pactl,
        sleep=lambda seconds: None,
        microphone_policy=policy,
    )

    _assert_pulse_selection(selection, built_in)


def test_lower_ranked_hidden_builtin_does_not_preempt_visible_external():
    external = "alsa_input.usb-studio-mic.analog-stereo"
    pactl = FakePactl(
        sources=_short_source(8, external),
        default=external,
        cards=[_card()],
    )

    selection = resolve_input_device(pactl_runner=pactl)

    _assert_pulse_selection(selection, external)
    assert ("--format=json", "list", "cards") not in pactl.calls
    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_no_safe_builtin_recovery_falls_back_to_visible_lower_winner():
    external = "alsa_input.usb-studio-mic.analog-stereo"
    policy = MicrophonePolicyConfig(priority=("built-in", "dji", "headset", "external"))
    pactl = FakePactl(
        sources=_short_source(8, external),
        default=external,
        cards=[],
    )

    selection = resolve_input_device(
        pactl_runner=pactl,
        microphone_policy=policy,
    )

    _assert_pulse_selection(selection, external)
    assert not any(call[0].startswith("set-") for call in pactl.calls)


def test_failed_applied_builtin_recovery_rolls_back_before_lower_fallback():
    external = "alsa_input.usb-studio-mic.analog-stereo"
    policy = MicrophonePolicyConfig(priority=("built-in", "dji", "headset", "external"))
    pactl = FakePactl(
        sources=_short_source(8, external),
        default=external,
        cards=[_card()],
    )

    selection = resolve_input_device(
        pactl_runner=pactl,
        sleep=lambda seconds: None,
        microphone_policy=policy,
    )

    _assert_pulse_selection(selection, external)
    assert pactl.cards[0]["active_profile"] == "output:analog-stereo"
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo+input:analog-stereo",
    ) in pactl.calls
    assert (
        "set-card-profile",
        "alsa_card.pci-test",
        "output:analog-stereo",
    ) in pactl.calls


def test_command_failure_before_mutation_can_use_confirmed_lower_fallback():
    external = "alsa_input.usb-studio-mic.analog-stereo"
    policy = MicrophonePolicyConfig(priority=("built-in", "dji", "headset", "external"))
    pactl = FakePactl(
        sources=_short_source(8, external),
        default=external,
        cards=[_card()],
    )
    pactl.fail.add(
        (
            "set-card-profile",
            "alsa_card.pci-test",
            "output:analog-stereo+input:analog-stereo",
        )
    )

    selection = resolve_input_device(
        pactl_runner=pactl,
        microphone_policy=policy,
    )

    _assert_pulse_selection(selection, external)
    assert pactl.cards[0]["active_profile"] == "output:analog-stereo"


def test_unconfirmed_recovery_rollback_never_uses_lower_fallback():
    external = "alsa_input.usb-studio-mic.analog-stereo"
    concurrent_default = "bluez_input.concurrent.headset-head-unit"
    policy = MicrophonePolicyConfig(priority=("built-in", "dji", "headset", "external"))
    pactl = FakePactl(
        sources=_short_source(8, external),
        default=external,
        cards=[_card()],
    )
    pactl.concurrent_default_on_source_identity_read = concurrent_default

    with pytest.raises(AudioDeviceError, match="did not appear"):
        resolve_input_device(
            pactl_runner=pactl,
            sleep=lambda seconds: None,
            microphone_policy=policy,
        )

    assert pactl.default == concurrent_default
    assert pactl.cards[0]["active_profile"] == (
        "output:analog-stereo+input:analog-stereo"
    )


def test_concurrent_profile_change_never_uses_lower_fallback():
    external = "alsa_input.usb-studio-mic.analog-stereo"
    concurrent_profile = "output:hdmi-stereo"
    policy = MicrophonePolicyConfig(priority=("built-in", "dji", "headset", "external"))
    pactl = FakePactl(
        sources=_short_source(8, external),
        default=external,
        cards=[_card()],
    )
    pactl.concurrent_profile_on_cards_read = (2, concurrent_profile)

    with pytest.raises(AudioDeviceError, match="did not appear"):
        resolve_input_device(
            pactl_runner=pactl,
            sleep=lambda seconds: None,
            microphone_policy=policy,
        )

    assert pactl.cards[0]["active_profile"] == concurrent_profile


def test_recovered_non_builtin_source_is_rolled_back_before_lower_fallback():
    external = "alsa_input.usb-studio-mic.analog-stereo"
    recovered_headset = "bluez_input.recovered.headset-head-unit"
    policy = MicrophonePolicyConfig(priority=("built-in", "dji", "headset", "external"))
    pactl = FakePactl(
        sources=_short_source(8, external),
        default=external,
        cards=[_card()],
    )
    pactl.after_profile_sources = _short_source(10, recovered_headset)
    pactl.after_profile_json_sources = [_json_source(recovered_headset, 2)]

    selection = resolve_input_device(
        pactl_runner=pactl,
        sleep=lambda seconds: None,
        microphone_policy=policy,
    )

    _assert_pulse_selection(selection, external)
    assert pactl.cards[0]["active_profile"] == "output:analog-stereo"


def test_same_capture_resolves_new_policy_at_each_prepare_without_midstream_handoff():
    dji = "alsa_input.usb-DJI_Wireless_Mic_Rx.analog-stereo"
    headset = "bluez_input.poly.headset-head-unit"
    pactl = FakePactl(
        sources=_short_source(8, headset) + _short_source(9, dji),
        default=headset,
    )
    policies = [MicrophonePolicyConfig()]
    streams = []
    opened_routes = []

    def resolver():
        return resolve_input_device(
            pactl_runner=pactl,
            dji_link_probe=lambda: True,
            microphone_policy=policies[0],
        )

    def factory(**kwargs):
        opened_routes.append(os.environ.get("PULSE_SOURCE"))
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream

    capture = AudioCapture(stream_factory=factory, input_resolver=resolver)
    capture.prepare()
    capture.start(lambda data: None)
    policies[0] = MicrophonePolicyConfig(
        priority=("headset", "dji", "external", "built-in")
    )
    # The active stream stays on its frozen first selection.
    assert streams[0].kwargs["device"] == 0
    assert streams[0].active
    capture.stop()

    capture.prepare()
    capture.start(lambda data: None)
    capture.stop()

    assert len(streams) == 2
    assert opened_routes == [dji, headset]
    assert pactl.calls.count(("--format=json", "list", "sources")) == 2


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
            _card("alsa_card.pci-second", index=3, candidate_priority=9000),
        ],
    )
    with pytest.raises(AudioDeviceError, match="ambiguous"):
        resolve_input_device(pactl_runner=pactl, sleep=lambda seconds: None)
    assert not any(call[0] == "set-card-profile" for call in pactl.calls)


def test_tied_card_profiles_fail_instead_of_guessing():
    pactl = FakePactl(
        sources=_short_source(9, "sink.monitor"),
        default="sink.monitor",
        cards=[
            _card("alsa_card.pci-a", index=2),
            _card("alsa_card.pci-b", index=3),
        ],
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
