"""Derive bounded correction candidates from an observed text edit.

The extractor is deliberately pure.  It receives the post-commit surrounding
text, the span committed by Murmur, and a later surrounding-text snapshot.
Multiple independent replacements may be captured, but unrelated edits,
insertions, deletions, and broad rewrites never become provider rules.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import Callable, Iterable, Literal

MAX_CANDIDATE_TEXT_CHARACTERS = 64
MAX_CHANGED_LEXICAL_TOKENS = 3
MIN_CANDIDATE_SIMILARITY = 0.4
MAX_REPLACEMENT_HUNKS = 8
MAX_DIFF_MIDDLE_TOKENS = 256

CorrectionCategory = Literal["recognition", "terminology", "formatting"]
CorrectionEvidence = Literal["strong", "medium"]


@dataclass(frozen=True, slots=True, repr=False)
class CorrectionCandidate:
    """One bounded adaptive correction, hidden from ordinary debug reprs."""

    wrong: str
    canonical: str
    category: CorrectionCategory = field(default="recognition", compare=False)
    evidence: CorrectionEvidence = field(default="strong", compare=False)


@dataclass(frozen=True, slots=True, repr=False)
class CorrectionExtractionResult:
    """Content-safe outcome plus private bounded candidates."""

    reason_code: str
    candidates: tuple[CorrectionCandidate, ...] = ()
    replacement_hunks: int = 0


class _TokenKind(Enum):
    WORD = "word"
    CJK = "cjk"
    SPACE = "space"
    PUNCTUATION = "punctuation"


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    start: int
    end: int
    kind: _TokenKind


def extract_correction(
    baseline_text: str,
    committed_start: int,
    committed_end: int,
    current_text: str,
    *,
    approved_terms: Iterable[str] = (),
    approved_term_resolver: Callable[[str], str] | None = None,
) -> CorrectionCandidate | None:
    """Return one conservative correction learned from a later edit.

    ``committed_start`` and ``committed_end`` are Python Unicode character
    offsets into ``baseline_text``.  ``approved_terms`` may contain the user's
    vocabulary followed by a system lexicon.  If removing separators from the
    observed canonical form uniquely identifies an approved term, that term's
    spelling is used (for example, ``bench mark`` becomes ``benchmark``).
    """

    result = extract_corrections(
        baseline_text,
        committed_start,
        committed_end,
        current_text,
        approved_terms=approved_terms,
        approved_term_resolver=approved_term_resolver,
    )
    if len(result.candidates) != 1 or result.replacement_hunks != 1:
        return None
    return result.candidates[0]


def extract_corrections(
    baseline_text: str,
    committed_start: int,
    committed_end: int,
    current_text: str,
    *,
    approved_terms: Iterable[str] = (),
    approved_term_resolver: Callable[[str], str] | None = None,
) -> CorrectionExtractionResult:
    """Capture one or more safe replacement hunks with an explicit reason.

    A single conservative replacement is strong evidence and may be activated
    immediately.  Several independent replacements are retained as medium
    evidence candidates for one explicit user confirmation.  Full surrounding
    text is used only in memory and is never part of the returned result.
    """

    if not isinstance(baseline_text, str) or not isinstance(current_text, str):
        return CorrectionExtractionResult("invalid-snapshot")
    if type(committed_start) is not int or type(committed_end) is not int:
        return CorrectionExtractionResult("invalid-committed-span")
    if not 0 <= committed_start < committed_end <= len(baseline_text):
        return CorrectionExtractionResult("invalid-committed-span")
    if baseline_text == current_text:
        return CorrectionExtractionResult("no-change")

    baseline_tokens = _tokenize(baseline_text)
    current_tokens = _tokenize(current_text)
    changes = _bounded_change_opcodes(baseline_tokens, current_tokens)
    if changes is None:
        return CorrectionExtractionResult("diff-too-complex")
    if not changes:
        return CorrectionExtractionResult("no-change")
    if len(changes) > MAX_REPLACEMENT_HUNKS:
        return CorrectionExtractionResult(
            "too-many-edits", replacement_hunks=len(changes)
        )

    for tag, baseline_first, baseline_last, current_first, current_last in changes:
        if (
            tag != "replace"
            or baseline_first == baseline_last
            or current_first == current_last
        ):
            return CorrectionExtractionResult(
                "insertion-or-deletion", replacement_hunks=len(changes)
            )
        changed_start = baseline_tokens[baseline_first].start
        changed_end = baseline_tokens[baseline_last - 1].end
        if changed_start < committed_start or changed_end > committed_end:
            return CorrectionExtractionResult(
                "edit-outside-committed-span", replacement_hunks=len(changes)
            )

    evidence: CorrectionEvidence = "strong" if len(changes) == 1 else "medium"
    candidates: list[CorrectionCandidate] = []
    for _tag, baseline_first, baseline_last, current_first, current_last in changes:
        baseline_slice = baseline_tokens[baseline_first:baseline_last]
        current_slice = current_tokens[current_first:current_last]
        baseline_lexical = sum(
            token.kind in {_TokenKind.WORD, _TokenKind.CJK} for token in baseline_slice
        )
        current_lexical = sum(
            token.kind in {_TokenKind.WORD, _TokenKind.CJK} for token in current_slice
        )
        if (
            evidence == "medium"
            and current_lexical > baseline_lexical
            and collapsed_term_key("".join(token.text for token in baseline_slice))
            != collapsed_term_key("".join(token.text for token in current_slice))
        ):
            return CorrectionExtractionResult(
                "insertion-or-deletion", replacement_hunks=len(changes)
            )
        candidate = _candidate_from_bounds(
            baseline_text,
            current_text,
            baseline_tokens,
            current_tokens,
            committed_start,
            committed_end,
            baseline_first,
            baseline_last,
            current_first,
            current_last,
            approved_terms=approved_terms,
            approved_term_resolver=approved_term_resolver,
            evidence=evidence,
        )
        if candidate is None:
            return CorrectionExtractionResult(
                "unsafe-or-broad-replacement", replacement_hunks=len(changes)
            )
        identity = (candidate.wrong, candidate.canonical)
        if all((item.wrong, item.canonical) != identity for item in candidates):
            candidates.append(candidate)
    if not candidates:
        return CorrectionExtractionResult(
            "unsafe-or-broad-replacement", replacement_hunks=len(changes)
        )
    return CorrectionExtractionResult(
        "strong-replacement" if evidence == "strong" else "multiple-replacements",
        tuple(candidates),
        replacement_hunks=len(changes),
    )


def _bounded_change_opcodes(
    baseline: tuple[_Token, ...],
    current: tuple[_Token, ...],
) -> tuple[tuple[str, int, int, int, int], ...] | None:
    """Diff only a bounded middle after linear common-edge trimming."""

    prefix = 0
    prefix_limit = min(len(baseline), len(current))
    while prefix < prefix_limit and baseline[prefix].text == current[prefix].text:
        prefix += 1
    baseline_last = len(baseline)
    current_last = len(current)
    while (
        baseline_last > prefix
        and current_last > prefix
        and baseline[baseline_last - 1].text == current[current_last - 1].text
    ):
        baseline_last -= 1
        current_last -= 1
    baseline_middle = baseline[prefix:baseline_last]
    current_middle = current[prefix:current_last]
    if max(len(baseline_middle), len(current_middle)) > MAX_DIFF_MIDDLE_TOKENS:
        return None
    matcher = SequenceMatcher(
        None,
        tuple(token.text for token in baseline_middle),
        tuple(token.text for token in current_middle),
        autojunk=False,
    )
    return tuple(
        (tag, i1 + prefix, i2 + prefix, j1 + prefix, j2 + prefix)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    )


def _candidate_from_bounds(
    baseline_text: str,
    current_text: str,
    baseline_tokens: tuple[_Token, ...],
    current_tokens: tuple[_Token, ...],
    committed_start: int,
    committed_end: int,
    baseline_first: int,
    baseline_last: int,
    current_first: int,
    current_last: int,
    *,
    approved_terms: Iterable[str],
    approved_term_resolver: Callable[[str], str] | None,
    evidence: CorrectionEvidence,
) -> CorrectionCandidate | None:
    if (
        sum(
            token.kind in {_TokenKind.WORD, _TokenKind.CJK}
            for token in baseline_tokens[baseline_first:baseline_last]
        )
        > MAX_CHANGED_LEXICAL_TOKENS
        or sum(
            token.kind in {_TokenKind.WORD, _TokenKind.CJK}
            for token in current_tokens[current_first:current_last]
        )
        > MAX_CHANGED_LEXICAL_TOKENS
    ):
        return None

    changed_start = baseline_tokens[baseline_first].start
    changed_end = baseline_tokens[baseline_last - 1].end
    if changed_start < committed_start or changed_end > committed_end:
        return None

    baseline_changed = baseline_tokens[baseline_first:baseline_last]
    current_changed = current_tokens[current_first:current_last]
    if not _contains_lexical_token(baseline_changed):
        return None
    if not _contains_lexical_token(current_changed):
        return None

    if _is_cross_script_replacement(baseline_changed, current_changed):
        expanded = _expand_to_unchanged_latin_context(
            baseline_tokens,
            current_tokens,
            baseline_first,
            baseline_last,
            current_first,
            current_last,
        )
        if expanded is None:
            # A bare CJK-to-Latin rule is too broad to learn automatically.
            return None
        baseline_first, baseline_last, current_first, current_last = expanded
        expanded_start = baseline_tokens[baseline_first].start
        expanded_end = baseline_tokens[baseline_last - 1].end
        if expanded_start < committed_start or expanded_end > committed_end:
            return None

    if _lexical_codepoint_count(baseline_tokens[baseline_first:baseline_last]) < 2:
        expanded = _expand_to_unchanged_lexical_context(
            baseline_tokens,
            current_tokens,
            baseline_first,
            baseline_last,
            current_first,
            current_last,
        )
        if expanded is None:
            return None
        baseline_first, baseline_last, current_first, current_last = expanded
        expanded_start = baseline_tokens[baseline_first].start
        expanded_end = baseline_tokens[baseline_last - 1].end
        if expanded_start < committed_start or expanded_end > committed_end:
            return None

    expanded = _expand_joined_punctuation_context(
        baseline_tokens,
        current_tokens,
        baseline_first,
        baseline_last,
        current_first,
        current_last,
    )
    baseline_first, baseline_last, current_first, current_last = expanded
    expanded_start = baseline_tokens[baseline_first].start
    expanded_end = baseline_tokens[baseline_last - 1].end
    if expanded_start < committed_start or expanded_end > committed_end:
        return None

    wrong = baseline_text[
        baseline_tokens[baseline_first].start : baseline_tokens[baseline_last - 1].end
    ].strip()
    canonical = current_text[
        current_tokens[current_first].start : current_tokens[current_last - 1].end
    ].strip()
    if not _valid_candidate_text(wrong) or not _valid_candidate_text(canonical):
        return None
    if wrong == canonical:
        return None

    if approved_term_resolver is not None:
        canonical = approved_term_resolver(canonical)
    else:
        canonical = canonicalize_with_approved_terms(canonical, approved_terms)
    if not _valid_candidate_text(canonical) or wrong == canonical:
        return None
    if not _specific_enough(wrong, canonical):
        return None
    return CorrectionCandidate(
        wrong=wrong,
        canonical=canonical,
        category=_classify_candidate(wrong, canonical),
        evidence=evidence,
    )


def _classify_candidate(wrong: str, canonical: str) -> CorrectionCategory:
    normalized_wrong = unicodedata.normalize("NFKC", wrong).casefold()
    normalized_canonical = unicodedata.normalize("NFKC", canonical).casefold()
    if " ".join(normalized_wrong.split()) == " ".join(normalized_canonical.split()):
        return "formatting"
    wrong_scripts = _lexical_scripts(wrong)
    canonical_scripts = _lexical_scripts(canonical)
    if wrong_scripts != canonical_scripts:
        return "terminology"
    return "recognition"


def _lexical_scripts(text: str) -> set[str]:
    scripts: set[str] = set()
    for character in text:
        if _is_cjk(character):
            scripts.add("cjk")
        elif character.isalpha():
            scripts.add("latin")
        elif character.isdigit():
            scripts.add("digit")
    return scripts


def canonicalize_with_approved_terms(
    text: str,
    approved_terms: Iterable[str],
) -> str:
    """Use an approved term when its separator-insensitive key is unique.

    Input order expresses preference: a personal-vocabulary spelling can be
    supplied before a larger system lexicon.  Different spellings with the
    same exact normalized form are harmless; genuinely ambiguous terms leave
    the observed text unchanged.
    """

    key = collapsed_term_key(text)
    if not key:
        return text

    preferred: str | None = None
    preferred_normalized: str | None = None
    ambiguous = False
    for term in approved_terms:
        if not isinstance(term, str) or not _valid_candidate_text(term.strip()):
            continue
        candidate = term.strip()
        if collapsed_term_key(candidate) != key:
            continue
        normalized = unicodedata.normalize("NFKC", candidate).casefold()
        if preferred is None:
            preferred = candidate
            preferred_normalized = normalized
        elif normalized != preferred_normalized:
            ambiguous = True
            break
    if preferred is None or ambiguous:
        return text
    return preferred


def collapsed_term_key(text: str) -> str:
    """Return a separator-insensitive NFKC/casefold key for lexicon lookup."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        character
        for character in normalized
        if character.isalnum() or unicodedata.category(character).startswith("M")
    )


def _tokenize(text: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            end = index + 1
            while end < len(text) and text[end].isspace():
                end += 1
            tokens.append(_Token(text[index:end], index, end, _TokenKind.SPACE))
            index = end
            continue
        if _is_cjk(character):
            tokens.append(_Token(character, index, index + 1, _TokenKind.CJK))
            index += 1
            continue
        if _is_word_character(character):
            end = index + 1
            while end < len(text):
                if _is_word_character(text[end]) and not _is_cjk(text[end]):
                    end += 1
                    continue
                if (
                    text[end] in {"-", "'", "\N{RIGHT SINGLE QUOTATION MARK}"}
                    and end + 1 < len(text)
                    and _is_word_character(text[end - 1])
                    and _is_word_character(text[end + 1])
                    and not _is_cjk(text[end + 1])
                ):
                    end += 1
                    continue
                break
            tokens.append(_Token(text[index:end], index, end, _TokenKind.WORD))
            index = end
            continue
        tokens.append(_Token(character, index, index + 1, _TokenKind.PUNCTUATION))
        index += 1
    return tuple(tokens)


def _single_replacement_bounds(
    baseline: tuple[_Token, ...],
    current: tuple[_Token, ...],
) -> tuple[int, int, int, int] | None:
    """Find one replacement in linear time and reject internal equal anchors."""

    prefix = 0
    prefix_limit = min(len(baseline), len(current))
    while prefix < prefix_limit and baseline[prefix].text == current[prefix].text:
        prefix += 1

    baseline_last = len(baseline)
    current_last = len(current)
    while (
        baseline_last > prefix
        and current_last > prefix
        and baseline[baseline_last - 1].text == current[current_last - 1].text
    ):
        baseline_last -= 1
        current_last -= 1

    if prefix == baseline_last or prefix == current_last:
        return None
    baseline_changed = baseline[prefix:baseline_last]
    current_changed = current[prefix:current_last]
    if (
        sum(
            token.kind in {_TokenKind.WORD, _TokenKind.CJK}
            for token in baseline_changed
        )
        > MAX_CHANGED_LEXICAL_TOKENS
        or sum(
            token.kind in {_TokenKind.WORD, _TokenKind.CJK} for token in current_changed
        )
        > MAX_CHANGED_LEXICAL_TOKENS
    ):
        return None

    # Two separated edits leave an equal token between them. Reject instead of
    # merging them into one broad replacement. The slices are already small in
    # lexical terms, so this bounded comparison cannot exhibit the quadratic
    # behavior of a general sequence matcher on a 4,096-character field.
    baseline_values = {token.text for token in baseline_changed}
    if any(token.text in baseline_values for token in current_changed):
        return None
    return prefix, baseline_last, prefix, current_last


def _expand_to_unchanged_latin_context(
    baseline: tuple[_Token, ...],
    current: tuple[_Token, ...],
    baseline_first: int,
    baseline_last: int,
    current_first: int,
    current_last: int,
) -> tuple[int, int, int, int] | None:
    right = _right_latin_context(
        baseline,
        current,
        baseline_last,
        current_last,
    )
    if right is not None:
        return baseline_first, right[0], current_first, right[1]

    left = _left_latin_context(
        baseline,
        current,
        baseline_first,
        current_first,
    )
    if left is not None:
        return left[0], baseline_last, left[1], current_last
    return None


def _expand_to_unchanged_lexical_context(
    baseline: tuple[_Token, ...],
    current: tuple[_Token, ...],
    baseline_first: int,
    baseline_last: int,
    current_first: int,
    current_last: int,
) -> tuple[int, int, int, int] | None:
    """Make a one-character source contextual instead of globally dangerous."""

    left = _left_matching_lexical_context(
        baseline,
        current,
        baseline_first,
        current_first,
    )
    if left is not None:
        return left[0], baseline_last, left[1], current_last
    right = _right_matching_lexical_context(
        baseline,
        current,
        baseline_last,
        current_last,
    )
    if right is not None:
        return baseline_first, right[0], current_first, right[1]
    return None


def _expand_joined_punctuation_context(
    baseline: tuple[_Token, ...],
    current: tuple[_Token, ...],
    baseline_first: int,
    baseline_last: int,
    current_first: int,
    current_last: int,
) -> tuple[int, int, int, int]:
    """Keep punctuation-bound suffixes inside one technical-term rule."""

    if baseline_last >= len(baseline) or current_last >= len(current):
        return baseline_first, baseline_last, current_first, current_last
    baseline_next = baseline[baseline_last]
    current_next = current[current_last]
    current_changed = current[current_first:current_last]
    if (
        not current_changed
        or current_changed[-1].kind is not _TokenKind.PUNCTUATION
        or baseline_next.kind not in {_TokenKind.WORD, _TokenKind.CJK}
        or current_next.kind not in {_TokenKind.WORD, _TokenKind.CJK}
        or baseline_next.text != current_next.text
    ):
        return baseline_first, baseline_last, current_first, current_last
    return baseline_first, baseline_last + 1, current_first, current_last + 1


def _left_matching_lexical_context(
    baseline: tuple[_Token, ...],
    current: tuple[_Token, ...],
    baseline_index: int,
    current_index: int,
) -> tuple[int, int] | None:
    baseline_index -= 1
    current_index -= 1
    while (
        baseline_index >= 0
        and current_index >= 0
        and baseline[baseline_index].kind not in {_TokenKind.WORD, _TokenKind.CJK}
        and current[current_index].kind not in {_TokenKind.WORD, _TokenKind.CJK}
        and baseline[baseline_index].text == current[current_index].text
    ):
        baseline_index -= 1
        current_index -= 1
    if (
        baseline_index >= 0
        and current_index >= 0
        and baseline[baseline_index].kind in {_TokenKind.WORD, _TokenKind.CJK}
        and current[current_index].kind in {_TokenKind.WORD, _TokenKind.CJK}
        and baseline[baseline_index].text == current[current_index].text
    ):
        return baseline_index, current_index
    return None


def _right_matching_lexical_context(
    baseline: tuple[_Token, ...],
    current: tuple[_Token, ...],
    baseline_index: int,
    current_index: int,
) -> tuple[int, int] | None:
    while (
        baseline_index < len(baseline)
        and current_index < len(current)
        and baseline[baseline_index].kind not in {_TokenKind.WORD, _TokenKind.CJK}
        and current[current_index].kind not in {_TokenKind.WORD, _TokenKind.CJK}
        and baseline[baseline_index].text == current[current_index].text
    ):
        baseline_index += 1
        current_index += 1
    if (
        baseline_index < len(baseline)
        and current_index < len(current)
        and baseline[baseline_index].kind in {_TokenKind.WORD, _TokenKind.CJK}
        and current[current_index].kind in {_TokenKind.WORD, _TokenKind.CJK}
        and baseline[baseline_index].text == current[current_index].text
    ):
        return baseline_index + 1, current_index + 1
    return None


def _right_latin_context(
    baseline: tuple[_Token, ...],
    current: tuple[_Token, ...],
    baseline_index: int,
    current_index: int,
) -> tuple[int, int] | None:
    while (
        baseline_index < len(baseline)
        and current_index < len(current)
        and baseline[baseline_index].kind is _TokenKind.SPACE
        and current[current_index].kind is _TokenKind.SPACE
        and baseline[baseline_index].text == current[current_index].text
    ):
        baseline_index += 1
        current_index += 1
    if (
        baseline_index < len(baseline)
        and current_index < len(current)
        and baseline[baseline_index].kind is _TokenKind.WORD
        and current[current_index].kind is _TokenKind.WORD
        and baseline[baseline_index].text == current[current_index].text
        and _contains_latin(baseline[baseline_index].text)
    ):
        return baseline_index + 1, current_index + 1
    return None


def _left_latin_context(
    baseline: tuple[_Token, ...],
    current: tuple[_Token, ...],
    baseline_index: int,
    current_index: int,
) -> tuple[int, int] | None:
    baseline_index -= 1
    current_index -= 1
    while (
        baseline_index >= 0
        and current_index >= 0
        and baseline[baseline_index].kind is _TokenKind.SPACE
        and current[current_index].kind is _TokenKind.SPACE
        and baseline[baseline_index].text == current[current_index].text
    ):
        baseline_index -= 1
        current_index -= 1
    if (
        baseline_index >= 0
        and current_index >= 0
        and baseline[baseline_index].kind is _TokenKind.WORD
        and current[current_index].kind is _TokenKind.WORD
        and baseline[baseline_index].text == current[current_index].text
        and _contains_latin(baseline[baseline_index].text)
    ):
        return baseline_index, current_index
    return None


def _is_cross_script_replacement(
    baseline: tuple[_Token, ...],
    current: tuple[_Token, ...],
) -> bool:
    baseline_text = "".join(token.text for token in baseline)
    current_text = "".join(token.text for token in current)
    return (_contains_cjk(baseline_text) and _contains_latin(current_text)) or (
        _contains_latin(baseline_text) and _contains_cjk(current_text)
    )


def _contains_lexical_token(tokens: tuple[_Token, ...]) -> bool:
    return any(token.kind in {_TokenKind.WORD, _TokenKind.CJK} for token in tokens)


def _lexical_codepoint_count(tokens: tuple[_Token, ...]) -> int:
    return sum(
        1
        for token in tokens
        for character in token.text
        if _is_word_character(character)
    )


def _specific_enough(wrong: str, canonical: str) -> bool:
    """Reject sentence rewrites and low-similarity global substitutions."""

    wrong_tokens = _tokenize(wrong)
    canonical_tokens = _tokenize(canonical)
    wrong_lexical = sum(
        token.kind in {_TokenKind.WORD, _TokenKind.CJK} for token in wrong_tokens
    )
    canonical_lexical = sum(
        token.kind in {_TokenKind.WORD, _TokenKind.CJK} for token in canonical_tokens
    )
    if (
        not 1 <= wrong_lexical <= MAX_CHANGED_LEXICAL_TOKENS
        or not 1 <= canonical_lexical <= MAX_CHANGED_LEXICAL_TOKENS
        or _lexical_codepoint_count(wrong_tokens) < 2
    ):
        return False
    wrong_key = collapsed_term_key(wrong)
    canonical_key = collapsed_term_key(canonical)
    if not wrong_key or not canonical_key:
        return False
    return (
        SequenceMatcher(
            None,
            wrong_key,
            canonical_key,
            autojunk=False,
        ).ratio()
        >= MIN_CANDIDATE_SIMILARITY
    )


def _valid_candidate_text(text: str) -> bool:
    return bool(
        text
        and len(text) <= MAX_CANDIDATE_TEXT_CHARACTERS
        and all(character.isprintable() for character in text)
    )


def _is_word_character(character: str) -> bool:
    category = unicodedata.category(character)
    return character.isalnum() or category.startswith("M")


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    )


def _contains_cjk(text: str) -> bool:
    return any(_is_cjk(character) for character in text)


def _contains_latin(text: str) -> bool:
    return any("LATIN" in unicodedata.name(character, "") for character in text)
