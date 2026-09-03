# SPDX-License-Identifier: GPL-3.0-only
"""Private output-style policy and deterministic terminal delivery.

The provider transcript remains the retained authoritative ASR result. A
bounded confirmed-correction stage runs first; ``clean`` is a separate local
deletion-only stage. Both run only after the provider finishes, and live
partials are never rewritten. Every result carries enough information for an
opted-in dataset record to replay delivery from the raw provider final.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from .clean_expression import (
    MAX_CLEAN_EXPRESSION_EDITS,
    CleanExpressionEdit,
    CleanExpressionResult,
    clean_expression,
)
from .confirmed_correction import (
    CONFIRMED_CORRECTION_PROCESSOR_NAME,
    CONFIRMED_CORRECTION_PROCESSOR_VERSION,
    MAX_CONFIRMED_CORRECTION_EDITS,
    ConfirmedCorrectionEdit,
    ConfirmedCorrectionResult,
    ConfirmedCorrectionReasonCode,
    apply_confirmed_corrections,
    replay_confirmed_corrections,
    validate_confirmed_correction_result,
)
from .config import (
    CorrectionPair,
    ConfigError,
    _load_private_bytes,
    _reject_duplicate_json_fields,
    _write_private_json,
    normalize_correction_pairs,
)

OUTPUT_STYLE_CONFIG_VERSION = 1
OUTPUT_STYLE_MODES = ("faithful", "clean")
DEFAULT_OUTPUT_STYLE_MODE = "faithful"
MAX_OUTPUT_STYLE_CONFIG_BYTES = 8 * 1024
OUTPUT_PROCESSOR_NAME = "openvoice-clean-expression"
OUTPUT_PROCESSOR_VERSION = 1

OutputStyleMode = Literal["faithful", "clean"]
DeliveryOutcome = Literal[
    "faithful",
    "unchanged",
    "cleaned",
    "input-too-large",
    "too-many-edits",
    "would-remove-all-content",
    "processor-error",
]


@dataclass(frozen=True, slots=True)
class OutputStyleConfig:
    """One user-selected output mode, frozen at utterance start."""

    mode: str = DEFAULT_OUTPUT_STYLE_MODE

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in OUTPUT_STYLE_MODES:
            raise ConfigError("output style mode is unsupported")


@dataclass(frozen=True, slots=True, repr=False)
class OutputDelivery:
    """The exact terminal text and replayable local transformation metadata."""

    mode: OutputStyleMode
    text: str = field(repr=False)
    processor: str
    processor_version: int
    outcome: DeliveryOutcome
    edits: tuple[CleanExpressionEdit, ...] = field(default=(), repr=False)
    correction_outcome: ConfirmedCorrectionReasonCode = "unchanged"
    correction_edits: tuple[ConfirmedCorrectionEdit, ...] = field(
        default=(), repr=False
    )
    correction_authority: tuple[CorrectionPair, ...] = field(default=(), repr=False)

    @property
    def changed(self) -> bool:
        return bool(self.correction_edits or self.edits)

    def as_record_document(self) -> dict[str, Any]:
        """Return style/transformation fields for an opted-in delivery record."""

        return {
            "mode": self.mode,
            "text": self.text,
            "review_status": "machine-derived-unreviewed",
            "pipeline": [
                {
                    "input_basis": "provider-final",
                    "processor": {
                        "name": CONFIRMED_CORRECTION_PROCESSOR_NAME,
                        "version": CONFIRMED_CORRECTION_PROCESSOR_VERSION,
                    },
                    "outcome": self.correction_outcome,
                    "edits": [_edit_document(edit) for edit in self.correction_edits],
                },
                {
                    "input_basis": "previous-stage",
                    "processor": {
                        "name": self.processor,
                        "version": self.processor_version,
                    },
                    "outcome": self.outcome,
                    "edits": [_edit_document(edit) for edit in self.edits],
                },
            ],
        }


Cleaner = Callable[[str], CleanExpressionResult]
Corrector = Callable[
    [str, tuple[CorrectionPair, ...] | list[CorrectionPair]],
    ConfirmedCorrectionResult,
]


def default_output_style_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "murmur-ime" / "output-style.json"


def load_output_style_config(
    path: str | os.PathLike[str] | None = None,
) -> OutputStyleConfig:
    """Load a private output policy; an absent file means faithful delivery."""

    config_path = Path(path) if path is not None else default_output_style_config_path()
    raw = _load_private_bytes(
        config_path,
        kind="output style configuration",
        limit=MAX_OUTPUT_STYLE_CONFIG_BYTES,
        missing_ok=True,
    )
    if raw is None:
        return OutputStyleConfig()
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ConfigError(
            "output style configuration is not valid UTF-8 JSON"
        ) from error
    if not isinstance(document, dict) or set(document) != {"version", "mode"}:
        raise ConfigError("output style configuration has unsupported fields")
    if (
        type(document.get("version")) is not int
        or document["version"] != OUTPUT_STYLE_CONFIG_VERSION
    ):
        raise ConfigError("output style configuration uses an unsupported version")
    return OutputStyleConfig(mode=document.get("mode"))


def save_output_style_config(
    mode: str,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Atomically persist one private policy without touching the voice service."""

    config = OutputStyleConfig(mode=mode)
    config_path = Path(path) if path is not None else default_output_style_config_path()
    return _write_private_json(
        config_path,
        {"version": OUTPUT_STYLE_CONFIG_VERSION, "mode": config.mode},
        kind="output style configuration",
        temporary_prefix=".output-style.json.",
    )


def deliver_output(
    provider_final: str,
    mode: str,
    *,
    corrections: tuple[CorrectionPair, ...] | list[CorrectionPair] = (),
    cleaner: Cleaner = clean_expression,
    corrector: Corrector = apply_confirmed_corrections,
) -> OutputDelivery:
    """Create terminal delivery with independently fail-open bounded stages.

    This function is deliberately total for a valid provider-final string and
    reviewed mode.  A cleaner exception or malformed result never blocks the
    accepted provider final and never leaks transcript content into diagnostics.
    """

    if not isinstance(provider_final, str):
        raise TypeError("provider_final must be a string")
    config = OutputStyleConfig(mode=mode)
    try:
        correction_authority = normalize_correction_pairs(corrections)
    except Exception:
        correction_authority = ()
        correction_result = ConfirmedCorrectionResult(
            provider_final,
            reason_code="processor-error",
        )
        corrected = provider_final
    else:
        try:
            correction_result = corrector(provider_final, correction_authority)
            if not isinstance(correction_result, ConfirmedCorrectionResult):
                raise TypeError("corrector returned an invalid result")
            corrected = validate_confirmed_correction_result(
                provider_final,
                correction_result,
                correction_authority,
            )
        except Exception:
            correction_result = ConfirmedCorrectionResult(
                provider_final,
                reason_code="processor-error",
            )
            corrected = provider_final
    if config.mode == "faithful":
        delivery = OutputDelivery(
            mode="faithful",
            text=corrected,
            processor="identity",
            processor_version=1,
            outcome="faithful",
            correction_outcome=correction_result.reason_code,
            correction_edits=correction_result.edits,
            correction_authority=correction_authority,
        )
        validate_output_delivery(
            provider_final,
            delivery,
            allowed_corrections=correction_authority,
        )
        return delivery

    try:
        result = cleaner(corrected)
        if not isinstance(result, CleanExpressionResult):
            raise TypeError("cleaner returned an invalid result")
        if (
            not isinstance(result.text, str)
            or not isinstance(result.edits, tuple)
            or len(result.edits) > MAX_CLEAN_EXPRESSION_EDITS
            or not isinstance(result.reason_code, str)
        ):
            raise TypeError("cleaner returned invalid result fields")
        if result.reason_code not in {
            "unchanged",
            "cleaned",
            "input-too-large",
            "too-many-edits",
            "would-remove-all-content",
        }:
            raise ValueError("cleaner returned an invalid outcome")
        _validate_clean_result(corrected, result)
        delivery = OutputDelivery(
            mode="clean",
            text=result.text,
            processor=OUTPUT_PROCESSOR_NAME,
            processor_version=OUTPUT_PROCESSOR_VERSION,
            outcome=result.reason_code,
            edits=result.edits,
            correction_outcome=correction_result.reason_code,
            correction_edits=correction_result.edits,
            correction_authority=correction_authority,
        )
        validate_output_delivery(
            provider_final,
            delivery,
            allowed_corrections=correction_authority,
        )
        return delivery
    except Exception:
        # No value derived from an untrusted/future cleaner escapes this
        # boundary. The fixed previous-stage fallback deliberately needs no
        # clean replay, so a validator regression cannot suppress a valid final.
        return OutputDelivery(
            mode="clean",
            text=corrected,
            processor=OUTPUT_PROCESSOR_NAME,
            processor_version=OUTPUT_PROCESSOR_VERSION,
            outcome="processor-error",
            correction_outcome=correction_result.reason_code,
            correction_edits=correction_result.edits,
            correction_authority=correction_authority,
        )


def validate_output_delivery(
    provider_final: str,
    delivery: OutputDelivery,
    *,
    allowed_corrections: tuple[CorrectionPair, ...]
    | list[CorrectionPair]
    | None = None,
) -> None:
    """Validate confirmed-replacement then identity/deletion-only invariants."""

    if not isinstance(provider_final, str) or not isinstance(delivery, OutputDelivery):
        raise TypeError("output delivery is invalid")
    if (
        not isinstance(delivery.mode, str)
        or not isinstance(delivery.text, str)
        or not isinstance(delivery.processor, str)
        or type(delivery.processor_version) is not int
        or not isinstance(delivery.outcome, str)
        or not isinstance(delivery.edits, tuple)
        or len(delivery.edits) > MAX_CLEAN_EXPRESSION_EDITS
        or not isinstance(delivery.correction_outcome, str)
        or not isinstance(delivery.correction_edits, tuple)
        or len(delivery.correction_edits) > MAX_CONFIRMED_CORRECTION_EDITS
        or not isinstance(delivery.correction_authority, tuple)
    ):
        raise ValueError("output delivery metadata is invalid")
    correction_authority = normalize_correction_pairs(delivery.correction_authority)
    if correction_authority != delivery.correction_authority:
        raise ValueError("output delivery correction authority is invalid")
    if allowed_corrections is not None:
        expected_authority = normalize_correction_pairs(allowed_corrections)
        if correction_authority != expected_authority:
            raise ValueError("output delivery correction authority is invalid")
    corrected = replay_confirmed_corrections(
        provider_final,
        delivery.correction_edits,
    )
    validate_confirmed_correction_result(
        provider_final,
        ConfirmedCorrectionResult(
            corrected,
            delivery.correction_edits,
            delivery.correction_outcome,
        ),
        correction_authority,
    )
    if delivery.mode == "faithful":
        if (
            delivery.text != corrected
            or delivery.processor != "identity"
            or delivery.processor_version != 1
            or delivery.outcome != "faithful"
            or delivery.edits
        ):
            raise ValueError("faithful output delivery is invalid")
        return
    if delivery.mode != "clean":
        raise ValueError("output delivery mode is invalid")
    if (
        delivery.processor != OUTPUT_PROCESSOR_NAME
        or delivery.processor_version != OUTPUT_PROCESSOR_VERSION
        or delivery.outcome
        not in {
            "unchanged",
            "cleaned",
            "input-too-large",
            "too-many-edits",
            "would-remove-all-content",
            "processor-error",
        }
    ):
        raise ValueError("clean output delivery metadata is invalid")
    result_reason = "cleaned" if delivery.outcome == "cleaned" else delivery.outcome
    if result_reason == "processor-error":
        if delivery.text != corrected or delivery.edits:
            raise ValueError("processor-error delivery changed corrected text")
        return
    _validate_clean_result(
        corrected,
        CleanExpressionResult(
            text=delivery.text,
            edits=delivery.edits,
            reason_code=result_reason,
        ),
    )


def _edit_document(
    edit: CleanExpressionEdit | ConfirmedCorrectionEdit,
) -> dict[str, int | str]:
    return {
        "start": edit.start,
        "end": edit.end,
        "kind": edit.kind,
        "reason": edit.reason,
        "source": edit.source,
        "replacement": edit.replacement,
    }


def _validate_clean_result(
    provider_final: str,
    result: CleanExpressionResult,
) -> None:
    """Prove the output is exactly the declared deletion-only edit replay."""

    cursor = 0
    pieces: list[str] = []
    for edit in result.edits:
        if (
            not isinstance(edit, CleanExpressionEdit)
            or type(edit.start) is not int
            or type(edit.end) is not int
            or not isinstance(edit.kind, str)
            or not isinstance(edit.reason, str)
            or not isinstance(edit.source, str)
            or not isinstance(edit.replacement, str)
            or edit.start < cursor
            or edit.start < 0
            or edit.end <= edit.start
            or edit.end > len(provider_final)
            or edit.replacement != ""
            or provider_final[edit.start : edit.end] != edit.source
            or (edit.kind == "filler" and edit.reason != "standalone-hesitation")
            or (
                edit.kind == "self-repetition"
                and edit.reason not in {"adjacent-exact-restart", "prefix-restart"}
            )
            or edit.kind not in {"filler", "self-repetition"}
        ):
            raise ValueError("cleaner returned an invalid edit")
        pieces.append(provider_final[cursor : edit.start])
        pieces.append(edit.replacement)
        cursor = edit.end
    pieces.append(provider_final[cursor:])
    replayed = "".join(pieces)
    if replayed != result.text:
        raise ValueError("cleaner result cannot be replayed")
    if result.edits and not any(character.isalnum() for character in result.text):
        raise ValueError("cleaner removed all lexical provider content")
    if result.reason_code == "cleaned" and not result.edits:
        raise ValueError("cleaned result has no edits")
    if result.reason_code != "cleaned" and (
        result.edits or result.text != provider_final
    ):
        raise ValueError("fallback result changed provider text")
