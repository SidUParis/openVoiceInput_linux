from __future__ import annotations

import pytest

from murmur_voice.adaptive_correction import (
    CorrectionCandidate,
    canonicalize_with_approved_terms,
    collapsed_term_key,
    extract_correction,
    extract_corrections,
)


def _extract(
    baseline: str,
    current: str,
    *,
    committed: str | None = None,
    approved_terms=(),
):
    committed = baseline if committed is None else committed
    start = baseline.index(committed)
    return extract_correction(
        baseline,
        start,
        start + len(committed),
        current,
        approved_terms=approved_terms,
    )


def test_cross_script_edit_expands_through_unchanged_latin_context():
    assert _extract("奔驰 mark", "bench mark") == CorrectionCandidate(
        wrong="奔驰 mark",
        canonical="bench mark",
    )


def test_approved_lexicon_collapses_bench_mark_to_benchmark():
    assert _extract(
        "奔驰 mark",
        "bench mark",
        approved_terms=("benchmark", "benchmarks"),
    ) == CorrectionCandidate(wrong="奔驰 mark", canonical="benchmark")


def test_personal_vocabulary_spelling_precedes_equivalent_system_term():
    assert (
        canonicalize_with_approved_terms(
            "open ai",
            ("OpenAI", "openai"),
        )
        == "OpenAI"
    )
    assert collapsed_term_key(" Open-AI ") == "openai"


def test_ambiguous_approved_spellings_do_not_rewrite_observation():
    assert (
        canonicalize_with_approved_terms(
            "re search",
            ("research", "re-search"),
        )
        == "re search"
    )


def test_latin_word_replacement_is_learned_as_one_whole_token():
    assert _extract("Ostro", "Austral") == CorrectionCandidate(
        wrong="Ostro",
        canonical="Austral",
    )


def test_semantic_punctuation_keeps_unchanged_suffix_inside_rule():
    assert _extract("are D", "R&D") == CorrectionCandidate(
        wrong="are D",
        canonical="R&D",
    )


def test_semantic_suffix_punctuation_is_never_removed():
    assert _extract("Cee", "C++") == CorrectionCandidate(
        wrong="Cee",
        canonical="C++",
    )


def test_unicode_latin_words_and_accents_are_whole_tokens():
    assert _extract("resume", "résumé") == CorrectionCandidate(
        wrong="resume",
        canonical="résumé",
    )


def test_one_character_cjk_replacement_expands_to_safe_left_context():
    candidate = _extract("今天开会", "今日开会")
    assert candidate == CorrectionCandidate(
        wrong="今天",
        canonical="今日",
    )
    assert candidate.category == "recognition"


def test_broad_polishing_is_not_learned_as_a_global_rule():
    assert _extract("今天会议很好", "请尽快发送报告") is None


def test_dissimilar_whole_phrase_rewrite_is_rejected():
    assert _extract("good meeting", "send report") is None


def test_context_free_one_character_source_is_rejected():
    assert _extract("天", "日") is None


@pytest.mark.parametrize(
    ("baseline", "current"),
    (
        ("Ostro", "new Ostro"),
        ("Ostro", ""),
        ("Ostro server", "Austral serveur"),
        ("same text", "same text"),
    ),
)
def test_insert_delete_multi_hunk_and_no_change_are_rejected(baseline, current):
    assert _extract(baseline, current) is None


def test_edit_outside_committed_span_is_rejected():
    baseline = "prefix Ostro suffix"
    assert (
        _extract(
            baseline,
            "changed Ostro suffix",
            committed="Ostro",
        )
        is None
    )


def test_cross_script_rule_without_unchanged_latin_context_is_rejected():
    assert _extract("奔驰", "bench") is None


def test_cross_script_left_context_is_used_when_no_right_context_exists():
    assert _extract("the 奔驰", "the bench") == CorrectionCandidate(
        wrong="the 奔驰",
        canonical="the bench",
    )


def test_cross_script_expansion_cannot_escape_committed_span():
    assert (
        _extract(
            "奔驰 mark",
            "bench mark",
            committed="奔驰",
        )
        is None
    )


@pytest.mark.parametrize(
    ("start", "end"),
    ((-1, 1), (0, 0), (0, 99), (True, 1)),
)
def test_invalid_committed_span_is_rejected(start, end):
    assert extract_correction("Ostro", start, end, "Austral") is None


def test_punctuation_only_replacement_is_not_learned():
    assert _extract("hello!", "hello?") is None


def test_candidate_length_is_bounded_for_provider_compatibility():
    baseline = "a" * 65
    current = "b" * 65
    assert _extract(baseline, current) is None


def test_multiple_independent_replacements_become_medium_candidates():
    result = extract_corrections(
        "Ostro uses openai",
        0,
        len("Ostro uses openai"),
        "Austral uses OpenAI",
    )

    assert result.reason_code == "multiple-replacements"
    assert result.replacement_hunks == 2
    assert [(item.wrong, item.canonical) for item in result.candidates] == [
        ("Ostro", "Austral"),
        ("openai", "OpenAI"),
    ]
    assert [item.evidence for item in result.candidates] == ["medium", "medium"]
    assert [item.category for item in result.candidates] == [
        "recognition",
        "formatting",
    ]


def test_insertion_among_replacements_rejects_all_candidates():
    result = extract_corrections(
        "Ostro uses openai",
        0,
        len("Ostro uses openai"),
        "Austral now uses OpenAI",
    )

    assert result.reason_code == "insertion-or-deletion"
    assert result.candidates == ()


def test_result_explains_no_change_and_outside_span():
    assert extract_corrections("same", 0, 4, "same").reason_code == "no-change"
    assert (
        extract_corrections(
            "prefix Ostro",
            len("prefix "),
            len("prefix Ostro"),
            "changed Ostro",
        ).reason_code
        == "edit-outside-committed-span"
    )


def test_multi_hunk_diff_is_bounded_after_linear_context_trimming():
    middle = " ".join(f"word{index}" for index in range(300))
    baseline = f"Ostro {middle} openai"
    current = f"Austral {middle} OpenAI"

    result = extract_corrections(baseline, 0, len(baseline), current)

    assert result.reason_code == "diff-too-complex"
    assert result.candidates == ()
