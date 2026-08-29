from __future__ import annotations

import json

import pytest

from murmur_voice import adaptive_runtime
from murmur_voice.adaptive_runtime import (
    AdaptiveCorrectionRuntime,
    load_adaptive_ledger,
    save_adaptive_ledger,
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
    assert not adaptive.exists()


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
