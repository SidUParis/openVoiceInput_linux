"""Pure adaptive-correction ledger validation, update, and compilation."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Literal

from .config import (
    MAX_CORRECTION_PAIRS,
    MAX_CORRECTION_TEXT_CHARACTERS,
    CorrectionPair,
)

ADAPTIVE_CORRECTIONS_SCHEMA_VERSION = 1
MAX_ADAPTIVE_ENTRIES = 500
MAX_ADAPTIVE_SUPPORT = 2_147_483_647

AdaptiveState = Literal["active", "conflicted", "suspended", "archived"]
ADAPTIVE_STATES: frozenset[str] = frozenset(
    {"active", "conflicted", "suspended", "archived"}
)


class AdaptiveStoreError(ValueError):
    """A content-free validation error safe to surface to callers."""


@dataclass(frozen=True, slots=True, repr=False)
class AdaptiveEntry:
    """One learned pair and its local lifecycle state."""

    wrong: str
    canonical: str
    state: AdaptiveState = "active"
    support: int = 1


@dataclass(frozen=True, slots=True, repr=False)
class AdaptiveLedger:
    """Versioned in-memory representation of the private learned ledger."""

    entries: tuple[AdaptiveEntry, ...] = field(default=(), repr=False)
    version: int = ADAPTIVE_CORRECTIONS_SCHEMA_VERSION


def parse_adaptive_ledger(document: Any) -> AdaptiveLedger:
    """Validate a decoded JSON-compatible ledger document."""

    if not isinstance(document, dict) or set(document) != {"version", "entries"}:
        raise AdaptiveStoreError("adaptive ledger has invalid top-level fields")
    version = document.get("version")
    if type(version) is not int or version != ADAPTIVE_CORRECTIONS_SCHEMA_VERSION:
        raise AdaptiveStoreError("adaptive ledger uses an unsupported schema")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise AdaptiveStoreError("adaptive ledger entries must be a list")
    if len(raw_entries) > MAX_ADAPTIVE_ENTRIES:
        raise AdaptiveStoreError("adaptive ledger contains too many entries")

    entries: list[AdaptiveEntry] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "wrong",
            "canonical",
            "state",
            "support",
        }:
            raise AdaptiveStoreError("adaptive ledger entry has invalid fields")
        entries.append(
            _validated_entry(
                AdaptiveEntry(
                    wrong=raw_entry.get("wrong"),
                    canonical=raw_entry.get("canonical"),
                    state=raw_entry.get("state"),
                    support=raw_entry.get("support"),
                )
            )
        )
    ledger = AdaptiveLedger(entries=tuple(entries))
    _validate_ledger_invariants(ledger)
    return ledger


def serialize_adaptive_ledger(ledger: AdaptiveLedger) -> dict[str, Any]:
    """Return a deterministic JSON-compatible representation of ``ledger``."""

    validated = _validated_ledger(ledger)
    return {
        "version": ADAPTIVE_CORRECTIONS_SCHEMA_VERSION,
        "entries": [
            {
                "wrong": entry.wrong,
                "canonical": entry.canonical,
                "state": entry.state,
                "support": entry.support,
            }
            for entry in validated.entries
        ],
    }


def record_correction(
    ledger: AdaptiveLedger,
    wrong: str,
    canonical: str,
) -> AdaptiveLedger:
    """Record evidence for a pair, preserving conflicts instead of overwriting.

    An identical normalized pair increments support.  A second canonical form
    for the same normalized wrong form marks every alternative ``conflicted``
    and appends the new evidence.  Suspended or archived pairs stay in their
    explicit state when merely observed again.
    """

    validated = _validated_ledger(ledger)
    new_entry = _validated_entry(
        AdaptiveEntry(wrong=wrong, canonical=canonical, state="active", support=1)
    )
    wrong_key = normalized_key(new_entry.wrong)
    canonical_key = normalized_key(new_entry.canonical)
    same_wrong_indexes = [
        index
        for index, entry in enumerate(validated.entries)
        if normalized_key(entry.wrong) == wrong_key
    ]
    same_pair_index = next(
        (
            index
            for index in same_wrong_indexes
            if normalized_key(validated.entries[index].canonical) == canonical_key
        ),
        None,
    )
    live_same_wrong_indexes = [
        index
        for index in same_wrong_indexes
        if validated.entries[index].state in {"active", "conflicted"}
    ]

    entries = list(validated.entries)
    if same_pair_index is not None:
        existing = entries[same_pair_index]
        entries[same_pair_index] = replace(
            existing,
            support=min(existing.support + 1, MAX_ADAPTIVE_SUPPORT),
        )
        if entries[same_pair_index].state in {"active", "conflicted"} and (
            len(
                {
                    normalized_key(entries[index].canonical)
                    for index in live_same_wrong_indexes
                }
            )
            > 1
        ):
            for index in live_same_wrong_indexes:
                entries[index] = replace(entries[index], state="conflicted")
        return AdaptiveLedger(entries=tuple(entries))

    if live_same_wrong_indexes:
        for index in live_same_wrong_indexes:
            entries[index] = replace(entries[index], state="conflicted")
        if len(entries) >= MAX_ADAPTIVE_ENTRIES:
            # Preserve the safety fact even when there is no room to retain
            # the new alternative: the previously active mapping is no longer
            # safe to send to the provider.
            return AdaptiveLedger(entries=tuple(entries))
        new_entry = replace(new_entry, state="conflicted")
    elif len(entries) >= MAX_ADAPTIVE_ENTRIES:
        raise AdaptiveStoreError("adaptive ledger contains too many entries")
    entries.append(new_entry)
    return AdaptiveLedger(entries=tuple(entries))


def compile_provider_corrections(
    manual_pairs: Iterable[CorrectionPair],
    ledger: AdaptiveLedger,
    *,
    limit: int = MAX_CORRECTION_PAIRS,
) -> tuple[CorrectionPair, ...]:
    """Compile a bounded provider view with manual corrections first.

    Learned conflicts and cycles are suppressed.  Manual sources reserve their
    normalized and overlapping forms.  Remaining learned rules are ordered by
    source specificity, support, and stable lexical keys, then only the most
    specific non-overlapping rules are emitted.  The ledger itself is never
    truncated when the provider limit is reached.
    """

    if type(limit) is not int or limit < 0 or limit > MAX_CORRECTION_PAIRS:
        raise AdaptiveStoreError("provider correction limit is invalid")
    validated = _validated_ledger(ledger)

    manual = tuple(_coerce_manual_pair(pair) for pair in manual_pairs)
    selected: list[CorrectionPair] = []
    seen_manual_exact: set[tuple[str, str]] = set()
    for pair in manual:
        exact = (pair.wrong, pair.canonical)
        if exact in seen_manual_exact:
            continue
        seen_manual_exact.add(exact)
        if len(selected) < limit:
            selected.append(pair)
    if len(selected) >= limit:
        return tuple(selected)

    manual_source_keys = {normalized_key(pair.wrong) for pair in manual}
    manual_canonical_keys = {normalized_key(pair.canonical) for pair in manual}
    manual_graph = {
        normalized_key(pair.wrong): normalized_key(pair.canonical)
        for pair in manual
        if normalized_key(pair.wrong) != normalized_key(pair.canonical)
    }

    active = [entry for entry in validated.entries if entry.state == "active"]
    canonicals_by_source: dict[str, set[str]] = {}
    for entry in active:
        canonicals_by_source.setdefault(normalized_key(entry.wrong), set()).add(
            normalized_key(entry.canonical)
        )
    unambiguous = [
        entry
        for entry in active
        if len(canonicals_by_source[normalized_key(entry.wrong)]) == 1
    ]

    adaptive_graph = dict(manual_graph)
    for entry in unambiguous:
        source = normalized_key(entry.wrong)
        target = normalized_key(entry.canonical)
        if source not in manual_source_keys and source != target:
            adaptive_graph[source] = target
    cyclic_sources = {
        normalized_key(entry.wrong)
        for entry in unambiguous
        if entry.wrong == entry.canonical
        or (
            normalized_key(entry.wrong) != normalized_key(entry.canonical)
            and _edge_is_cyclic(
                normalized_key(entry.wrong),
                normalized_key(entry.canonical),
                adaptive_graph,
            )
        )
    }

    learned_source_keys = {normalized_key(entry.wrong) for entry in unambiguous}
    all_source_keys = manual_source_keys | learned_source_keys
    cascade_sources: set[str] = set()
    for entry in unambiguous:
        source = normalized_key(entry.wrong)
        target = normalized_key(entry.canonical)
        if any(
            _sources_overlap(source, manual_canonical)
            for manual_canonical in manual_canonical_keys
        ):
            # A learned rule must never rewrite an explicit manual rule's
            # output, even when the provider happens to apply corrections
            # recursively or in an undocumented order.
            cascade_sources.add(source)
        if source == target:
            # Case/spelling presentation corrections such as openai -> OpenAI
            # are useful and cannot create a normalized provider chain.
            continue
        for other_source in all_source_keys:
            if not _sources_overlap(target, other_source):
                continue
            cascade_sources.add(source)
            if other_source in learned_source_keys:
                cascade_sources.add(other_source)

    candidates = [
        entry
        for entry in unambiguous
        if normalized_key(entry.wrong) not in manual_source_keys
        and normalized_key(entry.wrong) not in cyclic_sources
        and normalized_key(entry.wrong) not in cascade_sources
    ]
    candidates.sort(
        key=lambda entry: (
            -len(normalized_key(entry.wrong)),
            -entry.support,
            normalized_key(entry.wrong),
            normalized_key(entry.canonical),
            entry.wrong,
            entry.canonical,
        )
    )

    reserved_sources = list(manual_source_keys)
    emitted_adaptive_sources: set[str] = set()
    for entry in candidates:
        source = normalized_key(entry.wrong)
        if source in emitted_adaptive_sources:
            continue
        if any(_sources_overlap(source, reserved) for reserved in reserved_sources):
            continue
        selected.append(CorrectionPair(entry.wrong, entry.canonical))
        emitted_adaptive_sources.add(source)
        reserved_sources.append(source)
        if len(selected) >= limit:
            break
    return tuple(selected)


def normalized_key(text: str) -> str:
    """Normalize correction identity with NFKC, casefold, and folded spacing."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(normalized.split())


def _validated_ledger(ledger: AdaptiveLedger) -> AdaptiveLedger:
    if not isinstance(ledger, AdaptiveLedger):
        raise AdaptiveStoreError("adaptive ledger value is invalid")
    if (
        type(ledger.version) is not int
        or ledger.version != ADAPTIVE_CORRECTIONS_SCHEMA_VERSION
    ):
        raise AdaptiveStoreError("adaptive ledger uses an unsupported schema")
    if not isinstance(ledger.entries, (list, tuple)):
        raise AdaptiveStoreError("adaptive ledger entries must be a list")
    if len(ledger.entries) > MAX_ADAPTIVE_ENTRIES:
        raise AdaptiveStoreError("adaptive ledger contains too many entries")
    validated = AdaptiveLedger(
        entries=tuple(_validated_entry(entry) for entry in ledger.entries)
    )
    _validate_ledger_invariants(validated)
    return validated


def _validated_entry(entry: AdaptiveEntry) -> AdaptiveEntry:
    if not isinstance(entry, AdaptiveEntry):
        raise AdaptiveStoreError("adaptive ledger entry is invalid")
    wrong = _validated_text(entry.wrong)
    canonical = _validated_text(entry.canonical)
    if not isinstance(entry.state, str) or entry.state not in ADAPTIVE_STATES:
        raise AdaptiveStoreError("adaptive ledger entry state is invalid")
    if (
        type(entry.support) is not int
        or entry.support < 1
        or entry.support > MAX_ADAPTIVE_SUPPORT
    ):
        raise AdaptiveStoreError("adaptive ledger entry support is invalid")
    return AdaptiveEntry(
        wrong=wrong,
        canonical=canonical,
        state=entry.state,
        support=entry.support,
    )


def _validate_ledger_invariants(ledger: AdaptiveLedger) -> None:
    by_pair: set[tuple[str, str]] = set()
    for entry in ledger.entries:
        source = normalized_key(entry.wrong)
        canonical = normalized_key(entry.canonical)
        pair = (source, canonical)
        if pair in by_pair:
            raise AdaptiveStoreError("adaptive ledger contains a duplicate pair")
        by_pair.add(pair)


def _validated_text(value: Any) -> str:
    if not isinstance(value, str):
        raise AdaptiveStoreError("adaptive ledger text is invalid")
    if any(not character.isprintable() for character in value):
        raise AdaptiveStoreError("adaptive ledger text is invalid")
    text = value.strip()
    if not text or len(text) > MAX_CORRECTION_TEXT_CHARACTERS:
        raise AdaptiveStoreError("adaptive ledger text is invalid")
    return text


def _coerce_manual_pair(value: CorrectionPair) -> CorrectionPair:
    if not isinstance(value, CorrectionPair):
        raise AdaptiveStoreError("manual correction pair is invalid")
    return CorrectionPair(
        wrong=_validated_text(value.wrong),
        canonical=_validated_text(value.canonical),
    )


def _edge_is_cyclic(source: str, target: str, graph: dict[str, str]) -> bool:
    if source == target:
        return True
    seen: set[str] = set()
    current = target
    while current not in seen:
        if current == source:
            return True
        seen.add(current)
        next_value = graph.get(current)
        if next_value is None:
            return False
        current = next_value
    return False


def _sources_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    left_units = _source_units(left)
    right_units = _source_units(right)
    if not left_units or not right_units:
        return False
    if len(left_units) > len(right_units):
        left_units, right_units = right_units, left_units
    width = len(left_units)
    return any(
        right_units[index : index + width] == left_units
        for index in range(len(right_units) - width + 1)
    )


def _source_units(text: str) -> tuple[str, ...]:
    """Split overlap identity into word units and individual CJK characters."""

    units: list[str] = []
    word: list[str] = []

    def flush_word() -> None:
        if word:
            units.append("".join(word))
            word.clear()

    for character in text:
        codepoint = ord(character)
        is_cjk = (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
            or 0x20000 <= codepoint <= 0x323AF
        )
        if is_cjk:
            flush_word()
            units.append(character)
        elif character.isalnum() or unicodedata.category(character).startswith("M"):
            word.append(character)
        else:
            flush_word()
    flush_word()
    return tuple(units)
