from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from murmur_voice.adaptive_runtime import save_adaptive_ledger
from murmur_voice.adaptive_store import AdaptiveLedger, record_evidence
from murmur_voice.config import (
    load_config,
    load_corrections as load_corrections_file,
    load_vocabulary,
)
from murmur_voice.data_collection import DataCollectionConfig
from murmur_voice.interaction import InteractionConfig, load_interaction_config
from murmur_voice.microphone_policy import (
    DEFAULT_MICROPHONE_PRIORITY,
    MicrophonePolicyConfig,
    load_microphone_policy_config,
    save_microphone_policy_config,
)
from murmur_voice.settings_controller import (
    SYSTEMCTL,
    VOICE_SERVICE,
    DatasetStatistics,
    KeyState,
    ServiceSnapshot,
    SettingsController,
    SettingsError,
)


@dataclass
class FakeCompletedProcess:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class RecordingRunner:
    def __init__(self, active_state: str = "inactive") -> None:
        self.active_state = active_state
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        action = command[2]
        if action == "is-active":
            return FakeCompletedProcess(
                returncode=0 if self.active_state == "active" else 3,
                stdout=f"{self.active_state}\n",
            )
        if action == "enable":
            self.active_state = "active"
            return FakeCompletedProcess()
        if action == "disable":
            self.active_state = "inactive"
            return FakeCompletedProcess()
        raise AssertionError(f"unexpected action: {action}")


def _controller(tmp_path, runner=None, status_reader=None):
    options = {
        "config_path": tmp_path / "private" / "voice.json",
        "vocabulary_path": tmp_path / "private" / "vocabulary.json",
        "corrections_path": tmp_path / "private" / "corrections.json",
        "adaptive_corrections_path": (
            tmp_path / "private" / "adaptive-corrections.json"
        ),
        "data_collection_path": tmp_path / "private" / "data-collection.json",
        "microphone_policy_path": tmp_path / "private" / "microphone-priority.json",
        "interaction_path": tmp_path / "private" / "interaction.json",
        "runner": runner or RecordingRunner(),
    }
    if status_reader is not None:
        options["status_reader"] = status_reader
    return SettingsController(**options)


def _write_usage_summary(
    selected,
    utterance_id,
    *,
    recorded_at,
    duration_ms,
    character_count,
):
    summary = selected / "openvoiceinput-dataset-v1" / "usage" / f"{utterance_id}.json"
    summary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "openvoiceinput-private-usage-summary",
                "utterance_id": utterance_id,
                "recorded_at_utc": recorded_at,
                "audio_duration_ms": duration_ms,
                "non_whitespace_character_count": character_count,
            }
        ),
        encoding="utf-8",
    )
    summary.chmod(0o600)
    return summary


def test_key_save_is_private_never_runs_service_or_contacts_provider(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)
    secret = "test-private-key-sentinel"

    assert controller.key_state() is KeyState.MISSING
    controller.save_key(secret)

    path = tmp_path / "private" / "voice.json"
    assert controller.key_state() is KeyState.READY
    assert load_config(path).api_key == secret
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert runner.calls == []
    assert not hasattr(controller, "restart_service")


def test_key_error_never_contains_the_submitted_key(tmp_path):
    controller = _controller(tmp_path)
    secret = "private-key\nthat-must-not-appear"

    with pytest.raises(SettingsError) as captured:
        controller.save_key(secret)

    assert secret not in str(captured.value)
    assert "that-must-not-appear" not in str(captured.value)


def test_provider_selection_is_secret_free_and_key_replacement_preserves_it(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)

    selected = controller.save_provider("qwen-secret", "qwen")

    assert selected.provider == "qwen"
    assert selected.model == "qwen-audio-3.0-asr-flash-streaming"
    assert controller.provider_selection() == selected
    assert "qwen-secret" not in repr(selected)
    controller.save_key("replacement-secret")
    loaded = load_config(tmp_path / "private" / "voice.json")
    assert loaded.provider == "qwen"
    assert loaded.api_key == "replacement-secret"
    assert runner.calls == []


def test_provider_selection_rejects_planned_backend_without_leaking_key(tmp_path):
    controller = _controller(tmp_path)
    secret = "minimax-secret-sentinel"

    with pytest.raises(SettingsError) as captured:
        controller.save_provider(secret, "minimax")

    assert secret not in str(captured.value)


def test_key_clear_requires_inactive_service_and_never_contacts_provider(tmp_path):
    runner = RecordingRunner(active_state="inactive")

    def provider_reader(command):
        raise AssertionError(
            f"provider/control socket must not be contacted: {command}"
        )

    controller = _controller(tmp_path, runner, provider_reader)
    controller.save_key("private-key-sentinel")

    assert controller.clear_key() is True
    assert controller.key_state() is KeyState.MISSING
    assert controller.clear_key() is False
    assert [call[0] for call in runner.calls] == [
        (SYSTEMCTL, "--user", "is-active", VOICE_SERVICE),
        (SYSTEMCTL, "--user", "is-active", VOICE_SERVICE),
    ]


@pytest.mark.parametrize("active_state", ("active", "failed", "activating"))
def test_key_clear_refuses_every_service_state_except_inactive(tmp_path, active_state):
    runner = RecordingRunner(active_state=active_state)

    def daemon_reader(command):
        raise AssertionError(f"daemon socket must not be contacted: {command}")

    controller = _controller(tmp_path, runner, daemon_reader)
    secret = "private-key-that-must-not-appear"
    controller.save_key(secret)

    with pytest.raises(SettingsError, match="Disable and stop") as captured:
        controller.clear_key()

    assert load_config(tmp_path / "private" / "voice.json").api_key == secret
    assert secret not in str(captured.value)
    assert len(runner.calls) == 1


def test_key_clear_refuses_unknown_service_state_and_unsafe_key_file(tmp_path):
    class UnknownRunner(RecordingRunner):
        def __call__(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return FakeCompletedProcess(
                returncode=1, stdout="unexpected-private-output"
            )

    unknown_controller = _controller(tmp_path, UnknownRunner())
    unknown_controller.save_key("private-key-sentinel")
    with pytest.raises(SettingsError, match="Disable and stop") as captured:
        unknown_controller.clear_key()
    assert "unexpected-private-output" not in str(captured.value)

    runner = RecordingRunner(active_state="inactive")
    controller = _controller(tmp_path, runner)
    path = tmp_path / "private" / "voice.json"
    path.chmod(0o644)
    with pytest.raises(SettingsError, match="could not be removed safely") as captured:
        controller.clear_key()
    assert "private-key-sentinel" not in str(captured.value)
    assert path.exists()


def test_vocabulary_save_deduplicates_without_running_service(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)

    count = controller.save_vocabulary_text(" Alpha \nalpha\n中文\n\n")

    assert count == 2
    assert load_vocabulary(tmp_path / "private" / "vocabulary.json") == (
        "Alpha",
        "中文",
    )
    assert runner.calls == []


def test_vocabulary_error_never_contains_a_term(tmp_path):
    controller = _controller(tmp_path)
    private_term = "term-that-must-not-appear-" + ("界" * 65)

    with pytest.raises(SettingsError) as captured:
        controller.save_vocabulary_text(private_term)

    assert private_term not in str(captured.value)
    assert "term-that-must-not-appear" not in str(captured.value)


def test_corrections_load_save_normalize_without_running_service(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)

    count = controller.save_corrections(
        (
            ("  commonly misheard  ", "  canonical form  "),
            ("commonly misheard", "canonical form"),
            ("另一个错误", "标准写法"),
        )
    )

    assert count == 2
    assert controller.load_corrections() == (
        ("commonly misheard", "canonical form"),
        ("另一个错误", "标准写法"),
    )
    stored = load_corrections_file(tmp_path / "private" / "corrections.json")
    assert tuple((pair.wrong, pair.canonical) for pair in stored) == (
        ("commonly misheard", "canonical form"),
        ("另一个错误", "标准写法"),
    )
    assert runner.calls == []


def test_correction_error_never_contains_submitted_text(tmp_path):
    controller = _controller(tmp_path)
    private_wrong = "private-wrong-that-must-not-appear-" + ("界" * 65)
    private_canonical = "private-canonical-that-must-not-appear"

    with pytest.raises(SettingsError) as captured:
        controller.save_corrections(((private_wrong, private_canonical),))

    message = str(captured.value)
    assert private_wrong not in message
    assert private_canonical not in message
    assert "must-not-appear" not in message


def test_correction_load_error_never_contains_existing_private_text(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)
    private_text = "private-existing-correction-that-must-not-appear"
    path = tmp_path / "private" / "corrections.json"
    path.parent.mkdir(parents=True)
    path.write_text(private_text, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(SettingsError) as captured:
        controller.load_corrections()

    assert private_text not in str(captured.value)
    assert "must-not-appear" not in str(captured.value)
    assert runner.calls == []


def test_adaptive_snapshot_exposes_counts_recent_reason_and_confirmable_candidate(
    tmp_path,
):
    controller = _controller(tmp_path)
    path = tmp_path / "private" / "adaptive-corrections.json"
    ledger = record_evidence(
        AdaptiveLedger(),
        "Ostro",
        "Austral",
        state="candidate",
        category="recognition",
        evidence="medium",
    )
    save_adaptive_ledger(ledger, path)

    snapshot = controller.load_adaptive_learning()

    assert snapshot.statistics["candidate"] == 1
    assert snapshot.last_result is None
    assert len(snapshot.review_entries) == 1
    assert snapshot.review_entries[0].state == "candidate"

    assert controller.confirm_adaptive_learning("Ostro", "Austral") is True
    confirmed = controller.load_adaptive_learning()
    assert confirmed.statistics["active"] == 1
    assert confirmed.statistics["candidate"] == 0
    assert confirmed.last_result["reason_code"] == "explicitly-activated"


def test_explicit_adaptive_feedback_is_available_when_auto_capture_is_absent(tmp_path):
    controller = _controller(tmp_path)

    reason = controller.submit_adaptive_feedback(
        "Ostro uses openai",
        "Austral uses OpenAI",
    )

    assert reason == "explicit-feedback-activated"
    snapshot = controller.load_adaptive_learning()
    assert snapshot.statistics["active"] == 2
    assert snapshot.last_result["reason_code"] == "explicit-feedback-activated"


def test_status_start_and_stop_use_only_fixed_argv_without_a_shell(tmp_path):
    runner = RecordingRunner(active_state="active")
    status_commands = []

    def read_status(command):
        status_commands.append(command)
        return {"ok": True, "state": "recording", "code": "status"}

    controller = _controller(tmp_path, runner, read_status)
    controller.save_key("test-key")

    assert controller.service_status() == ServiceSnapshot(
        "active", "recording", "status"
    )
    controller.start_service()
    controller.stop_service()

    assert status_commands == ["status"]
    assert [call[0] for call in runner.calls] == [
        (SYSTEMCTL, "--user", "is-active", VOICE_SERVICE),
        (SYSTEMCTL, "--user", "enable", "--now", VOICE_SERVICE),
        (SYSTEMCTL, "--user", "disable", "--now", VOICE_SERVICE),
    ]
    for _, keywords in runner.calls:
        assert keywords["shell"] is False
        assert keywords["capture_output"] is True
        assert keywords["check"] is False
        assert keywords["text"] is True


def test_microphone_unavailable_status_is_allowlisted(tmp_path):
    controller = _controller(
        tmp_path,
        RecordingRunner(active_state="active"),
        lambda command: {
            "ok": False,
            "state": "idle",
            "code": "microphone-unavailable",
        },
    )

    assert controller.service_status() == ServiceSnapshot(
        "active", "idle", "microphone-unavailable"
    )


def test_microphone_policy_invalid_status_is_allowlisted(tmp_path):
    controller = _controller(
        tmp_path,
        RecordingRunner(active_state="active"),
        lambda command: {
            "ok": False,
            "state": "idle",
            "code": "microphone-policy-invalid",
        },
    )

    assert controller.service_status() == ServiceSnapshot(
        "active", "idle", "microphone-policy-invalid"
    )


def test_observing_and_adaptive_status_are_allowlisted(tmp_path):
    controller = _controller(
        tmp_path,
        RecordingRunner(active_state="active"),
        lambda command: {
            "ok": True,
            "state": "observing",
            "code": "adaptive-correction-learned",
        },
    )

    assert controller.service_status() == ServiceSnapshot(
        "active", "observing", "adaptive-correction-learned"
    )


def test_start_requires_safe_local_configuration_before_systemctl(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)

    with pytest.raises(SettingsError, match="valid saved API key"):
        controller.start_service()

    assert runner.calls == []


def test_start_validates_corrections_before_systemctl(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)
    controller.save_key("test-key")
    corrections_path = tmp_path / "private" / "corrections.json"
    corrections_path.write_text(
        '{"version": 1, "pairs": [{"wrong": "a", "canonical": "b"}]}',
        encoding="utf-8",
    )
    corrections_path.chmod(0o644)

    with pytest.raises(SettingsError, match="Valid explicit corrections"):
        controller.start_service()

    assert runner.calls == []


def test_start_validates_adaptive_corrections_before_systemctl(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)
    controller.save_key("test-key")
    adaptive_path = tmp_path / "private" / "adaptive-corrections.json"
    adaptive_path.write_text(
        '{"version":1,"entries":[]}',
        encoding="utf-8",
    )
    adaptive_path.chmod(0o644)

    with pytest.raises(SettingsError, match="Valid adaptive corrections"):
        controller.start_service()

    assert runner.calls == []


def test_systemctl_failure_does_not_echo_output_or_saved_key(tmp_path):
    secret = "private-key-that-must-not-appear"

    class FailingRunner(RecordingRunner):
        def __call__(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return FakeCompletedProcess(returncode=1, stderr=secret)

    runner = FailingRunner()
    controller = _controller(tmp_path, runner)
    controller.save_key(secret)

    with pytest.raises(SettingsError) as captured:
        controller.start_service()

    assert secret not in str(captured.value)
    assert all(secret not in str(call) for call in runner.calls)


def test_untrusted_daemon_status_is_allowlisted(tmp_path):
    private_term = "private-vocabulary-term"
    runner = RecordingRunner(active_state="active")
    controller = _controller(
        tmp_path,
        runner,
        lambda command: {
            "ok": True,
            "state": private_term,
            "code": "private-key-sentinel",
        },
    )

    snapshot = controller.service_status()

    assert snapshot == ServiceSnapshot("active", "unknown", "unknown")
    assert private_term not in repr(snapshot)


def test_unknown_systemctl_output_is_not_forwarded_to_the_view(tmp_path):
    private_output = "private-key-sentinel"

    class UnknownRunner(RecordingRunner):
        def __call__(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return FakeCompletedProcess(returncode=1, stdout=private_output)

    controller = _controller(tmp_path, UnknownRunner())

    snapshot = controller.service_status()

    assert snapshot == ServiceSnapshot("unknown")
    assert private_output not in repr(snapshot)


def test_data_collection_defaults_off_and_save_never_runs_service(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)
    selected = tmp_path / "personal-asr-records"
    selected.mkdir()

    assert controller.load_data_collection() == DataCollectionConfig()

    saved = controller.save_data_collection(True, selected)

    assert saved.enabled is True
    assert saved.directory == selected
    assert saved.dataset_id is not None
    assert controller.load_data_collection() == saved
    assert runner.calls == []


def test_interaction_defaults_and_save_are_private_local_and_hot_loaded(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)

    assert controller.load_interaction() == InteractionConfig()

    saved = controller.save_interaction("push_to_talk", 250, 90)
    path = tmp_path / "private" / "interaction.json"

    assert saved == InteractionConfig("push_to_talk", 250, 90)
    assert load_interaction_config(path) == saved
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert runner.calls == []


def test_interaction_rejects_invalid_mode_without_running_service(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)

    with pytest.raises(SettingsError, match="could not be saved safely"):
        controller.save_interaction("private-invalid-mode", 180, 120)

    assert not (tmp_path / "private" / "interaction.json").exists()
    assert runner.calls == []


def test_dataset_statistics_are_disabled_without_touching_storage(tmp_path):
    controller = _controller(tmp_path)

    assert controller.load_dataset_statistics() == DatasetStatistics("disabled")


def test_dataset_statistics_use_only_content_free_usage_summaries(tmp_path):
    controller = _controller(tmp_path)
    selected = tmp_path / "personal-asr-records"
    selected.mkdir()
    controller.save_data_collection(True, selected)
    _write_usage_summary(
        selected,
        "utterance-today",
        recorded_at="2026-08-31T00:05:00Z",
        duration_ms=12_500,
        character_count=88,
    )
    _write_usage_summary(
        selected,
        "utterance-yesterday",
        recorded_at="2026-08-30T23:55:00Z",
        duration_ms=7_500,
        character_count=32,
    )
    utterances = selected / "openvoiceinput-dataset-v1" / "utterances"
    transcript_trap = utterances / "utterance-today"
    transcript_trap.mkdir()
    # The scanner must not open this transcript-bearing record. A directory at
    # the same path would fail immediately if it tried.
    (transcript_trap / "record.json").mkdir()
    (utterances / "legacy-record").mkdir()

    statistics = controller.load_dataset_statistics(
        now=datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    )

    assert statistics == DatasetStatistics(
        state="ready",
        today_characters=88,
        today_seconds=12.5,
        today_utterances=1,
        total_characters=120,
        total_seconds=20.0,
        total_utterances=2,
        latest_recorded_at=datetime(2026, 8, 31, 0, 5, tzinfo=timezone.utc),
    )


def test_dataset_statistics_report_unavailable_and_never_recreate_mount(tmp_path):
    controller = _controller(tmp_path)
    selected = tmp_path / "mounted-records"
    selected.mkdir()
    controller.save_data_collection(True, selected)
    dataset_root = selected / "openvoiceinput-dataset-v1"
    moved = tmp_path / "disconnected-dataset"
    dataset_root.rename(moved)

    assert controller.load_dataset_statistics() == DatasetStatistics("unavailable")
    assert not dataset_root.exists()


def test_legacy_dataset_without_usage_index_is_not_backfilled(tmp_path):
    controller = _controller(tmp_path)
    selected = tmp_path / "personal-asr-records"
    selected.mkdir()
    controller.save_data_collection(True, selected)
    usage_root = selected / "openvoiceinput-dataset-v1" / "usage"
    usage_root.rmdir()
    transcript_trap = (
        selected
        / "openvoiceinput-dataset-v1"
        / "utterances"
        / "legacy-record"
        / "record.json"
    )
    transcript_trap.parent.mkdir()
    transcript_trap.mkdir()

    assert controller.load_dataset_statistics() == DatasetStatistics("unindexed")
    assert not usage_root.exists()


def test_dataset_statistics_skip_invalid_summary_without_private_output(tmp_path):
    controller = _controller(tmp_path)
    selected = tmp_path / "personal-asr-records"
    selected.mkdir()
    controller.save_data_collection(True, selected)
    summary = selected / "openvoiceinput-dataset-v1" / "usage" / "invalid-record.json"
    private_text = "private-transcript-that-must-not-appear"
    summary.write_text(private_text, encoding="utf-8")
    summary.chmod(0o600)

    statistics = controller.load_dataset_statistics()

    assert statistics.state == "limited"
    assert statistics.invalid_summaries == 1
    assert private_text not in repr(statistics)


def test_data_collection_enable_requires_absolute_existing_folder(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)

    with pytest.raises(SettingsError, match="absolute storage folder"):
        controller.save_data_collection(True, "relative-folder")
    with pytest.raises(SettingsError, match="folder is unavailable"):
        controller.save_data_collection(True, tmp_path / "missing-folder")

    assert runner.calls == []


def test_data_collection_error_never_echoes_existing_private_content(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)
    private_text = "private-data-collection-path-that-must-not-appear"
    path = tmp_path / "private" / "data-collection.json"
    path.parent.mkdir(parents=True)
    path.write_text(private_text, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(SettingsError) as captured:
        controller.load_data_collection()

    assert private_text not in str(captured.value)
    assert "must-not-appear" not in str(captured.value)
    assert runner.calls == []


def test_invalid_optional_data_collection_does_not_block_service_start(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)
    controller.save_key("test-key")
    data_collection_path = tmp_path / "private" / "data-collection.json"
    data_collection_path.write_text("{invalid", encoding="utf-8")
    data_collection_path.chmod(0o600)

    controller.start_service()

    assert [call[0] for call in runner.calls] == [
        (SYSTEMCTL, "--user", "enable", "--now", VOICE_SERVICE),
    ]


def test_microphone_priority_defaults_and_save_are_private_and_local(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)

    assert controller.load_microphone_policy() == MicrophonePolicyConfig()

    priority = ("headset", "dji", "external", "built-in")
    saved = controller.save_microphone_priority(priority)
    path = tmp_path / "private" / "microphone-priority.json"

    assert saved.priority == priority
    assert load_microphone_policy_config(path) == saved
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert runner.calls == []


def test_microphone_priority_rejects_incomplete_or_duplicate_categories(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)

    for invalid in (
        ("dji", "headset", "built-in"),
        ("dji", "headset", "external", "external"),
        (*DEFAULT_MICROPHONE_PRIORITY, "unknown"),
    ):
        with pytest.raises(SettingsError, match="could not be saved safely"):
            controller.save_microphone_priority(invalid)

    assert not (tmp_path / "private" / "microphone-priority.json").exists()
    assert runner.calls == []


def test_microphone_priority_save_preserves_same_category_source_preferences(
    tmp_path,
):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)
    path = tmp_path / "private" / "microphone-priority.json"
    headset_source = "alsa_input.usb-Poly_Poly_V4320-00.mono-fallback"
    save_microphone_policy_config(
        DEFAULT_MICROPHONE_PRIORITY,
        path,
        preferred_sources={"headset": headset_source},
    )
    previous_preferences = load_microphone_policy_config(path).preferred_sources

    saved = controller.save_microphone_priority(
        ("headset", "dji", "external", "built-in")
    )

    assert saved.preferred_sources == previous_preferences
    assert saved.priority == ("headset", "dji", "external", "built-in")
    assert runner.calls == []


def test_invalid_microphone_priority_is_reported_but_does_not_block_service_start(
    tmp_path,
):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)
    controller.save_key("test-key")
    path = tmp_path / "private" / "microphone-priority.json"
    private_text = "private-microphone-setting-that-must-not-appear"
    path.write_text(private_text, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(SettingsError, match="could not be loaded safely") as captured:
        controller.load_microphone_policy()

    assert private_text not in str(captured.value)
    assert path.read_text(encoding="utf-8") == private_text

    repaired = controller.save_microphone_priority(DEFAULT_MICROPHONE_PRIORITY)

    assert repaired == MicrophonePolicyConfig()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert private_text not in path.read_text(encoding="utf-8")

    controller.start_service()

    assert [call[0] for call in runner.calls] == [
        (SYSTEMCTL, "--user", "enable", "--now", VOICE_SERVICE),
    ]
