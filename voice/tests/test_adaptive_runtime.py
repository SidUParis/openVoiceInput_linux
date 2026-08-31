from __future__ import annotations

import json

import pytest

from murmur_voice import adaptive_runtime
from murmur_voice.adaptive_runtime import (
    AdaptiveCorrectionRuntime,
    adaptive_review_entries,
    adaptive_status_document,
    load_adaptive_ledger,
    save_adaptive_ledger,
    submit_explicit_feedback,
)
from murmur_voice.adaptive_store import AdaptiveEntry, AdaptiveLedger
from murmur_voice.config import (
    ConfigError,
    CorrectionPair,
    save_api_key,
    save_corrections,
    save_vocabulary,
)
from murmur_voice.preedit import ObservationSnapshot


def _runtime(tmp_path):
    config = tmp_path / "private" / "voice.json"
    vocabulary = tmp_path / "private" / "vocabulary.json"
    corrections = tmp_path / "private" / "corrections.json"
    adaptive = tmp_path / "private" / "adaptive-corrections.json"
    save_api_key("test-key", config)
    save_vocabulary((), vocabulary)
    save_corrections((), corrections)
    return (
        AdaptiveCorrectionRuntime(
            config_path=config,
            vocabulary_path=vocabulary,
            corrections_path=corrections,
            adaptive_path=adaptive,
        ),
        config,
        vocabulary,
        corrections,
        adaptive,
    )


def test_observation_persists_only_bounded_pair_and_increments_support(
    tmp_path, monkeypatch
):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, corrections
    monkeypatch.setattr(
        adaptive_runtime,
        "_lookup_system_dictionary",
        lambda key: {"benchmark": "benchmark", "austral": "Austral"}.get(key),
    )
    snapshot = ObservationSnapshot(
        baseline_text="unrelated prefix 奔驰 mark unrelated suffix",
        committed_start=len("unrelated prefix "),
        committed_end=len("unrelated prefix 奔驰 mark"),
        current_text="unrelated prefix bench mark unrelated suffix",
        cursor=len("unrelated prefix bench mark"),
        anchor=len("unrelated prefix bench mark"),
    )

    assert runtime.observe(snapshot)
    assert runtime.observe(snapshot)

    ledger = load_adaptive_ledger(adaptive)
    assert len(ledger.entries) == 1
    assert ledger.entries[0].wrong == "奔驰 mark"
    assert ledger.entries[0].canonical == "benchmark"
    assert ledger.entries[0].support == 2
    raw = adaptive.read_text(encoding="utf-8")
    assert "unrelated prefix" not in raw
    assert "unrelated suffix" not in raw
    assert adaptive.stat().st_mode & 0o777 == 0o600
    assert adaptive.parent.stat().st_mode & 0o777 == 0o700


def test_system_dictionary_lookup_is_cached_and_deduplicates_symlinks(
    tmp_path, monkeypatch
):
    dictionary = tmp_path / "en_US.dic"
    dictionary.write_text("3\nbenchmark\nbench-mark\nAustral\n", encoding="utf-8")
    alias = tmp_path / "en_GB.dic"
    alias.symlink_to(dictionary)
    monkeypatch.setattr(
        adaptive_runtime,
        "_SYSTEM_DICTIONARY_GLOBS",
        (str(tmp_path / "en_*.dic"),),
    )
    adaptive_runtime._lookup_system_dictionary.cache_clear()

    first = adaptive_runtime._lookup_system_dictionary("benchmark")
    dictionary.unlink()
    second = adaptive_runtime._lookup_system_dictionary("benchmark")

    assert first is None
    assert second is None


def test_personal_vocabulary_precedes_cached_system_dictionary(monkeypatch):
    monkeypatch.setattr(
        adaptive_runtime,
        "_lookup_system_dictionary",
        lambda key: "bench-mark" if key == "benchmark" else None,
    )

    assert (
        adaptive_runtime._canonicalize_approved_term(
            "bench mark",
            ("benchmark",),
        )
        == "benchmark"
    )


@pytest.mark.parametrize("term", ("R&D", "C++", ".NET", "a/b", "@name"))
def test_system_dictionary_never_removes_semantic_punctuation(term, monkeypatch):
    monkeypatch.setattr(
        adaptive_runtime,
        "_lookup_system_dictionary",
        lambda key: "unsafe-collapse",
    )

    assert adaptive_runtime._canonicalize_approved_term(term, ()) == term


def test_observation_with_a_selected_range_is_not_learned(tmp_path):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, corrections
    snapshot = ObservationSnapshot(
        baseline_text="Ostro",
        committed_start=0,
        committed_end=5,
        current_text="Austral",
        cursor=7,
        anchor=0,
    )

    assert runtime.observe(snapshot) is False
    assert load_adaptive_ledger(adaptive).last_result.reason_code == "selection-active"


def test_conflicting_evidence_is_recorded_but_not_reported_as_active(
    tmp_path, monkeypatch
):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, corrections
    monkeypatch.setattr(adaptive_runtime, "_lookup_system_dictionary", lambda key: None)
    first = ObservationSnapshot("Ostro", 0, 5, "Austral", 7, 7)
    second = ObservationSnapshot("Ostro", 0, 5, "Australia", 9, 9)

    assert runtime.observe(first) is True
    assert runtime.observe(second) is False

    ledger = load_adaptive_ledger(adaptive)
    assert [entry.state for entry in ledger.entries] == ["conflicted", "conflicted"]


def test_manual_same_source_prevents_adaptive_evidence_from_reporting_active(
    tmp_path, monkeypatch
):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, adaptive
    save_corrections((CorrectionPair("Ostro", "Austral"),), corrections)
    monkeypatch.setattr(adaptive_runtime, "_lookup_system_dictionary", lambda key: None)
    snapshot = ObservationSnapshot("Ostro", 0, 5, "Austral", 7, 7)

    assert runtime.observe(snapshot) is False


def test_each_client_factory_call_hot_reloads_manual_vocabulary_and_adaptive(
    tmp_path, monkeypatch
):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config
    settings = []

    class FakeClient:
        def __init__(self, provider_settings):
            settings.append(provider_settings)

    from murmur_voice import volcengine

    monkeypatch.setattr(volcengine, "VolcengineASRClient", FakeClient)
    runtime.create_asr_client()
    save_vocabulary(("Austral",), vocabulary)
    save_corrections((CorrectionPair("Ostro", "Austral"),), corrections)
    save_adaptive_ledger(
        AdaptiveLedger(entries=(AdaptiveEntry("bench mark", "benchmark"),)),
        adaptive,
    )
    runtime.create_asr_client()

    assert "hotwords" not in settings[0]
    assert "corrections" not in settings[0]
    assert settings[1]["hotwords"] == ("Austral",)
    assert settings[1]["corrections"] == (
        CorrectionPair("Ostro", "Austral"),
        CorrectionPair("bench mark", "benchmark"),
    )


def test_invalid_or_public_adaptive_file_fails_closed(tmp_path):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, corrections
    adaptive.write_text(json.dumps({"version": 1, "entries": []}), encoding="utf-8")
    adaptive.chmod(0o644)

    with pytest.raises(ConfigError, match="permissions must be 0600"):
        runtime.validate()


def test_symlink_adaptive_file_is_rejected(tmp_path):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, corrections
    target = tmp_path / "target.json"
    target.write_text('{"version":1,"entries":[]}', encoding="utf-8")
    target.chmod(0o600)
    adaptive.symlink_to(target)

    with pytest.raises(ConfigError, match="could not be opened safely"):
        runtime.validate()


def test_maximum_multibyte_ledger_round_trips_within_its_read_bound(tmp_path):
    character = "𐀀"
    entries = tuple(
        AdaptiveEntry(
            (f"{index:03d}" + character * 64)[:64],
            (f"{index + 500:03d}" + character * 64)[:64],
        )
        for index in range(500)
    )
    path = tmp_path / "private" / "adaptive-corrections.json"

    save_adaptive_ledger(AdaptiveLedger(entries=entries), path)

    assert load_adaptive_ledger(path) == AdaptiveLedger(entries=entries)


def test_multi_hunk_observation_is_persisted_as_candidates_then_confirmed(tmp_path):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary
    snapshot = ObservationSnapshot(
        baseline_text="Ostro uses openai",
        committed_start=0,
        committed_end=len("Ostro uses openai"),
        current_text="Austral uses OpenAI",
        cursor=len("Austral uses OpenAI"),
        anchor=len("Austral uses OpenAI"),
    )

    result = runtime.observe_result(snapshot)

    assert result.reason_code == "candidates-saved"
    assert result.captured_count == 2
    assert result.activated_count == 0
    assert result.candidate_count == 2
    assert {entry.state for entry in load_adaptive_ledger(adaptive).entries} == {
        "candidate"
    }
    assert len(adaptive_review_entries(adaptive)) == 2
    assert adaptive_status_document(adaptive)["statistics"]["candidate"] == 2

    confirmed = runtime.confirm("Ostro", "Austral")
    assert confirmed.reason_code == "explicitly-activated"
    assert confirmed.activated_count == 1
    assert adaptive_status_document(adaptive)["statistics"] == {
        "total": 2,
        "active": 1,
        "archived": 0,
        "candidate": 1,
        "conflicted": 0,
        "suspended": 0,
    }


def test_strong_single_observation_activates_once_and_records_reason(tmp_path):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, corrections
    snapshot = ObservationSnapshot("Ostro", 0, 5, "Austral", 7, 7)

    result = runtime.observe_result(snapshot)

    assert result.reason_code == "active-learned"
    assert result.activated_count == 1
    ledger = load_adaptive_ledger(adaptive)
    assert ledger.entries[0].state == "active"
    assert ledger.last_result.reason_code == "active-learned"


def test_rejected_observation_persists_reason_but_never_surrounding_text(tmp_path):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, corrections
    snapshot = ObservationSnapshot(
        "private prefix Ostro private suffix",
        len("private prefix "),
        len("private prefix Ostro"),
        "changed prefix Ostro private suffix",
        7,
        7,
    )

    result = runtime.observe_result(snapshot)

    assert result.reason_code == "edit-outside-committed-span"
    raw = adaptive.read_text(encoding="utf-8")
    assert "private prefix" not in raw
    assert "private suffix" not in raw
    assert load_adaptive_ledger(adaptive).entries == ()


def test_external_timeout_is_visible_without_correction_text(tmp_path):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, corrections

    result = runtime.record_external_result("observation-timeout")

    assert result.reason_code == "observation-timeout"
    assert adaptive_status_document(adaptive)["last_result"]["reason_code"] == (
        "observation-timeout"
    )


def test_malformed_snapshot_becomes_visible_reason_instead_of_exception(tmp_path):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, corrections

    result = runtime.observe_result(object())

    assert result.reason_code == "invalid-snapshot"
    assert load_adaptive_ledger(adaptive).last_result.reason_code == "invalid-snapshot"


def test_first_v2_outcome_atomically_migrates_v1_ledger_without_state_change(
    tmp_path,
):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, corrections
    adaptive.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "wrong": "legacy wrong",
                        "canonical": "legacy right",
                        "state": "active",
                        "support": 3,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    adaptive.chmod(0o600)

    runtime.record_external_result("surrounding-text-unavailable")

    document = json.loads(adaptive.read_text(encoding="utf-8"))
    assert document["version"] == 2
    assert document["entries"][0]["state"] == "active"
    assert document["entries"][0]["support"] == 3
    assert document["last_result"]["reason_code"] == ("surrounding-text-unavailable")


def test_explicit_cross_application_feedback_activates_without_storing_sentences(
    tmp_path,
):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del runtime, config
    provider = "private prefix Ostro uses openai private suffix"
    preferred = "private prefix Austral uses OpenAI private suffix"

    result = submit_explicit_feedback(
        adaptive,
        corrections,
        vocabulary,
        provider,
        preferred,
    )

    assert result.reason_code == "explicit-feedback-activated"
    assert result.activated_count == 2
    raw = adaptive.read_text(encoding="utf-8")
    assert "private prefix" not in raw
    assert "private suffix" not in raw
    assert {entry.evidence for entry in load_adaptive_ledger(adaptive).entries} == {
        "explicit"
    }


def test_status_distinguishes_explicit_adaptive_and_effective_provider_counts(
    tmp_path,
):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del runtime, config
    save_vocabulary(("Austral", "OpenAI"), vocabulary)
    save_corrections((CorrectionPair("Ostro", "Austral"),), corrections)
    save_adaptive_ledger(
        AdaptiveLedger(
            entries=(
                AdaptiveEntry("alpha beta", "destination one"),
                AdaptiveEntry("beta", "BETA"),
            )
        ),
        adaptive,
    )

    status = adaptive_status_document(
        adaptive,
        corrections_path=corrections,
        vocabulary_path=vocabulary,
    )

    assert status["statistics"]["active"] == 2
    assert status["provider_view"] == {
        "explicit_vocabulary_count": 2,
        "manual_correction_count": 1,
        "effective_correction_count": 2,
        "manual_effective_count": 1,
        "adaptive_effective_count": 1,
        "adaptive_suppressed_count": 1,
        "suppression_reasons": {"suppressed-overlap": 1},
    }


def test_confirmed_candidate_is_reloaded_and_used_by_the_next_client(
    tmp_path, monkeypatch
):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config
    vocabulary.unlink()
    corrections.unlink()
    save_adaptive_ledger(
        AdaptiveLedger(
            entries=(
                AdaptiveEntry(
                    "Ostro",
                    "Austral",
                    state="candidate",
                    evidence="medium",
                ),
            )
        ),
        adaptive,
    )
    captured_settings = []

    class FakeClient:
        def __init__(self, provider_settings):
            captured_settings.append(provider_settings)

    from murmur_voice import volcengine

    monkeypatch.setattr(volcengine, "VolcengineASRClient", FakeClient)

    result = runtime.confirm("Ostro", "Austral")
    runtime.create_asr_client()

    assert result.reason_code == "explicitly-activated"
    assert result.activated_count == 1
    assert captured_settings[-1]["corrections"] == (CorrectionPair("Ostro", "Austral"),)
    persisted = load_adaptive_ledger(adaptive)
    assert persisted.entries[0].state == "active"
    assert persisted.last_result.reason_code == "explicitly-activated"
    assert not vocabulary.exists()
    assert not corrections.exists()
    assert runtime.status_document()["provider_view"] == {
        "explicit_vocabulary_count": 0,
        "manual_correction_count": 0,
        "effective_correction_count": 1,
        "manual_effective_count": 0,
        "adaptive_effective_count": 1,
        "adaptive_suppressed_count": 0,
        "suppression_reasons": {},
    }


def test_confirm_reports_specific_manual_suppression_and_keeps_manual_authority(
    tmp_path,
):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary
    save_corrections((CorrectionPair("Ostro", "Australia"),), corrections)
    save_adaptive_ledger(
        AdaptiveLedger(
            entries=(
                AdaptiveEntry(
                    "Ostro",
                    "Austral",
                    state="candidate",
                    evidence="medium",
                ),
            )
        ),
        adaptive,
    )

    result = runtime.confirm("Ostro", "Austral")
    status = runtime.status_document()

    assert result.reason_code == "explicitly-suppressed-manual-source"
    assert result.activated_count == 0
    assert status["provider_view"]["manual_effective_count"] == 1
    assert status["provider_view"]["adaptive_effective_count"] == 0
    assert status["provider_view"]["suppression_reasons"] == {
        "suppressed-manual-source": 1
    }


def test_confirm_never_reports_success_when_post_write_reload_cannot_verify(
    tmp_path, monkeypatch
):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, corrections
    save_adaptive_ledger(
        AdaptiveLedger(
            entries=(
                AdaptiveEntry(
                    "private wrong",
                    "private canonical",
                    state="candidate",
                    evidence="medium",
                ),
            )
        ),
        adaptive,
    )
    real_load = adaptive_runtime.load_adaptive_ledger
    loads = 0

    def mismatched_reload(path=None):
        nonlocal loads
        loads += 1
        if loads >= 2:
            return AdaptiveLedger()
        return real_load(path)

    monkeypatch.setattr(adaptive_runtime, "load_adaptive_ledger", mismatched_reload)

    with pytest.raises(ConfigError, match="verification failed") as captured:
        runtime.confirm("private wrong", "private canonical")

    assert loads >= 2
    assert "private wrong" not in str(captured.value)
    assert "private canonical" not in str(captured.value)


def test_runtime_exposes_same_daemon_owned_explicit_feedback_path(tmp_path):
    runtime, config, vocabulary, corrections, adaptive = _runtime(tmp_path)
    del config, vocabulary, corrections

    result = runtime.submit_explicit_feedback(
        "private prefix Ostro private suffix",
        "private prefix Austral private suffix",
    )

    assert result.reason_code == "explicit-feedback-activated"
    raw = adaptive.read_text(encoding="utf-8")
    assert "private prefix" not in raw
    assert "private suffix" not in raw
