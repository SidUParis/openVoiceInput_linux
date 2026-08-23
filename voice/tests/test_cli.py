from __future__ import annotations

import io
import logging
import stat

import pytest

from murmur_voice import cli
from murmur_voice import session as session_module
from murmur_voice.config import VoiceConfig, load_config, load_vocabulary


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
    monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(
        cli, "load_config", lambda path: (_ for _ in ()).throw(cli.ConfigError("stop"))
    )
    websocket_logger = logging.getLogger("websockets")
    old_level = websocket_logger.level
    monkeypatch.setattr(websocket_logger, "setLevel", configured.append)

    assert cli._run(tmp_path / "voice.json", None, True) == 2
    assert configured == [logging.WARNING]
    websocket_logger.level = old_level


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


def test_run_loads_vocabulary_once_when_daemon_starts(tmp_path, monkeypatch):
    vocabulary_path = tmp_path / "vocabulary.json"
    captured = []

    class FakeSession:
        def __init__(self, config):
            captured.append(config)

    class FakeServer:
        def __init__(self, session, socket_path):
            del session, socket_path

        @staticmethod
        def serve_forever(signal_commands):
            del signal_commands

    monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(cli, "load_config", lambda path: VoiceConfig("test-key"))
    monkeypatch.setattr(cli, "load_vocabulary", lambda path: ("PrivateName", "专业词"))
    monkeypatch.setattr(session_module, "VoiceSession", FakeSession)
    monkeypatch.setattr(cli, "ControlServer", FakeServer)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)

    assert (
        cli._run(
            tmp_path / "voice.json",
            None,
            False,
            vocabulary_path=vocabulary_path,
        )
        == 0
    )

    assert len(captured) == 1
    assert captured[0].hotwords == ("PrivateName", "专业词")
    assert captured[0].provider_settings()["hotwords"] == (
        "PrivateName",
        "专业词",
    )
