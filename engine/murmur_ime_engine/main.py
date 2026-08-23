"""Executable entry point for dynamic IBus component registration."""

# ruff: noqa: E402 -- GI versions must be selected before repository imports.

from __future__ import annotations

import argparse
import logging
import signal

import gi

gi.require_version("IBus", "1.0")
from gi.repository import GLib, IBus  # noqa: E402

from .constants import COMPONENT_NAME, ENGINE_NAME, VERSION
from .dbus_service import PreeditDBusService
from .ibus_engine import MurmurFactory
from .registry import EngineRegistry

logger = logging.getLogger(__name__)


def build_component() -> IBus.Component:
    component = IBus.Component(
        name=COMPONENT_NAME,
        description="Murmur inline voice preedit prototype",
        version=VERSION,
        license="GPL-3.0-only",
        author="Murmur IME contributors",
        homepage="https://github.com/SidUParis/murmur-ime",
        textdomain="murmur-ime",
    )
    component.add_engine(
        IBus.EngineDesc(
            name=ENGINE_NAME,
            longname="Murmur Voice (prototype)",
            description="Inline streaming voice transcription",
            language="zh",
            license="GPL-3.0-only",
            author="Murmur IME contributors",
            icon="audio-input-microphone-symbolic",
            layout="default",
            symbol="🎙",
            rank=80,
        )
    )
    return component


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Murmur dynamic IBus preedit prototype"
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    IBus.init()
    loop = GLib.MainLoop()
    bus = IBus.Bus()
    if not bus.is_connected():
        logger.error("The IBus daemon is not reachable")
        return 1

    registry = EngineRegistry()
    factory = MurmurFactory(bus, registry)
    service = PreeditDBusService(registry, on_name_lost=loop.quit)

    def request_quit(*_args: object) -> bool:
        loop.quit()
        return GLib.SOURCE_REMOVE

    bus.connect("disconnected", request_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, request_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, request_quit)

    try:
        try:
            service.start()
        except (GLib.Error, RuntimeError) as error:
            logger.error("Could not start preedit D-Bus service: %s", error)
            return 1
        if not bus.register_component(build_component()):
            logger.error("IBus rejected dynamic component registration")
            return 1
        logger.info(
            "Registered dynamic IBus engine %s; no desktop restart required",
            ENGINE_NAME,
        )
        loop.run()
    finally:
        service.close()
        factory.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
