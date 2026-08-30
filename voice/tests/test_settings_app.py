from __future__ import annotations

from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
except ValueError:
    pytest.skip("GTK4 introspection data is not installed", allow_module_level=True)

from gi.repository import Gio, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("a GTK display is not available", allow_module_level=True)

from murmur_voice.data_collection import DataCollectionConfig  # noqa: E402
from murmur_voice.microphone_policy import (  # noqa: E402
    DEFAULT_MICROPHONE_PRIORITY,
    MicrophonePolicyConfig,
)
from murmur_voice.settings_app import APPLY_NOTICE, SettingsWindow  # noqa: E402
from murmur_voice.settings_controller import (  # noqa: E402
    CORRECTION_TEXT_LIMIT,
    KeyState,
    ServiceSnapshot,
    SettingsError,
)


class FakeController:
    def __init__(self) -> None:
        self.saved_key = None
        self.saved_vocabulary = None
        self.saved_corrections = None
        self.saved_microphone_priority = None
        self.saved_data_collection = None
        self.service_actions = []
        self.key_error = None
        self.clear_key_error = None
        self.clear_key_calls = 0
        self.clear_key_result = True
        self.vocabulary_error = None
        self.corrections_error = None
        self.loaded_corrections = (("existing mistake", "existing canonical form"),)
        self.microphone_policy_error = None
        self.loaded_microphone_policy = MicrophonePolicyConfig()
        self.data_collection_error = None
        self.loaded_data_collection = DataCollectionConfig()

    def key_state(self):
        return KeyState.READY

    def load_vocabulary(self):
        return ("existing-term",)

    def load_corrections(self):
        return self.loaded_corrections

    def save_key(self, api_key):
        if self.key_error is not None:
            raise self.key_error
        self.saved_key = api_key

    def clear_key(self):
        self.clear_key_calls += 1
        if self.clear_key_error is not None:
            raise self.clear_key_error
        return self.clear_key_result

    def save_vocabulary_text(self, text):
        if self.vocabulary_error is not None:
            raise self.vocabulary_error
        self.saved_vocabulary = text
        return len([line for line in text.split("\n") if line.strip()])

    def save_corrections(self, pairs):
        if self.corrections_error is not None:
            raise self.corrections_error
        self.saved_corrections = pairs
        normalized = []
        seen = set()
        for pair in pairs:
            if pair in seen:
                continue
            seen.add(pair)
            normalized.append(pair)
        self.loaded_corrections = tuple(normalized)
        return len(normalized)

    def load_microphone_policy(self):
        if self.microphone_policy_error is not None:
            raise self.microphone_policy_error
        return self.loaded_microphone_policy

    def save_microphone_priority(self, priority):
        if self.microphone_policy_error is not None:
            raise self.microphone_policy_error
        self.saved_microphone_priority = tuple(priority)
        self.loaded_microphone_policy = MicrophonePolicyConfig(priority=tuple(priority))
        return self.loaded_microphone_policy

    def load_data_collection(self):
        if self.data_collection_error is not None:
            raise self.data_collection_error
        return self.loaded_data_collection

    def save_data_collection(self, enabled, directory):
        if self.data_collection_error is not None:
            raise self.data_collection_error
        self.saved_data_collection = (enabled, directory)
        self.loaded_data_collection = DataCollectionConfig(
            enabled=enabled,
            directory=Path(directory) if directory is not None else None,
        )
        return self.loaded_data_collection

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


def _listbox_rows(listbox):
    rows = []
    child = listbox.get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    return rows


def _descendants(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


def test_password_entry_is_empty_masked_and_has_no_reveal_control(window):
    settings_window, _ = window

    assert isinstance(settings_window.key_entry, Gtk.PasswordEntry)
    assert settings_window.key_entry.get_text() == ""
    assert settings_window.key_entry.get_show_peek_icon() is False
    assert settings_window.key_status_label.get_text() == (
        "Configured. The stored key is never displayed."
    )


def test_settings_disclose_remote_audio_billing_and_cancel_boundary(window):
    settings_window, _ = window

    notice = settings_window.remote_audio_notice_label.get_text()

    assert "microphone audio" in notice
    assert "Volcengine" in notice
    assert "billed to your account" in notice
    assert "cannot retract" in notice


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


def test_key_clear_is_two_step_local_and_never_displays_the_key(window):
    settings_window, controller = window
    secret = "private-key-that-must-not-appear"
    settings_window.key_entry.set_text(secret)

    settings_window.clear_key_button.emit("clicked")

    assert controller.clear_key_calls == 0
    assert settings_window.clear_key_button.get_label() == ("Confirm clear saved key")
    assert "Nothing was removed" in settings_window.message_label.get_text()
    assert secret not in settings_window.message_label.get_text()

    settings_window.clear_key_button.emit("clicked")

    assert controller.clear_key_calls == 1
    assert settings_window.clear_key_button.get_label() == "Clear saved key…"
    assert settings_window.key_entry.get_text() == ""
    assert settings_window.key_status_label.get_text() == "No API key is configured."
    assert "No provider was contacted" in settings_window.message_label.get_text()
    assert secret not in settings_window.message_label.get_text()
    assert controller.service_actions == []


def test_key_clear_refusal_resets_confirmation_without_echoing_key(window):
    settings_window, controller = window
    secret = "private-key-that-must-not-appear"
    controller.clear_key_error = SettingsError(
        "Disable and stop the voice service before clearing the saved API key."
    )
    settings_window.key_entry.set_text(secret)

    settings_window.clear_key()
    settings_window.clear_key()

    assert controller.clear_key_calls == 1
    assert settings_window.clear_key_button.get_label() == "Clear saved key…"
    assert settings_window.key_entry.get_text() == ""
    assert "Disable and stop" in settings_window.message_label.get_text()
    assert secret not in settings_window.message_label.get_text()


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


def test_existing_corrections_load_into_a_bounded_unambiguous_list(window):
    settings_window, _ = window

    rows = _listbox_rows(settings_window.corrections_list)
    labels = [
        widget.get_text()
        for widget in _descendants(rows[0])
        if isinstance(widget, Gtk.Label)
    ]

    assert len(rows) == 1
    assert "Recognized as: existing mistake" in labels
    assert "Canonical text: existing canonical form" in labels
    assert settings_window.corrections_scroll.get_max_content_height() == 190
    assert settings_window.correction_wrong_entry.get_max_length() == (
        CORRECTION_TEXT_LIMIT
    )
    assert settings_window.correction_canonical_entry.get_max_length() == (
        CORRECTION_TEXT_LIMIT
    )


def test_correction_add_remove_and_save_are_local_and_explicit(window):
    settings_window, controller = window
    settings_window.correction_wrong_entry.set_text("new mistake")
    settings_window.correction_canonical_entry.set_text("new canonical form")

    settings_window.add_correction()

    assert settings_window.correction_wrong_entry.get_text() == ""
    assert settings_window.correction_canonical_entry.get_text() == ""
    assert len(_listbox_rows(settings_window.corrections_list)) == 2
    assert controller.saved_corrections is None
    assert controller.service_actions == []

    added_row = _listbox_rows(settings_window.corrections_list)[1]
    remove_button = next(
        widget
        for widget in _descendants(added_row)
        if isinstance(widget, Gtk.Button) and widget.get_label() == "Remove"
    )
    remove_button.emit("clicked")

    assert len(_listbox_rows(settings_window.corrections_list)) == 1
    settings_window.save_corrections()
    assert controller.saved_corrections == (
        ("existing mistake", "existing canonical form"),
    )
    assert "Saved 1 explicit correction pairs" in (
        settings_window.message_label.get_text()
    )
    assert controller.service_actions == []


def test_correction_add_rejects_duplicate_and_conflicting_wrong_form(window):
    settings_window, controller = window
    settings_window.correction_wrong_entry.set_text("existing mistake")
    settings_window.correction_canonical_entry.set_text("existing canonical form")

    settings_window.add_correction()

    assert len(_listbox_rows(settings_window.corrections_list)) == 1
    assert "already in the list" in settings_window.message_label.get_text()

    settings_window.correction_canonical_entry.set_text("different canonical form")
    settings_window.add_correction()

    assert len(_listbox_rows(settings_window.corrections_list)) == 1
    assert "different canonical correction" in (
        settings_window.message_label.get_text()
    )
    assert controller.saved_corrections is None
    assert controller.service_actions == []


def test_correction_entries_cap_text_before_it_enters_the_pending_list(window):
    settings_window, _ = window
    settings_window.correction_wrong_entry.set_text("界" * (CORRECTION_TEXT_LIMIT + 1))
    settings_window.correction_canonical_entry.set_text("canonical form")

    settings_window.add_correction()

    assert settings_window._correction_pairs[-1] == (
        "界" * CORRECTION_TEXT_LIMIT,
        "canonical form",
    )


def test_correction_save_reloads_normalized_rows_and_can_persist_empty(window):
    settings_window, controller = window
    duplicate = ("duplicate mistake", "canonical form")
    settings_window._replace_correction_rows((duplicate, duplicate))

    settings_window.save_corrections()

    assert controller.saved_corrections == (duplicate, duplicate)
    assert controller.loaded_corrections == (duplicate,)
    assert len(_listbox_rows(settings_window.corrections_list)) == 1

    row = _listbox_rows(settings_window.corrections_list)[0]
    remove_button = next(
        widget
        for widget in _descendants(row)
        if isinstance(widget, Gtk.Button) and widget.get_label() == "Remove"
    )
    remove_button.emit("clicked")
    settings_window.save_corrections()

    assert controller.saved_corrections == ()
    assert controller.loaded_corrections == ()
    assert _listbox_rows(settings_window.corrections_list) == []


def test_correction_validation_and_save_errors_never_echo_content(window):
    settings_window, controller = window
    private_wrong = "private-wrong-that-must-not-appear"
    private_canonical = "private-canonical-that-must-not-appear"
    settings_window.correction_wrong_entry.set_text(private_wrong)

    settings_window.add_correction()

    assert private_wrong not in settings_window.message_label.get_text()
    settings_window.correction_canonical_entry.set_text(private_canonical)
    settings_window.add_correction()
    controller.corrections_error = SettingsError(
        "The explicit corrections could not be saved safely."
    )

    settings_window.save_corrections()

    message = settings_window.message_label.get_text()
    assert private_wrong not in message
    assert private_canonical not in message
    assert "must-not-appear" not in message
    assert controller.service_actions == []


def test_correction_explanation_names_provider_scope_and_bounded_learning(window):
    settings_window, _ = window

    explanation = settings_window.corrections_help_label.get_text()

    assert "Volcengine" in explanation
    assert "each dictation request" in explanation
    assert "five-second" in explanation
    assert "ambiguous or conflicting" in explanation


def test_service_controls_are_explicit_and_offer_no_restart(window):
    settings_window, controller = window

    assert settings_window.start_service_button.get_label() == (
        "Enable and start service"
    )
    assert "cancels active dictation" in (
        settings_window.stop_service_button.get_label()
    )
    assert not hasattr(controller, "restart_service")


def test_microphone_unavailable_status_has_actionable_label(window):
    settings_window, _ = window

    settings_window._set_service_snapshot(
        ServiceSnapshot("active", "idle", "microphone-unavailable")
    )

    label = settings_window.service_status_label.get_text()
    assert "no usable microphone" in label
    assert "reconnect or select an input" in label


def test_microphone_policy_invalid_status_has_repair_action(window):
    settings_window, _ = window

    settings_window._set_service_snapshot(
        ServiceSnapshot("active", "idle", "microphone-policy-invalid")
    )

    label = settings_window.service_status_label.get_text()
    assert "microphone priority is invalid or unsafe" in label
    assert "open settings and save a complete order" in label


def test_local_collection_is_off_by_default_and_discloses_exact_scope(window):
    settings_window, _ = window

    notice = settings_window.data_collection_notice_label.get_text()

    assert settings_window.data_collection_check.get_active() is False
    assert settings_window.data_collection_directory_entry.get_text() == ""
    assert settings_window.data_collection_directory_entry.get_editable() is False
    assert "Off by default" in notice
    assert "authoritative Volcengine final" in notice
    assert "WAV" in notice
    assert "unreviewed pseudo-label" in notice
    assert "Volcengine" in notice
    assert "openvoiceinput-dataset-v1" in notice
    assert "spoken_verbatim and preferred_output remain empty" in notice
    assert "uploads" in notice
    assert "trains a model" in notice
    assert "unpublished queued records" in notice
    assert "already published records are retained" in notice
    assert settings_window.choose_data_collection_directory_button.get_label() == (
        "Choose folder…"
    )


def test_data_collection_save_is_explicit_local_and_never_starts_service(
    window, tmp_path
):
    settings_window, controller = window
    selected = tmp_path / "personal-asr-records"
    selected.mkdir()
    settings_window.data_collection_check.set_active(True)
    settings_window.data_collection_directory_entry.set_text(str(selected))

    settings_window.save_data_collection()

    assert controller.saved_data_collection == (True, str(selected))
    assert settings_window.data_collection_check.get_active() is True
    assert settings_window.data_collection_directory_entry.get_text() == str(selected)
    assert "collection is enabled" in settings_window.message_label.get_text()
    assert controller.service_actions == []


def test_data_collection_save_error_does_not_start_service_or_echo_path(
    window, tmp_path
):
    settings_window, controller = window
    private_path = tmp_path / "private-path-that-must-not-appear"
    controller.data_collection_error = SettingsError(
        "The selected local data collection folder is unavailable."
    )
    settings_window.data_collection_check.set_active(True)
    settings_window.data_collection_directory_entry.set_text(str(private_path))

    settings_window.save_data_collection()

    message = settings_window.message_label.get_text()
    assert "unavailable" in message
    assert str(private_path) not in message
    assert controller.saved_data_collection is None
    assert controller.service_actions == []


def test_folder_chooser_response_sets_only_a_local_filesystem_path(window, tmp_path):
    settings_window, _ = window
    selected = tmp_path / "chosen-records"
    selected.mkdir()

    class FakeChooser:
        def __init__(self):
            self.destroyed = False

        def get_file(self):
            return Gio.File.new_for_path(str(selected))

        def destroy(self):
            self.destroyed = True

    chooser = FakeChooser()
    settings_window._data_collection_chooser = chooser

    settings_window._on_data_collection_directory_response(
        chooser, Gtk.ResponseType.ACCEPT
    )

    assert settings_window.data_collection_directory_entry.get_text() == str(selected)
    assert chooser.destroyed is True
    assert settings_window._data_collection_chooser is None


def test_microphone_note_discloses_dynamic_and_audio_routing_boundaries(window):
    settings_window, _ = window

    notice = settings_window.microphone_selection_notice_label.get_text()

    assert "Before each new dictation" in notice
    assert "falls through this order" in notice
    assert "never switches mid-stream" in notice
    assert "never moves the playback sink" in notice
    assert "requests set-default-source" in notice
    assert "Host audio policy may recompute a default" in notice
    assert "A2DP is not a headset microphone" in notice
    assert "call profiles are not switched automatically" in notice


def test_microphone_priority_defaults_to_all_four_ranked_categories(window):
    settings_window, _ = window

    assert tuple(settings_window._microphone_priority) == (DEFAULT_MICROPHONE_PRIORITY)
    rows = _listbox_rows(settings_window.microphone_priority_list)
    labels = [
        widget.get_text()
        for row in rows
        for widget in _descendants(row)
        if isinstance(widget, Gtk.Label)
    ]

    assert len(rows) == 4
    assert "DJI Mic Mini 2 receiver" in labels
    assert "Headset microphone" in labels
    assert "Other external microphone" in labels
    assert "Built-in computer microphone" in labels

    first_buttons = [
        widget for widget in _descendants(rows[0]) if isinstance(widget, Gtk.Button)
    ]
    last_buttons = [
        widget for widget in _descendants(rows[-1]) if isinstance(widget, Gtk.Button)
    ]
    assert (
        next(
            button for button in first_buttons if button.get_label() == "Move up"
        ).get_sensitive()
        is False
    )
    assert (
        next(
            button for button in last_buttons if button.get_label() == "Move down"
        ).get_sensitive()
        is False
    )


def test_microphone_priority_reorder_and_save_are_local_and_hot_loaded(window):
    settings_window, controller = window
    first_row = _listbox_rows(settings_window.microphone_priority_list)[0]
    move_down = next(
        widget
        for widget in _descendants(first_row)
        if isinstance(widget, Gtk.Button) and widget.get_label() == "Move down"
    )

    move_down.emit("clicked")

    assert tuple(settings_window._microphone_priority) == (
        "headset",
        "dji",
        "external",
        "built-in",
    )
    assert controller.saved_microphone_priority is None
    assert "future dictations" in settings_window.message_label.get_text()

    settings_window.save_microphone_priority()

    assert controller.saved_microphone_priority == (
        "headset",
        "dji",
        "external",
        "built-in",
    )
    message = settings_window.message_label.get_text()
    assert "next dictation reevaluates all usable inputs" in message
    assert "active utterance keeps its current microphone" in message
    assert controller.service_actions == []


def test_microphone_priority_load_failure_shows_error_and_safe_default(application):
    controller = FakeController()
    controller.microphone_policy_error = SettingsError(
        "The microphone priority setting could not be loaded safely."
    )
    settings_window = SettingsWindow(
        application,
        controller,
        refresh_service_on_start=False,
    )
    try:
        assert tuple(settings_window._microphone_priority) == (
            DEFAULT_MICROPHONE_PRIORITY
        )
        assert "could not be loaded safely" in (
            settings_window.message_label.get_text()
        )
        assert controller.saved_microphone_priority is None
        assert controller.service_actions == []
    finally:
        settings_window.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    (
        ("data-collection-failed", "dictation still completed"),
        ("data-collection-unavailable", "dictation continues"),
    ),
)
def test_optional_collection_status_is_visible_without_marking_service_stopped(
    window, code, expected
):
    settings_window, _ = window

    settings_window._set_service_snapshot(ServiceSnapshot("active", "idle", code))

    label = settings_window.service_status_label.get_text()
    assert "Service status: running" in label
    assert expected in label
