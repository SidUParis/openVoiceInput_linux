from __future__ import annotations

import pytest

from murmur_voice import clean_expression as clean_expression_module
from murmur_voice.clean_expression import (
    MAX_CLEAN_EXPRESSION_CODEPOINTS,
    MAX_CLEAN_EXPRESSION_EDITS,
    CleanExpressionEdit,
    clean_expression,
)


def _apply_edits(original: str, edits: tuple[CleanExpressionEdit, ...]) -> str:
    result = original
    for edit in reversed(edits):
        assert result[edit.start : edit.end] == edit.source
        result = result[: edit.start] + edit.replacement + result[edit.end :]
    return result


def _is_subsequence(candidate: str, original: str) -> bool:
    iterator = iter(original)
    return all(character in iterator for character in candidate)


@pytest.mark.parametrize(
    ("provider_final", "expected"),
    (
        ("我我觉得这样可以。", "我觉得这样可以。"),
        ("那那我们继续。", "那我们继续。"),
        ("你要你要重新检查。", "你要重新检查。"),
        ("在这个在这个分数上。", "在这个分数上。"),
        ("这个这个需要验证。", "这个需要验证。"),
        ("但 但是我们还要验证。", "但是我们还要验证。"),
    ),
)
def test_high_confidence_adjacent_restarts_are_folded(provider_final, expected):
    result = clean_expression(provider_final)

    assert result.text == expected
    assert result.reason_code == "cleaned"
    assert len(result.edits) == 1
    assert result.edits[0].kind == "self-repetition"
    assert result.edits[0].replacement == ""
    assert _apply_edits(provider_final, result.edits) == expected
    assert _is_subsequence(expected, provider_final)


@pytest.mark.parametrize(
    ("provider_final", "expected"),
    (
        ("我觉得，呃，这个可以。", "我觉得，这个可以。"),
        ("我觉得，嗯，这个可以。", "我觉得，这个可以。"),
        ("Um, I think this works.", "Um, I think this works."),
        ("um, I think this works.", "I think this works."),
        ("I think, um, this works.", "I think this works."),
        ("Je pense, euh, que ça marche.", "Je pense que ça marche."),
        ("我觉得，呃。", "我觉得。"),
    ),
)
def test_only_standalone_high_confidence_fillers_are_removed(
    provider_final,
    expected,
):
    result = clean_expression(provider_final)

    assert result.text == expected
    assert _apply_edits(provider_final, result.edits) == expected
    assert _is_subsequence(expected, provider_final)
    assert all(edit.replacement == "" for edit in result.edits)
    if provider_final != expected:
        assert {edit.kind for edit in result.edits} == {"filler"}


@pytest.mark.parametrize(
    "provider_final",
    (
        "我们看看这个结果。",
        "我再想想。",
        "人人都有机会。",
        "一层一层地检查。",
        "这个方案可能能解决问题。",
        "是否可行？",
        "就是这样。",
        "然后继续。",
        "其实没有问题。",
        "嗯，我知道了。",
        "我嗯了一声。",
        "嗯嗯，我知道了。",
        "嗯哼，可以。",
        "嗯了一下。",
        "我说，嗯哼，这样可以。",
        "呃呃，我再想想。",
        "“嗯”表示同意。",
        "WinoBias benchmark",
        "très très important",
        "在线在线处理。",
        "对称对称结构。",
        "我爱我爱罗。",
        "我们支持我们支持的团队。",
        "你要你要的，我要我要的。",
        "我想我想的办法。",
        "我认为我认为的结论成立。",
        "我们要我们要的结果。",
        "museum enum euhler",
        "Bonjour ; euh ; suite",
        "中文： 呃 ：继续",
    ),
)
def test_meaningful_reduplication_terms_and_substrings_are_preserved(provider_final):
    result = clean_expression(provider_final)

    assert result.text == provider_final
    assert result.edits == ()
    assert result.reason_code == "unchanged"


def test_multiple_independent_deletions_keep_original_offsets_and_order():
    provider_final = "我我觉得，呃，这个在这个在这个阶段可以。"

    result = clean_expression(provider_final)

    assert result.text == "我觉得，这个在这个阶段可以。"
    assert [edit.kind for edit in result.edits] == [
        "self-repetition",
        "filler",
        "self-repetition",
    ]
    assert list(result.edits) == sorted(result.edits, key=lambda item: item.start)
    assert all(
        left.end <= right.start
        for left, right in zip(result.edits, result.edits[1:], strict=False)
    )
    assert _apply_edits(provider_final, result.edits) == result.text
    assert _is_subsequence(result.text, provider_final)


def test_punctuation_and_spacing_are_not_globally_normalized():
    provider_final = "Bonjour !  中文， English ; français ?"

    result = clean_expression(provider_final)

    assert result.text == provider_final
    assert result.edits == ()


@pytest.mark.parametrize("provider_final", ("", "   ", "。", "嗯", "呃"))
def test_empty_or_content_free_input_is_not_erased(provider_final):
    result = clean_expression(provider_final)

    assert result.text == provider_final
    assert result.edits == ()
    assert result.reason_code == "unchanged"


def test_structured_edit_uses_original_codepoint_coordinates_and_reason():
    provider_final = "前缀，你要你要继续。"

    result = clean_expression(provider_final)

    assert result.edits == (
        CleanExpressionEdit(
            start=3,
            end=5,
            kind="self-repetition",
            reason="adjacent-exact-restart",
            source="你要",
        ),
    )
    assert result.text == "前缀，你要继续。"


def test_non_string_input_is_not_coerced_into_visible_text():
    with pytest.raises(TypeError, match="provider_final must be a string"):
        clean_expression(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "provider_final",
    (
        "text, um, uh",
        "我，呃，我",
        "但 但 但是继续",
        "我我觉得，呃，这个在这个在这个阶段可以。",
        "um, uh, text",
    ),
)
def test_cleanup_is_idempotent(provider_final):
    first = clean_expression(provider_final)
    second = clean_expression(first.text)

    assert second.text == first.text
    assert second.edits == ()
    assert second.reason_code == "unchanged"


def test_oversized_input_returns_raw_before_running_detectors(monkeypatch):
    provider_final = "a" * (MAX_CLEAN_EXPRESSION_CODEPOINTS + 1)

    def fail_if_called(_text):
        raise AssertionError("detector must not run for oversized input")

    monkeypatch.setattr(
        clean_expression_module,
        "_repeated_character_edits",
        fail_if_called,
    )

    result = clean_expression(provider_final)

    assert result.text == provider_final
    assert result.edits == ()
    assert result.reason_code == "input-too-large"


def test_maximum_sized_unchanged_input_is_accepted():
    provider_final = "a" * MAX_CLEAN_EXPRESSION_CODEPOINTS

    result = clean_expression(provider_final)

    assert result.text == provider_final
    assert result.reason_code == "unchanged"


def test_more_than_edit_budget_returns_raw_with_content_free_reason():
    provider_final = "。".join("我我" for _ in range(MAX_CLEAN_EXPRESSION_EDITS + 1))

    result = clean_expression(provider_final)

    assert result.text == provider_final
    assert result.edits == ()
    assert result.reason_code == "too-many-edits"


def test_exact_edit_budget_is_accepted():
    provider_final = "。".join("我我" for _ in range(MAX_CLEAN_EXPRESSION_EDITS))

    result = clean_expression(provider_final)

    assert result.reason_code == "cleaned"
    assert len(result.edits) == MAX_CLEAN_EXPRESSION_EDITS


def test_removing_every_lexical_token_falls_back_to_raw_with_reason():
    provider_final = "um, uh"

    result = clean_expression(provider_final)

    assert result.text == provider_final
    assert result.edits == ()
    assert result.reason_code == "would-remove-all-content"
