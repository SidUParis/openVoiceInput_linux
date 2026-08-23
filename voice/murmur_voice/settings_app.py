"""Small native GTK4 settings window for voice-provider onboarding."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .settings_controller import (  # noqa: E402
    KeyState,
    ServiceSnapshot,
    SettingsController,
    SettingsError,
)

APPLICATION_ID = "io.github.SidUParis.OpenVoiceInputLinux.Settings"
APPLY_NOTICE = (
    "Saved locally. The service was not restarted; disable/stop and then "
    "enable/start it manually to apply the new settings."
)

_SERVICE_LABELS = {
    "active": "running",
    "activating": "starting",
    "deactivating": "stopping",
    "failed": "failed",
    "inactive": "stopped",
    "reloading": "reloading",
    "unknown": "unavailable",
}
_SESSION_LABELS = {
    "idle": "idle",
    "recording": "recording",
    "starting": "opening microphone",
    "stopping": "finalizing",
    "unavailable": "control socket unavailable",
    "unknown": "unknown session state",
}
_STATUS_LABELS = {
    "audio-backpressure": "audio buffer is full",
    "capture-start-failed": "microphone could not start",
    "final-timeout": "final recognition timed out",
    "preedit-final-rejected": "focused input rejected the final text",
    "preedit-lost": "focused input was lost",
    "preedit-rejected": "focused input rejected dictation",
    "preedit-unavailable": "focused input does not support dictation",
    "provider-auth": "provider authentication failed",
    "provider-error": "provider connection failed",
    "recording-limit-warning": "recording limit is near",
}


class SettingsWindow(Gtk.ApplicationWindow):
    """A bounded UI that never receives an existing provider key."""

    def __init__(
        self,
        application: Gtk.Application,
        controller: SettingsController | None = None,
        *,
        refresh_service_on_start: bool = True,
    ) -> None:
        super().__init__(application=application, title="Open Voice Input Linux")
        self.set_default_size(560, 640)
        self._controller = controller or SettingsController()
        self._service_busy = False

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        page.set_margin_top(20)
        page.set_margin_bottom(20)
        page.set_margin_start(20)
        page.set_margin_end(20)
        self.set_child(page)

        title = Gtk.Label(label="Voice input settings", xalign=0)
        title.add_css_class("title-1")
        page.append(title)

        provider_title = Gtk.Label(label="Volcengine API key", xalign=0)
        provider_title.add_css_class("heading")
        page.append(provider_title)

        self.key_status_label = Gtk.Label(xalign=0, wrap=True)
        page.append(self.key_status_label)

        self.key_entry = Gtk.PasswordEntry()
        self.key_entry.set_show_peek_icon(False)
        self.key_entry.set_property("placeholder-text", "Paste a new API key")
        self.key_entry.set_hexpand(True)
        page.append(self.key_entry)

        key_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.save_key_button = Gtk.Button(label="Save new key")
        self.save_key_button.connect("clicked", self._on_save_key)
        key_actions.append(self.save_key_button)
        page.append(key_actions)

        page.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        vocabulary_title = Gtk.Label(label="Personal vocabulary", xalign=0)
        vocabulary_title.add_css_class("heading")
        page.append(vocabulary_title)

        vocabulary_help = Gtk.Label(
            label=(
                "One name or specialist term per line. These explicit terms are "
                "sent to Volcengine with each dictation request."
            ),
            xalign=0,
            wrap=True,
        )
        page.append(vocabulary_help)

        self.vocabulary_view = Gtk.TextView(
            accepts_tab=False,
            monospace=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        vocabulary_scroll = Gtk.ScrolledWindow()
        vocabulary_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        vocabulary_scroll.set_min_content_height(150)
        vocabulary_scroll.set_child(self.vocabulary_view)
        page.append(vocabulary_scroll)

        self.save_vocabulary_button = Gtk.Button(label="Save vocabulary")
        self.save_vocabulary_button.connect("clicked", self._on_save_vocabulary)
        page.append(self.save_vocabulary_button)

        page.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        service_title = Gtk.Label(label="Voice service", xalign=0)
        service_title.add_css_class("heading")
        page.append(service_title)

        self.service_status_label = Gtk.Label(
            label="Service status: checking…", xalign=0, wrap=True
        )
        page.append(self.service_status_label)

        service_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.start_service_button = Gtk.Button(label="Enable and start service")
        self.start_service_button.connect("clicked", self._on_start_service)
        service_actions.append(self.start_service_button)
        self.stop_service_button = Gtk.Button(
            label="Disable and stop (cancels active dictation)"
        )
        self.stop_service_button.connect("clicked", self._on_stop_service)
        service_actions.append(self.stop_service_button)
        self.refresh_service_button = Gtk.Button(label="Refresh")
        self.refresh_service_button.connect("clicked", self._on_refresh_service)
        service_actions.append(self.refresh_service_button)
        page.append(service_actions)

        self.message_label = Gtk.Label(xalign=0, wrap=True, selectable=False)
        page.append(self.message_label)

        self._load_local_settings()
        if refresh_service_on_start:
            self.refresh_service_status()
        else:
            self._set_service_controls_busy(False)

    def _load_local_settings(self) -> None:
        state = self._controller.key_state()
        self._set_key_state(state)
        try:
            terms = self._controller.load_vocabulary()
        except SettingsError as error:
            self._show_error(str(error))
            return
        self.vocabulary_view.get_buffer().set_text("\n".join(terms))

    def _set_key_state(self, state: KeyState) -> None:
        labels = {
            KeyState.MISSING: "No API key is configured.",
            KeyState.READY: "Configured. The stored key is never displayed.",
            KeyState.INVALID: "The saved API key file is invalid or unsafe.",
        }
        self.key_status_label.set_text(labels[state])

    def _on_save_key(self, button: Gtk.Button) -> None:
        del button
        self.save_key()

    def save_key(self) -> None:
        api_key = self.key_entry.get_text()
        try:
            if not api_key.strip():
                self._show_error("Enter a new API key before saving.")
                return
            self._controller.save_key(api_key)
        except SettingsError as error:
            self._show_error(str(error))
        except Exception:
            self._show_error("The API key could not be saved safely.")
        else:
            self._set_key_state(KeyState.READY)
            self._show_message(APPLY_NOTICE)
        finally:
            # PasswordEntry is deliberately never prefilled and is cleared on
            # every save attempt so a key does not linger in the window.
            self.key_entry.set_text("")

    def _on_save_vocabulary(self, button: Gtk.Button) -> None:
        del button
        self.save_vocabulary()

    def save_vocabulary(self) -> None:
        buffer = self.vocabulary_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        try:
            count = self._controller.save_vocabulary_text(text)
        except SettingsError as error:
            self._show_error(str(error))
        except Exception:
            self._show_error("The personal vocabulary could not be saved safely.")
        else:
            self._show_message(f"Saved {count} vocabulary entries. {APPLY_NOTICE}")

    def _on_refresh_service(self, button: Gtk.Button) -> None:
        del button
        self.refresh_service_status()

    def refresh_service_status(self) -> None:
        self._run_service_operation(self._controller.service_status)

    def _on_start_service(self, button: Gtk.Button) -> None:
        del button
        self._run_service_operation(self._start_and_read_status)

    def _start_and_read_status(self) -> ServiceSnapshot:
        self._controller.start_service()
        return self._controller.service_status()

    def _on_stop_service(self, button: Gtk.Button) -> None:
        del button
        self._run_service_operation(self._stop_and_read_status)

    def _stop_and_read_status(self) -> ServiceSnapshot:
        self._controller.stop_service()
        return self._controller.service_status()

    def _run_service_operation(self, operation: Callable[[], ServiceSnapshot]) -> None:
        if self._service_busy:
            return
        self._set_service_controls_busy(True)

        def worker() -> None:
            try:
                snapshot = operation()
            except SettingsError as error:
                GLib.idle_add(self._finish_service_operation, None, str(error))
            except Exception:
                GLib.idle_add(
                    self._finish_service_operation,
                    None,
                    "The voice service operation failed safely.",
                )
            else:
                GLib.idle_add(self._finish_service_operation, snapshot, None)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_service_operation(
        self, snapshot: ServiceSnapshot | None, error: str | None
    ) -> bool:
        self._set_service_controls_busy(False)
        if error is not None:
            self._show_error(error)
        elif snapshot is not None:
            self._set_service_snapshot(snapshot)
        return GLib.SOURCE_REMOVE

    def _set_service_controls_busy(self, busy: bool) -> None:
        self._service_busy = busy
        self.start_service_button.set_sensitive(not busy)
        self.stop_service_button.set_sensitive(not busy)
        self.refresh_service_button.set_sensitive(not busy)

    def _set_service_snapshot(self, snapshot: ServiceSnapshot) -> None:
        service = _SERVICE_LABELS.get(snapshot.active_state, "unavailable")
        parts = [f"Service status: {service}"]
        if snapshot.session_state is not None:
            parts.append(
                _SESSION_LABELS.get(snapshot.session_state, "unknown session state")
            )
        detail = _STATUS_LABELS.get(snapshot.status_code or "")
        if detail is not None:
            parts.append(detail)
        self.service_status_label.set_text(" — ".join(parts))

        running = snapshot.active_state in {
            "active",
            "activating",
            "deactivating",
            "reloading",
        }
        known = snapshot.active_state != "unknown"
        self.start_service_button.set_sensitive(known and not running)
        self.stop_service_button.set_sensitive(known and running)

    def _show_message(self, message: str) -> None:
        self.message_label.remove_css_class("error")
        self.message_label.set_text(message)

    def _show_error(self, message: str) -> None:
        self.message_label.add_css_class("error")
        self.message_label.set_text(message)


class SettingsApplication(Gtk.Application):
    def __init__(self, controller: SettingsController | None = None) -> None:
        super().__init__(application_id=APPLICATION_ID)
        self._controller = controller
        self._window: SettingsWindow | None = None

    def do_activate(self) -> None:
        if self._window is None:
            self._window = SettingsWindow(self, self._controller)
        self._window.present()


def main(arguments: Sequence[str] | None = None) -> int:
    application = SettingsApplication()
    argv = list(arguments) if arguments is not None else sys.argv
    return int(application.run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
