"""Conservative, deterministic cleanup for one provider-final transcript.

This module deliberately implements a much smaller contract than general
"polishing".  It only deletes a narrow set of standalone hesitation tokens and
high-confidence adjacent self-restarts.  It never inserts text, changes a term,
normalizes punctuation, or crosses a sentence boundary.

All edit offsets refer to the original Python Unicode string.  Callers can
therefore retain the provider final alongside a reviewable, auditable diff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CleanExpressionKind = Literal["filler", "self-repetition"]
CleanExpressionReason = Literal[
    "standalone-hesitation",
    "adjacent-exact-restart",
    "prefix-restart",
]
CleanExpressionReasonCode = Literal[
    "unchanged",
    "cleaned",
    "input-too-large",
    "too-many-edits",
    "would-remove-all-content",
]

MAX_CLEAN_EXPRESSION_CODEPOINTS = 4096
MAX_CLEAN_EXPRESSION_EDITS = 64

_HORIZONTAL_SPACES = frozenset({" ", "\t", "\u00a0", "\u202f"})
_CLAUSE_SEPARATORS = frozenset({",", "，", "、"})
_SENTENCE_TERMINATORS = frozenset({".", "!", "?", "。", "！", "？"})
_ALLOWED_FILLER_BOUNDARIES = _CLAUSE_SEPARATORS | _SENTENCE_TERMINATORS
_QUOTE_MARKS = frozenset({"'", '"', "‘", "’", "“", "”", "«", "»", "‹", "›"})

# Capitalised forms are intentionally absent: ``UM`` may be an acronym.  The
# Chinese ``嗯`` receives an additional, stricter interstitial-context check.
_STANDALONE_FILLERS = ("euh", "um", "uh", "呃", "额", "嗯")

# Only pronouns/demonstratives and one discourse restart are safe enough to
# collapse as raw doubled characters.  Common lexical reduplication is not
# inferred from a dictionary or a language model.
_RESTART_CHARACTERS = frozenset({"我", "你", "他", "她", "它", "这", "那", "但"})

# These forms document and lock the important non-goals.  Most are already
# excluded by the restart trigger, but keeping an explicit whitelist prevents a
# future broadening of the detector from silently changing their semantics.
_PROTECTED_REDUPLICATIONS = frozenset(
    {
        "看看",
        "想想",
        "说说",
        "试试",
        "问问",
        "听听",
        "走走",
        "读读",
        "写写",
        "学学",
        "聊聊",
        "改改",
        "找找",
        "等等",
        "慢慢",
        "常常",
        "往往",
        "处处",
        "人人",
        "家家",
        "天天",
        "年年",
        "月月",
        "时时",
        "渐渐",
        "好好",
        "小小",
        "大大",
        "个个",
        "层层",
        "轻轻",
        "一层一层",
        "一个一个",
        "一步一步",
        "一点一点",
        "一次一次",
    }
)

# Exact shapes only: a repeated phrase is not evidence enough to delete it.
# These short scaffolds are the bounded subset observed as ASR self-restarts;
# arbitrary pronoun-led phrases would corrupt names (``我爱我爱罗``) and valid
# relative clauses (``我们支持我们支持的团队``).
_CURATED_RESTART_UNITS = frozenset(
    {
        "这个",
        "那个",
        "在这个",
        "在那个",
        "你要",
    }
)
_RELATIVE_CLAUSE_GUARDED_UNITS = frozenset({"你要"})
_MAX_RESTART_UNIT_CODEPOINTS = 8


@dataclass(frozen=True, slots=True)
class CleanExpressionEdit:
    """One deletion in original-text coordinates."""

    start: int
    end: int
    kind: CleanExpressionKind
    reason: CleanExpressionReason
    source: str = field(repr=False)
    replacement: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class CleanExpressionResult:
    """Clean text plus the complete bounded audit trail used to derive it."""

    text: str = field(repr=False)
    edits: tuple[CleanExpressionEdit, ...] = ()
    reason_code: CleanExpressionReasonCode = "unchanged"

    @property
    def changed(self) -> bool:
        return bool(self.edits)


@dataclass(frozen=True, slots=True)
class _EditBounds:
    start: int
    end: int
    kind: CleanExpressionKind
    reason: CleanExpressionReason


def clean_expression(provider_final: str) -> CleanExpressionResult:
    """Delete only high-confidence fillers and adjacent self-restarts.

    The returned text is guaranteed to be a Unicode-codepoint subsequence of
    ``provider_final``.  Invalid non-string input is a programmer error rather
    than something to coerce into user-visible text.
    """

    if not isinstance(provider_final, str):
        raise TypeError("provider_final must be a string")
    if len(provider_final) > MAX_CLEAN_EXPRESSION_CODEPOINTS:
        return CleanExpressionResult(provider_final, reason_code="input-too-large")
    if not provider_final or not _has_lexical_content(provider_final):
        return CleanExpressionResult(provider_final)

    cleaned, deletion_reasons, operation_count = _clean_to_fixed_point(provider_final)
    if operation_count > MAX_CLEAN_EXPRESSION_EDITS:
        return CleanExpressionResult(provider_final, reason_code="too-many-edits")
    if cleaned == provider_final:
        return CleanExpressionResult(provider_final)
    if not cleaned or not _has_lexical_content(cleaned):
        # A string made solely of hesitations is not silently converted into an
        # empty commit.  This also keeps the function total for stacked fillers.
        return CleanExpressionResult(
            provider_final,
            reason_code="would-remove-all-content",
        )
    edits = _original_coordinate_edits(provider_final, deletion_reasons)
    if len(edits) > MAX_CLEAN_EXPRESSION_EDITS:
        return CleanExpressionResult(provider_final, reason_code="too-many-edits")
    if _apply_deletions(provider_final, edits) != cleaned:
        raise AssertionError("clean-expression origin mapping invariant violated")
    return CleanExpressionResult(cleaned, edits, "cleaned")


def _clean_to_fixed_point(
    original: str,
) -> tuple[
    str,
    dict[int, tuple[CleanExpressionKind, CleanExpressionReason]],
    int,
]:
    """Run bounded deletion passes while retaining original coordinates.

    Each successful pass removes at least one codepoint.  The 64-operation
    budget is a fixed constant, so the bounded scanners remain linear in the
    at-most-4096-codepoint input.  The sixty-fifth operation is observed only
    to select the content-free fallback reason; none of its text is returned.
    """

    current = original
    origins = list(range(len(original)))
    deletion_reasons: dict[
        int,
        tuple[CleanExpressionKind, CleanExpressionReason],
    ] = {}
    operation_count = 0

    while operation_count <= MAX_CLEAN_EXPRESSION_EDITS:
        bounds = _clean_once(current)
        if not bounds:
            break
        operation_count += len(bounds)
        if operation_count > MAX_CLEAN_EXPRESSION_EDITS:
            break

        pieces: list[str] = []
        retained_origins: list[int] = []
        cursor = 0
        for edit in bounds:
            pieces.append(current[cursor : edit.start])
            retained_origins.extend(origins[cursor : edit.start])
            reason = (edit.kind, edit.reason)
            for origin in origins[edit.start : edit.end]:
                deletion_reasons[origin] = reason
            cursor = edit.end
        pieces.append(current[cursor:])
        retained_origins.extend(origins[cursor:])
        current = "".join(pieces)
        origins = retained_origins
    return current, deletion_reasons, operation_count


def _clean_once(text: str) -> tuple[_EditBounds, ...]:
    candidates = (
        *_repeated_character_edits(text),
        *_repeated_phrase_edits(text),
        *_prefix_restart_edits(text),
        *_standalone_filler_edits(text),
    )
    return _non_overlapping_edits(candidates)


def _original_coordinate_edits(
    original: str,
    deletion_reasons: dict[
        int,
        tuple[CleanExpressionKind, CleanExpressionReason],
    ],
) -> tuple[CleanExpressionEdit, ...]:
    edits: list[CleanExpressionEdit] = []
    ordered = sorted(deletion_reasons)
    cursor = 0
    while cursor < len(ordered):
        start = ordered[cursor]
        kind, reason = deletion_reasons[start]
        end = start + 1
        cursor += 1
        while (
            cursor < len(ordered)
            and ordered[cursor] == end
            and deletion_reasons[ordered[cursor]] == (kind, reason)
        ):
            end += 1
            cursor += 1
        edits.append(
            CleanExpressionEdit(
                start=start,
                end=end,
                kind=kind,
                reason=reason,
                source=original[start:end],
            )
        )
    return tuple(edits)


def _repeated_character_edits(text: str) -> tuple[_EditBounds, ...]:
    edits: list[_EditBounds] = []
    index = 0
    while index < len(text):
        character = text[index]
        run_end = index + 1
        while run_end < len(text) and text[run_end] == character:
            run_end += 1
        if (
            run_end - index >= 2
            and character in _RESTART_CHARACTERS
            and text[index:run_end] not in _PROTECTED_REDUPLICATIONS
        ):
            edits.append(
                _EditBounds(
                    index,
                    run_end - 1,
                    "self-repetition",
                    "adjacent-exact-restart",
                )
            )
        index = run_end
    return tuple(edits)


def _repeated_phrase_edits(text: str) -> tuple[_EditBounds, ...]:
    edits: list[_EditBounds] = []
    index = 0
    while index < len(text):
        maximum = min(_MAX_RESTART_UNIT_CODEPOINTS, (len(text) - index) // 2)
        matched = False
        for length in range(maximum, 0, -1):
            unit = text[index : index + length]
            if not _eligible_restart_unit(unit):
                continue
            second_start = _consume_restart_separator(text, index + length)
            if text[second_start : second_start + length] != unit:
                continue
            second_end = second_start + length
            if (
                unit in _RELATIVE_CLAUSE_GUARDED_UNITS
                and second_end < len(text)
                and text[second_end] in {"的", "地", "得"}
            ):
                continue
            source_form = text[index : second_start + length]
            if source_form in _PROTECTED_REDUPLICATIONS:
                continue
            edits.append(
                _EditBounds(
                    index,
                    second_start,
                    "self-repetition",
                    "adjacent-exact-restart",
                )
            )
            # Start again at the retained occurrence so a three-part stutter is
            # reduced to its final formulation without overlapping edits.
            index = second_start
            matched = True
            break
        if not matched:
            index += 1
    return tuple(edits)


def _prefix_restart_edits(text: str) -> tuple[_EditBounds, ...]:
    """Handle a false start such as ``但 但是`` without general prefix rules."""

    edits: list[_EditBounds] = []
    index = 0
    while True:
        index = text.find("但", index)
        if index < 0:
            break
        if index and not _is_restart_left_boundary(text[index - 1]):
            index += 1
            continue
        second_start = _consume_restart_separator(text, index + 1)
        if text.startswith("但是", second_start):
            edits.append(
                _EditBounds(
                    index,
                    second_start,
                    "self-repetition",
                    "prefix-restart",
                )
            )
            index = second_start + 2
        else:
            index += 1
    return tuple(edits)


def _standalone_filler_edits(text: str) -> tuple[_EditBounds, ...]:
    edits: list[_EditBounds] = []
    index = 0
    while index < len(text):
        filler = next(
            (item for item in _STANDALONE_FILLERS if text.startswith(item, index)),
            None,
        )
        if filler is None:
            index += 1
            continue
        end = index + len(filler)
        if not _valid_filler_boundaries(text, index, end):
            index += 1
            continue
        if filler == "嗯" and not (
            _has_lexical_content(text[:index]) and _has_lexical_content(text[end:])
        ):
            index = end
            continue
        start, deletion_end = _filler_deletion_bounds(text, index, end)
        remaining = text[:start] + text[deletion_end:]
        if start < deletion_end and _has_lexical_content(remaining):
            edits.append(
                _EditBounds(
                    start,
                    deletion_end,
                    "filler",
                    "standalone-hesitation",
                )
            )
        index = end
    return tuple(edits)


def _eligible_restart_unit(unit: str) -> bool:
    if len(unit) == 1:
        return unit in _RESTART_CHARACTERS
    return unit in _CURATED_RESTART_UNITS


def _consume_restart_separator(text: str, start: int) -> int:
    index = start
    while index < len(text) and text[index] in _HORIZONTAL_SPACES:
        index += 1
    if index < len(text) and text[index] in _CLAUSE_SEPARATORS:
        index += 1
        while index < len(text) and text[index] in _HORIZONTAL_SPACES:
            index += 1
    return index


def _valid_filler_boundaries(text: str, start: int, end: int) -> bool:
    if start > 0 and not _allowed_filler_side(text, start - 1, step=-1):
        return False
    if end < len(text) and not _allowed_filler_side(text, end, step=1):
        return False
    return True


def _allowed_filler_side(text: str, index: int, *, step: int) -> bool:
    immediate = text[index]
    if immediate not in _HORIZONTAL_SPACES:
        return immediate in _ALLOWED_FILLER_BOUNDARIES

    while 0 <= index < len(text) and text[index] in _HORIZONTAL_SPACES:
        index += step
    if not 0 <= index < len(text):
        return True
    nearest = text[index]
    if nearest in _QUOTE_MARKS:
        return False
    return (
        nearest.isalnum() or _is_cjk(nearest) or nearest in _ALLOWED_FILLER_BOUNDARIES
    )


def _filler_deletion_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left_space_start = start
    while left_space_start > 0 and text[left_space_start - 1] in _HORIZONTAL_SPACES:
        left_space_start -= 1
    left_mark = left_space_start - 1

    right_space_end = end
    while right_space_end < len(text) and text[right_space_end] in _HORIZONTAL_SPACES:
        right_space_end += 1
    right_mark = right_space_end if right_space_end < len(text) else None

    if right_mark is not None and text[right_mark] in _CLAUSE_SEPARATORS:
        if left_mark >= 0 and text[left_mark] == "," and text[right_mark] == ",":
            # In English/French prose the two commas only bracket the filler.
            # Delete both commas but retain the existing following space.
            return left_mark, right_mark + 1
        deletion_end = right_mark + 1
        while deletion_end < len(text) and text[deletion_end] in _HORIZONTAL_SPACES:
            deletion_end += 1
        return start, deletion_end

    if (
        left_mark >= 0
        and text[left_mark] in _CLAUSE_SEPARATORS
        and (right_mark is None or text[right_mark] in _SENTENCE_TERMINATORS)
    ):
        return left_mark, end

    # With whitespace-only boundaries, retain the left spacing and consume the
    # right spacing.  This preserves the original English/French word gap.
    return start, right_space_end


def _non_overlapping_edits(
    candidates: tuple[_EditBounds, ...],
) -> tuple[_EditBounds, ...]:
    ordered = sorted(
        candidates,
        key=lambda edit: (
            edit.start,
            -(edit.end - edit.start),
            0 if edit.reason == "prefix-restart" else 1,
            0 if edit.kind == "self-repetition" else 1,
        ),
    )
    accepted: list[_EditBounds] = []
    for candidate in ordered:
        if candidate.start >= candidate.end:
            continue
        if accepted and candidate.start <= accepted[-1].end:
            previous = accepted[-1]
            if candidate.kind == previous.kind and candidate.reason == previous.reason:
                accepted[-1] = _EditBounds(
                    previous.start,
                    max(previous.end, candidate.end),
                    previous.kind,
                    previous.reason,
                )
            continue
        accepted.append(candidate)
    return tuple(accepted)


def _apply_deletions(
    original: str,
    edits: tuple[CleanExpressionEdit, ...],
) -> str:
    text = original
    for edit in reversed(edits):
        if edit.replacement or original[edit.start : edit.end] != edit.source:
            raise AssertionError("clean-expression edit invariant violated")
        text = text[: edit.start] + text[edit.end :]
    return text


def _is_restart_left_boundary(character: str) -> bool:
    return (
        character in _HORIZONTAL_SPACES
        or character in _CLAUSE_SEPARATORS
        or character in _SENTENCE_TERMINATORS
        or character in {";", "；", ":", "："}
    )


def _has_lexical_content(text: str) -> bool:
    return any(character.isalnum() or _is_cjk(character) for character in text)


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2EBEF
    )
