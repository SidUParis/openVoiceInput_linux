from __future__ import annotations

import stat
import logging

from murmur_voice import cli
from murmur_voice.config import load_config


class _InteractiveInput:
    @staticmethod
    def isatty():
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
