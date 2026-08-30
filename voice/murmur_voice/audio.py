# SPDX-License-Identifier: GPL-3.0-only
# Portions adapted from Doubao Murmur, Copyright (c) 2026 lilong7676,
# supplied under MIT. Modifications Copyright (c) 2026 Open Voice Input Linux
# contributors. See NOTICE.md for source paths and the preserved MIT terms.
"""Capture 16 kHz mono Int16 PCM through sounddevice/PortAudio."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from .dji_microphone import is_dji_source, probe_dji_link_state
from .microphone_policy import MicrophonePolicyConfig

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_DTYPE = "int16"
BLOCK_SIZE = 4096

_MAX_PACTL_OUTPUT_BYTES = 512 * 1024
_SOURCE_REENUMERATION_DELAYS = (0.0, 0.05, 0.1, 0.2, 0.4)
_PACTL_COMMAND_TIMEOUT_SECONDS = 0.5
_PREFLIGHT_FORWARD_TIMEOUT_SECONDS = 3.0
_PREFLIGHT_ROLLBACK_TIMEOUT_SECONDS = 7.0
MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS = (
    _PREFLIGHT_FORWARD_TIMEOUT_SECONDS + _PREFLIGHT_ROLLBACK_TIMEOUT_SECONDS
)

logger = logging.getLogger(__name__)
_PULSE_SOURCE_ENVIRONMENT_LOCK = threading.Lock()


class AudioDeviceError(RuntimeError):
    """A safe, content-free failure to find or open a usable microphone."""


class MicrophonePolicyError(AudioDeviceError):
    """A fixed failure for an existing invalid/unsafe microphone policy."""


class _PactlCommandError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _PulseSource:
    name: str
    state: str
    card_index: int | None = None
    category: str | None = None
    available: bool | None = None


@dataclass(frozen=True, slots=True)
class _PulseInputSelection:
    """An exact Pulse source to bind to one newly opened recording stream."""

    source: str
    portaudio_device: int

    def __post_init__(self) -> None:
        if (
            _safe_name(self.source) != self.source
            or _pulse_index(self.portaudio_device) is None
        ):
            raise AudioDeviceError("invalid PulseAudio input selection")


@dataclass(frozen=True, slots=True)
class _ProfileRecovery:
    card: str
    card_index: int
    alsa_card: str | None
    bus_path: str | None
    previous_profile: str
    profile: str
    priority: int


class _PreflightBudget:
    """Give forward recovery 3 s and reserve 7 s for safe rollback."""

    def __init__(
        self,
        runner: Callable[[Sequence[str]], str] | None,
        sleep: Callable[[float], None],
        monotonic: Callable[[], float],
    ) -> None:
        started = monotonic()
        self._forward_deadline = started + _PREFLIGHT_FORWARD_TIMEOUT_SECONDS
        self._hard_deadline = started + MICROPHONE_PREFLIGHT_TIMEOUT_SECONDS
        self._runner = runner
        self._sleep = sleep
        self._monotonic = monotonic

    def forward(self, arguments: Sequence[str]) -> str:
        return self._call(arguments, self._forward_deadline)

    def rollback(self, arguments: Sequence[str]) -> str:
        return self._call(arguments, self._hard_deadline)

    def pause(self, seconds: float) -> None:
        remaining = self._remaining(self._forward_deadline)
        if seconds >= remaining:
            raise AudioDeviceError("microphone preflight timed out")
        self._sleep(seconds)
        self._remaining(self._forward_deadline)

    def _call(self, arguments: Sequence[str], deadline: float) -> str:
        remaining = self._remaining(deadline)
        try:
            if self._runner is None:
                result = _run_pactl(
                    arguments,
                    timeout=min(_PACTL_COMMAND_TIMEOUT_SECONDS, remaining),
                )
            else:
                result = self._runner(arguments)
        except Exception as error:
            if self._monotonic() >= deadline:
                raise AudioDeviceError("microphone preflight timed out") from error
            raise
        self._remaining(deadline)
        return result

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise AudioDeviceError("microphone preflight timed out")
        return remaining


class AudioCapture:
    """Small microphone boundary with an injectable stream for offline tests."""

    def __init__(
        self,
        stream_factory: Callable[..., Any] | None = None,
        input_resolver: Callable[[], _PulseInputSelection | int | str | None]
        | None = None,
    ) -> None:
        self._stream_factory = stream_factory or _default_stream_factory
        # A custom stream factory is an offline-test boundary by default. A
        # caller can still inject a resolver explicitly when testing selection.
        if input_resolver is not None:
            self._input_resolver = input_resolver
        elif stream_factory is None:
            self._input_resolver = resolve_input_device
        else:
            self._input_resolver = lambda: None
        self._stream: Any | None = None
        self._prepared_device: _PulseInputSelection | int | str | None = None
        self._is_prepared = False
        self._on_audio_data: Callable[[bytes], None] | None = None
        self._lock = threading.RLock()

    @property
    def is_capturing(self) -> bool:
        with self._lock:
            stream = self._stream
        return bool(stream is not None and getattr(stream, "active", False))

    def prepare(self) -> None:
        """Resolve a fresh input without opening it or capturing any audio."""

        with self._lock:
            if self._stream is not None:
                raise RuntimeError("audio capture is already active")
            try:
                self._prepared_device = self._input_resolver()
            except AudioDeviceError:
                raise
            except Exception as error:
                raise AudioDeviceError("microphone discovery failed") from error
            self._is_prepared = True

    def start(self, on_audio_data: Callable[[bytes], None]) -> None:
        """Start capture; the callback runs on PortAudio's audio thread."""

        if not callable(on_audio_data):
            raise TypeError("on_audio_data must be callable")
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("audio capture is already active")
            if not self._is_prepared:
                self.prepare()
            device = self._prepared_device
            self._prepared_device = None
            self._is_prepared = False
            self._on_audio_data = on_audio_data
            stream: Any | None = None
            try:
                stream_options: dict[str, Any] = {
                    "samplerate": SAMPLE_RATE,
                    "channels": CHANNELS,
                    "dtype": SAMPLE_DTYPE,
                    "blocksize": BLOCK_SIZE,
                    "callback": self._audio_callback,
                    "latency": "low",
                }
                pulse_source: str | None = None
                if isinstance(device, _PulseInputSelection):
                    pulse_source = device.source
                    stream_options["device"] = device.portaudio_device
                elif device is not None:
                    stream_options["device"] = device
                # Serialize every PortAudio open.  Otherwise a simultaneous
                # non-Pulse selection could inherit another capture's brief
                # process-global PULSE_SOURCE override.
                with _pulse_source_environment(pulse_source):
                    stream = self._stream_factory(**stream_options)
                    self._stream = stream
                    stream.start()
            except Exception as error:
                self._stream = None
                self._on_audio_data = None
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        logger.error("Failed microphone stream cleanup")
                raise AudioDeviceError("microphone could not be opened") from error
        logger.info("Audio capture started")

    def stop(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
            self._prepared_device = None
            self._is_prepared = False
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


def resolve_input_device(
    *,
    sounddevice_module: Any | None = None,
    pactl_runner: Callable[[Sequence[str]], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    dji_link_probe: Callable[[], bool | None] = probe_dji_link_state,
    microphone_policy: MicrophonePolicyConfig = MicrophonePolicyConfig(),
) -> _PulseInputSelection | int | str | None:
    """Resolve one microphone afresh for each explicitly started recording.

    On PulseAudio/PipeWire desktops, return an exact per-stream source binding.
    A stale monitor default is left unchanged.  The narrow output-only-card
    state seen after a Bluetooth input disconnect may be extended with its
    matching input profile.  No source is unmuted and no volume is changed.
    """

    if not isinstance(microphone_policy, MicrophonePolicyConfig):
        raise MicrophonePolicyError("microphone priority configuration is invalid")

    budget = _PreflightBudget(pactl_runner, sleep, monotonic)
    try:
        # First establish that pactl is usable.  Only then require PortAudio's
        # exact Pulse endpoint; no card profile has been mutated at this point.
        sources = _list_pulse_sources(budget.forward)
    except FileNotFoundError:
        # Only failure of the first pactl probe establishes a non-Pulse host.
        return _resolve_portaudio_input(sounddevice_module or _load_sounddevice())
    except AudioDeviceError:
        raise
    except Exception as error:
        raise AudioDeviceError("microphone discovery failed") from error

    try:
        pulse_device = _resolve_pulse_portaudio_device(sounddevice_module)
        return _ensure_pulse_input(
            budget.forward,
            budget.pause,
            budget.rollback,
            sources,
            pulse_device,
            dji_link_probe,
            microphone_policy,
        )
    except FileNotFoundError as error:
        # Once pactl answered, losing it mid-transaction is an uncertain Pulse
        # failure, never permission to switch to a generic PortAudio fallback.
        raise AudioDeviceError(
            "PulseAudio disappeared during microphone preflight"
        ) from error
    except AudioDeviceError:
        raise
    except Exception as error:
        raise AudioDeviceError("microphone discovery failed") from error


def _ensure_pulse_input(
    runner: Callable[[Sequence[str]], str],
    sleep: Callable[[float], None],
    rollback_runner: Callable[[Sequence[str]], str],
    sources: Sequence[_PulseSource],
    pulse_device: int,
    dji_link_probe: Callable[[], bool | None] = probe_dji_link_state,
    microphone_policy: MicrophonePolicyConfig = MicrophonePolicyConfig(),
) -> _PulseInputSelection:
    default_source = _get_default_source(runner)
    if default_source is None:
        # A global profile/default mutation is not safely reversible unless
        # the current default was observed first.
        raise AudioDeviceError("PulseAudio default source could not be determined")
    usable_sources = _usable_sources(sources)
    selected, eligible_sources, unknown_dji_default = _choose_policy_source(
        usable_sources,
        default_source,
        dji_link_probe,
        microphone_policy,
    )
    fallback = selected or unknown_dji_default
    if not _should_attempt_builtin_recovery(
        microphone_policy,
        eligible_sources,
        selected,
    ):
        if fallback is not None:
            return _PulseInputSelection(fallback.name, pulse_device)
        raise AudioDeviceError("microphone policy has no unambiguous available source")

    # A hidden built-in input is a policy candidate, not merely a last-resort
    # error path. If its rank is above the best visible winner (or every
    # higher category was ambiguous), safely extend the exact existing output
    # profile and re-enumerate. Never recover an external/USB output-only card.
    try:
        recovery = _choose_profile_recovery(_list_cards(runner))
    except Exception:
        if fallback is not None:
            return _PulseInputSelection(fallback.name, pulse_device)
        raise

    try:
        runner(("set-card-profile", recovery.card, recovery.profile))
    except Exception as error:
        # The command may have been applied just as the forward budget expired
        # (or before pactl reported an error). A visible lower-ranked fallback
        # is safe only after the previous profile/default are confirmed again.
        _rollback_recovery(rollback_runner, recovery, default_source)
        if fallback is not None and _recovery_is_reverted(
            rollback_runner,
            recovery,
            default_source,
        ):
            return _PulseInputSelection(fallback.name, pulse_device)
        if isinstance(error, (AudioDeviceError, FileNotFoundError)):
            raise
        raise AudioDeviceError("microphone profile recovery failed") from error
    try:
        recovered_sources = _wait_for_recovered_sources(
            runner,
            sleep,
            recovery,
        )
        recovered = _choose_recovered_builtin_source(recovered_sources)
    except Exception:
        _rollback_recovery(rollback_runner, recovery, default_source)
        if fallback is not None and _recovery_is_reverted(
            rollback_runner,
            recovery,
            default_source,
        ):
            return _PulseInputSelection(fallback.name, pulse_device)
        raise
    # Keeping this same-output duplex profile is intentional: the exact
    # recovered source is bound only to this recording stream below.
    return _PulseInputSelection(recovered.name, pulse_device)


def _choose_policy_source(
    sources: Sequence[_PulseSource],
    default_source: str,
    link_probe: Callable[[], bool | None],
    policy: MicrophonePolicyConfig,
) -> tuple[_PulseSource | None, tuple[_PulseSource, ...], _PulseSource | None]:
    """Select the first unambiguous currently usable policy category.

    DJI sources are eligible only after a positive link probe. A negative
    probe excludes them. An unknown probe also avoids promoting DJI, but the
    already-current DJI default remains a last resort when no non-DJI source
    can be selected; this keeps compatibility with a separate router that may
    own the receiver's USB status interface.

    Within a category, an exact configured source wins, then the live system
    default, then a unique candidate. An unresolved category is skipped so a
    lower-priority class can keep dictation available without guessing.
    """

    candidates = tuple(sources)
    dji_sources = tuple(
        source for source in candidates if _source_category(source) == "dji"
    )
    online: bool | None = None
    if len(dji_sources) == 1:
        try:
            online = link_probe()
        except Exception:
            online = None

    if online is True or not dji_sources:
        eligible = candidates
    else:
        eligible = tuple(source for source in candidates if source not in dji_sources)

    for category in policy.priority:
        category_sources = tuple(
            source for source in eligible if _source_category(source) == category
        )
        if not category_sources:
            continue
        preferred_name = policy.preferred_source_for(category)
        if preferred_name is not None:
            preferred = tuple(
                source for source in category_sources if source.name == preferred_name
            )
            if len(preferred) == 1:
                return (
                    preferred[0],
                    eligible,
                    _unknown_dji_default(dji_sources, default_source, online),
                )
        current = tuple(
            source for source in category_sources if source.name == default_source
        )
        if len(current) == 1:
            return (
                current[0],
                eligible,
                _unknown_dji_default(dji_sources, default_source, online),
            )
        if len(category_sources) == 1:
            return (
                category_sources[0],
                eligible,
                _unknown_dji_default(dji_sources, default_source, online),
            )

    return (
        None,
        eligible,
        _unknown_dji_default(
            dji_sources,
            default_source,
            online,
        ),
    )


def _unknown_dji_default(
    dji_sources: Sequence[_PulseSource],
    default_source: str,
    online: bool | None,
) -> _PulseSource | None:
    if online is not None or len(dji_sources) != 1:
        return None
    source = dji_sources[0]
    return source if source.name == default_source else None


def _should_attempt_builtin_recovery(
    policy: MicrophonePolicyConfig,
    eligible_sources: Sequence[_PulseSource],
    selected: _PulseSource | None,
) -> bool:
    if any(_source_category(source) == "built-in" for source in eligible_sources):
        return False
    if selected is None:
        return True
    built_in_rank = policy.priority.index("built-in")
    selected_rank = policy.priority.index(_source_category(selected))
    return built_in_rank < selected_rank


def _choose_recovered_builtin_source(
    sources: Sequence[_PulseSource],
) -> _PulseSource:
    built_in = tuple(
        source
        for source in _usable_sources(sources)
        if _source_category(source) == "built-in"
    )
    if len(built_in) != 1:
        raise AudioDeviceError("recovered built-in microphone selection is ambiguous")
    return built_in[0]


def _list_pulse_sources(
    runner: Callable[[Sequence[str]], str],
) -> tuple[_PulseSource, ...]:
    """Enumerate sources with metadata, falling back to legacy short output."""

    try:
        output = runner(("--format=json", "list", "sources"))
    except FileNotFoundError:
        # Preserve this sentinel for resolve_input_device's no-pactl fallback;
        # a present-but-failing pactl must instead fail closed below.
        raise
    except AudioDeviceError:
        raise
    except Exception:
        output = None
    if output is not None:
        if len(output.encode("utf-8", errors="replace")) > _MAX_PACTL_OUTPUT_BYTES:
            raise AudioDeviceError("PulseAudio source list is too large")
        try:
            document = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            document = None
        if isinstance(document, list):
            parsed = tuple(_pulse_source_from_json(item) for item in document)
            if any(source is None for source in parsed):
                raise AudioDeviceError(
                    "PulseAudio source discovery returned invalid data"
                )
            sources = tuple(source for source in parsed if source is not None)
            # An empty JSON list is a valid observation: there are no sources.
            return sources

    try:
        output = runner(("list", "short", "sources"))
    except FileNotFoundError:
        raise
    except AudioDeviceError:
        raise
    except Exception as error:
        raise AudioDeviceError("PulseAudio source discovery failed") from error
    if len(output.encode("utf-8", errors="replace")) > _MAX_PACTL_OUTPUT_BYTES:
        raise AudioDeviceError("PulseAudio source list is too large")
    sources: list[_PulseSource] = []
    malformed = False
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            malformed = True
            continue
        name = fields[1].strip()
        if not name or "\x00" in name or len(name) > 512:
            malformed = True
            continue
        state = fields[-1].strip().upper() if len(fields) >= 5 else "UNKNOWN"
        sources.append(_PulseSource(name, state, category=_category_from_name(name)))
    if malformed and not sources:
        raise AudioDeviceError("PulseAudio returned an invalid source list")
    return tuple(sources)


def _pulse_source_from_json(value: Any) -> _PulseSource | None:
    if not isinstance(value, Mapping):
        return None
    name = _safe_name(value.get("name"))
    if name is None:
        return None
    raw_card_index = value.get("card")
    card_index = _pulse_index(raw_card_index)
    properties = value.get("properties")
    safe_properties = properties if isinstance(properties, Mapping) else {}
    device_class = str(safe_properties.get("device.class") or "").casefold()
    media_class = str(safe_properties.get("media.class") or "").casefold()
    if device_class == "monitor" or any(
        marker in media_class for marker in ("sink", "monitor")
    ):
        category = "monitor"
    else:
        category = _category_from_metadata(name, value, safe_properties)
    available = _active_port_available(value)
    state = str(value.get("state") or "UNKNOWN").strip().upper()
    return _PulseSource(name, state, card_index, category, available)


def _category_from_metadata(
    name: str,
    source: Mapping[Any, Any],
    properties: Mapping[Any, Any],
) -> str:
    vendor_id = _normalized_hex_identifier(
        properties.get("device.vendor.id")
        or properties.get("device.vendor_id")
        or properties.get("usb.vendor_id")
    )
    product_id = _normalized_hex_identifier(
        properties.get("device.product.id")
        or properties.get("device.product_id")
        or properties.get("usb.product_id")
    )
    if vendor_id is not None or product_id is not None:
        if vendor_id == "2ca3" and product_id == "4011":
            return "dji"
    elif is_dji_source(name):
        return "dji"

    active_port_text = _active_port_text(source)
    form_factor = str(properties.get("device.form_factor") or "").casefold()
    bus = str(properties.get("device.bus") or "").casefold()
    identity_text = " ".join(
        (
            name.casefold(),
            active_port_text,
            form_factor,
            str(properties.get("device.description") or "").casefold(),
            str(properties.get("device.product.name") or "").casefold(),
        )
    )
    if name.casefold().startswith("bluez_input.") or any(
        marker in identity_text
        for marker in ("headset", "headphone", "handsfree", "hands-free", "earbud")
    ):
        return "headset"
    if "external" in active_port_text:
        return "external"
    if (
        "internal" in active_port_text
        or form_factor == "internal"
        or bus in {"pci", "platform"}
        or name.casefold().startswith(
            ("alsa_input.pci-", "alsa_input.platform-", "alsa_input.soc-")
        )
    ):
        return "built-in"
    return "external"


def _category_from_name(name: str) -> str:
    return _category_from_metadata(name, {}, {})


def _source_category(source: _PulseSource) -> str:
    return source.category or _category_from_name(source.name)


def _normalized_hex_identifier(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return f"{value:04x}"
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    if candidate.startswith("0x"):
        candidate = candidate[2:]
    if not candidate or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        return None
    return candidate.lstrip("0") or "0"


def _active_port_text(source: Mapping[Any, Any]) -> str:
    active_port = source.get("active_port")
    if isinstance(active_port, Mapping):
        values = (
            active_port.get("name"),
            active_port.get("description"),
            active_port.get("type"),
        )
    else:
        values = (active_port,)
    active_name = _safe_name(active_port) if isinstance(active_port, str) else None
    matched = _find_port(source.get("ports"), active_name)
    if matched is not None:
        values = (
            *values,
            matched.get("name"),
            matched.get("description"),
            matched.get("type"),
        )
    return " ".join(str(value).casefold() for value in values if value is not None)


def _active_port_available(source: Mapping[Any, Any]) -> bool | None:
    active_port = source.get("active_port")
    active_name = _safe_name(active_port) if isinstance(active_port, str) else None
    port = active_port if isinstance(active_port, Mapping) else None
    if port is None:
        port = _find_port(source.get("ports"), active_name)
    if not isinstance(port, Mapping):
        return None
    value = port.get("availability", port.get("available"))
    if value is True:
        return True
    if value is False:
        return False
    normalized = str(value or "").strip().casefold().replace("_", "-").replace(" ", "-")
    if normalized in {"yes", "available", "true"}:
        return True
    if normalized in {"no", "not-available", "unavailable", "false"}:
        return False
    return None


def _find_port(value: Any, name: str | None) -> Mapping[Any, Any] | None:
    if name is None:
        return None
    if isinstance(value, Mapping):
        candidate = value.get(name)
        return candidate if isinstance(candidate, Mapping) else None
    if isinstance(value, list):
        matches = [
            item
            for item in value
            if isinstance(item, Mapping) and _safe_name(item.get("name")) == name
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def _get_default_source(runner: Callable[[Sequence[str]], str]) -> str | None:
    try:
        value = runner(("get-default-source",)).strip()
    except FileNotFoundError:
        raise
    except AudioDeviceError:
        raise
    except Exception:
        value = ""
    direct = _safe_name(value)
    if direct is not None:
        return direct
    try:
        info = runner(("info",))
    except FileNotFoundError:
        raise
    except AudioDeviceError:
        raise
    except Exception:
        return None
    for line in info.splitlines():
        label, separator, candidate = line.partition(":")
        if separator and label.strip() == "Default Source":
            return _safe_name(candidate.strip())
    return None


def _usable_sources(sources: Sequence[_PulseSource]) -> tuple[_PulseSource, ...]:
    return tuple(
        source
        for source in sources
        if not _is_monitor_source(source.name)
        and _source_category(source) != "monitor"
        and source.available is not False
        and source.state != "UNAVAILABLE"
    )


def _is_monitor_source(name: str) -> bool:
    return name.casefold().endswith(".monitor")


def _list_cards(runner: Callable[[Sequence[str]], str]) -> Any:
    try:
        output = runner(("--format=json", "list", "cards"))
    except FileNotFoundError:
        raise
    except AudioDeviceError:
        raise
    except Exception as error:
        raise AudioDeviceError("audio-card discovery failed") from error
    if len(output.encode("utf-8", errors="replace")) > _MAX_PACTL_OUTPUT_BYTES:
        raise AudioDeviceError("audio-card list is too large")
    try:
        document = json.loads(output)
    except (TypeError, json.JSONDecodeError) as error:
        raise AudioDeviceError("audio-card discovery returned invalid data") from error
    if not isinstance(document, list):
        raise AudioDeviceError("audio-card discovery returned invalid data")
    return document


def _list_card_sources(
    runner: Callable[[Sequence[str]], str], recovery: _ProfileRecovery
) -> tuple[_PulseSource, ...]:
    """Read sources bound by a strict Pulse/PipeWire card identity."""

    try:
        output = runner(("--format=json", "list", "sources"))
    except FileNotFoundError:
        raise
    except AudioDeviceError:
        raise
    except Exception as error:
        raise AudioDeviceError("audio-source identity discovery failed") from error
    if len(output.encode("utf-8", errors="replace")) > _MAX_PACTL_OUTPUT_BYTES:
        raise AudioDeviceError("audio-source identity list is too large")
    try:
        document = json.loads(output)
    except (TypeError, json.JSONDecodeError) as error:
        raise AudioDeviceError(
            "audio-source identity discovery returned invalid data"
        ) from error
    if not isinstance(document, list):
        raise AudioDeviceError("audio-source identity discovery returned invalid data")

    sources: list[_PulseSource] = []
    for item in document:
        if not isinstance(item, Mapping):
            continue
        parsed_source = _pulse_source_from_json(item)
        if parsed_source is None:
            continue
        name = parsed_source.name
        raw_card_index = item.get("card")
        source_card_index = _pulse_index(raw_card_index)
        properties = item.get("properties")
        if not isinstance(properties, Mapping):
            continue
        device_class = str(properties.get("device.class") or "").casefold()
        source_card_name = _safe_name(properties.get("device.name"))
        source_alsa_card = _safe_name(properties.get("alsa.card"))
        source_bus_path = _safe_name(properties.get("device.bus_path"))
        identity_conflicts = (
            (source_card_name is not None and source_card_name != recovery.card)
            or (
                recovery.alsa_card is not None
                and source_alsa_card is not None
                and source_alsa_card != recovery.alsa_card
            )
            or (
                recovery.bus_path is not None
                and source_bus_path is not None
                and source_bus_path != recovery.bus_path
            )
        )
        if raw_card_index is None:
            if source_card_name is not None:
                bound_to_card = source_card_name == recovery.card
            else:
                bound_to_card = (
                    recovery.alsa_card is not None
                    and recovery.bus_path is not None
                    and source_alsa_card == recovery.alsa_card
                    and source_bus_path == recovery.bus_path
                )
        elif source_card_index is None:
            # Invalid non-null indexes must not fall back to a name.
            bound_to_card = False
        else:
            bound_to_card = source_card_index == recovery.card_index
        if (
            not bound_to_card
            or identity_conflicts
            or _is_monitor_source(name)
            or device_class == "monitor"
            or _source_category(parsed_source) == "monitor"
            or parsed_source.available is False
            or parsed_source.state == "UNAVAILABLE"
        ):
            continue
        sources.append(parsed_source)
    return tuple(sources)


def _choose_profile_recovery(cards: Any) -> _ProfileRecovery:
    recoveries: list[_ProfileRecovery] = []
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        card_name = _safe_name(card.get("name"))
        card_index = _pulse_index(card.get("index"))
        properties = card.get("properties")
        alsa_card = (
            _safe_name(properties.get("alsa.card"))
            if isinstance(properties, Mapping)
            else None
        )
        bus_path = (
            _safe_name(properties.get("device.bus_path"))
            if isinstance(properties, Mapping)
            else None
        )
        device_bus = (
            str(properties.get("device.bus") or "").casefold()
            if isinstance(properties, Mapping)
            else ""
        )
        form_factor = (
            str(properties.get("device.form_factor") or "").casefold()
            if isinstance(properties, Mapping)
            else ""
        )
        active_name = _active_profile_name(card.get("active_profile"))
        profiles = card.get("profiles")
        if (
            card_name is None
            or card_index is None
            or not card_name.startswith("alsa_card.")
            or not (
                card_name.startswith(("alsa_card.pci-", "alsa_card.platform-"))
                or device_bus in {"pci", "platform"}
                or form_factor == "internal"
            )
            or active_name is None
            or not isinstance(profiles, Mapping)
        ):
            continue
        active = profiles.get(active_name)
        if not isinstance(active, Mapping):
            continue
        active_sinks = _nonnegative_int(active.get("sinks"))
        active_sources = _nonnegative_int(active.get("sources"))
        if active_sinks is None or active_sinks < 1 or active_sources != 0:
            continue

        card_recoveries: list[_ProfileRecovery] = []
        prefix = f"{active_name}+input:"
        for profile_name, profile in profiles.items():
            safe_profile = _safe_name(profile_name)
            if (
                safe_profile is None
                or not safe_profile.startswith(prefix)
                or not isinstance(profile, Mapping)
                or profile.get("available") is not True
                or _nonnegative_int(profile.get("sinks")) != active_sinks
            ):
                continue
            sources = _nonnegative_int(profile.get("sources"))
            priority = _nonnegative_int(profile.get("priority"))
            if sources != 1 or priority is None:
                continue
            card_recoveries.append(
                _ProfileRecovery(
                    card_name,
                    card_index,
                    alsa_card,
                    bus_path,
                    active_name,
                    safe_profile,
                    priority,
                )
            )

        if card_recoveries:
            highest_priority = max(recovery.priority for recovery in card_recoveries)
            winners = [
                recovery
                for recovery in card_recoveries
                if recovery.priority == highest_priority
            ]
            if len(winners) != 1:
                raise AudioDeviceError(
                    "input-capable card profile selection is ambiguous"
                )
            recoveries.append(winners[0])

    if not recoveries:
        raise AudioDeviceError("no safe input-capable card profile is available")
    if len(recoveries) != 1:
        raise AudioDeviceError("input-capable card profile selection is ambiguous")
    return recoveries[0]


def _active_profile_name(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("name")
    return _safe_name(value)


def _safe_name(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 512:
        return None
    return value


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _pulse_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _wait_for_recovered_sources(
    runner: Callable[[Sequence[str]], str],
    sleep: Callable[[float], None],
    recovery: _ProfileRecovery,
) -> tuple[_PulseSource, ...]:
    for delay in _SOURCE_REENUMERATION_DELAYS:
        if delay:
            sleep(delay)
        sources = _list_card_sources(runner, recovery)
        if sources:
            return sources
    raise AudioDeviceError("microphone source did not appear after profile recovery")


def _rollback_recovery(
    runner: Callable[[Sequence[str]], str],
    recovery: _ProfileRecovery,
    previous_default: str,
) -> None:
    """Best-effort profile rollback that preserves unrecognized live state."""

    try:
        current_profile = _current_card_profile(runner, recovery.card)
        if current_profile == recovery.previous_profile:
            return
        if current_profile != recovery.profile:
            logger.warning("Audio-card profile changed concurrently; not rolling back")
            return
        try:
            live_default = _get_default_source(runner)
        except Exception:
            live_default = None
        if live_default != previous_default:
            logger.warning(
                "Default microphone changed or is unreadable; preserving "
                "recovered profile"
            )
            return

        # pactl has no compare-and-swap. Re-read both values immediately before
        # the final mutation to narrow, but not eliminate, the remaining race.
        if _current_card_profile(runner, recovery.card) != recovery.profile:
            logger.warning("Audio-card profile changed concurrently; not rolling back")
            return
        if _get_default_source(runner) != previous_default:
            logger.warning(
                "Default microphone changed or is unreadable; preserving "
                "recovered profile"
            )
            return
        runner(("set-card-profile", recovery.card, recovery.previous_profile))
    except Exception:
        logger.error("Audio-card profile rollback failed")


def _recovery_is_reverted(
    runner: Callable[[Sequence[str]], str],
    recovery: _ProfileRecovery,
    previous_default: str,
) -> bool:
    """Confirm rollback before using an already-observed lower-ranked source."""

    try:
        if _current_card_profile(runner, recovery.card) != recovery.previous_profile:
            return False
        return _get_default_source(runner) == previous_default
    except Exception:
        return False


@contextmanager
def _pulse_source_environment(source: str | None) -> Iterator[None]:
    """Serialize stream opens and optionally bind one exact Pulse source."""

    if source is not None and _safe_name(source) != source:
        raise AudioDeviceError("invalid PulseAudio microphone source")
    with _PULSE_SOURCE_ENVIRONMENT_LOCK:
        if source is None:
            yield
            return
        was_present = "PULSE_SOURCE" in os.environ
        previous = os.environ.get("PULSE_SOURCE")
        os.environ["PULSE_SOURCE"] = source
        try:
            yield
        finally:
            if was_present:
                # os.environ values are always strings when present.
                assert previous is not None
                os.environ["PULSE_SOURCE"] = previous
            else:
                os.environ.pop("PULSE_SOURCE", None)


def _current_card_profile(
    runner: Callable[[Sequence[str]], str], card: str
) -> str | None:
    matches = [
        item
        for item in _list_cards(runner)
        if isinstance(item, Mapping) and _safe_name(item.get("name")) == card
    ]
    if len(matches) != 1:
        return None
    return _active_profile_name(matches[0].get("active_profile"))


def _choose_source(sources: Sequence[_PulseSource]) -> _PulseSource:
    if not sources:
        raise AudioDeviceError("no usable microphone source is available")
    if len(sources) != 1:
        raise AudioDeviceError("microphone source selection is ambiguous")
    return sources[0]


def _resolve_pulse_portaudio_device(
    sounddevice_module: Any | None = None,
) -> int:
    """Require one usable PortAudio input whose exact device name is ``pulse``."""

    sounddevice_module = sounddevice_module or _load_sounddevice()
    try:
        devices = sounddevice_module.query_devices()
    except Exception as error:
        raise AudioDeviceError("PulseAudio PortAudio input is unavailable") from error

    candidates: list[int] = []
    for index, device in enumerate(devices):
        if not isinstance(device, Mapping):
            continue
        if device.get("name") != "pulse":
            continue
        channels = _nonnegative_int(device.get("max_input_channels"))
        if channels is None or channels < 1:
            continue
        try:
            sounddevice_module.check_input_settings(
                device=index,
                channels=CHANNELS,
                dtype=SAMPLE_DTYPE,
                samplerate=SAMPLE_RATE,
            )
        except Exception:
            continue
        candidates.append(index)
    if len(candidates) != 1:
        raise AudioDeviceError("no unique usable PulseAudio PortAudio input exists")
    return candidates[0]


def _resolve_portaudio_input(sounddevice_module: Any) -> int | str | None:
    try:
        default = sounddevice_module.query_devices(kind="input")
    except Exception:
        default = None
    default_index = (
        _pulse_index(default.get("index")) if isinstance(default, Mapping) else None
    )
    if _is_trustworthy_portaudio_device(default) and default_index is not None:
        try:
            sounddevice_module.check_input_settings(
                device=default_index,
                channels=CHANNELS,
                dtype=SAMPLE_DTYPE,
                samplerate=SAMPLE_RATE,
            )
        except Exception:
            pass
        else:
            return default_index

    try:
        devices = sounddevice_module.query_devices()
    except Exception as error:
        raise AudioDeviceError("no usable microphone is available") from error
    candidates: list[int] = []
    for index, device in enumerate(devices):
        if not _is_trustworthy_portaudio_device(device):
            continue
        try:
            sounddevice_module.check_input_settings(
                device=index,
                channels=CHANNELS,
                dtype=SAMPLE_DTYPE,
                samplerate=SAMPLE_RATE,
            )
        except Exception:
            continue
        candidates.append(index)
    if len(candidates) != 1:
        raise AudioDeviceError("no unique usable microphone is available")
    return candidates[0]


def _is_trustworthy_portaudio_device(device: Any) -> bool:
    if not isinstance(device, Mapping):
        return False
    channels = _nonnegative_int(device.get("max_input_channels"))
    name = str(device.get("name") or "").strip().casefold()
    if channels is None or channels < 1 or not name or "monitor" in name:
        return False
    # Without pactl, generic routing aliases cannot prove that they do not
    # resolve to an output monitor. Prefer an inspectable hardware device.
    return name not in {"default", "pulse", "pipewire"}


def _run_pactl(arguments: Sequence[str], *, timeout: float) -> str:
    executable = shutil.which("pactl")
    if executable is None:
        raise FileNotFoundError("pactl is unavailable")
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    try:
        result = subprocess.run(
            [executable, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=environment,
        )
    except FileNotFoundError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise _PactlCommandError("pactl command failed") from error
    if result.returncode != 0:
        raise _PactlCommandError("pactl command failed")
    return result.stdout


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
