# SPDX-License-Identifier: GPL-3.0-only
"""Deterministic terminal enforcement for explicitly confirmed corrections.

Provider correction context is useful recognition guidance, but it is not a
delivery guarantee.  This module performs one bounded, non-cascading pass over
the authoritative provider final.  Only the already-validated pairs supplied
by the caller are considered; live hypotheses are never rewritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .config import CorrectionPair, normalize_correction_pairs

CONFIRMED_CORRECTION_PROCESSOR_NAME = "openvoice-confirmed-correction"
CONFIRMED_CORRECTION_PROCESSOR_VERSION = 1
MAX_CONFIRMED_CORRECTION_CODEPOINTS = 4096
MAX_CONFIRMED_CORRECTION_UTF8_BYTES = 16 * 1024
MAX_CONFIRMED_CORRECTION_EDITS = 64

ConfirmedCorrectionReasonCode = Literal[
    "unchanged",
    "corrected",
    "input-too-large",
    "output-too-large",
    "too-many-edits",
    "processor-error",
]

_ASCII_WORD = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)
_CODE_OR_URL_ADJACENT = frozenset("/\\@#=?&%:")


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmedCorrectionEdit:
    """One confirmed replacement in the provider-final coordinate space."""

    start: int
    end: int
    kind: Literal["confirmed-correction"] = "confirmed-correction"
    reason: Literal["explicit-user-rule"] = "explicit-user-rule"
    source: str = field(default="", repr=False)
    replacement: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmedCorrectionResult:
    """Corrected text and a bounded replayable audit trail."""

    text: str = field(repr=False)
    edits: tuple[ConfirmedCorrectionEdit, ...] = field(default=(), repr=False)
    reason_code: ConfirmedCorrectionReasonCode = "unchanged"

    @property
    def changed(self) -> bool:
        return bool(self.edits)


@dataclass(frozen=True, slots=True, repr=False)
class _Candidate:
    start: int
    end: int
    rule_index: int
    replacement: str = field(repr=False)


def apply_confirmed_corrections(
    provider_final: str,
    corrections: tuple[CorrectionPair, ...] | list[CorrectionPair],
) -> ConfirmedCorrectionResult:
    """Apply a leftmost-longest, single-pass confirmed-correction snapshot.

    ASCII terms match exact case, require lexical boundaries and are excluded
    from common URL, identifier, dotted-name and Markdown-code contexts. The
    operation is intentionally fail-open: size/edit limits preserve the raw
    provider final.
    """

    if not isinstance(provider_final, str):
        raise TypeError("provider_final must be a string")
    try:
        encoded_size = len(provider_final.encode("utf-8"))
    except UnicodeEncodeError:
        return ConfirmedCorrectionResult(provider_final, reason_code="input-too-large")
    if (
        len(provider_final) > MAX_CONFIRMED_CORRECTION_CODEPOINTS
        or encoded_size > MAX_CONFIRMED_CORRECTION_UTF8_BYTES
    ):
        return ConfirmedCorrectionResult(provider_final, reason_code="input-too-large")

    pairs = normalize_correction_pairs(corrections)
    if not provider_final or not pairs:
        return ConfirmedCorrectionResult(provider_final)

    code_spans = _backtick_code_spans(provider_final)
    candidates: list[_Candidate] = []
    for rule_index, pair in enumerate(pairs):
        for match in re.finditer(re.escape(pair.wrong), provider_final):
            start, end = match.span()
            if provider_final[start:end] == pair.canonical:
                continue
            if not _safe_match_context(
                provider_final,
                start,
                end,
                pair.wrong,
                code_spans,
            ):
                continue
            candidates.append(_Candidate(start, end, rule_index, pair.canonical))

    candidates.sort(
        key=lambda item: (item.start, -(item.end - item.start), item.rule_index)
    )
    selected: list[_Candidate] = []
    cursor = 0
    for candidate in candidates:
        if candidate.start < cursor:
            continue
        selected.append(candidate)
        cursor = candidate.end
        if len(selected) > MAX_CONFIRMED_CORRECTION_EDITS:
            return ConfirmedCorrectionResult(
                provider_final,
                reason_code="too-many-edits",
            )
    if not selected:
        return ConfirmedCorrectionResult(provider_final)

    edits = tuple(
        ConfirmedCorrectionEdit(
            start=item.start,
            end=item.end,
            source=provider_final[item.start : item.end],
            replacement=item.replacement,
        )
        for item in selected
    )
    corrected = replay_confirmed_corrections(provider_final, edits)
    if not _within_delivery_limit(corrected):
        return ConfirmedCorrectionResult(
            provider_final,
            reason_code="output-too-large",
        )
    return ConfirmedCorrectionResult(corrected, edits, "corrected")


def replay_confirmed_corrections(
    provider_final: str,
    edits: tuple[ConfirmedCorrectionEdit, ...],
) -> str:
    """Validate and replay one non-overlapping correction stage."""

    if not isinstance(provider_final, str) or not isinstance(edits, tuple):
        raise TypeError("confirmed correction replay is invalid")
    if len(edits) > MAX_CONFIRMED_CORRECTION_EDITS:
        raise ValueError("confirmed correction edit limit exceeded")
    pieces: list[str] = []
    cursor = 0
    for edit in edits:
        if (
            not isinstance(edit, ConfirmedCorrectionEdit)
            or type(edit.start) is not int
            or type(edit.end) is not int
            or edit.kind != "confirmed-correction"
            or edit.reason != "explicit-user-rule"
            or not isinstance(edit.source, str)
            or not isinstance(edit.replacement, str)
            or edit.start < cursor
            or edit.start < 0
            or edit.end <= edit.start
            or edit.end > len(provider_final)
            or provider_final[edit.start : edit.end] != edit.source
        ):
            raise ValueError("confirmed correction edit is invalid")
        pieces.append(provider_final[cursor : edit.start])
        pieces.append(edit.replacement)
        cursor = edit.end
    pieces.append(provider_final[cursor:])
    return "".join(pieces)


def validate_confirmed_correction_result(
    provider_final: str,
    result: ConfirmedCorrectionResult,
    allowed_corrections: tuple[CorrectionPair, ...] | list[CorrectionPair],
) -> str:
    """Return the replayed text after proving the complete stage invariant."""

    if not isinstance(result, ConfirmedCorrectionResult):
        raise TypeError("confirmed correction result is invalid")
    if result.reason_code not in {
        "unchanged",
        "corrected",
        "input-too-large",
        "output-too-large",
        "too-many-edits",
        "processor-error",
    }:
        raise ValueError("confirmed correction outcome is invalid")
    replayed = replay_confirmed_corrections(provider_final, result.edits)
    if replayed != result.text:
        raise ValueError("confirmed correction result cannot be replayed")
    pairs = normalize_correction_pairs(allowed_corrections)
    if result.reason_code == "corrected":
        if not result.edits or result.text == provider_final:
            raise ValueError("corrected result has no effective edits")
        if not _within_delivery_limit(result.text):
            raise ValueError("corrected result exceeds the delivery limit")
        code_spans = _backtick_code_spans(provider_final)
        for edit in result.edits:
            if not any(
                edit.source == pair.wrong
                and edit.replacement == pair.canonical
                and _safe_match_context(
                    provider_final,
                    edit.start,
                    edit.end,
                    pair.wrong,
                    code_spans,
                )
                for pair in pairs
            ):
                raise ValueError("confirmed correction authority is invalid")
    elif result.edits or result.text != provider_final:
        raise ValueError("confirmed correction fallback changed provider text")
    return replayed


def _safe_match_context(
    text: str,
    start: int,
    end: int,
    source: str,
    code_spans: tuple[tuple[int, int], ...],
) -> bool:
    if source.isascii():
        if source[0] in _ASCII_WORD and start > 0 and text[start - 1] in _ASCII_WORD:
            return False
        if source[-1] in _ASCII_WORD and end < len(text) and text[end] in _ASCII_WORD:
            return False
    if any(
        span_start <= start and end <= span_end for span_start, span_end in code_spans
    ):
        return False
    token_start = start
    while token_start > 0 and not _token_boundary(text[token_start - 1]):
        token_start -= 1
    token_end = end
    while token_end < len(text) and not _token_boundary(text[token_end]):
        token_end += 1
    token = text[token_start:token_end]
    if any(marker in token for marker in ("://", "www.", "/", "\\", "@", "::", "->")):
        return False
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    if before in _CODE_OR_URL_ADJACENT or after in _CODE_OR_URL_ADJACENT:
        return False
    if before == "." and start >= 2 and text[start - 2] in _ASCII_WORD:
        return False
    if after == "." and end + 1 < len(text) and text[end + 1] in _ASCII_WORD:
        return False
    return True


def _within_delivery_limit(text: str) -> bool:
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return (
        len(text) <= MAX_CONFIRMED_CORRECTION_CODEPOINTS
        and encoded_size <= MAX_CONFIRMED_CORRECTION_UTF8_BYTES
    )


def _token_boundary(character: str) -> bool:
    return character.isspace() or character in "，。！？；,!?;\"'“”‘’()（）[]【】{}"


def _backtick_code_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if text[index] != "`":
            index += 1
            continue
        width = 1
        while index + width < len(text) and text[index + width] == "`":
            width += 1
        closing = text.find("`" * width, index + width)
        if closing < 0:
            spans.append((index, len(text)))
            break
        spans.append((index, closing + width))
        index = closing + width
    return tuple(spans)
