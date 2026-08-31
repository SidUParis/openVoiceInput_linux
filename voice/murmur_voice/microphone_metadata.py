# SPDX-License-Identifier: GPL-3.0-only
"""Privacy-preserving microphone provenance for optional dataset records.

Raw Pulse source names, USB serials, Bluetooth addresses, and user-visible
hardware labels deliberately never cross this module's public boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

MICROPHONE_CATEGORIES = frozenset({"dji", "headset", "external", "built-in"})
MICROPHONE_FINGERPRINT_SCOPES = frozenset({"device-model", "category"})
MICROPHONE_SELECTION_BACKENDS = frozenset({"pulse", "portaudio"})
MICROPHONE_SELECTION_PROVENANCE = frozenset(
    {
        "policy-preferred",
        "system-default-within-category",
        "unique-policy-candidate",
        "current-dji-default-link-unknown",
        "recovered-built-in-profile",
        "portaudio-default",
        "unique-portaudio-candidate",
    }
)
DJI_LINK_STATES = frozenset(
    {"online", "offline", "unknown", "not-present", "not-probed-multiple"}
)
ACTUAL_ROUTE_OBSERVATION_METHODS = frozenset(
    {"pulse-source-output", "portaudio-opened-device"}
)
MAX_ROUTE_OBSERVATIONS = 16

_FINGERPRINT_PATTERN = re.compile(r"mic-v1-[0-9a-f]{24}\Z")
_SAFE_BUSES = frozenset({"usb", "pci", "platform", "bluetooth", "firewire"})
_SAFE_FORM_FACTORS = frozenset(
    {
        "internal",
        "headset",
        "headphone",
        "hands-free",
        "microphone",
        "webcam",
    }
)


@dataclass(frozen=True, slots=True)
class MicrophoneIdentity:
    """A stable, deliberately non-unique identifier for a safe device class."""

    category: str
    fingerprint: str
    fingerprint_scope: str

    def __post_init__(self) -> None:
        if self.category not in MICROPHONE_CATEGORIES:
            raise ValueError("microphone category is invalid")
        if not _FINGERPRINT_PATTERN.fullmatch(self.fingerprint):
            raise ValueError("microphone fingerprint is invalid")
        if self.fingerprint_scope not in MICROPHONE_FINGERPRINT_SCOPES:
            raise ValueError("microphone fingerprint scope is invalid")

    def as_record_document(self) -> dict[str, str]:
        return {
            "category": self.category,
            "fingerprint": self.fingerprint,
            "fingerprint_scope": self.fingerprint_scope,
        }


@dataclass(frozen=True, slots=True)
class MicrophoneSelectionMetadata:
    """The source requested when the recording stream was opened."""

    identity: MicrophoneIdentity
    backend: str
    provenance: str
    dji_link_state_at_selection: str

    def __post_init__(self) -> None:
        if self.backend not in MICROPHONE_SELECTION_BACKENDS:
            raise ValueError("microphone selection backend is invalid")
        if self.provenance not in MICROPHONE_SELECTION_PROVENANCE:
            raise ValueError("microphone selection provenance is invalid")
        if self.dji_link_state_at_selection not in DJI_LINK_STATES:
            raise ValueError("DJI link state is invalid")

    def as_record_document(self) -> dict[str, str]:
        return {
            "backend": self.backend,
            **self.identity.as_record_document(),
            "provenance": self.provenance,
            "dji_link_state_at_selection": self.dji_link_state_at_selection,
        }


@dataclass(frozen=True, slots=True)
class MicrophoneRouteObservation:
    """One observed actual source-output route, without its Pulse source name."""

    identity: MicrophoneIdentity
    first_observed_ms: int

    def __post_init__(self) -> None:
        if (
            type(self.first_observed_ms) is not int
            or self.first_observed_ms < 0
            or self.first_observed_ms > 600_000
        ):
            raise ValueError("microphone route observation time is invalid")

    def as_record_document(self) -> dict[str, Any]:
        return {
            **self.identity.as_record_document(),
            "first_observed_ms": self.first_observed_ms,
        }


@dataclass(frozen=True, slots=True)
class MicrophoneCaptureMetadata:
    """Immutable snapshot delivered asynchronously while capture continues."""

    selection: MicrophoneSelectionMetadata
    actual_route_observation_method: str
    actual_routes: tuple[MicrophoneRouteObservation, ...] = ()
    observation_truncated: bool = False

    def __post_init__(self) -> None:
        if self.actual_route_observation_method not in ACTUAL_ROUTE_OBSERVATION_METHODS:
            raise ValueError("microphone route observation method is invalid")
        if len(self.actual_routes) > MAX_ROUTE_OBSERVATIONS:
            raise ValueError("too many microphone route observations")
        if type(self.observation_truncated) is not bool:
            raise ValueError("microphone route truncation state is invalid")
        previous = -1
        for route in self.actual_routes:
            if route.first_observed_ms < previous:
                raise ValueError("microphone route observations are unordered")
            previous = route.first_observed_ms

    def as_record_document(self) -> dict[str, Any]:
        return {
            "selection": self.selection.as_record_document(),
            "actual": {
                "status": "observed" if self.actual_routes else "unknown",
                "observation_method": self.actual_route_observation_method,
                "routes": [route.as_record_document() for route in self.actual_routes],
                "route_changed": len(self.actual_routes) > 1,
                "observation_truncated": self.observation_truncated,
            },
        }


def privacy_preserving_microphone_identity(
    category: str,
    *,
    bus: Any = None,
    vendor_id: str | None = None,
    product_id: str | None = None,
    form_factor: Any = None,
) -> MicrophoneIdentity:
    """Hash only allowlisted hardware class/model facts.

    The raw source name is intentionally not accepted. Identical device models
    may share a fingerprint; that privacy-preserving collision is preferable to
    deriving an identifier from a USB serial, Bluetooth address, or custom name.
    """

    if category not in MICROPHONE_CATEGORIES:
        raise ValueError("microphone category is invalid")
    safe_bus = str(bus or "").strip().casefold()
    if safe_bus not in _SAFE_BUSES:
        safe_bus = "unknown"
    safe_form_factor = str(form_factor or "").strip().casefold().replace("_", "-")
    if safe_form_factor not in _SAFE_FORM_FACTORS:
        safe_form_factor = "unknown"
    safe_vendor = _safe_hex_identifier(vendor_id)
    safe_product = _safe_hex_identifier(product_id)
    scope = (
        "device-model"
        if safe_vendor is not None or safe_product is not None
        else "category"
    )
    document = {
        "version": 1,
        "category": category,
        "bus": safe_bus,
        "vendor_id": safe_vendor,
        "product_id": safe_product,
        "form_factor": safe_form_factor,
    }
    canonical = json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    digest = hashlib.sha256(canonical).hexdigest()[:24]
    return MicrophoneIdentity(category, f"mic-v1-{digest}", scope)


def _safe_hex_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip().casefold()
    if not candidate or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        return None
    return candidate
