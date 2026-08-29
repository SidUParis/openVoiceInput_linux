from __future__ import annotations

# ruff: noqa: E402 -- Gio's version must be selected before importing it.

import unittest

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from murmur_ime_engine.constants import DBUS_INTERFACE
from murmur_ime_engine.dbus_service import INTROSPECTION_XML, PreeditDBusService
from murmur_ime_engine.session import ObservationResult


class _Invocation:
    def __init__(self) -> None:
        self.value = None

    def return_value(self, value) -> None:
        self.value = value


class _Registry:
    def __init__(self, result: ObservationResult) -> None:
        self.result = result
        self.calls = []

    def finish_observation(self, owner: str, utterance_id: str) -> ObservationResult:
        self.calls.append((owner, utterance_id))
        return self.result


class DBusContractTests(unittest.TestCase):
    def test_exact_bridge_signatures(self) -> None:
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        interface = node.interfaces[0]
        self.assertEqual(interface.name, DBUS_INTERFACE)
        methods = {
            method.name: (
                "".join(arg.signature for arg in method.in_args),
                "".join(arg.signature for arg in method.out_args),
            )
            for method in interface.methods
        }
        self.assertEqual(
            methods,
            {
                "Acquire": ("s", "b"),
                "Partial": ("sts", "b"),
                "Final": ("sts", "b"),
                "FinishObservation": ("s", "bsuusuu"),
                "Cancel": ("s", "b"),
            },
        )

    def test_finish_observation_preserves_sender_and_exact_result_shape(self) -> None:
        result = ObservationResult(
            consumed=True,
            accepted=True,
            baseline_text="前奔驰 Mark",
            committed_start=1,
            committed_end=8,
            current_text="前bench Mark",
            cursor=6,
            anchor=6,
        )
        registry = _Registry(result)
        service = PreeditDBusService(registry)  # type: ignore[arg-type]
        invocation = _Invocation()

        service._on_method_call(
            None,  # type: ignore[arg-type]
            ":1.40",
            "/org/murmur/IME/Preedit1",
            DBUS_INTERFACE,
            "FinishObservation",
            GLib.Variant("(s)", ("utt-1",)),
            invocation,  # type: ignore[arg-type]
        )

        self.assertEqual(registry.calls, [(":1.40", "utt-1")])
        self.assertIsNotNone(invocation.value)
        self.assertEqual(
            invocation.value.unpack(),
            (True, "前奔驰 Mark", 1, 8, "前bench Mark", 6, 6),
        )


if __name__ == "__main__":
    unittest.main()
