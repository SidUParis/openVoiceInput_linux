"""Command-line entry point for the self-contained voice daemon."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import queue
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from .audio import AudioCapture, MicrophonePolicyError, resolve_input_device

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
from .data_collection import (
    DataCollectionRuntime,
    default_data_collection_config_path,
)
from .engine_restore import EngineRestoreState, RestoreError, restore_saved_engine
from .microphone_policy import (
    MicrophonePolicyConfig,
    default_microphone_policy_config_path,
    load_microphone_policy_config,
)


DATA_COLLECTION_CLOSE_TIMEOUT_SECONDS = 10.0


class _MicrophonePriorityResolver:
    """Load one private policy snapshot before external start mutations."""

    def __init__(
        self,
        path: Path,
        *,
        resolver: Callable[..., Any] = resolve_input_device,
    ) -> None:
        self._path = path
        self._resolver = resolver
        self._lock = threading.Lock()
        self._prepared_policy: MicrophonePolicyConfig | None = None

    def validate(self) -> None:
        """Stage the policy for one prepare, without touching audio state."""

        policy = self._load()
        with self._lock:
            self._prepared_policy = policy

    def __call__(self) -> Any:
        """Resolve with the staged policy, or load for a direct prepare call."""

        with self._lock:
            policy = self._prepared_policy
            self._prepared_policy = None
        if policy is None:
            policy = self._load()
        return self._resolver(microphone_policy=policy)

    def _load(self) -> MicrophonePolicyConfig:
        try:
            return load_microphone_policy_config(self._path)
        except ConfigError as error:
            raise MicrophonePolicyError(
                "microphone priority configuration is invalid"
            ) from error


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
    run_parser.add_argument(
        "--data-collection",
        type=Path,
        default=default_data_collection_config_path(),
    )
    run_parser.add_argument(
        "--microphone-priority",
        type=Path,
        default=default_microphone_policy_config_path(),
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
            data_collection_path=options.data_collection,
            microphone_policy_path=options.microphone_priority,
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
    data_collection_path: Path | None = None,
    microphone_policy_path: Path | None = None,
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
    data_collection_runtime: DataCollectionRuntime | None = None
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
        try:
            # The optional configuration is deliberately not validated here:
            # missing or malformed collection consent must never keep the
            # voice service from starting or ordinary dictation from working.
            data_collection_runtime = DataCollectionRuntime(
                config_path=(
                    data_collection_path or default_data_collection_config_path()
                )
            )
        except (OSError, RuntimeError):
            logger = logging.getLogger(__name__)
            logger.error("Optional local data collection is unavailable")
        # Delay GI, sounddevice, and provider imports until run. Status and
        # configure remain useful on systems missing optional runtime pieces.
        from .session import VoiceSession

        microphone_resolver = _MicrophonePriorityResolver(
            microphone_policy_path or default_microphone_policy_config_path()
        )

        session = VoiceSession(
            config,
            asr_client_factory=runtime.create_asr_client,
            audio_capture=AudioCapture(input_resolver=microphone_resolver),
            microphone_policy_validator=microphone_resolver.validate,
            observation_handler=runtime.observe,
            data_collection_factory=(
                data_collection_runtime.begin
                if data_collection_runtime is not None
                else None
            ),
            data_collection_status_reader=(
                data_collection_runtime.status_code
                if data_collection_runtime is not None
                else None
            ),
        )
        server = ControlServer(session, socket_path)
    except (ConfigError, ControlError, ImportError, RuntimeError) as error:
        _close_data_collection_runtime(data_collection_runtime)
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
    finally:
        _close_data_collection_runtime(data_collection_runtime)
    return 0


def _close_data_collection_runtime(
    runtime: DataCollectionRuntime | None,
) -> None:
    if runtime is None:
        return
    try:
        if not runtime.close(timeout=DATA_COLLECTION_CLOSE_TIMEOUT_SECONDS):
            logging.getLogger(__name__).warning(
                "Optional local data writer did not finish before shutdown"
            )
    except Exception:
        logging.getLogger(__name__).error(
            "Optional local data writer could not be closed cleanly"
        )


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
