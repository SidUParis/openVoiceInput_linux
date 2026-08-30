from __future__ import annotations

import io
import json
import logging
import stat

import pytest

from murmur_voice import cli
from murmur_voice import adaptive_runtime as adaptive_runtime_module
from murmur_voice import session as session_module
from murmur_voice.audio import AudioCapture, MicrophonePolicyError
from murmur_voice.adaptive_runtime import save_adaptive_ledger
from murmur_voice.adaptive_store import AdaptiveLedger, record_evidence
from murmur_voice.config import (
    VoiceConfig,
    load_config,
    load_vocabulary,
)
from murmur_voice.microphone_policy import save_microphone_policy_config


class _InteractiveInput:
    @staticmethod
    def isatty():
        return True


class _VocabularyInput(io.StringIO):
    def isatty(self):
        return True


class _AdaptivePairInput(io.StringIO):
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


def test_configure_can_select_qwen_without_putting_key_on_argv(tmp_path, monkeypatch):
    destination = tmp_path / "private" / "voice.json"
    answers = iter(("QWEN-PRIVATE-KEY", "QWEN-PRIVATE-KEY"))
    monkeypatch.setattr(cli.sys, "stdin", _InteractiveInput())
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))

    assert cli._configure(destination, "qwen") == 0

    config = load_config(destination)
    assert config.provider == "qwen"
    assert config.model == "qwen-audio-3.0-asr-flash-streaming"
    assert "QWEN-PRIVATE-KEY" not in cli.build_parser().format_help()


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


def test_adaptive_status_is_content_free_and_confirm_activates_candidate(
    tmp_path, capsys, monkeypatch
):
    adaptive = tmp_path / "private" / "adaptive-corrections.json"
    corrections = tmp_path / "private" / "corrections.json"
    ledger = record_evidence(
        AdaptiveLedger(),
        "private wrong",
        "private canonical",
        state="candidate",
        category="recognition",
        evidence="medium",
    )
    save_adaptive_ledger(ledger, adaptive)

    monkeypatch.setattr(
        cli.sys,
        "stdin",
        _AdaptivePairInput("private wrong\nprivate canonical\n"),
    )
    assert cli.main(["adaptive-status", "--adaptive-corrections", str(adaptive)]) == 0
    status_output = capsys.readouterr().out
    status = json.loads(status_output)
    assert status["statistics"]["candidate"] == 1
    assert "private wrong" not in status_output
    assert "private canonical" not in status_output

    assert (
        cli.main(
            [
                "adaptive-confirm",
                "--adaptive-corrections",
                str(adaptive),
                "--corrections",
                str(corrections),
            ]
        )
        == 0
    )
    confirmation_output = capsys.readouterr().out
    assert json.loads(confirmation_output)["activated_count"] == 1
    assert "private wrong" not in confirmation_output
    assert "private canonical" not in confirmation_output


def test_adaptive_confirmation_text_cannot_be_passed_on_command_line():
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "adaptive-confirm",
                "--wrong",
                "private wrong",
                "--canonical",
                "private canonical",
            ]
        )


def test_run_parser_accepts_only_a_corrections_file_path_not_pair_values(tmp_path):
    corrections_path = tmp_path / "corrections.json"
    adaptive_path = tmp_path / "adaptive-corrections.json"
    data_collection_path = tmp_path / "data-collection.json"
    microphone_priority_path = tmp_path / "microphone-priority.json"
    interaction_path = tmp_path / "interaction.json"
    parser = cli.build_parser()

    options = parser.parse_args(
        [
            "run",
            "--corrections",
            str(corrections_path),
            "--adaptive-corrections",
            str(adaptive_path),
            "--data-collection",
            str(data_collection_path),
            "--microphone-priority",
            str(microphone_priority_path),
            "--interaction",
            str(interaction_path),
        ]
    )

    assert options.corrections == corrections_path
    assert options.adaptive_corrections == adaptive_path
    assert options.data_collection == data_collection_path
    assert options.microphone_priority == microphone_priority_path
    assert options.interaction == interaction_path
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--wrong", "private wrong form"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--canonical", "private canonical form"])


def test_microphone_priority_resolver_hot_loads_one_policy_per_prepare(tmp_path):
    path = tmp_path / "private" / "microphone-priority.json"
    observed = []

    def resolve(*, microphone_policy):
        observed.append(microphone_policy.priority)
        return "offline-device"

    resolver = cli._MicrophonePriorityResolver(path, resolver=resolve)
    resolver.validate()
    assert resolver() == "offline-device"

    updated = ("headset", "dji", "external", "built-in")
    save_microphone_policy_config(updated, path)
    resolver.validate()
    assert resolver() == "offline-device"

    assert observed == [
        ("dji", "headset", "external", "built-in"),
        updated,
    ]


def test_microphone_priority_resolver_rejects_invalid_file_before_resolution(
    tmp_path,
):
    path = tmp_path / "private" / "microphone-priority.json"
    path.parent.mkdir(mode=0o700)
    path.write_text("not-json\n", encoding="utf-8")
    path.chmod(0o600)

    def must_not_resolve(**kwargs):
        raise AssertionError(f"audio resolver must not run: {kwargs}")

    resolver = cli._MicrophonePriorityResolver(path, resolver=must_not_resolve)

    with pytest.raises(MicrophonePolicyError):
        resolver.validate()
    with pytest.raises(MicrophonePolicyError):
        resolver()


def test_run_wires_per_dictation_hot_reload_and_adaptive_observer(
    tmp_path, monkeypatch
):
    vocabulary_path = tmp_path / "vocabulary.json"
    corrections_path = tmp_path / "corrections.json"
    adaptive_path = tmp_path / "adaptive-corrections.json"
    data_collection_path = tmp_path / "data-collection.json"
    microphone_priority_path = tmp_path / "microphone-priority.json"
    captured = []
    runtime_arguments = []
    data_runtime_arguments = []
    data_runtime_closes = []

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

    class FakeDataCollectionRuntime:
        def __init__(self, *, config_path):
            data_runtime_arguments.append(config_path)

        @staticmethod
        def begin(utterance_id):
            del utterance_id
            return None

        @staticmethod
        def status_code():
            return "none"

        @staticmethod
        def close(*, timeout):
            data_runtime_closes.append(timeout)
            return True

    class FakeServer:
        def __init__(self, session, socket_path, *, interaction=None):
            del session, socket_path, interaction

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
    monkeypatch.setattr(cli, "DataCollectionRuntime", FakeDataCollectionRuntime)
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
            data_collection_path=data_collection_path,
            microphone_policy_path=microphone_priority_path,
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
    assert data_runtime_arguments == [data_collection_path]
    assert data_runtime_closes == [cli.DATA_COLLECTION_CLOSE_TIMEOUT_SECONDS]
    config, options = captured[0]
    assert config.api_key == "test-key"
    assert options["asr_client_factory"] is FakeRuntime.create_asr_client
    assert isinstance(options["audio_capture"], AudioCapture)
    assert callable(options["microphone_policy_validator"])
    assert options["observation_handler"] is FakeRuntime.observe
    assert options["data_collection_factory"] is FakeDataCollectionRuntime.begin
    assert (
        options["data_collection_status_reader"]
        is FakeDataCollectionRuntime.status_code
    )


def test_invalid_optional_data_collection_config_does_not_block_daemon_start(
    tmp_path, monkeypatch
):
    data_collection_path = tmp_path / "data-collection.json"
    data_collection_path.write_text("not-json\n", encoding="utf-8")
    data_collection_path.chmod(0o600)
    served = []

    class FakeRuntime:
        def __init__(self, **kwargs):
            del kwargs

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
            del config, kwargs

    class FakeServer:
        def __init__(self, session, socket_path, *, interaction=None):
            del session, socket_path, interaction

        @staticmethod
        def serve_forever(signal_commands):
            del signal_commands
            served.append(True)

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
            data_collection_path=data_collection_path,
        )
        == 0
    )
    assert served == [True]


def test_optional_data_writer_start_failure_does_not_block_daemon(
    tmp_path, monkeypatch
):
    captured = []

    class FakeRuntime:
        def __init__(self, **kwargs):
            del kwargs

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

    class FailedDataRuntime:
        def __init__(self, **kwargs):
            del kwargs
            raise RuntimeError("optional writer unavailable")

    class FakeSession:
        def __init__(self, config, **kwargs):
            del config
            captured.append(kwargs)

    class FakeServer:
        def __init__(self, session, socket_path, *, interaction=None):
            del session, socket_path, interaction

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
    monkeypatch.setattr(cli, "DataCollectionRuntime", FailedDataRuntime)
    monkeypatch.setattr(session_module, "VoiceSession", FakeSession)
    monkeypatch.setattr(cli, "ControlServer", FakeServer)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)

    assert cli._run(tmp_path / "voice.json", None, False) == 0
    assert captured[0]["data_collection_factory"] is None
    assert captured[0]["data_collection_status_reader"] is None
