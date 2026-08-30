"""Small native GTK4 settings window for voice-provider onboarding."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .microphone_policy import (  # noqa: E402
    DEFAULT_MICROPHONE_PRIORITY,
    MICROPHONE_CATEGORIES,
)
from .settings_controller import (  # noqa: E402
    CORRECTION_PAIR_LIMIT,
    CORRECTION_TEXT_LIMIT,
    KeyState,
    ServiceSnapshot,
    SettingsController,
    SettingsError,
)

APPLICATION_ID = "io.github.SidUParis.OpenVoiceInputLinux.Settings"
APPLY_NOTICE = "Saved locally. The next dictation loads the new settings."

_MICROPHONE_CATEGORY_LABELS = {
    "dji": "DJI Mic Mini 2 receiver",
    "headset": "Headset microphone",
    "external": "Other external microphone",
    "built-in": "Built-in computer microphone",
}
_MICROPHONE_CATEGORY_DESCRIPTIONS = {
    "dji": "The DJI receiver while its transmitter is proven online.",
    "headset": "USB, 3.5 mm, or an active Bluetooth HSP/HFP microphone.",
    "external": "Another USB or externally connected capture device.",
    "built-in": "The computer's internal fallback microphone.",
}

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
    "observing": "watching for a local correction",
    "recording": "recording",
    "starting": "opening microphone",
    "stopping": "finalizing",
    "unavailable": "control socket unavailable",
    "unknown": "unknown session state",
}
_STATUS_LABELS = {
    "audio-backpressure": "audio buffer is full",
    "capture-start-failed": "microphone could not start",
    "data-collection-failed": (
        "optional local data was not confirmed durable; dictation still completed"
    ),
    "data-collection-unavailable": (
        "optional local data collection is unavailable; dictation continues"
    ),
    "final-timeout": "final recognition timed out",
    "microphone-unavailable": "no usable microphone; reconnect or select an input",
    "microphone-policy-invalid": (
        "microphone priority is invalid or unsafe; open settings and save a "
        "complete order"
    ),
    "adaptive-correction-failed": "adaptive correction could not be saved",
    "adaptive-correction-learned": "adaptive correction learned for future dictation",
    "recognition-context-invalid": "recognition context files are invalid or unsafe",
    "preedit-final-rejected": "focused input rejected the final text",
    "preedit-lost": "focused input was lost",
    "preedit-rejected": "focused input rejected dictation",
    "preedit-unavailable": "focused input does not support dictation",
    "provider-auth": "provider authentication failed",
    "provider-error": "provider connection failed",
    "recording-limit-warning": "recording limit is near",
    "start-timeout": "microphone start timed out; try again",
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
        self.set_default_size(620, 820)
        self._controller = controller or SettingsController()
        self._service_busy = False
        self._collection_busy = False
        self._window_closed = False
        self._key_clear_armed = False
        self._correction_pairs: list[tuple[str, str]] = []
        self._microphone_priority: list[str] = list(DEFAULT_MICROPHONE_PRIORITY)
        self._data_collection_chooser: Gtk.FileChooserNative | None = None
        self.connect("close-request", self._on_close_request)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        page.set_margin_top(20)
        page.set_margin_bottom(20)
        page.set_margin_start(20)
        page.set_margin_end(20)
        page_scroll = Gtk.ScrolledWindow()
        page_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page_scroll.set_child(page)
        self.set_child(page_scroll)

        title = Gtk.Label(label="Voice input settings", xalign=0)
        title.add_css_class("title-1")
        page.append(title)

        provider_title = Gtk.Label(label="Volcengine API key", xalign=0)
        provider_title.add_css_class("heading")
        page.append(provider_title)

        self.key_status_label = Gtk.Label(xalign=0, wrap=True)
        page.append(self.key_status_label)

        self.remote_audio_notice_label = Gtk.Label(
            label=(
                "During an explicitly started dictation, microphone audio is "
                "streamed to Volcengine and billed to your account. Cancelling "
                "cannot retract audio that was already sent."
            ),
            xalign=0,
            wrap=True,
        )
        page.append(self.remote_audio_notice_label)

        self.key_entry = Gtk.PasswordEntry()
        self.key_entry.set_show_peek_icon(False)
        self.key_entry.set_property("placeholder-text", "Paste a new API key")
        self.key_entry.set_hexpand(True)
        page.append(self.key_entry)

        key_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.save_key_button = Gtk.Button(label="Save new key")
        self.save_key_button.connect("clicked", self._on_save_key)
        key_actions.append(self.save_key_button)
        self.clear_key_button = Gtk.Button(label="Clear saved key…")
        self.clear_key_button.add_css_class("destructive-action")
        self.clear_key_button.connect("clicked", self._on_clear_key)
        key_actions.append(self.clear_key_button)
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

        corrections_title = Gtk.Label(
            label="Explicit corrections (optional, experimental)", xalign=0
        )
        corrections_title.add_css_class("heading")
        page.append(corrections_title)

        self.corrections_help_label = Gtk.Label(
            label=(
                "Every saved pair is sent to Volcengine with each dictation "
                "request. Strict single replacements made during the five-second "
                "post-dictation window are learned separately for future requests; "
                "ambiguous or conflicting edits are not activated."
            ),
            xalign=0,
            wrap=True,
        )
        page.append(self.corrections_help_label)

        correction_inputs = Gtk.Grid(column_spacing=8, row_spacing=6)
        correction_inputs.attach(
            Gtk.Label(label="Recognized incorrectly as", xalign=0), 0, 0, 1, 1
        )
        correction_inputs.attach(
            Gtk.Label(label="Replace with canonical text", xalign=0), 1, 0, 1, 1
        )
        self.correction_wrong_entry = Gtk.Entry(
            placeholder_text="Often misrecognized phrase"
        )
        self.correction_wrong_entry.set_max_length(CORRECTION_TEXT_LIMIT)
        self.correction_wrong_entry.set_hexpand(True)
        correction_inputs.attach(self.correction_wrong_entry, 0, 1, 1, 1)
        self.correction_canonical_entry = Gtk.Entry(
            placeholder_text="Preferred canonical text"
        )
        self.correction_canonical_entry.set_max_length(CORRECTION_TEXT_LIMIT)
        self.correction_canonical_entry.set_hexpand(True)
        correction_inputs.attach(self.correction_canonical_entry, 1, 1, 1, 1)
        self.add_correction_button = Gtk.Button(label="Add correction")
        self.add_correction_button.connect("clicked", self._on_add_correction)
        correction_inputs.attach(self.add_correction_button, 2, 1, 1, 1)
        page.append(correction_inputs)

        self.corrections_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
        )
        self.corrections_list.add_css_class("boxed-list")
        self.corrections_scroll = Gtk.ScrolledWindow()
        self.corrections_scroll.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self.corrections_scroll.set_min_content_height(90)
        self.corrections_scroll.set_max_content_height(190)
        self.corrections_scroll.set_propagate_natural_height(True)
        self.corrections_scroll.set_child(self.corrections_list)
        page.append(self.corrections_scroll)

        self.save_corrections_button = Gtk.Button(label="Save explicit corrections")
        self.save_corrections_button.connect("clicked", self._on_save_corrections)
        page.append(self.save_corrections_button)

        page.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        microphone_title = Gtk.Label(label="Microphone priority", xalign=0)
        microphone_title.add_css_class("heading")
        page.append(microphone_title)

        self.microphone_selection_notice_label = Gtk.Label(
            label=(
                "Rank all microphone categories from highest to lowest. Before "
                "each new dictation, Open Voice Input reevaluates the currently "
                "usable inputs and falls through this order when a preferred "
                "category is unavailable. An active utterance keeps one microphone "
                "until it ends; it never switches mid-stream. This app never moves "
                "the playback sink or requests set-default-source. Host audio policy "
                "may recompute a default after safe capture-profile recovery. "
                "Playback-only Bluetooth A2DP is not a headset microphone, and "
                "Bluetooth call profiles are not switched automatically."
            ),
            xalign=0,
            wrap=True,
        )
        page.append(self.microphone_selection_notice_label)

        self.microphone_priority_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
        )
        self.microphone_priority_list.add_css_class("boxed-list")
        page.append(self.microphone_priority_list)

        self.save_microphone_priority_button = Gtk.Button(
            label="Save microphone priority"
        )
        self.save_microphone_priority_button.connect(
            "clicked", self._on_save_microphone_priority
        )
        page.append(self.save_microphone_priority_button)

        page.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        collection_title = Gtk.Label(
            label="Training-data collection (optional)", xalign=0
        )
        collection_title.add_css_class("heading")
        page.append(collection_title)

        self.data_collection_notice_label = Gtk.Label(
            label=(
                "Off by default. For an enabled utterance whose authoritative "
                "Volcengine final is successfully committed, this stores a WAV "
                "and provider_final (an "
                "unreviewed pseudo-label) under openvoiceinput-dataset-v1 in the "
                "selected absolute POSIX folder. The folder may be local or a "
                "remote filesystem already mounted by the OS (for example SSHFS). "
                "This app does not mount a host or accept SSH or Google Drive URLs; "
                "back up complete local/Orange records to Google Drive asynchronously. "
                "spoken_verbatim and preferred_output remain empty until a later "
                "review workflow. Nothing here uploads the dataset or trains a model. "
                "A disconnected mount can lose an unpublished record because there is "
                "no fallback spool. Disabling immediately blocks unpublished queued "
                "records; already published records are retained."
            ),
            xalign=0,
            wrap=True,
        )
        page.append(self.data_collection_notice_label)

        self.data_collection_check = Gtk.CheckButton(
            label="Keep WAV + unreviewed provider final in selected folder"
        )
        page.append(self.data_collection_check)

        collection_path_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self.data_collection_directory_entry = Gtk.Entry(
            placeholder_text="Choose a local or already-mounted absolute folder"
        )
        self.data_collection_directory_entry.set_editable(False)
        self.data_collection_directory_entry.set_hexpand(True)
        collection_path_row.append(self.data_collection_directory_entry)
        self.choose_data_collection_directory_button = Gtk.Button(
            label="Choose folder…"
        )
        self.choose_data_collection_directory_button.connect(
            "clicked", self._on_choose_data_collection_directory
        )
        collection_path_row.append(self.choose_data_collection_directory_button)
        page.append(collection_path_row)

        self.save_data_collection_button = Gtk.Button(label="Save collection setting")
        self.save_data_collection_button.connect(
            "clicked", self._on_save_data_collection
        )
        page.append(self.save_data_collection_button)

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
        else:
            self.vocabulary_view.get_buffer().set_text("\n".join(terms))
        try:
            pairs = self._controller.load_corrections()
        except SettingsError as error:
            self._show_error(str(error))
        else:
            self._replace_correction_rows(pairs)
        try:
            microphone_policy = self._controller.load_microphone_policy()
        except SettingsError as error:
            self._replace_microphone_priority_rows(DEFAULT_MICROPHONE_PRIORITY)
            self._show_error(str(error))
        else:
            self._replace_microphone_priority_rows(microphone_policy.priority)
        try:
            collection = self._controller.load_data_collection()
        except SettingsError as error:
            self.data_collection_check.set_active(False)
            self.data_collection_directory_entry.set_text("")
            self._show_error(str(error))
        else:
            self.data_collection_check.set_active(collection.enabled)
            directory = collection.directory
            self.data_collection_directory_entry.set_text(
                str(directory) if directory is not None else ""
            )

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
        self._reset_key_clear_confirmation()
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

    def _on_clear_key(self, button: Gtk.Button) -> None:
        del button
        self.clear_key()

    def clear_key(self) -> None:
        if not self._key_clear_armed:
            self._key_clear_armed = True
            self.clear_key_button.set_label("Confirm clear saved key")
            self._show_message(
                "Nothing was removed. First disable and stop the voice service, "
                "then click Confirm clear saved key to permanently remove only "
                "the locally saved API key."
            )
            return

        self._reset_key_clear_confirmation()
        try:
            removed = self._controller.clear_key()
        except SettingsError as error:
            self._show_error(str(error))
        except Exception:
            self._show_error("The saved API key could not be removed safely.")
        else:
            self._set_key_state(KeyState.MISSING)
            if removed:
                self._show_message(
                    "The locally saved API key was removed. No provider was contacted."
                )
            else:
                self._show_message(
                    "No saved API key was present. No provider was contacted."
                )
        finally:
            self.key_entry.set_text("")

    def _reset_key_clear_confirmation(self) -> None:
        self._key_clear_armed = False
        self.clear_key_button.set_label("Clear saved key…")

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

    def _on_add_correction(self, button: Gtk.Button) -> None:
        del button
        self.add_correction()

    def add_correction(self) -> None:
        wrong = self.correction_wrong_entry.get_text().strip()
        canonical = self.correction_canonical_entry.get_text().strip()
        if not wrong or not canonical:
            self._show_error("Enter both sides of the explicit correction.")
            return
        for existing_wrong, existing_canonical in self._correction_pairs:
            if existing_wrong != wrong:
                continue
            if existing_canonical == canonical:
                self._show_error("That explicit correction is already in the list.")
            else:
                self._show_error(
                    "That recognized form already has a different canonical "
                    "correction. Remove it before adding a replacement."
                )
            return
        if len(self._correction_pairs) >= CORRECTION_PAIR_LIMIT:
            self._show_error(
                f"At most {CORRECTION_PAIR_LIMIT} explicit corrections can be saved."
            )
            return
        pair = (wrong, canonical)
        self._correction_pairs.append(pair)
        self._append_correction_row(pair)
        self.correction_wrong_entry.set_text("")
        self.correction_canonical_entry.set_text("")
        self._show_message(
            "Correction added locally to this list. Use Save explicit corrections "
            "to store it."
        )

    def _replace_correction_rows(self, pairs: Sequence[tuple[str, str]]) -> None:
        child = self.corrections_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.corrections_list.remove(child)
            child = next_child
        self._correction_pairs = list(pairs)
        for pair in self._correction_pairs:
            self._append_correction_row(pair)

    def _append_correction_row(self, pair: tuple[str, str]) -> None:
        wrong, canonical = pair
        row = Gtk.ListBoxRow()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        content.set_margin_top(6)
        content.set_margin_bottom(6)
        content.set_margin_start(8)
        content.set_margin_end(8)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.append(Gtk.Label(label=f"Recognized as: {wrong}", xalign=0, wrap=True))
        text.append(
            Gtk.Label(label=f"Canonical text: {canonical}", xalign=0, wrap=True)
        )
        content.append(text)
        remove_button = Gtk.Button(label="Remove")
        remove_button.connect("clicked", self._on_remove_correction, row)
        content.append(remove_button)
        row.set_child(content)
        self.corrections_list.append(row)

    def _on_remove_correction(self, button: Gtk.Button, row: Gtk.ListBoxRow) -> None:
        del button
        index = row.get_index()
        if index < 0 or index >= len(self._correction_pairs):
            self._show_error("The correction could not be removed safely.")
            return
        del self._correction_pairs[index]
        self.corrections_list.remove(row)
        self._show_message(
            "Correction removed locally from this list. Use Save explicit "
            "corrections to store the change."
        )

    def _on_save_corrections(self, button: Gtk.Button) -> None:
        del button
        self.save_corrections()

    def save_corrections(self) -> None:
        try:
            count = self._controller.save_corrections(tuple(self._correction_pairs))
            normalized_pairs = self._controller.load_corrections()
        except SettingsError as error:
            self._show_error(str(error))
        except Exception:
            self._show_error("The explicit corrections could not be saved safely.")
        else:
            self._replace_correction_rows(normalized_pairs)
            self._show_message(
                f"Saved {count} explicit correction pairs. {APPLY_NOTICE}"
            )

    def _replace_microphone_priority_rows(self, priority: Sequence[str]) -> None:
        normalized = tuple(priority)
        if len(normalized) != len(MICROPHONE_CATEGORIES) or set(normalized) != set(
            MICROPHONE_CATEGORIES
        ):
            normalized = DEFAULT_MICROPHONE_PRIORITY

        child = self.microphone_priority_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.microphone_priority_list.remove(child)
            child = next_child

        self._microphone_priority = list(normalized)
        last_index = len(self._microphone_priority) - 1
        for index, category in enumerate(self._microphone_priority):
            row = Gtk.ListBoxRow()
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            content.set_margin_top(6)
            content.set_margin_bottom(6)
            content.set_margin_start(8)
            content.set_margin_end(8)

            rank = Gtk.Label(label=f"{index + 1}", width_chars=2)
            content.append(rank)

            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text.set_hexpand(True)
            text.append(
                Gtk.Label(
                    label=_MICROPHONE_CATEGORY_LABELS[category],
                    xalign=0,
                    wrap=True,
                )
            )
            description = Gtk.Label(
                label=_MICROPHONE_CATEGORY_DESCRIPTIONS[category],
                xalign=0,
                wrap=True,
            )
            description.add_css_class("dim-label")
            text.append(description)
            content.append(text)

            move_up = Gtk.Button(label="Move up")
            move_up.set_sensitive(index > 0)
            move_up.connect("clicked", self._on_move_microphone_category, category, -1)
            content.append(move_up)

            move_down = Gtk.Button(label="Move down")
            move_down.set_sensitive(index < last_index)
            move_down.connect("clicked", self._on_move_microphone_category, category, 1)
            content.append(move_down)

            row.set_child(content)
            self.microphone_priority_list.append(row)

    def _on_move_microphone_category(
        self,
        button: Gtk.Button,
        category: str,
        offset: int,
    ) -> None:
        del button
        try:
            current_index = self._microphone_priority.index(category)
        except ValueError:
            self._show_error("The microphone priority could not be changed safely.")
            return
        target_index = current_index + offset
        if target_index < 0 or target_index >= len(self._microphone_priority):
            return
        priority = list(self._microphone_priority)
        priority[current_index], priority[target_index] = (
            priority[target_index],
            priority[current_index],
        )
        self._replace_microphone_priority_rows(priority)
        self._show_message(
            "Microphone order changed in this window. Save microphone priority "
            "to apply it to future dictations."
        )

    def _on_save_microphone_priority(self, button: Gtk.Button) -> None:
        del button
        self.save_microphone_priority()

    def save_microphone_priority(self) -> None:
        try:
            policy = self._controller.save_microphone_priority(
                tuple(self._microphone_priority)
            )
        except SettingsError as error:
            self._show_error(str(error))
        except Exception:
            self._show_error(
                "The microphone priority setting could not be saved safely."
            )
        else:
            self._replace_microphone_priority_rows(policy.priority)
            self._show_message(
                "Saved microphone priority. The next dictation reevaluates all "
                "usable inputs; an active utterance keeps its current microphone."
            )

    def _on_choose_data_collection_directory(self, button: Gtk.Button) -> None:
        del button
        chooser = Gtk.FileChooserNative.new(
            "Choose dataset folder (local or already mounted)",
            self,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "Select",
            "Cancel",
        )
        current = self.data_collection_directory_entry.get_text().strip()
        if current:
            chooser.set_current_folder(Gio.File.new_for_path(current))
        chooser.connect("response", self._on_data_collection_directory_response)
        self._data_collection_chooser = chooser
        chooser.show()

    def _on_data_collection_directory_response(
        self,
        chooser: Gtk.FileChooserNative,
        response: int,
    ) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            selected = chooser.get_file()
            selected_path = selected.get_path() if selected is not None else None
            if selected_path is None:
                self._show_error(
                    "Choose a local filesystem path or an already-mounted folder."
                )
            else:
                self.data_collection_directory_entry.set_text(selected_path)
        chooser.destroy()
        if chooser is self._data_collection_chooser:
            self._data_collection_chooser = None

    def _on_save_data_collection(self, button: Gtk.Button) -> None:
        del button
        if self._collection_busy:
            return
        enabled = self.data_collection_check.get_active()
        directory_text = self.data_collection_directory_entry.get_text().strip()
        self._collection_busy = True
        self.save_data_collection_button.set_sensitive(False)
        self.choose_data_collection_directory_button.set_sensitive(False)

        def worker() -> None:
            try:
                collection = self._controller.save_data_collection(
                    enabled,
                    directory_text or None,
                )
            except SettingsError as error:
                GLib.idle_add(self._finish_data_collection_save, None, str(error))
            except Exception:
                GLib.idle_add(
                    self._finish_data_collection_save,
                    None,
                    "The local data collection setting could not be saved safely.",
                )
            else:
                GLib.idle_add(
                    self._finish_data_collection_save,
                    collection,
                    None,
                )

        threading.Thread(target=worker, daemon=True).start()

    def save_data_collection(self) -> None:
        enabled = self.data_collection_check.get_active()
        directory_text = self.data_collection_directory_entry.get_text().strip()
        try:
            collection = self._controller.save_data_collection(
                enabled,
                directory_text or None,
            )
        except SettingsError as error:
            self._show_error(str(error))
        except Exception:
            self._show_error(
                "The local data collection setting could not be saved safely."
            )
        else:
            self._apply_data_collection_result(collection)

    def _finish_data_collection_save(self, collection, error: str | None) -> bool:
        self._collection_busy = False
        if self._window_closed:
            return GLib.SOURCE_REMOVE
        self.save_data_collection_button.set_sensitive(True)
        self.choose_data_collection_directory_button.set_sensitive(True)
        if error is not None:
            self._show_error(error)
        elif collection is not None:
            self._apply_data_collection_result(collection)
        return GLib.SOURCE_REMOVE

    def _apply_data_collection_result(self, collection) -> None:
        self.data_collection_check.set_active(collection.enabled)
        directory = collection.directory
        self.data_collection_directory_entry.set_text(
            str(directory) if directory is not None else ""
        )
        if collection.enabled:
            self._show_message(
                "WAV and unreviewed provider-final collection is enabled for the "
                "selected filesystem folder. Each new dictation reads this choice; "
                "keep a remote mount connected."
            )
        else:
            self._show_message(
                "Local training-data collection is disabled. Current or queued "
                "unpublished records will not be written; published records remain."
            )

    def _on_close_request(self, window: Gtk.Window) -> bool:
        del window
        self._window_closed = True
        chooser = self._data_collection_chooser
        self._data_collection_chooser = None
        if chooser is not None:
            chooser.destroy()
        return False

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
        self.clear_key_button.set_sensitive(not busy)
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
