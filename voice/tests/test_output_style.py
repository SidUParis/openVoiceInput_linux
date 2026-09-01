from __future__ import annotations

import json
import stat
from dataclasses import replace

import pytest

from murmur_voice.clean_expression import CleanExpressionEdit, CleanExpressionResult
from murmur_voice.config import ConfigError
from murmur_voice.output_style import (
    OUTPUT_PROCESSOR_NAME,
    OUTPUT_PROCESSOR_VERSION,
    OutputStyleConfig,
    deliver_output,
    load_output_style_config,
    save_output_style_config,
    validate_output_delivery,
)


def test_missing_private_config_defaults_to_faithful(tmp_path):
    path = tmp_path / "missing" / "output-style.json"

    assert load_output_style_config(path) == OutputStyleConfig("faithful")
    assert not path.exists()


@pytest.mark.parametrize("mode", ("faithful", "clean"))
def test_private_config_round_trip_is_atomic_and_private(tmp_path, mode):
    path = tmp_path / "private" / "output-style.json"

    destination = save_output_style_config(mode, path)

    assert destination == path
    assert load_output_style_config(path) == OutputStyleConfig(mode)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "mode": mode,
    }
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".output-style.json.*"))


@pytest.mark.parametrize(
    "document",
    (
        {"version": 2, "mode": "faithful"},
        {"version": 1, "mode": "polish"},
        {"version": 1, "mode": "clean", "extra": True},
        {"version": True, "mode": "clean"},
    ),
)
def test_existing_config_is_strict(document, tmp_path):
    path = tmp_path / "output-style.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ConfigError):
        load_output_style_config(path)


def test_output_style_rejects_public_or_linked_private_file(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"version":1,"mode":"clean"}\n', encoding="utf-8")
    target.chmod(0o600)
    linked = tmp_path / "linked.json"
    linked.symlink_to(target)

    with pytest.raises(ConfigError):
        load_output_style_config(linked)

    target.chmod(0o644)
    with pytest.raises(ConfigError):
        load_output_style_config(target)


def test_faithful_delivery_never_calls_cleaner():
    def fail(_text):
        raise AssertionError("faithful mode must not invoke cleaner")

    delivery = deliver_output("我我觉得，呃，可以。", "faithful", cleaner=fail)

    assert delivery.mode == "faithful"
    assert delivery.text == "我我觉得，呃，可以。"
    assert delivery.outcome == "faithful"
    assert delivery.processor == "identity"
    assert delivery.edits == ()
    assert delivery.changed is False


def test_clean_delivery_has_replayable_machine_derived_audit():
    raw = "我我觉得，呃，可以。"

    delivery = deliver_output(raw, "clean")
    document = delivery.as_record_document()

    assert delivery.text == "我觉得，可以。"
    assert delivery.changed is True
    assert document == {
        "mode": "clean",
        "text": "我觉得，可以。",
        "review_status": "machine-derived-unreviewed",
        "processor": {
            "name": OUTPUT_PROCESSOR_NAME,
            "version": OUTPUT_PROCESSOR_VERSION,
        },
        "outcome": "cleaned",
        "edits": [
            {
                "start": 0,
                "end": 1,
                "kind": "self-repetition",
                "reason": "adjacent-exact-restart",
                "source": "我",
                "replacement": "",
            },
            {
                "start": 5,
                "end": 7,
                "kind": "filler",
                "reason": "standalone-hesitation",
                "source": "呃，",
                "replacement": "",
            },
        ],
    }
    replay = raw
    for edit in reversed(document["edits"]):
        assert replay[edit["start"] : edit["end"]] == edit["source"]
        replay = replay[: edit["start"]] + edit["replacement"] + replay[edit["end"] :]
    assert replay == document["text"]


def test_clean_delivery_does_not_change_terms_numbers_or_letter_case():
    raw = "WinoBias benchmark 42 OpenAI modèle-3"

    delivery = deliver_output(raw, "clean")

    assert delivery.text == raw
    assert delivery.outcome == "unchanged"
    assert delivery.edits == ()


@pytest.mark.parametrize(
    "outcome",
    ("input-too-large", "too-many-edits", "would-remove-all-content"),
)
def test_clean_declined_outcomes_fall_back_to_raw(outcome):
    raw = "raw provider final"

    delivery = deliver_output(
        raw,
        "clean",
        cleaner=lambda _text: CleanExpressionResult(raw, reason_code=outcome),
    )

    assert delivery.text == raw
    assert delivery.outcome == outcome
    assert delivery.edits == ()
    assert delivery.changed is False


def test_cleaner_failure_falls_back_to_raw_without_blocking():
    raw = "private provider final"

    def fail(_text):
        raise RuntimeError(raw)

    delivery = deliver_output(raw, "clean", cleaner=fail)

    assert delivery.text == raw
    assert delivery.outcome == "processor-error"
    assert raw not in repr(delivery)


def test_malformed_cleaner_result_falls_back_to_raw():
    raw = "我我觉得"
    malformed = CleanExpressionResult(
        "完全不同",
        (
            CleanExpressionEdit(
                start=0,
                end=1,
                kind="self-repetition",
                reason="adjacent-exact-restart",
                source="我",
            ),
        ),
        "cleaned",
    )

    delivery = deliver_output(raw, "clean", cleaner=lambda _text: malformed)

    assert delivery.text == raw
    assert delivery.outcome == "processor-error"
    assert delivery.edits == ()


def test_malformed_cleaner_cannot_delete_the_complete_authoritative_final():
    raw = "我"
    malformed = CleanExpressionResult(
        "",
        (
            CleanExpressionEdit(
                start=0,
                end=1,
                kind="self-repetition",
                reason="adjacent-exact-restart",
                source="我",
            ),
        ),
        "cleaned",
    )

    delivery = deliver_output(raw, "clean", cleaner=lambda _text: malformed)

    assert delivery.text == raw
    assert delivery.outcome == "processor-error"
    assert delivery.edits == ()


def test_malformed_cleaner_cannot_leave_only_punctuation():
    raw = "我。"
    malformed = CleanExpressionResult(
        "。",
        (
            CleanExpressionEdit(
                start=0,
                end=1,
                kind="self-repetition",
                reason="adjacent-exact-restart",
                source="我",
            ),
        ),
        "cleaned",
    )

    delivery = deliver_output(raw, "clean", cleaner=lambda _text: malformed)

    assert delivery.text == raw
    assert delivery.outcome == "processor-error"
    assert delivery.edits == ()


def test_replayable_cleaner_result_with_list_edits_falls_back_to_raw():
    raw = "我我继续"
    edit = CleanExpressionEdit(
        start=0,
        end=1,
        kind="self-repetition",
        reason="adjacent-exact-restart",
        source="我",
    )
    malformed = CleanExpressionResult(
        "我继续",
        [edit],  # type: ignore[arg-type]
        "cleaned",
    )

    delivery = deliver_output(raw, "clean", cleaner=lambda _text: malformed)

    assert delivery.text == raw
    assert delivery.outcome == "processor-error"
    assert delivery.edits == ()


def test_invalid_mode_is_never_silently_coerced():
    with pytest.raises(ConfigError, match="unsupported"):
        deliver_output("provider final", "polish")


def test_delivery_validator_rejects_boolean_processor_version():
    valid = deliver_output("provider final", "faithful")

    with pytest.raises(ValueError, match="metadata"):
        validate_output_delivery(
            "provider final",
            replace(valid, processor_version=True),
        )


def test_delivery_validator_rejects_unknown_edit_kind_and_reason():
    raw = "我我继续"
    valid = deliver_output(raw, "clean")
    malformed_edit = replace(valid.edits[0], kind="unknown", reason="unknown")

    with pytest.raises(ValueError, match="invalid edit"):
        validate_output_delivery(
            raw,
            replace(valid, edits=(malformed_edit,)),
        )
