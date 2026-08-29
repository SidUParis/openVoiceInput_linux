from __future__ import annotations

import stat
from dataclasses import dataclass

import pytest

from murmur_voice.config import (
    load_config,
    load_corrections as load_corrections_file,
    load_vocabulary,
)
from murmur_voice.settings_controller import (
    SYSTEMCTL,
    VOICE_SERVICE,
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
        "runner": runner or RecordingRunner(),
    }
    if status_reader is not None:
        options["status_reader"] = status_reader
    return SettingsController(**options)


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
