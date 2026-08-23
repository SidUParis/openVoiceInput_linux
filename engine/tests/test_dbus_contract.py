from __future__ import annotations

# ruff: noqa: E402 -- Gio's version must be selected before importing it.

import unittest

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio

from murmur_ime_engine.constants import DBUS_INTERFACE
from murmur_ime_engine.dbus_service import INTROSPECTION_XML


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
                "Cancel": ("s", "b"),
            },
        )


if __name__ == "__main__":
    unittest.main()
