from __future__ import annotations

import pytest

from murmur_voice.adaptive_store import (
    ADAPTIVE_CORRECTIONS_SCHEMA_VERSION,
    MAX_ADAPTIVE_ENTRIES,
    AdaptiveEntry,
    AdaptiveLedger,
    AdaptiveLastResult,
    AdaptiveStoreError,
    activate_correction,
    adaptive_statistics,
    compile_provider_corrections,
    normalized_key,
    parse_adaptive_ledger,
    record_correction,
    record_evidence,
    serialize_adaptive_ledger,
    with_last_result,
)
from murmur_voice.config import MAX_CORRECTION_PAIRS, CorrectionPair


def test_parse_serialize_round_trip_preserves_all_states_and_support():
    document = {
        "version": ADAPTIVE_CORRECTIONS_SCHEMA_VERSION,
        "entries": [
            {
                "wrong": "Ostro",
                "canonical": "Austral",
                "state": "active",
                "support": 3,
            },
            {
                "wrong": "old",
                "canonical": "older",
                "state": "archived",
                "support": 1,
            },
            {
                "wrong": "pause",
                "canonical": "paused",
                "state": "suspended",
                "support": 2,
            },
        ],
    }
    ledger = parse_adaptive_ledger(document)

    assert serialize_adaptive_ledger(ledger) == document
    assert "Ostro" not in repr(ledger)
    assert "Austral" not in repr(ledger.entries[0])


def test_repeated_normalized_pair_increments_support_without_duplicate():
    ledger = record_correction(AdaptiveLedger(), "  Ostro ", "Austral")
    ledger = record_correction(ledger, "ＯＳＴＲＯ", "AUSTRAL")

    assert len(ledger.entries) == 1
    assert ledger.entries[0] == AdaptiveEntry("Ostro", "Austral", support=2)


def test_same_normalized_wrong_with_new_canonical_marks_both_conflicted():
    ledger = record_correction(AdaptiveLedger(), "Ostro", "Austral")
    ledger = record_correction(ledger, "ＯＳＴＲＯ", "Australia")

    assert [entry.state for entry in ledger.entries] == [
        "conflicted",
        "conflicted",
    ]
    assert compile_provider_corrections((), ledger) == ()


def test_observing_suspended_pair_increments_support_without_reactivation():
    ledger = AdaptiveLedger(
        entries=(AdaptiveEntry("wrong", "right", state="suspended", support=4),)
    )
    updated = record_correction(ledger, "WRONG", "RIGHT")

    assert updated.entries[0].state == "suspended"
    assert updated.entries[0].support == 5


def test_manual_pairs_are_first_and_block_same_or_overlapping_learned_sources():
    manual = (CorrectionPair("mark", "Mark"),)
    ledger = AdaptiveLedger(
        entries=(
            AdaptiveEntry("MARK", "benchmark", support=10),
            AdaptiveEntry("奔驰 mark", "benchmark", support=9),
            AdaptiveEntry("Ostro", "Austral", support=2),
        )
    )

    assert compile_provider_corrections(manual, ledger) == (
        manual[0],
        CorrectionPair("Ostro", "Austral"),
    )


def test_more_specific_learned_source_wins_overlap_even_with_lower_support():
    ledger = AdaptiveLedger(
        entries=(
            AdaptiveEntry("奔驰", "bench", support=100),
            AdaptiveEntry("奔驰 mark", "benchmark", support=1),
        )
    )

    assert compile_provider_corrections((), ledger) == (
        CorrectionPair("奔驰 mark", "benchmark"),
    )


def test_direct_and_indirect_cycles_are_suppressed_but_case_correction_survives():
    ledger = AdaptiveLedger(
        entries=(
            AdaptiveEntry("alpha", "beta"),
            AdaptiveEntry("beta", "gamma"),
            AdaptiveEntry("gamma", "alpha"),
            AdaptiveEntry("same", "SAME"),
            AdaptiveEntry("safe", "target"),
        )
    )

    assert compile_provider_corrections((), ledger) == (
        CorrectionPair("safe", "target"),
        CorrectionPair("same", "SAME"),
    )


def test_noncyclic_chains_and_self_containing_canonicals_are_suppressed():
    ledger = AdaptiveLedger(
        entries=(
            AdaptiveEntry("alpha", "beta"),
            AdaptiveEntry("beta", "gamma"),
            AdaptiveEntry("project", "project server"),
            AdaptiveEntry("unrelated", "stable"),
        )
    )

    assert compile_provider_corrections((), ledger) == (
        CorrectionPair("unrelated", "stable"),
    )


def test_case_only_presentation_correction_is_not_mistaken_for_a_cycle():
    ledger = AdaptiveLedger(entries=(AdaptiveEntry("openai", "OpenAI"),))

    assert compile_provider_corrections((), ledger) == (
        CorrectionPair("openai", "OpenAI"),
    )


def test_exact_identity_mapping_is_suppressed():
    ledger = AdaptiveLedger(entries=(AdaptiveEntry("same", "same"),))

    assert compile_provider_corrections((), ledger) == ()


def test_learned_cycle_with_manual_rule_is_suppressed_but_manual_is_preserved():
    manual = (CorrectionPair("canonical", "wrong"),)
    ledger = AdaptiveLedger(entries=(AdaptiveEntry("wrong", "canonical"),))

    assert compile_provider_corrections(manual, ledger) == manual


@pytest.mark.parametrize(
    ("manual", "learned"),
    (
        (CorrectionPair("alpha", "beta"), AdaptiveEntry("beta", "gamma")),
        (
            CorrectionPair("奔驰", "bench mark"),
            AdaptiveEntry("bench mark", "benchmark"),
        ),
    ),
)
def test_learned_rule_cannot_rewrite_manual_canonical(manual, learned):
    ledger = AdaptiveLedger(entries=(learned,))

    assert compile_provider_corrections((manual,), ledger) == (manual,)


def test_conflicted_suspended_and_archived_entries_never_reach_provider():
    ledger = AdaptiveLedger(
        entries=(
            AdaptiveEntry("a", "b", state="conflicted"),
            AdaptiveEntry("c", "d", state="suspended"),
            AdaptiveEntry("e", "f", state="archived"),
            AdaptiveEntry("g", "h", state="active"),
        )
    )

    assert compile_provider_corrections((), ledger) == (CorrectionPair("g", "h"),)


def test_provider_limit_does_not_truncate_ledger():
    ledger = AdaptiveLedger(
        entries=tuple(
            AdaptiveEntry(f"wrong-{index}", f"right-{index}")
            for index in range(MAX_CORRECTION_PAIRS + 1)
        )
    )

    compiled = compile_provider_corrections((), ledger)

    assert len(compiled) == MAX_CORRECTION_PAIRS
    assert len(ledger.entries) == MAX_CORRECTION_PAIRS + 1


def test_manual_pairs_consume_provider_capacity_before_learned_pairs():
    manual = tuple(
        CorrectionPair(f"manual-{index}", f"target-{index}") for index in range(49)
    )
    ledger = AdaptiveLedger(
        entries=(
            AdaptiveEntry("long learned source", "first"),
            AdaptiveEntry("short", "second"),
        )
    )

    compiled = compile_provider_corrections(manual, ledger)

    assert compiled[:49] == manual
    assert compiled[49] == CorrectionPair("long learned source", "first")


def test_punctuation_manual_source_does_not_suppress_unrelated_learned_rule():
    manual = (CorrectionPair("!", "?"),)
    ledger = AdaptiveLedger(entries=(AdaptiveEntry("Ostro", "Austral"),))

    assert compile_provider_corrections(manual, ledger) == (
        CorrectionPair("!", "?"),
        CorrectionPair("Ostro", "Austral"),
    )


def test_normalized_key_uses_nfkc_casefold_and_collapsed_whitespace():
    assert normalized_key("  ＯＳＴＲＯ\tServer  ") == "ostro server"


@pytest.mark.parametrize(
    "document",
    (
        {},
        {"version": 1, "entries": [], "extra": True},
        {"version": True, "entries": []},
        {"version": 3, "entries": []},
        {"version": 1, "entries": {}},
        {
            "version": 1,
            "entries": [
                {
                    "wrong": "private-wrong-sentinel",
                    "canonical": "private-canonical-sentinel",
                    "state": "unknown",
                    "support": 1,
                }
            ],
        },
        {
            "version": 1,
            "entries": [
                {
                    "wrong": "private-wrong-sentinel",
                    "canonical": "private-canonical-sentinel",
                    "state": "active",
                    "support": True,
                }
            ],
        },
    ),
)
def test_parser_rejects_invalid_schema_without_echoing_content(document):
    with pytest.raises(AdaptiveStoreError) as captured:
        parse_adaptive_ledger(document)

    assert "private-wrong-sentinel" not in str(captured.value)
    assert "private-canonical-sentinel" not in str(captured.value)


def test_parser_rejects_duplicate_normalized_pair():
    document = {
        "version": 1,
        "entries": [
            {"wrong": "Ostro", "canonical": "Austral", "state": "active", "support": 1},
            {
                "wrong": "ＯＳＴＲＯ",
                "canonical": "AUSTRAL",
                "state": "active",
                "support": 2,
            },
        ],
    }

    with pytest.raises(AdaptiveStoreError, match="duplicate"):
        parse_adaptive_ledger(document)


def test_archived_conflict_can_coexist_with_one_resolved_active_choice():
    document = {
        "version": 1,
        "entries": [
            {"wrong": "same", "canonical": "first", "state": "active", "support": 1},
            {
                "wrong": "SAME",
                "canonical": "second",
                "state": "archived",
                "support": 1,
            },
        ],
    }

    ledger = parse_adaptive_ledger(document)

    assert compile_provider_corrections((), ledger) == (
        CorrectionPair("same", "first"),
    )


def test_reobserving_resolved_pair_preserves_archived_alternative():
    ledger = AdaptiveLedger(
        entries=(
            AdaptiveEntry("Ostro", "Austral", state="active", support=1),
            AdaptiveEntry("OSTRO", "Australia", state="archived", support=3),
        )
    )

    updated = record_correction(ledger, "ostro", "austral")

    assert updated.entries == (
        AdaptiveEntry("Ostro", "Austral", state="active", support=2),
        AdaptiveEntry("OSTRO", "Australia", state="archived", support=3),
    )
    assert compile_provider_corrections((), updated) == (
        CorrectionPair("Ostro", "Austral"),
    )


def test_new_evidence_does_not_reactivate_only_archived_alternative():
    ledger = AdaptiveLedger(
        entries=(AdaptiveEntry("Ostro", "Australia", state="archived"),)
    )

    updated = record_correction(ledger, "Ostro", "Austral")

    assert updated.entries[0].state == "archived"
    assert updated.entries[1].state == "active"


def test_compiler_suppresses_divergent_active_canonicals_even_if_file_was_edited():
    ledger = AdaptiveLedger(
        entries=(
            AdaptiveEntry("same", "first", state="active"),
            AdaptiveEntry("SAME", "second", state="active"),
        )
    )

    assert compile_provider_corrections((), ledger) == ()


def test_v1_ledger_migrates_in_memory_without_activating_new_rules():
    legacy = {
        "version": 1,
        "entries": [
            {
                "wrong": "legacy wrong",
                "canonical": "legacy right",
                "state": "active",
                "support": 2,
            }
        ],
    }

    ledger = parse_adaptive_ledger(legacy)

    assert ledger.version == ADAPTIVE_CORRECTIONS_SCHEMA_VERSION
    assert ledger.entries == (AdaptiveEntry("legacy wrong", "legacy right", support=2),)
    assert serialize_adaptive_ledger(ledger)["version"] == 2


def test_medium_evidence_stays_candidate_until_explicit_confirmation():
    ledger = record_evidence(
        AdaptiveLedger(),
        "Ostro",
        "Austral",
        state="candidate",
        category="recognition",
        evidence="medium",
    )

    assert ledger.entries[0].state == "candidate"
    assert compile_provider_corrections((), ledger) == ()

    confirmed = activate_correction(ledger, "ostro", "AUSTRAL")
    assert confirmed.entries[0].state == "active"
    assert confirmed.entries[0].evidence == "explicit"
    assert compile_provider_corrections((), confirmed) == (
        CorrectionPair("Ostro", "Austral"),
    )


def test_explicit_choice_archives_conflicting_alternatives():
    ledger = record_evidence(
        AdaptiveLedger(),
        "same",
        "first",
        state="candidate",
        category="recognition",
        evidence="medium",
    )
    ledger = record_evidence(
        ledger,
        "same",
        "second",
        state="candidate",
        category="recognition",
        evidence="medium",
    )
    assert [entry.state for entry in ledger.entries] == ["conflicted", "conflicted"]

    resolved = activate_correction(ledger, "same", "second")
    assert [entry.state for entry in resolved.entries] == ["archived", "active"]


def test_last_result_round_trip_and_statistics_are_content_free():
    ledger = record_evidence(
        AdaptiveLedger(),
        "private wrong",
        "private right",
        state="candidate",
        category="terminology",
        evidence="medium",
    )
    ledger = with_last_result(
        ledger,
        AdaptiveLastResult(
            "candidates-saved",
            captured_count=1,
            candidate_count=1,
            replacement_hunks=2,
        ),
    )

    restored = parse_adaptive_ledger(serialize_adaptive_ledger(ledger))

    assert restored == ledger
    assert adaptive_statistics(restored) == {
        "total": 1,
        "active": 0,
        "archived": 0,
        "candidate": 1,
        "conflicted": 0,
        "suspended": 0,
    }


def test_new_entry_is_refused_only_after_ledger_capacity_is_reached():
    ledger = AdaptiveLedger(
        entries=tuple(
            AdaptiveEntry(f"source-{index}", f"target-{index}")
            for index in range(MAX_ADAPTIVE_ENTRIES)
        )
    )

    with pytest.raises(AdaptiveStoreError, match="too many"):
        record_correction(ledger, "new source", "new target")

    repeated = record_correction(ledger, "SOURCE-0", "TARGET-0")
    assert len(repeated.entries) == MAX_ADAPTIVE_ENTRIES
    assert repeated.entries[0].support == 2


def test_full_ledger_still_suspends_an_old_mapping_when_conflict_arrives():
    ledger = AdaptiveLedger(
        entries=(AdaptiveEntry("Ostro", "Austral"),)
        + tuple(
            AdaptiveEntry(
                f"archived-{index}",
                f"target-{index}",
                state="archived",
            )
            for index in range(MAX_ADAPTIVE_ENTRIES - 1)
        )
    )

    updated = record_correction(ledger, "OSTRO", "Australia")

    assert len(updated.entries) == MAX_ADAPTIVE_ENTRIES
    assert updated.entries[0].state == "conflicted"
    assert compile_provider_corrections((), updated) == ()


def test_limit_validation_is_bounded_by_provider_contract():
    with pytest.raises(AdaptiveStoreError):
        compile_provider_corrections((), AdaptiveLedger(), limit=-1)
    with pytest.raises(AdaptiveStoreError):
        compile_provider_corrections(
            (),
            AdaptiveLedger(),
            limit=MAX_CORRECTION_PAIRS + 1,
        )


def test_direct_dataclass_values_receive_the_same_strict_validation():
    with pytest.raises(AdaptiveStoreError, match="schema"):
        serialize_adaptive_ledger(AdaptiveLedger(version=True))
    with pytest.raises(AdaptiveStoreError, match="state"):
        serialize_adaptive_ledger(
            AdaptiveLedger(entries=(AdaptiveEntry("x", "y", state=[]),))
        )
