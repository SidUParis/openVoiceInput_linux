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
        self.save_vocabulary_calls = 0
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
        self.save_vocabulary_calls += 1
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


def _label_texts(widget):
    return [
        child.get_text()
        for child in _descendants(widget)
        if isinstance(child, Gtk.Label)
    ]


def test_settings_use_six_chinese_first_pages_and_native_cards(window):
    settings_window, _ = window

    assert isinstance(settings_window.settings_sidebar, Gtk.StackSidebar)
    assert settings_window.settings_sidebar.get_stack() is (
        settings_window.settings_stack
    )
    assert settings_window.settings_stack.get_pages().get_n_items() == 6
    assert settings_window.settings_stack.get_visible_child_name() == "overview"

    expected_pages = {
        "overview": "概览与服务",
        "cloud": "云端识别",
        "vocabulary": "个人词表",
        "corrections": "纠错学习",
        "microphones": "麦克风",
        "collection": "数据留存",
    }
    for name, title in expected_pages.items():
        child = settings_window.settings_stack.get_child_by_name(name)
        assert child is not None
        assert title in _label_texts(child)

    cards = [
        child
        for child in _descendants(settings_window)
        if child.has_css_class("settings-card")
    ]
    assert len(cards) >= 8


def test_overview_presents_lightweight_boundary_without_personal_hotkey(window):
    settings_window, _ = window
    overview = settings_window.settings_stack.get_child_by_name("overview")
    copy = " ".join(_label_texts(overview))

    assert "轻量" in copy
    assert "原生 GTK4" in copy
    assert "不捆绑本地大模型" in copy
    assert "使用你设置的快捷键" in copy
    assert "右 Alt" not in copy


def test_password_entry_is_empty_masked_and_has_no_reveal_control(window):
    settings_window, _ = window

    assert isinstance(settings_window.key_entry, Gtk.PasswordEntry)
    assert settings_window.key_entry.get_text() == ""
    assert settings_window.key_entry.get_show_peek_icon() is False
    assert settings_window.key_status_label.get_text() == (
        "已配置。保存的 Key 永远不会显示在窗口中。"
    )
    assert settings_window.overview_key_status_label.get_text() == "已安全配置"


def test_settings_disclose_remote_audio_billing_and_cancel_boundary(window):
    settings_window, _ = window

    notice = settings_window.remote_audio_notice_label.get_text()

    assert "麦克风音频" in notice
    assert "火山引擎" in notice
    assert "账号" in notice
    assert "计费" in notice
    assert "无法撤回" in notice


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
    assert settings_window.clear_key_button.get_label() == ("确认清除已保存的 Key")
    assert "尚未删除任何内容" in settings_window.message_label.get_text()
    assert secret not in settings_window.message_label.get_text()

    settings_window.clear_key_button.emit("clicked")

    assert controller.clear_key_calls == 1
    assert settings_window.clear_key_button.get_label() == "清除已保存的 Key…"
    assert settings_window.key_entry.get_text() == ""
    assert settings_window.key_status_label.get_text() == "尚未配置 API Key。"
    assert "没有联系任何云服务" in settings_window.message_label.get_text()
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
    assert settings_window.clear_key_button.get_label() == "清除已保存的 Key…"
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


def test_vocabulary_button_performs_one_explicit_save(window):
    settings_window, controller = window
    settings_window.vocabulary_view.get_buffer().set_text("奔驰\nMark")

    settings_window.save_vocabulary_button.emit("clicked")

    assert controller.save_vocabulary_calls == 1
    assert controller.saved_vocabulary == "奔驰\nMark"
    assert "已保存 2 个词条" in settings_window.message_label.get_text()
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
    assert "误识别：existing mistake" in labels
    assert "标准写法：existing canonical form" in labels
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
        if isinstance(widget, Gtk.Button) and widget.get_label() == "移除"
    )
    remove_button.emit("clicked")

    assert len(_listbox_rows(settings_window.corrections_list)) == 1
    settings_window.save_corrections()
    assert controller.saved_corrections == (
        ("existing mistake", "existing canonical form"),
    )
    assert "已保存 1 条明确纠错" in (settings_window.message_label.get_text())
    assert controller.service_actions == []


def test_correction_add_rejects_duplicate_and_conflicting_wrong_form(window):
    settings_window, controller = window
    settings_window.correction_wrong_entry.set_text("existing mistake")
    settings_window.correction_canonical_entry.set_text("existing canonical form")

    settings_window.add_correction()

    assert len(_listbox_rows(settings_window.corrections_list)) == 1
    assert "已经在列表中" in settings_window.message_label.get_text()

    settings_window.correction_canonical_entry.set_text("different canonical form")
    settings_window.add_correction()

    assert len(_listbox_rows(settings_window.corrections_list)) == 1
    assert "另一条标准写法" in (settings_window.message_label.get_text())
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
        if isinstance(widget, Gtk.Button) and widget.get_label() == "移除"
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

    assert "火山引擎" in explanation
    assert "听写请求" in explanation
    assert "5 秒" in explanation
    assert "含糊或冲突" in explanation


def test_service_controls_are_explicit_and_offer_no_restart(window):
    settings_window, controller = window

    assert settings_window.start_service_button.get_label() == "启用并启动"
    assert "取消当前听写" in (settings_window.stop_service_button.get_label())
    assert not hasattr(controller, "restart_service")


def test_microphone_unavailable_status_has_actionable_label(window):
    settings_window, _ = window

    settings_window._set_service_snapshot(
        ServiceSnapshot("active", "idle", "microphone-unavailable")
    )

    label = settings_window.service_status_label.get_text()
    assert "没有可用麦克风" in label
    assert "重新连接或调整输入顺序" in label


def test_microphone_policy_invalid_status_has_repair_action(window):
    settings_window, _ = window

    settings_window._set_service_snapshot(
        ServiceSnapshot("active", "idle", "microphone-policy-invalid")
    )

    label = settings_window.service_status_label.get_text()
    assert "麦克风顺序无效或不安全" in label
    assert "保存一个完整顺序" in label


def test_local_collection_is_off_by_default_and_discloses_exact_scope(window):
    settings_window, _ = window

    notice = settings_window.data_collection_notice_label.get_text()

    assert settings_window.data_collection_check.get_active() is False
    assert settings_window.data_collection_directory_entry.get_text() == ""
    assert settings_window.data_collection_directory_entry.get_editable() is False
    assert "默认关闭" in notice
    assert "火山引擎最终结果已成功确认" in notice
    assert "WAV" in notice
    assert "未经复核的伪标签" in notice
    assert "火山引擎" in notice
    assert "openvoiceinput-dataset-v1" in notice
    assert "已经挂载的远程文件系统" in notice
    assert "不会连接或挂载远程主机" in notice
    assert "Google Drive URL" in notice
    assert "异步" in notice
    assert "已挂载目录中的完整记录" in notice
    assert "Orange" not in notice
    assert "spoken_verbatim" in notice
    assert "preferred_output 会保持为空" in notice
    assert "不会上传数据集" in notice
    assert "不会训练模型" in notice
    assert "没有本地兜底队列" in notice
    assert "尚未发布的排队记录" in notice
    assert "已经发布的记录会继续保留" in notice
    assert "已挂载" in (
        settings_window.data_collection_directory_entry.get_placeholder_text()
    )
    assert settings_window.choose_data_collection_directory_button.get_label() == (
        "选择文件夹…"
    )
    assert settings_window.data_collection_check.get_label() == (
        "在所选目录保留 WAV 与未经复核的 provider_final"
    )
    assert settings_window.save_data_collection_button.get_label() == (
        "保存数据留存设置"
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
    assert "已为所选目录开启" in settings_window.message_label.get_text()
    assert "保持连接" in settings_window.message_label.get_text()
    assert settings_window.overview_collection_status_label.get_text() == "已开启"
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

    assert "你的当前自定义顺序" in notice
    assert "并非项目推荐顺序" in notice
    assert "每次开始新听写前" in notice
    assert "从上到下依次尝试" in notice
    assert "不会中途切换" in notice
    assert "不会移动播放输出" in notice
    assert "不会请求 set-default-source" in notice
    assert "系统音频策略仍可能自行重算默认输入" in notice
    assert "A2DP 不算耳麦麦克风" in notice
    assert "不会自动切换蓝牙通话配置" in notice


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
    assert "无线麦克风" in labels
    assert "耳麦麦克风" in labels
    assert "其他外接麦克风" in labels
    assert "电脑内置麦克风" in labels
    assert all("DJI" not in label for label in labels)

    first_buttons = [
        widget for widget in _descendants(rows[0]) if isinstance(widget, Gtk.Button)
    ]
    last_buttons = [
        widget for widget in _descendants(rows[-1]) if isinstance(widget, Gtk.Button)
    ]
    assert (
        next(
            button for button in first_buttons if button.get_label() == "上移"
        ).get_sensitive()
        is False
    )
    assert (
        next(
            button for button in last_buttons if button.get_label() == "下移"
        ).get_sensitive()
        is False
    )


def test_microphone_priority_reorder_and_save_are_local_and_hot_loaded(window):
    settings_window, controller = window
    first_row = _listbox_rows(settings_window.microphone_priority_list)[0]
    move_down = next(
        widget
        for widget in _descendants(first_row)
        if isinstance(widget, Gtk.Button) and widget.get_label() == "下移"
    )

    move_down.emit("clicked")

    assert tuple(settings_window._microphone_priority) == (
        "headset",
        "dji",
        "external",
        "built-in",
    )
    assert controller.saved_microphone_priority is None
    assert "后续听写" in settings_window.message_label.get_text()

    settings_window.save_microphone_priority()

    assert controller.saved_microphone_priority == (
        "headset",
        "dji",
        "external",
        "built-in",
    )
    message = settings_window.message_label.get_text()
    assert "下一次听写会重新检查可用输入" in message
    assert "一次听写期间" in message
    assert "固定使用同一个麦克风" in message
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
        ("data-collection-failed", "本次听写已经完成"),
        ("data-collection-unavailable", "听写仍会继续"),
    ),
)
def test_optional_collection_status_is_visible_without_marking_service_stopped(
    window, code, expected
):
    settings_window, _ = window

    settings_window._set_service_snapshot(ServiceSnapshot("active", "idle", code))

    label = settings_window.service_status_label.get_text()
    assert "语音服务：运行中" in label
    assert expected in label


def test_late_service_completion_does_not_update_a_closed_window(window):
    settings_window, _ = window
    original = settings_window.service_status_label.get_text()
    settings_window._service_busy = True
    settings_window._window_closed = True

    result = settings_window._finish_service_operation(
        ServiceSnapshot("active", "recording", None), None
    )

    assert result is False
    assert settings_window._service_busy is False
    assert settings_window.service_status_label.get_text() == original
