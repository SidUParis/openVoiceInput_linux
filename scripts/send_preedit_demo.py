#!/usr/bin/env python3
"""Send a deterministic Chinese preedit sequence to Open Voice Input Linux."""

from __future__ import annotations

import argparse
import sys
import time
import uuid

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402


BUS_NAME = "org.murmur.IME.Preedit1"
OBJECT_PATH = "/org/murmur/IME/Preedit1"
INTERFACE = "org.murmur.IME.Preedit1"
CALL_TIMEOUT_MS = 5_000

DEFAULT_PARTIALS = (
    "这",
    "这是",
    "这是一个",
    "这是一个在光标处",
    "这是一个在光标处实时显示",
    "这是一个在光标处实时显示的语音草稿",
)
DEFAULT_FINAL = "这是一个在光标处实时显示并最终提交的语音输入演示。"


class DemoRejected(RuntimeError):
    """Raised when the engine safely rejects a demo event."""


def call_accepted(
    proxy: Gio.DBusProxy,
    method: str,
    parameters: GLib.Variant,
) -> None:
    reply = proxy.call_sync(
        method,
        parameters,
        Gio.DBusCallFlags.NONE,
        CALL_TIMEOUT_MS,
        None,
    )
    (accepted,) = reply.unpack()
    if not accepted:
        raise DemoRejected(
            f"{method} was rejected; keep a text field focused with "
            "Open Voice Input Linux active"
        )


def build_proxy() -> Gio.DBusProxy:
    # All calls intentionally reuse one proxy/session-bus connection. Acquire
    # is bound to that connection's unique D-Bus sender by the engine.
    return Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SESSION,
        Gio.DBusProxyFlags.NONE,
        None,
        BUS_NAME,
        OBJECT_PATH,
        INTERFACE,
        None,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Show deterministic Chinese partial text at the active Open Voice Input "
            "Linux caret, then commit a final sentence. No microphone or network is "
            "used."
        )
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.45,
        help="seconds between partial updates (default: 0.45)",
    )
    parser.add_argument(
        "--final-text",
        default=DEFAULT_FINAL,
        help="text sent by Final after the built-in partial sequence",
    )
    parser.add_argument(
        "--utterance-id",
        default=None,
        help="stable demo id; defaults to a fresh UUID",
    )
    parser.add_argument(
        "--cancel",
        action="store_true",
        help="clear the last partial with Cancel instead of committing Final",
    )
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay must be zero or greater")
    return args


def run_demo(args: argparse.Namespace) -> None:
    proxy = build_proxy()
    utterance_id = args.utterance_id or f"demo-{uuid.uuid4()}"

    call_accepted(proxy, "Acquire", GLib.Variant("(s)", (utterance_id,)))
    print(f"Acquire: {utterance_id}")

    revision = 0
    try:
        for revision, text in enumerate(DEFAULT_PARTIALS, start=1):
            call_accepted(
                proxy,
                "Partial",
                GLib.Variant("(sts)", (utterance_id, revision, text)),
            )
            print(f"Partial {revision}: {text}")
            time.sleep(args.delay)

        revision += 1
        if args.cancel:
            call_accepted(proxy, "Cancel", GLib.Variant("(s)", (utterance_id,)))
            print("Cancel: preedit cleared; nothing committed")
        else:
            call_accepted(
                proxy,
                "Final",
                GLib.Variant("(sts)", (utterance_id, revision, args.final_text)),
            )
            print(f"Final {revision}: {args.final_text}")
    except BaseException:
        # Make a best-effort cleanup if the demo is interrupted after Acquire.
        try:
            proxy.call_sync(
                "Cancel",
                GLib.Variant("(s)", (utterance_id,)),
                Gio.DBusCallFlags.NONE,
                CALL_TIMEOUT_MS,
                None,
            )
        except GLib.Error:
            pass
        raise


def main() -> int:
    args = parse_args()
    try:
        run_demo(args)
    except (GLib.Error, DemoRejected) as error:
        print(f"preedit demo failed: {error}", file=sys.stderr)
        print(
            "Check that the Open Voice Input Linux preedit engine is running, "
            "selected, and focused in a text field.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\npreedit demo interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
