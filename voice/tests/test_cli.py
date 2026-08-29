from __future__ import annotations

import io
import logging
import stat

import pytest

from murmur_voice import cli
from murmur_voice import adaptive_runtime as adaptive_runtime_module
from murmur_voice import session as session_module
from murmur_voice.config import (
    VoiceConfig,
    load_config,
    load_vocabulary,
)


class _InteractiveInput:
    @staticmethod
    def isatty():
        return True


class _VocabularyInput(io.StringIO):
    def isatty(self):
        return True


def test_configure_prompts_twice_without_echoing_or_accepting_key_on_argv(
    tmp_path, monkeypatch, capsys
):
    destination = tmp_path / "private" / "voice.json"
    answers = iter(("TOP-SECRET-TEST-KEY", "TOP-SECRET-TEST-KEY"))
    prompts = []
    monkeypatch.setattr(cli.sys, "stdin", _InteractiveInput())
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda prompt: (prompts.append(prompt), next(answers))[1],
    )

    assert cli._configure(destination) == 0

    output = capsys.readouterr()
    assert len(prompts) == 2
    assert "TOP-SECRET-TEST-KEY" not in output.out + output.err
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert load_config(destination).api_key == "TOP-SECRET-TEST-KEY"
    assert "api-key" not in cli.build_parser().format_help()


def test_verbose_daemon_keeps_websocket_header_logging_disabled(tmp_path, monkeypatch):
    configured = []

    class FailingRuntime:
        def __init__(self, **kwargs):
            del kwargs

        @staticmethod
        def validate():
            raise cli.ConfigError("stop")

    monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(
        adaptive_runtime_module, "AdaptiveCorrectionRuntime", FailingRuntime
    )
    monkeypatch.setattr(cli, "_restore_engine", lambda path: 0)
    websocket_logger = logging.getLogger("websockets")
    old_level = websocket_logger.level
    monkeypatch.setattr(websocket_logger, "setLevel", configured.append)

    assert cli._run(tmp_path / "voice.json", None, True) == 2
    assert configured == [logging.WARNING]
    websocket_logger.level = old_level


def test_restore_engine_subcommand_uses_only_private_state_path(
    tmp_path, monkeypatch, capsys
):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    state_path = runtime / "murmur-ime" / "previous-ibus-engine"
    seen = []
    monkeypatch.setattr(
        cli,
        "restore_saved_engine",
        lambda state: seen.append(state.path) or True,
    )

    assert cli.main(["restore-engine", "--state", str(state_path)]) == 0
    assert seen == [state_path]
    assert capsys.readouterr().out == ""


def test_run_restores_crash_state_before_loading_configuration(tmp_path, monkeypatch):
    order = []

    class FailingRuntime:
        def __init__(self, **kwargs):
            del kwargs

        @staticmethod
        def validate():
            order.append("config")
            raise cli.ConfigError("stop")

    monkeypatch.setattr(
        cli,
        "_restore_engine",
        lambda path: order.append("restore") or 0,
    )
    monkeypatch.setattr(
        adaptive_runtime_module,
        "AdaptiveCorrectionRuntime",
        FailingRuntime,
    )

    assert cli._run(tmp_path / "voice.json", None, False) == 2
    assert order == ["restore", "config"]


def test_interactive_vocabulary_is_line_based_private_and_not_echoed(
    tmp_path, monkeypatch, capsys
):
    destination = tmp_path / "private" / "vocabulary.json"
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        _VocabularyInput("  PrivateName  \nprivatename\n专业词\n\n"),
    )

    assert cli._configure_vocabulary(destination, None) == 0

    output = capsys.readouterr()
    assert "PrivateName" not in output.out + output.err
    assert "专业词" not in output.out + output.err
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert load_vocabulary(destination) == ("PrivateName", "专业词")


def test_vocabulary_file_import_needs_no_tty_and_never_prints_terms(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "terms.txt"
    source.write_text("PrivateName\n专业词\n", encoding="utf-8")
    source.chmod(0o600)
    destination = tmp_path / "private" / "vocabulary.json"
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO())

    assert cli._configure_vocabulary(destination, source) == 0

    output = capsys.readouterr()
    assert "PrivateName" not in output.out + output.err
    assert "专业词" not in output.out + output.err
    assert load_vocabulary(destination) == ("PrivateName", "专业词")


def test_interactive_vocabulary_rejects_non_tty(tmp_path, monkeypatch, capsys):
    destination = tmp_path / "vocabulary.json"
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("PrivateName\n"))

    assert cli._configure_vocabulary(destination, None) == 2

    output = capsys.readouterr()
    assert "PrivateName" not in output.out + output.err
    assert not destination.exists()


def test_vocabulary_terms_cannot_be_passed_on_command_line():
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["vocabulary", "--term", "PrivateName"])


def test_run_parser_accepts_only_a_corrections_file_path_not_pair_values(tmp_path):
    corrections_path = tmp_path / "corrections.json"
    adaptive_path = tmp_path / "adaptive-corrections.json"
    parser = cli.build_parser()

    options = parser.parse_args(
        [
            "run",
            "--corrections",
            str(corrections_path),
            "--adaptive-corrections",
            str(adaptive_path),
        ]
    )

    assert options.corrections == corrections_path
    assert options.adaptive_corrections == adaptive_path
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--wrong", "private wrong form"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--canonical", "private canonical form"])


def test_run_wires_per_dictation_hot_reload_and_adaptive_observer(
    tmp_path, monkeypatch
):
    vocabulary_path = tmp_path / "vocabulary.json"
    corrections_path = tmp_path / "corrections.json"
    adaptive_path = tmp_path / "adaptive-corrections.json"
    captured = []
    runtime_arguments = []

    class FakeRuntime:
        def __init__(self, **kwargs):
            runtime_arguments.append(kwargs)

        @staticmethod
        def validate():
            return VoiceConfig("test-key")

        @staticmethod
        def create_asr_client():
            return object()

        @staticmethod
        def observe(snapshot):
            del snapshot
            return False

    class FakeSession:
        def __init__(self, config, **kwargs):
            captured.append((config, kwargs))

    class FakeServer:
        def __init__(self, session, socket_path):
            del session, socket_path

        @staticmethod
        def serve_forever(signal_commands):
            del signal_commands

    monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_restore_engine", lambda path: 0)
    monkeypatch.setattr(
        adaptive_runtime_module,
        "AdaptiveCorrectionRuntime",
        FakeRuntime,
    )
    monkeypatch.setattr(session_module, "VoiceSession", FakeSession)
    monkeypatch.setattr(cli, "ControlServer", FakeServer)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)

    assert (
        cli._run(
            tmp_path / "voice.json",
            None,
            False,
            vocabulary_path=vocabulary_path,
            corrections_path=corrections_path,
            adaptive_corrections_path=adaptive_path,
        )
        == 0
    )

    assert runtime_arguments == [
        {
            "config_path": tmp_path / "voice.json",
            "vocabulary_path": vocabulary_path,
            "corrections_path": corrections_path,
            "adaptive_path": adaptive_path,
        }
    ]
    assert len(captured) == 1
    config, options = captured[0]
    assert config.api_key == "test-key"
    assert options["asr_client_factory"] is FakeRuntime.create_asr_client
    assert options["observation_handler"] is FakeRuntime.observe
