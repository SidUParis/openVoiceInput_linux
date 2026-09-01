from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
except ValueError:
    pytest.skip("GTK4 introspection data is not installed", allow_module_level=True)

from gi.repository import Gio, GLib, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("a GTK display is not available", allow_module_level=True)

from murmur_voice.data_collection import DataCollectionConfig  # noqa: E402
from murmur_voice.control import LastReview, ReviewSubmitReply  # noqa: E402
from murmur_voice.interaction import InteractionConfig  # noqa: E402
from murmur_voice.output_style import OutputStyleConfig  # noqa: E402
from murmur_voice.output_target import OutputTargetConfig  # noqa: E402
from murmur_voice.microphone_policy import (  # noqa: E402
    DEFAULT_MICROPHONE_PRIORITY,
    MicrophonePolicyConfig,
)
from murmur_voice.settings_app import (  # noqa: E402
    APPLY_NOTICE,
    SETTINGS_HELP,
    SettingsApplication,
    SettingsWindow,
    main,
)
from murmur_voice.settings_controller import (  # noqa: E402
    CORRECTION_TEXT_LIMIT,
    AdaptiveLearningSnapshot,
    DatasetStatistics,
    KeyState,
    ProviderSelection,
    ServiceSnapshot,
    SettingsError,
)


class FakeController:
    def __init__(self) -> None:
        self.saved_key = None
        self.saved_provider = None
        self.loaded_provider_selection = ProviderSelection("volcengine", None)
        self.saved_vocabulary = None
        self.save_vocabulary_calls = 0
        self.saved_corrections = None
        self.saved_microphone_priority = None
        self.saved_data_collection = None
        self.saved_interaction = None
        self.saved_output_style = None
        self.saved_output_target = None
        self.submitted_adaptive_feedback = None
        self.submitted_last_review = None
        self.review_submit_reply = ReviewSubmitReply(
            True,
            "review-submitted",
            "explicit-feedback-activated",
            "feedback-disabled",
        )
        self.loaded_last_review = LastReview(
            "utterance-1",
            "Ostro uses openai",
            "Ostro uses OpenAI",
        )
        self.last_review_error = None
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
        self.loaded_interaction = InteractionConfig()
        self.loaded_output_style = OutputStyleConfig()
        self.loaded_output_target = OutputTargetConfig()
        self.loaded_dataset_statistics = DatasetStatistics("disabled")
        self.dataset_statistics_calls = 0
        self.dataset_statistics_started = threading.Event()
        self.dataset_statistics_gate = None

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

    def provider_selection(self):
        return self.loaded_provider_selection

    def save_provider(self, api_key, provider, model=None):
        if self.key_error is not None:
            raise self.key_error
        self.saved_key = api_key
        self.saved_provider = (provider, model)
        self.loaded_provider_selection = ProviderSelection(provider, model)
        return self.loaded_provider_selection

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

    def submit_adaptive_feedback(self, provider_text, preferred_text):
        self.submitted_adaptive_feedback = (provider_text, preferred_text)
        return "explicit-feedback-activated"

    def load_last_review(self):
        if self.last_review_error is not None:
            raise self.last_review_error
        return self.loaded_last_review

    def submit_last_review(self, utterance_id, spoken_verbatim):
        self.submitted_last_review = (utterance_id, spoken_verbatim)
        return self.review_submit_reply

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

    def load_interaction(self):
        return self.loaded_interaction

    def save_interaction(
        self, mode, minimum_hold_milliseconds, release_timeout_seconds
    ):
        self.saved_interaction = (
            mode,
            minimum_hold_milliseconds,
            release_timeout_seconds,
        )
        self.loaded_interaction = InteractionConfig(*self.saved_interaction)
        return self.loaded_interaction

    def load_output_style(self):
        return self.loaded_output_style

    def save_output_style(self, mode):
        self.saved_output_style = mode
        self.loaded_output_style = OutputStyleConfig(mode)
        return self.loaded_output_style

    def load_output_target(self):
        return self.loaded_output_target

    def save_output_target(self, target):
        self.saved_output_target = target
        self.loaded_output_target = OutputTargetConfig(target)
        return self.loaded_output_target

    def load_dataset_statistics(self):
        self.dataset_statistics_calls += 1
        self.dataset_statistics_started.set()
        if self.dataset_statistics_gate is not None:
            self.dataset_statistics_gate.wait(timeout=2)
        return self.loaded_dataset_statistics

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
        refresh_statistics_on_start=False,
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


def test_settings_use_seven_chinese_first_pages_and_native_cards(window):
    settings_window, _ = window

    assert isinstance(settings_window.settings_sidebar, Gtk.StackSidebar)
    assert settings_window.settings_sidebar.get_stack() is (
        settings_window.settings_stack
    )
    assert settings_window.settings_stack.get_pages().get_n_items() == 7
    assert settings_window.settings_stack.get_visible_child_name() == "overview"

    expected_pages = {
        "overview": "首页",
        "cloud": "云端识别",
        "interaction": "快捷键与按住说话",
        "vocabulary": "个人词表",
        "corrections": "纠错学习",
        "microphones": "麦克风",
        "collection": "数据留存",
    }
    for name, title in expected_pages.items():
        child = settings_window.settings_stack.get_child_by_name(name)
        assert child is not None
        assert title in _label_texts(child)
    sidebar_copy = " ".join(_label_texts(settings_window.settings_sidebar))
    assert all(title in sidebar_copy for title in expected_pages.values())

    cards = [
        child
        for child in _descendants(settings_window)
        if child.has_css_class("settings-card")
    ]
    assert len(cards) >= 8


def test_interaction_mode_is_user_selected_and_never_hardcodes_right_alt(window):
    settings_window, controller = window
    interaction_page = settings_window.settings_stack.get_child_by_name("interaction")
    copy = " ".join(_label_texts(interaction_page))

    assert settings_window.toggle_mode_button.get_active() is True
    assert settings_window.minimum_hold_spin.get_sensitive() is False
    assert "具体按键始终由你" in copy
    assert "Right Alt" not in copy
    assert "右 Alt" not in copy
    assert "Wayland" in settings_window.interaction_boundary_label.get_text()

    settings_window.push_to_talk_mode_button.set_active(True)
    settings_window.minimum_hold_spin.set_value(250)
    settings_window.release_timeout_spin.set_value(90)
    settings_window.save_interaction()

    assert controller.saved_interaction == ("push_to_talk", 250, 90)
    assert settings_window.minimum_hold_spin.get_sensitive() is True
    assert "下一次按下" in settings_window.message_label.get_text()
    assert controller.service_actions == []


def test_overview_presents_lightweight_boundary_without_personal_hotkey(window):
    settings_window, _ = window
    overview = settings_window.settings_stack.get_child_by_name("overview")
    copy = " ".join(_label_texts(overview))

    assert "轻量" in copy
    assert "IBus 原生" in copy
    assert "中文优先" in copy
    assert "使用你设置的快捷键" in copy
    assert "打开首页不会启动麦克风" in copy
    assert "右 Alt" not in copy


def test_overview_has_private_usage_metrics_recent_status_and_quick_actions(window):
    settings_window, _ = window
    overview = settings_window.settings_stack.get_child_by_name("overview")
    copy = " ".join(_label_texts(overview))

    for expected in (
        "今日字数",
        "今日时长",
        "听写次数",
        "累计字数",
        "累计时长",
        "累计听写",
        "最近留存",
        "最近状态",
        "完善个人词表",
        "管理纠错学习",
        "选择麦克风",
        "查看数据留存",
    ):
        assert expected in copy
    assert "数据集根目录下不含正文的 usage 索引" in copy
    assert "最近识别" not in copy


def test_ready_statistics_render_without_transcript_or_path(window):
    settings_window, _ = window
    private_text = "private-transcript-that-must-not-appear"
    statistics = DatasetStatistics(
        state="ready",
        today_characters=1234,
        today_seconds=65,
        today_utterances=7,
        total_characters=9876,
        total_seconds=3665,
        total_utterances=42,
        latest_recorded_at=datetime(2026, 8, 31, 10, 30, tzinfo=timezone.utc),
    )

    settings_window._apply_dataset_statistics(statistics)

    assert settings_window.today_characters_label.get_text() == "1,234"
    assert settings_window.today_duration_label.get_text() == "1 分钟"
    assert settings_window.today_utterances_label.get_text() == "7"
    assert settings_window.total_characters_label.get_text() == "9,876"
    assert settings_window.total_duration_label.get_text() == "1 小时 1 分"
    assert settings_window.total_utterances_label.get_text() == "42"
    assert settings_window.latest_activity_label.get_text() != "—"
    assert "未读取或展示任何转写正文" in (
        settings_window.statistics_status_label.get_text()
    )
    assert private_text not in " ".join(_label_texts(settings_window))


@pytest.mark.parametrize(
    ("state", "expected"),
    (
        ("disabled", "不会扫描以前选择过的目录"),
        ("unavailable", "普通听写仍可继续"),
        ("unindexed", "不会读取 record.json 来回填"),
    ),
)
def test_disabled_or_unavailable_statistics_are_unknown_not_zero(
    window, state, expected
):
    settings_window, _ = window

    settings_window._apply_dataset_statistics(DatasetStatistics(state))

    assert settings_window.today_characters_label.get_text() == "—"
    assert settings_window.total_utterances_label.get_text() == "—"
    assert expected in settings_window.statistics_status_label.get_text()


def test_statistics_refresh_is_background_and_single_flight(window):
    settings_window, controller = window
    controller.loaded_dataset_statistics = DatasetStatistics(
        state="ready",
        today_characters=9,
        total_characters=9,
        today_utterances=1,
        total_utterances=1,
    )
    controller.dataset_statistics_gate = threading.Event()

    started_at = time.monotonic()
    settings_window.refresh_dataset_statistics()
    elapsed = time.monotonic() - started_at
    assert elapsed < 0.25
    assert controller.dataset_statistics_started.wait(timeout=1)

    settings_window.refresh_dataset_statistics()
    assert controller.dataset_statistics_calls == 1
    assert settings_window.refresh_statistics_button.get_sensitive() is False

    controller.dataset_statistics_gate.set()
    deadline = time.monotonic() + 2
    context = GLib.MainContext.default()
    while settings_window._statistics_busy and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)

    assert settings_window._statistics_busy is False
    assert settings_window.today_characters_label.get_text() == "9"


def test_late_statistics_completion_does_not_update_a_closed_window(window):
    settings_window, _ = window
    original = settings_window.statistics_status_label.get_text()
    settings_window._statistics_busy = True
    settings_window._window_closed = True

    result = settings_window._finish_dataset_statistics(
        settings_window._statistics_generation,
        DatasetStatistics(state="ready", total_utterances=99),
    )

    assert result is False
    assert settings_window._statistics_busy is False
    assert settings_window.statistics_status_label.get_text() == original


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
    assert "你选择的服务" in notice
    assert "账号" in notice
    assert "计费" in notice
    assert "无法撤回" in notice
    assert "MiniMax" in notice


def test_ready_provider_can_be_selected_without_exposing_an_existing_key(window):
    settings_window, controller = window

    assert settings_window._selected_provider_id() == "volcengine"
    settings_window.provider_combo.set_selected(1)
    settings_window.key_entry.set_text("qwen-key-sentinel")

    settings_window.save_key()

    assert controller.saved_provider == ("qwen", None)
    assert controller.saved_key == "qwen-key-sentinel"
    assert settings_window.key_entry.get_text() == ""
    assert settings_window._selected_provider_id() == "qwen"
    assert "MiniMax" not in settings_window.provider_description_label.get_text()


def test_output_style_is_faithful_by_default_and_discloses_clean_boundary(window):
    settings_window, _ = window

    assert settings_window.faithful_output_button.get_active() is True
    assert settings_window.clean_output_button.get_active() is False
    notice = settings_window.output_style_notice_label.get_text()
    assert "本机" in notice
    assert "不调用 LLM" in notice
    assert "不会为清理发起额外网络请求" in notice
    assert "术语、数字、大小写" in notice
    assert "回退为识别原文" in notice
    assert "跳过自动学习" in notice


def test_clean_output_style_save_applies_next_utterance_without_service_action(window):
    settings_window, controller = window
    settings_window.clean_output_button.set_active(True)

    settings_window.save_output_style()

    assert controller.saved_output_style == "clean"
    assert settings_window.clean_output_button.get_active() is True
    assert "下一条听写" in settings_window.message_label.get_text()
    assert controller.service_actions == []


def test_remote_desktop_output_defaults_to_caret_and_discloses_boundaries(window):
    settings_window, _ = window

    assert settings_window.caret_output_target_button.get_active() is True
    assert settings_window.clipboard_output_target_button.get_active() is False
    notice = settings_window.output_target_notice_label.get_text()
    assert "默认关闭" in notice
    assert "authoritative final" in notice
    assert "不复制实时 partial" in notice
    assert "不自动粘贴" in notice
    assert "手动按 Ctrl+V" in notice
    assert "其他应用可能随后覆盖剪贴板" in notice
    assert "本机与远程会话" in notice
    assert "密码、API Key、验证码" in notice
    assert "surrounding text" in notice
    assert "不会启动自动纠错学习" in notice
    assert "xclip" in notice
    assert "wl-copy" in notice


def test_clipboard_target_save_is_explicit_and_never_starts_service(window):
    settings_window, controller = window
    settings_window.clipboard_output_target_button.set_active(True)

    settings_window.save_output_target()

    assert controller.saved_output_target == "clipboard"
    assert settings_window.clipboard_output_target_button.get_active() is True
    assert "下一条终稿" in settings_window.message_label.get_text()
    assert "手动按 Ctrl+V" in settings_window.message_label.get_text()
    assert controller.service_actions == []


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

    assert "当前识别服务" in explanation
    assert "下一次听写" in explanation
    assert "拆分多处替换" in explanation
    assert "中等置信与冲突项" in explanation


def test_adaptive_view_distinguishes_sources_from_effective_provider_context(window):
    settings_window, _ = window
    settings_window._replace_adaptive_learning(
        AdaptiveLearningSnapshot(
            statistics={
                "active": 3,
                "candidate": 2,
                "conflicted": 1,
                "suspended": 0,
                "archived": 0,
                "total": 6,
            },
            last_result={"reason_code": "explicitly-suppressed-capacity"},
            review_entries=(),
            provider_view={
                "explicit_vocabulary_count": 4,
                "manual_correction_count": 2,
                "effective_correction_count": 4,
                "manual_effective_count": 2,
                "adaptive_effective_count": 2,
                "adaptive_suppressed_count": 1,
                "suppression_reasons": {"suppressed-capacity": 1},
            },
        )
    )

    summary = settings_window.adaptive_provider_view_label.get_text()
    assert "明确词汇 4" in summary
    assert "明确纠错 2" in summary
    assert "自适应生效 2" in summary
    assert "纠错上下文共 4" in summary
    assert "另有 1 条" in summary
    assert "容量已满" in settings_window.adaptive_recent_label.get_text()


def test_cross_application_feedback_entry_is_explicit_and_clears_after_submit(window):
    settings_window, controller = window
    assert settings_window.adaptive_provider_entry.get_editable() is False
    settings_window._on_load_last_review(settings_window.load_last_review_button)
    assert settings_window.adaptive_provider_entry.get_text() == "Ostro uses openai"
    assert settings_window.adaptive_preferred_entry.get_text() == "Ostro uses openai"
    assert settings_window.adaptive_delivered_entry.get_text() == "Ostro uses OpenAI"
    assert settings_window.adaptive_delivered_entry.get_editable() is False
    settings_window.adaptive_preferred_entry.set_text("Austral uses OpenAI")

    settings_window._on_submit_adaptive_feedback(
        settings_window.submit_adaptive_feedback_button
    )

    assert controller.submitted_last_review == (
        "utterance-1",
        "Austral uses OpenAI",
    )
    assert settings_window.adaptive_provider_entry.get_text() == ""
    assert settings_window.adaptive_preferred_entry.get_text() == ""
    assert settings_window.adaptive_delivered_entry.get_text() == ""
    message = settings_window.message_label.get_text()
    assert "下一次听写" in message
    assert "数据留存未启用" in message


def test_review_last_opens_correction_page_and_never_calls_service(window):
    settings_window, controller = window

    settings_window.open_last_review()

    assert settings_window.settings_stack.get_visible_child_name() == "corrections"
    assert settings_window.adaptive_provider_entry.get_text() == "Ostro uses openai"
    assert settings_window.adaptive_provider_entry.get_editable() is False
    assert settings_window.adaptive_delivered_entry.get_text() == "Ostro uses OpenAI"
    assert "实际说出的逐字内容" in settings_window.message_label.get_text()
    assert controller.service_actions == []


@pytest.mark.parametrize(
    ("feedback_code", "expected"),
    (
        ("feedback-failed", "训练反馈未能加入保存队列"),
        ("feedback-queued", "尚未确认最终落盘"),
    ),
)
def test_review_submission_distinguishes_feedback_persistence(
    window, feedback_code, expected
):
    settings_window, controller = window
    controller.review_submit_reply = ReviewSubmitReply(
        True,
        "review-submitted",
        "explicit-feedback-activated",
        feedback_code,
    )
    settings_window.open_last_review()
    settings_window.adaptive_preferred_entry.set_text("Austral uses OpenAI")

    settings_window._on_submit_adaptive_feedback(
        settings_window.submit_adaptive_feedback_button
    )

    assert expected in settings_window.message_label.get_text()
    assert settings_window._loaded_review_id is None


def test_review_copy_forbids_polishing_language_and_handles_expiry(window):
    settings_window, controller = window
    controller.loaded_last_review = None

    settings_window._on_load_last_review(settings_window.load_last_review_button)

    assert settings_window.adaptive_provider_entry.get_text() == ""
    assert settings_window.adaptive_preferred_entry.get_text() == ""
    assert settings_window.adaptive_delivered_entry.get_text() == ""
    assert "十分钟" in settings_window.message_label.get_text()
    notice = settings_window.adaptive_feedback_notice_label.get_text()
    assert "去口头词" in notice
    assert "润色" in notice
    assert "ASR 标注" in notice


def test_failed_review_reload_clears_all_previous_private_text(window):
    settings_window, controller = window
    settings_window.open_last_review()
    assert settings_window.adaptive_provider_entry.get_text()
    assert settings_window.adaptive_preferred_entry.get_text()
    assert settings_window.adaptive_delivered_entry.get_text()
    controller.last_review_error = SettingsError("review unavailable")

    settings_window._on_load_last_review(settings_window.load_last_review_button)

    assert settings_window._loaded_review_id is None
    assert settings_window.adaptive_provider_entry.get_text() == ""
    assert settings_window.adaptive_preferred_entry.get_text() == ""
    assert settings_window.adaptive_delivered_entry.get_text() == ""


@pytest.mark.parametrize("failure_kind", ("settings-error", "non-ok"))
def test_failed_review_submission_clears_all_private_text(window, failure_kind):
    settings_window, controller = window
    settings_window.open_last_review()
    settings_window.adaptive_preferred_entry.set_text("human verbatim")
    if failure_kind == "settings-error":

        def fail(_utterance_id, _spoken_verbatim):
            raise SettingsError("The recent recognition result expired.")

        controller.submit_last_review = fail
    else:
        controller.review_submit_reply = ReviewSubmitReply(False, "stale-review")

    settings_window._on_submit_adaptive_feedback(
        settings_window.submit_adaptive_feedback_button
    )

    assert settings_window._loaded_review_id is None
    assert settings_window.adaptive_provider_entry.get_text() == ""
    assert settings_window.adaptive_preferred_entry.get_text() == ""
    assert settings_window.adaptive_delivered_entry.get_text() == ""


def test_review_last_command_line_is_forwardable_without_registering_a_hotkey():
    class Window:
        def __init__(self):
            self.presentations = 0
            self.reviews = 0

        def present(self):
            self.presentations += 1

        def open_last_review(self):
            self.reviews += 1

    class RecordingApplication(SettingsApplication):
        def __init__(self):
            super().__init__(FakeController())
            self.activations = 0
            self._window = Window()

        def activate(self):
            self.activations += 1
            self.do_activate()

    class CommandLine:
        def __init__(self, arguments):
            self.arguments = arguments
            self.errors = []

        def get_arguments(self):
            return self.arguments

        def printerr(self, message):
            self.errors.append(message)

    application = RecordingApplication()
    command_line = CommandLine(["open-voice-input-settings", "--review-last"])

    assert application.do_command_line(command_line) == 0
    assert application.activations == 1
    assert application._review_last_requested is False
    assert application._window.presentations == 1
    assert application._window.reviews == 1
    assert command_line.errors == []

    # A second process forwards the same command line to the already-running
    # application instance and reopens/reloads the same review page.
    assert application.do_command_line(command_line) == 0
    assert application.activations == 2
    assert application._window.presentations == 2
    assert application._window.reviews == 2

    invalid = CommandLine(["open-voice-input-settings", "--listen-to-all-keys"])
    assert application.do_command_line(invalid) == 2
    assert application.activations == 2
    assert invalid.errors == ["unsupported settings argument\n"]


@pytest.mark.parametrize("help_argument", ("--help", "-h"))
def test_help_exits_without_registering_gtk_application(
    monkeypatch, capsys, help_argument
):
    def fail_if_constructed():
        raise AssertionError("help must not register a GtkApplication")

    monkeypatch.setattr(
        "murmur_voice.settings_app.SettingsApplication",
        fail_if_constructed,
    )

    assert main(["open-voice-input-settings", help_argument]) == 0
    captured = capsys.readouterr()
    assert captured.out == SETTINGS_HELP
    assert captured.err == ""


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


@pytest.mark.parametrize(
    ("status_code", "expected"),
    (
        ("clipboard-armed", "下一条终稿会复制"),
        ("clipboard-ready", "上一条终稿已复制；剪贴板可能已被其他应用覆盖"),
        ("clipboard-unavailable", "xclip（X11）或 wl-clipboard（Wayland）"),
        ("clipboard-copy-failed", "没有自动粘贴"),
        ("output-target-invalid", "远程桌面页重新保存"),
    ),
)
def test_clipboard_status_is_actionable(window, status_code, expected):
    settings_window, _ = window

    settings_window._set_service_snapshot(
        ServiceSnapshot("active", "idle", status_code)
    )

    assert expected in settings_window.service_status_label.get_text()


def test_local_collection_is_off_by_default_and_discloses_exact_scope(window):
    settings_window, _ = window

    notice = settings_window.data_collection_notice_label.get_text()

    assert settings_window.data_collection_check.get_active() is False
    assert settings_window.data_collection_directory_entry.get_text() == ""
    assert settings_window.data_collection_directory_entry.get_editable() is False
    assert "默认关闭" in notice
    assert "当前识别服务的最终结果已成功确认" in notice
    assert "WAV" in notice
    assert "未经复核的伪标签" in notice
    assert "provider_final" in notice
    assert "delivery" in notice
    assert "实际交付" in notice
    assert "可从原文重放" in notice
    assert "机器生成" in notice
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
        "在所选目录保留 WAV、原始识别与实际交付结果"
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
        refresh_statistics_on_start=False,
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
