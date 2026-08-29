#!/usr/bin/env python3
"""Run a real, local-only IBus preedit smoke test in an isolated X session."""

from __future__ import annotations

import argparse
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
WINDOW_TITLE = "Open Voice Input Linux Isolated Preedit Probe"
FINAL_TEXT = "这是一个在光标处实时显示并最终提交的语音输入演示。"
OBSERVATION_WRONG = "Ostro"
OBSERVATION_CANONICAL = "Austral"
EXPECTED_COMMITTED_TEXT = FINAL_TEXT + OBSERVATION_CANONICAL
PARTIAL_COUNT = 6
SYSTEM_PATH = "/usr/bin:/bin"
MINIMUM_RENDERED_PIXEL_CHANGE = 500
COMMAND_TIMEOUT_SECONDS = 8
SESSION_TIMEOUT_SECONDS = 30
TERMINATION_GRACE_SECONDS = 3


class SmokeFailure(RuntimeError):
    """Raised when the isolated smoke contract is not satisfied."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify real IBus caret-local partial/final behavior with fixed synthetic "
            "text. The test uses a temporary HOME, private D-Bus/IBus and Xvfb; it "
            "does not use a microphone, provider key or network service."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="new directory for private logs and partial/final screenshots",
    )
    parser.add_argument("--session-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--probe-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--select-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--display", help=argparse.SUPPRESS)
    parser.add_argument("--state-file", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def system_command(name: str) -> str:
    command = shutil.which(name, path=SYSTEM_PATH)
    if command is None:
        raise SmokeFailure(f"missing smoke-test command: {name}")
    return command


def require_commands(*names: str) -> None:
    missing = [name for name in names if shutil.which(name, path=SYSTEM_PATH) is None]
    if missing:
        raise SmokeFailure(f"missing smoke-test commands: {', '.join(missing)}")


def prepare_output_dir(requested: Path | None) -> Path:
    if requested is None:
        return Path(tempfile.mkdtemp(prefix="openvoice-isolated-preedit."))

    path = requested.expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise SmokeFailure(f"output path already exists: {path}")
    path.mkdir(mode=0o700, parents=False)
    return path


def terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    return True


def terminate_process_group(process: subprocess.Popen[bytes] | None) -> None:
    """Terminate and reap a process plus every child in its private group."""

    if process is None:
        return
    group_id = process.pid
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
        return

    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        process.poll()
        if not _process_group_exists(group_id):
            break
        time.sleep(0.05)
    else:
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=TERMINATION_GRACE_SECONDS)


def wait_for(
    predicate: object,
    *,
    description: str,
    timeout: float = 8.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        time.sleep(0.1)
    raise SmokeFailure(f"timed out waiting for {description}")


def read_display_number(read_fd: int, xvfb: subprocess.Popen[bytes]) -> str:
    ready, _, _ = select.select([read_fd], [], [], 8)
    if not ready:
        raise SmokeFailure("Xvfb did not report a display number")
    raw = os.read(read_fd, 64).strip()
    if xvfb.poll() is not None:
        raise SmokeFailure("Xvfb exited before the smoke session started")
    if not raw.isdigit():
        raise SmokeFailure("Xvfb returned an invalid display number")
    return f":{raw.decode('ascii')}"


def run_parent(args: argparse.Namespace) -> int:
    require_commands("Xvfb", "dbus-run-session")
    output = prepare_output_dir(args.output_dir)
    read_fd, write_fd = os.pipe()
    xvfb: subprocess.Popen[bytes] | None = None
    session: subprocess.Popen[bytes] | None = None

    try:
        with (output / "xvfb.log").open("wb") as xvfb_log:
            xvfb = subprocess.Popen(
                [
                    system_command("Xvfb"),
                    "-displayfd",
                    str(write_fd),
                    "-screen",
                    "0",
                    "1024x768x24",
                    "-nolisten",
                    "tcp",
                ],
                pass_fds=(write_fd,),
                env={"PATH": SYSTEM_PATH, "LANG": "C.UTF-8"},
                stdout=xvfb_log,
                stderr=subprocess.STDOUT,
            )
        os.close(write_fd)
        write_fd = -1
        display = read_display_number(read_fd, xvfb)

        command = [
            system_command("dbus-run-session"),
            "--",
            sys.executable,
            "-I",
            str(SCRIPT),
            "--session-child",
            "--display",
            display,
            "--output-dir",
            str(output),
        ]
        with (output / "session.log").open("wb") as session_log:
            session_environment = isolated_environment(output, display)
            session_environment.pop("DBUS_SESSION_BUS_ADDRESS", None)
            session_environment.pop("AT_SPI_BUS_ADDRESS", None)
            session = subprocess.Popen(
                command,
                cwd=REPOSITORY,
                env=session_environment,
                stdout=session_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                session_returncode = session.wait(timeout=SESSION_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                raise SmokeFailure("isolated preedit session timed out") from error
            finally:
                terminate_process_group(session)
                session = None
        if session_returncode != 0:
            raise SmokeFailure(
                f"isolated preedit session failed; inspect {output / 'session.log'}"
            )

        engine_log = (output / "engine.log").read_text(encoding="utf-8")
        if engine_log.count("Accepted voice partial revision=") != PARTIAL_COUNT:
            raise SmokeFailure("engine did not accept the exact partial sequence")
        if engine_log.count("Committed voice final revision=") != 2:
            raise SmokeFailure("engine did not commit the two exact final results")
        if (output / "committed.txt").read_text(encoding="utf-8") != (
            EXPECTED_COMMITTED_TEXT
        ):
            raise SmokeFailure("probe did not receive the corrected final text")
        if (output / "observation.txt").read_text(encoding="ascii") != "accepted\n":
            raise SmokeFailure("engine did not return a post-commit observation")
        for screenshot in (
            output / "baseline.png",
            output / "partial.png",
            output / "final.png",
        ):
            if not screenshot.is_file() or screenshot.stat().st_size < 1_000:
                raise SmokeFailure(f"missing smoke screenshot: {screenshot}")

        print("isolated real-IBus preedit smoke passed")
        print(f"partial screenshot: {output / 'partial.png'}")
        print(f"final screenshot:   {output / 'final.png'}")
        print("partial text was visible while committed state remained empty")
        print("the visual preedit sequence committed its final exactly once")
        print("one same-focus post-commit edit was observed exactly once")
        print("microphone/provider/key/network service: not used")
        return 0
    except (OSError, subprocess.SubprocessError, SmokeFailure) as error:
        print(f"isolated preedit smoke failed: {error}", file=sys.stderr)
        print(f"private diagnostics: {output}", file=sys.stderr)
        return 1
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)
        terminate_process_group(session)
        terminate(xvfb)


def isolated_environment(output: Path, display: str) -> dict[str, str]:
    home = output / "home"
    runtime = output / "runtime"
    for directory in (home, runtime):
        directory.mkdir(mode=0o700, exist_ok=True)
    environment = {
        "PATH": SYSTEM_PATH,
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "DISPLAY": display,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_RUNTIME_DIR": str(runtime),
        "IBUS_ADDRESS": f"unix:path={output / 'ibus.sock'}",
        "GTK_IM_MODULE": "ibus",
        "XMODIFIERS": "@im=ibus",
        "GDK_BACKEND": "x11",
        "GSETTINGS_BACKEND": "memory",
        "GIO_USE_VFS": "local",
        "GSK_RENDERER": "cairo",
        "LIBGL_ALWAYS_SOFTWARE": "1",
        "NO_AT_BRIDGE": "1",
        "OPENVOICE_ENTRY_STATE": str(output / "committed.txt"),
    }
    for inherited in ("DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS"):
        if inherited in os.environ:
            environment[inherited] = os.environ[inherited]
    for directory in (
        Path(environment["XDG_CONFIG_HOME"]),
        Path(environment["XDG_CACHE_HOME"]),
        Path(environment["XDG_DATA_HOME"]),
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return environment


def select_isolated_engine() -> None:
    import gi

    gi.require_version("IBus", "1.0")
    from gi.repository import IBus

    IBus.init()
    bus = IBus.Bus()
    if not bus.is_connected() or not bus.set_global_engine("murmur-voice"):
        raise SmokeFailure("could not select the isolated murmur-voice engine")


def find_probe_window(environment: dict[str, str]) -> str | None:
    completed = subprocess.run(
        [
            system_command("xdotool"),
            "search",
            "--onlyvisible",
            "--name",
            "Isolated Preedit Probe",
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return next(
        (line for line in completed.stdout.splitlines() if line.isdigit()), None
    )


def take_screenshot(environment: dict[str, str], destination: Path) -> None:
    subprocess.run(
        [
            system_command("import"),
            "-display",
            environment["DISPLAY"],
            "-window",
            "root",
            destination,
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=8,
        check=True,
    )


def rendered_pixel_difference(
    environment: dict[str, str], first: Path, second: Path
) -> int:
    completed = subprocess.run(
        [system_command("compare"), "-metric", "AE", first, second, "null:"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=8,
        check=False,
    )
    if completed.returncode not in (0, 1):
        raise SmokeFailure("ImageMagick could not compare smoke screenshots")
    try:
        return int(float(completed.stdout.strip()))
    except ValueError as error:
        raise SmokeFailure("ImageMagick returned an invalid pixel metric") from error


def run_session_child(args: argparse.Namespace) -> int:
    if args.output_dir is None or args.display is None:
        return 2
    output = args.output_dir.absolute()
    require_commands("ibus-daemon", "xdotool", "xwininfo", "import", "compare")
    environment = isolated_environment(output, args.display)
    ibus: subprocess.Popen[bytes] | None = None
    engine: subprocess.Popen[bytes] | None = None
    probe: subprocess.Popen[bytes] | None = None
    sender: subprocess.Popen[bytes] | None = None
    observation_sender: subprocess.Popen[bytes] | None = None

    try:
        with (output / "ibus.log").open("wb") as ibus_log:
            ibus = subprocess.Popen(
                [
                    system_command("ibus-daemon"),
                    "--replace",
                    "--single",
                    "--xim",
                    "--panel",
                    "disable",
                    "--config",
                    "disable",
                    "--emoji-extension",
                    "disable",
                    "--address",
                    environment["IBUS_ADDRESS"],
                ],
                env=environment,
                stdout=ibus_log,
                stderr=subprocess.STDOUT,
            )
        wait_for(
            lambda: (output / "ibus.sock").is_socket(),
            description="private IBus socket",
        )

        with (output / "engine.log").open("wb") as engine_log:
            engine = subprocess.Popen(
                [str(REPOSITORY / "engine/murmur-ime-engine"), "--verbose"],
                cwd=REPOSITORY,
                env=environment,
                stdout=engine_log,
                stderr=subprocess.STDOUT,
            )
        wait_for(
            lambda: (
                (output / "engine.log").is_file()
                and "Registered dynamic IBus engine"
                in (output / "engine.log").read_text(encoding="utf-8")
            ),
            description="dynamic IBus engine registration",
        )
        with (output / "select-engine.log").open("wb") as select_log:
            subprocess.run(
                [sys.executable, "-I", str(SCRIPT), "--select-child"],
                env=environment,
                stdout=select_log,
                stderr=subprocess.STDOUT,
                timeout=8,
                check=True,
            )

        with (output / "probe.log").open("wb") as probe_log:
            probe = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(SCRIPT),
                    "--probe-child",
                    "--state-file",
                    str(output / "committed.txt"),
                ],
                env=environment,
                stdout=probe_log,
                stderr=subprocess.STDOUT,
            )
        window: str | None = None

        def window_is_ready() -> bool:
            nonlocal window
            window = find_probe_window(environment)
            return window is not None

        wait_for(window_is_ready, description="focused GTK probe window")
        if window is None:
            raise SmokeFailure("focused GTK probe window disappeared")
        with (output / "windows.txt").open("wb") as windows_log:
            subprocess.run(
                [system_command("xwininfo"), "-root", "-tree"],
                env=environment,
                stdout=windows_log,
                stderr=subprocess.STDOUT,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=True,
            )
        subprocess.run(
            [system_command("xdotool"), "windowfocus", "--sync", window],
            env=environment,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=True,
        )
        subprocess.run(
            [
                system_command("xdotool"),
                "mousemove",
                "--window",
                window,
                "320",
                "75",
                "click",
                "1",
            ],
            env=environment,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=True,
        )
        time.sleep(0.5)
        take_screenshot(environment, output / "baseline.png")

        with (output / "sender.log").open("wb") as sender_log:
            sender = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(REPOSITORY / "scripts/send_preedit_demo.py"),
                    "--delay",
                    "0.9",
                    "--utterance-id",
                    "isolated-preedit-smoke",
                ],
                cwd=REPOSITORY,
                env=environment,
                stdout=sender_log,
                stderr=subprocess.STDOUT,
            )
        engine_log_path = output / "engine.log"

        def partial_is_active() -> bool:
            engine_progress = engine_log_path.read_text(encoding="utf-8")
            return (
                "Accepted voice partial revision=" in engine_progress
                and "Committed voice final revision=" not in engine_progress
            )

        wait_for(partial_is_active, description="first active preedit partial")
        time.sleep(0.2)
        state = output / "committed.txt"
        if state.exists() and state.stat().st_size:
            raise SmokeFailure("a partial result was incorrectly committed")
        if "Committed voice final revision=" in engine_log_path.read_text(
            encoding="utf-8"
        ):
            raise SmokeFailure("synthetic final arrived before the partial screenshot")
        take_screenshot(environment, output / "partial.png")
        if (
            rendered_pixel_difference(
                environment, output / "baseline.png", output / "partial.png"
            )
            < MINIMUM_RENDERED_PIXEL_CHANGE
        ):
            raise SmokeFailure("partial preedit did not visibly change the GTK entry")

        if sender.wait(timeout=10) != 0:
            raise SmokeFailure("synthetic preedit sender was rejected")
        sender = None
        wait_for(
            lambda: state.exists() and state.read_text(encoding="utf-8") == FINAL_TEXT,
            description="exact final commit",
        )
        take_screenshot(environment, output / "final.png")
        if (
            rendered_pixel_difference(
                environment, output / "partial.png", output / "final.png"
            )
            < MINIMUM_RENDERED_PIXEL_CHANGE
        ):
            raise SmokeFailure("final commit did not visibly change the GTK entry")

        observation_trigger = output / "observation-trigger"
        with (output / "observation-sender.log").open("wb") as sender_log:
            observation_sender = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(REPOSITORY / "scripts/send_preedit_demo.py"),
                    "--no-partials",
                    "--delay",
                    "0",
                    "--utterance-id",
                    "isolated-observation-smoke",
                    "--final-text",
                    OBSERVATION_WRONG,
                    "--observation-trigger",
                    str(observation_trigger),
                    "--observation-output",
                    str(output / "observation.txt"),
                ],
                cwd=REPOSITORY,
                env=environment,
                stdout=sender_log,
                stderr=subprocess.STDOUT,
            )
        wait_for(
            lambda: (
                state.exists()
                and state.read_text(encoding="utf-8") == FINAL_TEXT + OBSERVATION_WRONG
            ),
            description="second synthetic final commit",
        )
        subprocess.run(
            [
                system_command("xdotool"),
                "key",
                "--window",
                window,
                "BackSpace",
                "BackSpace",
                "BackSpace",
                "BackSpace",
                "BackSpace",
            ],
            env=environment,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=True,
        )
        subprocess.run(
            [
                system_command("xdotool"),
                "type",
                "--window",
                window,
                "--delay",
                "20",
                OBSERVATION_CANONICAL,
            ],
            env=environment,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=True,
        )
        wait_for(
            lambda: state.read_text(encoding="utf-8") == EXPECTED_COMMITTED_TEXT,
            description="same-focus corrected text",
        )
        observation_trigger.write_text("ready\n", encoding="ascii")
        observation_trigger.chmod(0o600)
        if observation_sender.wait(timeout=10) != 0:
            raise SmokeFailure("post-commit observation sender was rejected")
        observation_sender = None
        wait_for(
            lambda: (output / "observation.txt").is_file(),
            description="accepted post-commit observation",
        )
        return 0
    except (OSError, subprocess.SubprocessError, SmokeFailure) as error:
        print(f"session child failed: {error}", file=sys.stderr)
        return 1
    finally:
        for process in (observation_sender, sender, probe, engine, ibus):
            terminate(process)


def run_probe_child(args: argparse.Namespace) -> int:
    if args.state_file is None:
        return 2
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    Gtk.init()
    loop = GLib.MainLoop()
    window = Gtk.Window(title=WINDOW_TITLE)
    window.set_default_size(760, 260)
    page = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=16,
        margin_top=24,
        margin_bottom=24,
        margin_start=24,
        margin_end=24,
    )
    page.append(Gtk.Label(label="Isolated IBus preedit probe", xalign=0))
    entry = Gtk.Entry()
    entry.set_placeholder_text("Partial text should render here before commit")
    page.append(entry)
    committed = Gtk.Label(label="Committed: <empty>", xalign=0)
    page.append(committed)
    window.set_child(page)

    def changed(widget: Gtk.Entry) -> None:
        text = widget.get_text()
        committed.set_text(f"Committed: {text}" if text else "Committed: <empty>")
        args.state_file.write_text(text, encoding="utf-8")

    def close_requested(*_args: object) -> bool:
        loop.quit()
        return False

    def focus_entry() -> bool:
        entry.grab_focus()
        return GLib.SOURCE_REMOVE

    entry.connect("changed", changed)
    window.connect("close-request", close_requested)
    window.present()
    GLib.idle_add(focus_entry)
    loop.run()
    return 0


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    if args.probe_child:
        return run_probe_child(args)
    if args.select_child:
        select_isolated_engine()
        return 0
    if args.session_child:
        return run_session_child(args)
    return run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
