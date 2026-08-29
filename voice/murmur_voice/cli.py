"""Command-line entry point for the self-contained voice daemon."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import queue
import signal
import sys
from pathlib import Path
from typing import Sequence

from .config import (
    MAX_VOCABULARY_ENTRIES,
    MAX_VOCABULARY_TERM_CHARACTERS,
    ConfigError,
    default_adaptive_corrections_path,
    default_config_path,
    default_corrections_path,
    default_vocabulary_path,
    load_vocabulary_import,
    normalize_vocabulary_terms,
    save_api_key,
    save_vocabulary,
)
from .control import (
    KNOWN_COMMANDS,
    ControlError,
    ControlServer,
    request_command,
)
from .engine_restore import EngineRestoreState, RestoreError, restore_saved_engine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="murmur-voice-daemon",
        description="Volcengine voice input through native IBus preedit",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run in the foreground")
    run_parser.add_argument("--config", type=Path, default=default_config_path())
    run_parser.add_argument(
        "--vocabulary", type=Path, default=default_vocabulary_path()
    )
    run_parser.add_argument(
        "--corrections", type=Path, default=default_corrections_path()
    )
    run_parser.add_argument(
        "--adaptive-corrections",
        type=Path,
        default=default_adaptive_corrections_path(),
    )
    run_parser.add_argument("--socket", type=Path)
    run_parser.add_argument("--verbose", action="store_true")

    configure_parser = subparsers.add_parser(
        "configure", help="securely prompt for and store the API key"
    )
    configure_parser.add_argument("--config", type=Path, default=default_config_path())

    vocabulary_parser = subparsers.add_parser(
        "vocabulary", help="replace the optional private personal vocabulary"
    )
    vocabulary_parser.add_argument(
        "--vocabulary", type=Path, default=default_vocabulary_path()
    )
    vocabulary_parser.add_argument(
        "--import-file",
        type=Path,
        help="read a private UTF-8 file containing one term per line",
    )

    restore_parser = subparsers.add_parser(
        "restore-engine",
        help="restore an IBus engine left selected after an interrupted session",
    )
    restore_parser.add_argument(
        "--state",
        type=Path,
        help="override the private runtime restore-state path",
    )

    for command in sorted(KNOWN_COMMANDS):
        command_parser = subparsers.add_parser(
            command, help=f"send {command} to the running daemon"
        )
        command_parser.add_argument("--socket", type=Path)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(arguments)
    if options.command == "configure":
        return _configure(options.config)
    if options.command == "vocabulary":
        return _configure_vocabulary(options.vocabulary, options.import_file)
    if options.command == "restore-engine":
        return _restore_engine(options.state)
    if options.command == "run":
        return _run(
            options.config,
            options.socket,
            options.verbose,
            vocabulary_path=options.vocabulary,
            corrections_path=options.corrections,
            adaptive_corrections_path=options.adaptive_corrections,
        )
    try:
        response = request_command(options.command, options.socket)
    except ControlError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0 if response.get("ok") is True else 1


def _configure(path: Path) -> int:
    if not sys.stdin.isatty():
        print("configure requires an interactive TTY", file=sys.stderr)
        return 2
    first = getpass.getpass("Volcengine API key: ")
    second = getpass.getpass("Confirm API key: ")
    if first != second:
        print("API keys did not match", file=sys.stderr)
        return 2
    try:
        destination = save_api_key(first, path)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"Saved private voice configuration to {destination}")
    return 0


def _configure_vocabulary(path: Path, import_path: Path | None) -> int:
    try:
        if import_path is not None:
            terms = load_vocabulary_import(import_path)
        else:
            if not sys.stdin.isatty():
                print(
                    "vocabulary entry requires an interactive TTY or --import-file",
                    file=sys.stderr,
                )
                return 2
            terms = _read_interactive_vocabulary()
        destination = save_vocabulary(terms, path)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"Saved {len(terms)} private vocabulary entries to {destination}")
    return 0


def _read_interactive_vocabulary() -> tuple[str, ...]:
    print("Enter one personal vocabulary term per line; an empty line saves.")
    terms: list[str] = []
    while True:
        print("term> ", end="", flush=True)
        line = sys.stdin.readline(MAX_VOCABULARY_TERM_CHARACTERS + 2)
        if not line:
            break
        if line.endswith("\n"):
            line = line[:-1]
        if not line.strip():
            break
        terms.append(line)
        if len(terms) > MAX_VOCABULARY_ENTRIES:
            raise ConfigError(
                f"personal vocabulary exceeds {MAX_VOCABULARY_ENTRIES} entries"
            )
    return normalize_vocabulary_terms(terms)


def _run(
    config_path: Path,
    socket_path: Path | None,
    verbose: bool,
    *,
    vocabulary_path: Path | None = None,
    corrections_path: Path | None = None,
    adaptive_corrections_path: Path | None = None,
) -> int:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # websockets DEBUG output can include HTTP request headers.  Keep it at
    # WARNING even when our own lifecycle logging is verbose so X-Api-Key is
    # never exposed through dependency logs.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    if _restore_engine(None) != 0:
        return 1
    try:
        from .adaptive_runtime import AdaptiveCorrectionRuntime

        runtime = AdaptiveCorrectionRuntime(
            config_path=config_path,
            vocabulary_path=vocabulary_path or default_vocabulary_path(),
            corrections_path=corrections_path or default_corrections_path(),
            adaptive_path=(
                adaptive_corrections_path or default_adaptive_corrections_path()
            ),
        )
        config = runtime.validate()
        # Delay GI, sounddevice, and provider imports until run. Status and
        # configure remain useful on systems missing optional runtime pieces.
        from .session import VoiceSession

        session = VoiceSession(
            config,
            asr_client_factory=runtime.create_asr_client,
            observation_handler=runtime.observe,
        )
        server = ControlServer(session, socket_path)
    except (ConfigError, ControlError, ImportError, RuntimeError) as error:
        # Configuration/control errors are authored locally and contain no key.
        print(str(error), file=sys.stderr)
        return 2

    signal_commands: queue.SimpleQueue[str] = queue.SimpleQueue()
    signal_map = {
        signal.SIGUSR1: "start",
        signal.SIGUSR2: "stop",
        signal.SIGHUP: "cancel",
        signal.SIGINT: "shutdown",
        signal.SIGTERM: "shutdown",
    }

    def enqueue_signal(signum, frame) -> None:
        del frame
        signal_commands.put(signal_map[signum])

    for signum in signal_map:
        signal.signal(signum, enqueue_signal)
    try:
        server.serve_forever(signal_commands)
    except ControlError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


def _restore_engine(path: Path | None) -> int:
    try:
        state = EngineRestoreState(path)
    except RestoreError:
        print("private IBus restore state is unavailable", file=sys.stderr)
        return 1
    if not restore_saved_engine(state):
        print("the previous IBus engine could not be restored", file=sys.stderr)
        return 1
    return 0
