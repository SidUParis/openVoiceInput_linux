from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
except ValueError:
    pytest.skip("GTK4 introspection data is not installed", allow_module_level=True)

from gi.repository import Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("a GTK display is not available", allow_module_level=True)

from murmur_voice.settings_app import APPLY_NOTICE, SettingsWindow  # noqa: E402
from murmur_voice.settings_controller import (  # noqa: E402
    KeyState,
    ServiceSnapshot,
    SettingsError,
)


class FakeController:
    def __init__(self) -> None:
        self.saved_key = None
        self.saved_vocabulary = None
        self.service_actions = []
        self.key_error = None
        self.vocabulary_error = None

    def key_state(self):
        return KeyState.READY

    def load_vocabulary(self):
        return ("existing-term",)

    def save_key(self, api_key):
        if self.key_error is not None:
            raise self.key_error
        self.saved_key = api_key

    def save_vocabulary_text(self, text):
        if self.vocabulary_error is not None:
            raise self.vocabulary_error
        self.saved_vocabulary = text
        return len([line for line in text.split("\n") if line.strip()])

    def service_status(self):
        self.service_actions.append("status")
        return ServiceSnapshot("inactive")

    def start_service(self):
        self.service_actions.append("start")

    def stop_service(self):
        self.service_actions.append("stop")


@pytest.fixture(scope="module")
def application():
    application = Gtk.Application(
        application_id="io.github.SidUParis.OpenVoiceInputLinux.Settings.Tests"
    )
    application.register()
    yield application
    application.quit()


@pytest.fixture
def window(application):
    controller = FakeController()
    result = SettingsWindow(
        application,
        controller,
        refresh_service_on_start=False,
    )
    yield result, controller
    result.close()


def test_password_entry_is_empty_masked_and_has_no_reveal_control(window):
    settings_window, _ = window

    assert isinstance(settings_window.key_entry, Gtk.PasswordEntry)
    assert settings_window.key_entry.get_text() == ""
    assert settings_window.key_entry.get_show_peek_icon() is False
    assert settings_window.key_status_label.get_text() == (
        "Configured. The stored key is never displayed."
    )


def test_key_save_clears_entry_and_never_restarts_service(window):
    settings_window, controller = window
    secret = "private-key-sentinel"
    settings_window.key_entry.set_text(secret)

    settings_window.save_key()

    assert controller.saved_key == secret
    assert settings_window.key_entry.get_text() == ""
    assert settings_window.message_label.get_text() == APPLY_NOTICE
    assert secret not in settings_window.message_label.get_text()
    assert controller.service_actions == []


def test_key_failure_clears_entry_without_echoing_key(window):
    settings_window, controller = window
    secret = "private-key-that-must-not-appear"
    controller.key_error = SettingsError("The API key could not be saved safely.")
    settings_window.key_entry.set_text(secret)

    settings_window.save_key()

    assert settings_window.key_entry.get_text() == ""
    assert secret not in settings_window.message_label.get_text()
    assert controller.service_actions == []


def test_vocabulary_error_does_not_echo_private_terms(window):
    settings_window, controller = window
    private_term = "private-vocabulary-term"
    controller.vocabulary_error = SettingsError(
        "The personal vocabulary could not be saved safely."
    )
    settings_window.vocabulary_view.get_buffer().set_text(private_term)

    settings_window.save_vocabulary()

    assert private_term not in settings_window.message_label.get_text()
    assert controller.service_actions == []


def test_service_controls_are_explicit_and_offer_no_restart(window):
    settings_window, controller = window

    assert settings_window.start_service_button.get_label() == (
        "Enable and start service"
    )
    assert "cancels active dictation" in (
        settings_window.stop_service_button.get_label()
    )
    assert not hasattr(controller, "restart_service")
