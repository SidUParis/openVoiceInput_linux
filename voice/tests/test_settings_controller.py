from __future__ import annotations

import stat
from dataclasses import dataclass

import pytest

from murmur_voice.config import load_config, load_vocabulary
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


def test_start_requires_safe_local_configuration_before_systemctl(tmp_path):
    runner = RecordingRunner()
    controller = _controller(tmp_path, runner)

    with pytest.raises(SettingsError, match="valid saved API key"):
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
