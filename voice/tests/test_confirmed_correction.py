from __future__ import annotations

from murmur_voice.confirmed_correction import (
    MAX_CONFIRMED_CORRECTION_EDITS,
    ConfirmedCorrectionResult,
    apply_confirmed_corrections,
    replay_confirmed_corrections,
)
from murmur_voice.config import CorrectionPair


def test_confirmed_ascii_correction_uses_lexical_boundaries():
    result = apply_confirmed_corrections(
        "Elas Elastic Elasticsearch ELAS",
        (CorrectionPair("Elas", "ILaaS"),),
    )

    assert result.text == "ILaaS Elastic Elasticsearch ELAS"
    assert result.reason_code == "corrected"
    assert len(result.edits) == 1
    assert (
        replay_confirmed_corrections("Elas Elastic Elasticsearch ELAS", result.edits)
        == result.text
    )


def test_ascii_corrections_are_case_sensitive_to_avoid_pronoun_hijacking():
    result = apply_confirmed_corrections(
        "US asked us",
        (
            CorrectionPair("US", "United States"),
            CorrectionPair("us", "ourselves"),
        ),
    )

    assert result.text == "United States asked ourselves"


def test_confirmed_ascii_correction_does_not_rewrite_urls_or_code_tokens():
    raw = (
        "https://example.test/Elas https://example.test/package-Elas "
        "`Elas` foo_Elas package.Elas Elas。"
    )

    result = apply_confirmed_corrections(
        raw,
        (CorrectionPair("Elas", "ILaaS"),),
    )

    assert result.text == (
        "https://example.test/Elas https://example.test/package-Elas "
        "`Elas` foo_Elas package.Elas ILaaS。"
    )
    assert len(result.edits) == 1


def test_unclosed_markdown_code_span_is_conservatively_left_unchanged():
    raw = "plain Elas then `code Elas"

    result = apply_confirmed_corrections(
        raw,
        (CorrectionPair("Elas", "ILaaS"),),
    )

    assert result.text == "plain ILaaS then `code Elas"


def test_non_ascii_rules_do_not_rewrite_markdown_code_or_url_paths():
    raw = "plain 伊拉斯 `伊拉斯` https://example.test/伊拉斯"

    result = apply_confirmed_corrections(
        raw,
        (CorrectionPair("伊拉斯", "ILaaS"),),
    )

    assert result.text == "plain ILaaS `伊拉斯` https://example.test/伊拉斯"


def test_longest_confirmed_rule_wins_without_cascading():
    result = apply_confirmed_corrections(
        "bench mark mark",
        (
            CorrectionPair("mark", "MARK"),
            CorrectionPair("bench mark", "benchmark"),
            CorrectionPair("benchmark", "SHOULD-NOT-CASCADE"),
        ),
    )

    assert result.text == "benchmark MARK"
    assert [edit.source for edit in result.edits] == ["bench mark", "mark"]


def test_non_ascii_confirmed_rule_is_exact_and_replayable():
    raw = "伊拉斯和伊拉斯"
    result = apply_confirmed_corrections(
        raw,
        (CorrectionPair("伊拉斯", "ILaaS"),),
    )

    assert result.text == "ILaaS和ILaaS"
    assert replay_confirmed_corrections(raw, result.edits) == result.text


def test_too_many_confirmed_edits_fails_open_to_raw():
    raw = " ".join("Elas" for _ in range(MAX_CONFIRMED_CORRECTION_EDITS + 1))

    result = apply_confirmed_corrections(
        raw,
        (CorrectionPair("Elas", "ILaaS"),),
    )

    assert result == ConfirmedCorrectionResult(raw, reason_code="too-many-edits")


def test_expanded_output_over_delivery_limit_fails_open_to_raw():
    prefix = " ".join("x" for _ in range(MAX_CONFIRMED_CORRECTION_EDITS))
    raw = prefix + (" y" * ((4096 - len(prefix)) // 2))
    raw = raw + ("z" * (4096 - len(raw)))
    assert len(raw) == 4096

    result = apply_confirmed_corrections(
        raw,
        (CorrectionPair("x", "X" * 64),),
    )

    assert result == ConfirmedCorrectionResult(raw, reason_code="output-too-large")


def test_oversized_input_fails_open_without_scanning_rules():
    raw = "x" * 4097

    result = apply_confirmed_corrections(
        raw,
        (CorrectionPair("x", "y"),),
    )

    assert result == ConfirmedCorrectionResult(raw, reason_code="input-too-large")


def test_private_text_and_rules_are_hidden_from_repr():
    private_raw = "private Elas"
    result = apply_confirmed_corrections(
        private_raw,
        (CorrectionPair("Elas", "ILaaS"),),
    )

    assert private_raw not in repr(result)
    assert "Elas" not in repr(result)
    assert "ILaaS" not in repr(result)
