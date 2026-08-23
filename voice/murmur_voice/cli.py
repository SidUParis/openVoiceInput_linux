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

from .config import ConfigError, default_config_path, load_config, save_api_key
from .control import (
    KNOWN_COMMANDS,
    ControlError,
    ControlServer,
    request_command,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="murmur-voice-daemon",
        description="Volcengine voice input through native IBus preedit",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run in the foreground")
    run_parser.add_argument("--config", type=Path, default=default_config_path())
    run_parser.add_argument("--socket", type=Path)
    run_parser.add_argument("--verbose", action="store_true")

    configure_parser = subparsers.add_parser(
        "configure", help="securely prompt for and store the API key"
    )
    configure_parser.add_argument("--config", type=Path, default=default_config_path())

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
    if options.command == "run":
        return _run(options.config, options.socket, options.verbose)
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


def _run(config_path: Path, socket_path: Path | None, verbose: bool) -> int:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # websockets DEBUG output can include HTTP request headers.  Keep it at
    # WARNING even when our own lifecycle logging is verbose so X-Api-Key is
    # never exposed through dependency logs.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        config = load_config(config_path)
        # Delay GI, sounddevice, and provider imports until run. Status and
        # configure remain useful on systems missing optional runtime pieces.
        from .session import VoiceSession

        session = VoiceSession(config)
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
