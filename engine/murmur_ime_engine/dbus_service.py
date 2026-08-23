"""Session D-Bus endpoint used by the existing voice sidecar."""

# ruff: noqa: E402 -- GI versions must be selected before repository imports.

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .constants import DBUS_INTERFACE, DBUS_NAME, DBUS_PATH
from .policy import valid_preedit_text, valid_utterance_id
from .registry import EngineRegistry

logger = logging.getLogger(__name__)

INTROSPECTION_XML = f"""
<node>
  <interface name="{DBUS_INTERFACE}">
    <method name="Acquire">
      <arg name="utterance_id" type="s" direction="in"/>
      <arg name="accepted" type="b" direction="out"/>
    </method>
    <method name="Partial">
      <arg name="utterance_id" type="s" direction="in"/>
      <arg name="revision" type="t" direction="in"/>
      <arg name="text" type="s" direction="in"/>
      <arg name="accepted" type="b" direction="out"/>
    </method>
    <method name="Final">
      <arg name="utterance_id" type="s" direction="in"/>
      <arg name="revision" type="t" direction="in"/>
      <arg name="text" type="s" direction="in"/>
      <arg name="accepted" type="b" direction="out"/>
    </method>
    <method name="Cancel">
      <arg name="utterance_id" type="s" direction="in"/>
      <arg name="accepted" type="b" direction="out"/>
    </method>
  </interface>
</node>
"""


class PreeditDBusService:
    """Exports a minimal, focus-safe bridge on the user's session bus."""

    def __init__(
        self,
        registry: EngineRegistry,
        on_name_lost: Callable[[], None] | None = None,
    ) -> None:
        self._registry = registry
        self._on_name_lost_callback = on_name_lost
        self._connection: Gio.DBusConnection | None = None
        self._registration_id = 0
        self._subscription_id = 0
        self._owns_name = False

    def start(self) -> None:
        self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # Claim the well-known name synchronously *before* registering the
        # IBus component.  A second engine process must fail here instead of
        # briefly replacing the first process's dynamic IBus registration.
        reply = self._connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant(
                "(su)",
                (DBUS_NAME, int(Gio.BusNameOwnerFlags.DO_NOT_QUEUE)),
            ),
            GLib.VariantType.new("(u)"),
            Gio.DBusCallFlags.NONE,
            1_000,
            None,
        )
        (request_result,) = reply.unpack()
        if request_result not in (1, 4):  # PRIMARY_OWNER / ALREADY_OWNER
            self._connection = None
            raise RuntimeError(f"Session D-Bus name is already owned: {DBUS_NAME}")
        self._owns_name = True

        try:
            node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
            self._registration_id = self._connection.register_object(
                DBUS_PATH,
                node.interfaces[0],
                self._on_method_call,
                None,
                None,
            )
            self._subscription_id = self._connection.signal_subscribe(
                "org.freedesktop.DBus",
                "org.freedesktop.DBus",
                "NameOwnerChanged",
                "/org/freedesktop/DBus",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_name_owner_changed,
                None,
            )
            self._connection.connect("closed", self._on_connection_closed)
        except Exception:
            self.close()
            raise
        logger.info("Acquired session D-Bus name %s", DBUS_NAME)

    def close(self) -> None:
        self._registry.shutdown()
        if self._connection and self._subscription_id:
            self._connection.signal_unsubscribe(self._subscription_id)
            self._subscription_id = 0
        if self._connection and self._registration_id:
            self._connection.unregister_object(self._registration_id)
            self._registration_id = 0
        if self._connection and self._owns_name:
            try:
                self._connection.call_sync(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "ReleaseName",
                    GLib.Variant("(s)", (DBUS_NAME,)),
                    GLib.VariantType.new("(u)"),
                    Gio.DBusCallFlags.NONE,
                    1_000,
                    None,
                )
            except GLib.Error:
                pass
            self._owns_name = False
        self._connection = None

    def _on_connection_closed(
        self,
        connection: Gio.DBusConnection,
        remote_peer_vanished: bool,
        error: GLib.Error | None,
    ) -> None:
        if self._on_name_lost_callback:
            self._on_name_lost_callback()

    def _on_name_owner_changed(
        self,
        connection: Gio.DBusConnection,
        sender_name: str,
        object_path: str,
        interface_name: str,
        signal_name: str,
        parameters: GLib.Variant,
        user_data: object,
    ) -> None:
        name, old_owner, new_owner = parameters.unpack()
        if name.startswith(":") and old_owner and not new_owner:
            self._registry.owner_vanished(name)

    @staticmethod
    def _return_bool(invocation: Gio.DBusMethodInvocation, accepted: bool) -> None:
        invocation.return_value(GLib.Variant("(b)", (accepted,)))

    def _on_method_call(
        self,
        connection: Gio.DBusConnection,
        sender: str,
        object_path: str,
        interface_name: str,
        method_name: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        try:
            if method_name == "Acquire":
                (utterance_id,) = parameters.unpack()
                accepted = valid_utterance_id(utterance_id) and self._registry.acquire(
                    sender, utterance_id
                )
            elif method_name in ("Partial", "Final"):
                utterance_id, revision, text = parameters.unpack()
                if not valid_utterance_id(utterance_id) or not valid_preedit_text(text):
                    accepted = False
                elif method_name == "Partial":
                    accepted = self._registry.partial(
                        sender, utterance_id, int(revision), text
                    )
                else:
                    accepted = self._registry.final(
                        sender, utterance_id, int(revision), text
                    )
            elif method_name == "Cancel":
                (utterance_id,) = parameters.unpack()
                accepted = valid_utterance_id(utterance_id) and self._registry.cancel(
                    sender, utterance_id
                )
            else:
                invocation.return_dbus_error(
                    "org.freedesktop.DBus.Error.UnknownMethod",
                    "Unknown preedit method",
                )
                return
        except Exception:
            # Never include untrusted transcript content in logs or errors.
            logger.error("Preedit D-Bus method failed")
            accepted = False
        self._return_bool(invocation, accepted)
