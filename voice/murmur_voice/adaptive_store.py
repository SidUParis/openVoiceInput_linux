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

ADAPTIVE_CORRECTIONS_SCHEMA_VERSION = 2
LEGACY_ADAPTIVE_CORRECTIONS_SCHEMA_VERSION = 1
MAX_ADAPTIVE_ENTRIES = 500
MAX_ADAPTIVE_SUPPORT = 2_147_483_647

AdaptiveState = Literal["candidate", "active", "conflicted", "suspended", "archived"]
ADAPTIVE_STATES: frozenset[str] = frozenset(
    {"candidate", "active", "conflicted", "suspended", "archived"}
)
AdaptiveCategory = Literal["recognition", "terminology", "formatting"]
AdaptiveEvidence = Literal["strong", "medium", "explicit"]
ProviderCorrectionStatus = Literal[
    "effective-manual",
    "effective-adaptive",
    "suppressed-manual-source",
    "suppressed-conflicting-active",
    "suppressed-cycle",
    "suppressed-cascade",
    "suppressed-overlap",
    "suppressed-capacity",
]
ADAPTIVE_CATEGORIES = frozenset({"recognition", "terminology", "formatting"})
ADAPTIVE_EVIDENCE = frozenset({"strong", "medium", "explicit"})
MAX_ADAPTIVE_RESULT_COUNT = MAX_ADAPTIVE_ENTRIES


class AdaptiveStoreError(ValueError):
    """A content-free validation error safe to surface to callers."""


@dataclass(frozen=True, slots=True, repr=False)
class AdaptiveEntry:
    """One learned pair and its local lifecycle state."""

    wrong: str
    canonical: str
    state: AdaptiveState = "active"
    support: int = 1
    category: AdaptiveCategory = "recognition"
    evidence: AdaptiveEvidence = "strong"


@dataclass(frozen=True, slots=True)
class AdaptiveLastResult:
    """Transcript-free persisted outcome for UI and CLI observability."""

    reason_code: str
    captured_count: int = 0
    activated_count: int = 0
    candidate_count: int = 0
    conflicted_count: int = 0
    replacement_hunks: int = 0


@dataclass(frozen=True, slots=True, repr=False)
class AdaptiveLedger:
    """Versioned in-memory representation of the private learned ledger."""

    entries: tuple[AdaptiveEntry, ...] = field(default=(), repr=False)
    last_result: AdaptiveLastResult | None = None
    version: int = ADAPTIVE_CORRECTIONS_SCHEMA_VERSION


@dataclass(frozen=True, slots=True, repr=False)
class ProviderCorrectionDecision:
    """One content-private compiler decision for an active correction."""

    wrong: str
    canonical: str
    origin: Literal["manual", "adaptive"]
    status: ProviderCorrectionStatus


@dataclass(frozen=True, slots=True, repr=False)
class ProviderCorrectionReport:
    """The exact bounded provider view plus content-free diagnostics."""

    pairs: tuple[CorrectionPair, ...] = field(default=(), repr=False)
    decisions: tuple[ProviderCorrectionDecision, ...] = field(default=(), repr=False)

    def status_for(self, wrong: str, canonical: str) -> ProviderCorrectionStatus | None:
        """Return the effective/suppressed decision for one normalized pair."""

        identity = (normalized_key(wrong), normalized_key(canonical))
        # A matching explicit rule is authoritative even when the adaptive
        # ledger retains the same pair for evidence/history.
        for decision in self.decisions:
            if decision.origin != "manual":
                continue
            if (
                normalized_key(decision.wrong),
                normalized_key(decision.canonical),
            ) == identity:
                return decision.status
        for decision in self.decisions:
            if decision.origin != "adaptive":
                continue
            if (
                normalized_key(decision.wrong),
                normalized_key(decision.canonical),
            ) == identity:
                return decision.status
        return None

    def statistics(self) -> dict[str, Any]:
        """Return only counts and allowlisted reason codes, never pair text."""

        manual_effective = sum(
            decision.status == "effective-manual" for decision in self.decisions
        )
        adaptive_effective = sum(
            decision.status == "effective-adaptive" for decision in self.decisions
        )
        suppression_reasons: dict[str, int] = {}
        for decision in self.decisions:
            if decision.origin != "adaptive" or not decision.status.startswith(
                "suppressed-"
            ):
                continue
            suppression_reasons[decision.status] = (
                suppression_reasons.get(decision.status, 0) + 1
            )
        return {
            "effective_correction_count": len(self.pairs),
            "manual_effective_count": manual_effective,
            "adaptive_effective_count": adaptive_effective,
            "adaptive_suppressed_count": sum(suppression_reasons.values()),
            "suppression_reasons": dict(sorted(suppression_reasons.items())),
        }


def parse_adaptive_ledger(document: Any) -> AdaptiveLedger:
    """Validate a decoded JSON-compatible ledger document."""

    if not isinstance(document, dict) or set(document) not in (
        {"version", "entries"},
        {"version", "entries", "last_result"},
    ):
        raise AdaptiveStoreError("adaptive ledger has invalid top-level fields")
    version = document.get("version")
    if type(version) is not int or version not in {
        LEGACY_ADAPTIVE_CORRECTIONS_SCHEMA_VERSION,
        ADAPTIVE_CORRECTIONS_SCHEMA_VERSION,
    }:
        raise AdaptiveStoreError("adaptive ledger uses an unsupported schema")
    if (
        version == LEGACY_ADAPTIVE_CORRECTIONS_SCHEMA_VERSION
        and "last_result" in document
    ):
        raise AdaptiveStoreError("adaptive ledger has invalid top-level fields")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise AdaptiveStoreError("adaptive ledger entries must be a list")
    if len(raw_entries) > MAX_ADAPTIVE_ENTRIES:
        raise AdaptiveStoreError("adaptive ledger contains too many entries")

    entries: list[AdaptiveEntry] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) not in (
            {"wrong", "canonical", "state", "support"},
            {"wrong", "canonical", "state", "support", "category", "evidence"},
        ):
            raise AdaptiveStoreError("adaptive ledger entry has invalid fields")
        entries.append(
            _validated_entry(
                AdaptiveEntry(
                    wrong=raw_entry.get("wrong"),
                    canonical=raw_entry.get("canonical"),
                    state=raw_entry.get("state"),
                    support=raw_entry.get("support"),
                    category=raw_entry.get("category", "recognition"),
                    evidence=raw_entry.get("evidence", "strong"),
                )
            )
        )
    last_result = (
        _validated_last_result(document.get("last_result"))
        if "last_result" in document
        else None
    )
    ledger = AdaptiveLedger(entries=tuple(entries), last_result=last_result)
    _validate_ledger_invariants(ledger)
    return ledger


def serialize_adaptive_ledger(ledger: AdaptiveLedger) -> dict[str, Any]:
    """Return a deterministic JSON-compatible representation of ``ledger``."""

    validated = _validated_ledger(ledger)
    document: dict[str, Any] = {
        "version": ADAPTIVE_CORRECTIONS_SCHEMA_VERSION,
        "entries": [],
    }
    for entry in validated.entries:
        serialized_entry: dict[str, Any] = {
            "wrong": entry.wrong,
            "canonical": entry.canonical,
            "state": entry.state,
            "support": entry.support,
        }
        if entry.category != "recognition" or entry.evidence != "strong":
            serialized_entry["category"] = entry.category
            serialized_entry["evidence"] = entry.evidence
        document["entries"].append(serialized_entry)
    if validated.last_result is not None:
        document["last_result"] = _serialize_last_result(validated.last_result)
    return document


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

    return record_evidence(
        ledger,
        wrong,
        canonical,
        state="active",
        category="recognition",
        evidence="strong",
    )


def record_evidence(
    ledger: AdaptiveLedger,
    wrong: str,
    canonical: str,
    *,
    state: Literal["candidate", "active"],
    category: AdaptiveCategory,
    evidence: AdaptiveEvidence,
) -> AdaptiveLedger:
    """Record classified evidence without making medium evidence active."""

    validated = _validated_ledger(ledger)
    new_entry = _validated_entry(
        AdaptiveEntry(
            wrong=wrong,
            canonical=canonical,
            state=state,
            support=1,
            category=category,
            evidence=evidence,
        )
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
        if validated.entries[index].state in {"candidate", "active", "conflicted"}
    ]

    entries = list(validated.entries)
    if same_pair_index is not None:
        existing = entries[same_pair_index]
        entries[same_pair_index] = replace(
            existing,
            support=min(existing.support + 1, MAX_ADAPTIVE_SUPPORT),
        )
        if (
            state == "active"
            and entries[same_pair_index].state == "candidate"
            and len(
                {
                    normalized_key(entries[index].canonical)
                    for index in live_same_wrong_indexes
                }
            )
            == 1
        ):
            entries[same_pair_index] = replace(
                entries[same_pair_index],
                state="active",
                category=category,
                evidence=evidence,
            )
        if entries[same_pair_index].state in {
            "candidate",
            "active",
            "conflicted",
        } and (
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
        return AdaptiveLedger(entries=tuple(entries), last_result=validated.last_result)

    if live_same_wrong_indexes:
        for index in live_same_wrong_indexes:
            entries[index] = replace(entries[index], state="conflicted")
        if len(entries) >= MAX_ADAPTIVE_ENTRIES:
            # Preserve the safety fact even when there is no room to retain
            # the new alternative: the previously active mapping is no longer
            # safe to send to the provider.
            return AdaptiveLedger(
                entries=tuple(entries), last_result=validated.last_result
            )
        new_entry = replace(new_entry, state="conflicted")
    elif len(entries) >= MAX_ADAPTIVE_ENTRIES:
        raise AdaptiveStoreError("adaptive ledger contains too many entries")
    entries.append(new_entry)
    return AdaptiveLedger(entries=tuple(entries), last_result=validated.last_result)


def activate_correction(
    ledger: AdaptiveLedger,
    wrong: str,
    canonical: str,
) -> AdaptiveLedger:
    """Explicitly activate one retained choice and archive its alternatives."""

    validated = _validated_ledger(ledger)
    source = normalized_key(_validated_text(wrong))
    target = normalized_key(_validated_text(canonical))
    chosen = next(
        (
            index
            for index, entry in enumerate(validated.entries)
            if normalized_key(entry.wrong) == source
            and normalized_key(entry.canonical) == target
        ),
        None,
    )
    if chosen is None:
        raise AdaptiveStoreError("adaptive correction candidate was not found")
    entries = list(validated.entries)
    for index, entry in enumerate(entries):
        if normalized_key(entry.wrong) != source:
            continue
        entries[index] = replace(
            entry,
            state="active" if index == chosen else "archived",
            evidence="explicit" if index == chosen else entry.evidence,
        )
    return AdaptiveLedger(entries=tuple(entries), last_result=validated.last_result)


def with_last_result(
    ledger: AdaptiveLedger,
    result: AdaptiveLastResult,
) -> AdaptiveLedger:
    """Attach one validated transcript-free diagnostic outcome."""

    validated = _validated_ledger(ledger)
    return AdaptiveLedger(
        entries=validated.entries,
        last_result=_validated_last_result(result),
    )


def adaptive_statistics(ledger: AdaptiveLedger) -> dict[str, int]:
    """Return bounded lifecycle counts without exposing correction text."""

    validated = _validated_ledger(ledger)
    counts = {state: 0 for state in sorted(ADAPTIVE_STATES)}
    for entry in validated.entries:
        counts[entry.state] += 1
    return {"total": len(validated.entries), **counts}


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

    return compile_provider_correction_report(
        manual_pairs,
        ledger,
        limit=limit,
    ).pairs


def compile_terminal_corrections(
    manual_pairs: Iterable[CorrectionPair],
    ledger: AdaptiveLedger,
    *,
    limit: int = MAX_CORRECTION_PAIRS,
) -> tuple[CorrectionPair, ...]:
    """Compile deterministic delivery rules from explicit authority only.

    Manual pairs are explicit by definition.  Learned pairs must be both
    active and backed by explicit review; automatically activated strong
    evidence remains provider guidance until a person confirms it.  Reusing
    the provider compiler preserves its conflict, overlap, cascade and capacity
    safeguards for the selected explicit subset.
    """

    validated = _validated_ledger(ledger)
    explicit = AdaptiveLedger(
        entries=tuple(
            entry
            for entry in validated.entries
            if entry.state == "active" and entry.evidence == "explicit"
        )
    )
    return compile_provider_corrections(manual_pairs, explicit, limit=limit)


def compile_provider_correction_report(
    manual_pairs: Iterable[CorrectionPair],
    ledger: AdaptiveLedger,
    *,
    limit: int = MAX_CORRECTION_PAIRS,
) -> ProviderCorrectionReport:
    """Compile the exact provider view and explain every active suppression.

    The report is deliberately private-by-construction: pair text is excluded
    from ``repr`` and its public statistics contain only counts and fixed
    reason codes.  ``compile_provider_corrections`` remains the provider-facing
    compatibility API and delegates to this single implementation.
    """

    if type(limit) is not int or limit < 0 or limit > MAX_CORRECTION_PAIRS:
        raise AdaptiveStoreError("provider correction limit is invalid")
    validated = _validated_ledger(ledger)

    manual = tuple(_coerce_manual_pair(pair) for pair in manual_pairs)
    selected: list[CorrectionPair] = []
    decisions: list[ProviderCorrectionDecision] = []
    seen_manual_exact: set[tuple[str, str]] = set()
    for pair in manual:
        exact = (pair.wrong, pair.canonical)
        if exact in seen_manual_exact:
            continue
        seen_manual_exact.add(exact)
        if len(selected) < limit:
            selected.append(pair)
            decisions.append(
                ProviderCorrectionDecision(
                    pair.wrong,
                    pair.canonical,
                    "manual",
                    "effective-manual",
                )
            )
        else:
            decisions.append(
                ProviderCorrectionDecision(
                    pair.wrong,
                    pair.canonical,
                    "manual",
                    "suppressed-capacity",
                )
            )

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
    ambiguous_sources = {
        source
        for source, canonicals in canonicals_by_source.items()
        if len(canonicals) > 1
    }

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

    status_by_identity: dict[tuple[str, str], ProviderCorrectionStatus] = {}
    for entry in active:
        source = normalized_key(entry.wrong)
        identity = (source, normalized_key(entry.canonical))
        if source in manual_source_keys:
            status_by_identity[identity] = "suppressed-manual-source"
        elif source in ambiguous_sources:
            status_by_identity[identity] = "suppressed-conflicting-active"
        elif source in cyclic_sources:
            status_by_identity[identity] = "suppressed-cycle"
        elif source in cascade_sources:
            status_by_identity[identity] = "suppressed-cascade"

    candidates = [
        entry
        for entry in unambiguous
        if (
            normalized_key(entry.wrong),
            normalized_key(entry.canonical),
        )
        not in status_by_identity
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
        identity = (source, normalized_key(entry.canonical))
        if source in emitted_adaptive_sources:
            continue
        if len(selected) >= limit:
            status_by_identity[identity] = "suppressed-capacity"
            continue
        if any(_sources_overlap(source, reserved) for reserved in reserved_sources):
            status_by_identity[identity] = "suppressed-overlap"
            continue
        selected.append(CorrectionPair(entry.wrong, entry.canonical))
        status_by_identity[identity] = "effective-adaptive"
        emitted_adaptive_sources.add(source)
        reserved_sources.append(source)

    for entry in active:
        identity = (
            normalized_key(entry.wrong),
            normalized_key(entry.canonical),
        )
        status = status_by_identity.get(identity)
        if status is None:
            # A same-source duplicate cannot survive ledger validation unless
            # its canonical differs, which is classified above as conflict.
            # Keep this fail-closed fallback content-free and capacity-safe.
            status = "suppressed-overlap"
        decisions.append(
            ProviderCorrectionDecision(
                entry.wrong,
                entry.canonical,
                "adaptive",
                status,
            )
        )
    return ProviderCorrectionReport(tuple(selected), tuple(decisions))


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
        entries=tuple(_validated_entry(entry) for entry in ledger.entries),
        last_result=(
            _validated_last_result(ledger.last_result)
            if ledger.last_result is not None
            else None
        ),
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
    if not isinstance(entry.category, str) or entry.category not in ADAPTIVE_CATEGORIES:
        raise AdaptiveStoreError("adaptive ledger entry category is invalid")
    if not isinstance(entry.evidence, str) or entry.evidence not in ADAPTIVE_EVIDENCE:
        raise AdaptiveStoreError("adaptive ledger entry evidence is invalid")
    return AdaptiveEntry(
        wrong=wrong,
        canonical=canonical,
        state=entry.state,
        support=entry.support,
        category=entry.category,
        evidence=entry.evidence,
    )


def _validated_last_result(value: Any) -> AdaptiveLastResult:
    if isinstance(value, dict):
        if set(value) != {
            "reason_code",
            "captured_count",
            "activated_count",
            "candidate_count",
            "conflicted_count",
            "replacement_hunks",
        }:
            raise AdaptiveStoreError("adaptive last result has invalid fields")
        value = AdaptiveLastResult(
            reason_code=value.get("reason_code"),
            captured_count=value.get("captured_count"),
            activated_count=value.get("activated_count"),
            candidate_count=value.get("candidate_count"),
            conflicted_count=value.get("conflicted_count"),
            replacement_hunks=value.get("replacement_hunks"),
        )
    if not isinstance(value, AdaptiveLastResult):
        raise AdaptiveStoreError("adaptive last result is invalid")
    if (
        not isinstance(value.reason_code, str)
        or not 1 <= len(value.reason_code) <= 64
        or any(
            not (character.islower() or character.isdigit() or character == "-")
            for character in value.reason_code
        )
    ):
        raise AdaptiveStoreError("adaptive last result reason is invalid")
    counts = (
        value.captured_count,
        value.activated_count,
        value.candidate_count,
        value.conflicted_count,
        value.replacement_hunks,
    )
    if any(
        type(count) is not int or count < 0 or count > MAX_ADAPTIVE_RESULT_COUNT
        for count in counts
    ):
        raise AdaptiveStoreError("adaptive last result count is invalid")
    return value


def _serialize_last_result(value: AdaptiveLastResult) -> dict[str, Any]:
    validated = _validated_last_result(value)
    return {
        "reason_code": validated.reason_code,
        "captured_count": validated.captured_count,
        "activated_count": validated.activated_count,
        "candidate_count": validated.candidate_count,
        "conflicted_count": validated.conflicted_count,
        "replacement_hunks": validated.replacement_hunks,
    }


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
