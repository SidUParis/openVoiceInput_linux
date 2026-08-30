"""Small native GTK4 settings window for voice-provider onboarding."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .microphone_policy import (  # noqa: E402
    DEFAULT_MICROPHONE_PRIORITY,
    MICROPHONE_CATEGORIES,
)
from .interaction import (  # noqa: E402
    DEFAULT_INTERACTION_MODE,
    DEFAULT_MINIMUM_HOLD_MILLISECONDS,
    DEFAULT_RELEASE_TIMEOUT_SECONDS,
)
from .providers import PROVIDER_DESCRIPTORS  # noqa: E402
from .settings_controller import (  # noqa: E402
    CORRECTION_PAIR_LIMIT,
    CORRECTION_TEXT_LIMIT,
    DatasetStatistics,
    ADAPTIVE_FEEDBACK_TEXT_LIMIT,
    AdaptiveLearningSnapshot,
    KeyState,
    ProviderSelection,
    ServiceSnapshot,
    SettingsController,
    SettingsError,
)

APPLICATION_ID = "io.github.SidUParis.OpenVoiceInputLinux.Settings"
APPLY_NOTICE = "已安全保存到本机；下一次听写会自动读取新设置。"

_MICROPHONE_CATEGORY_LABELS = {
    "dji": "无线麦克风",
    "headset": "耳麦麦克风",
    "external": "其他外接麦克风",
    "built-in": "电脑内置麦克风",
}
_MICROPHONE_CATEGORY_DESCRIPTIONS = {
    "dji": "受支持的无线接收器，且发射端已确认在线。",
    "headset": "USB、3.5 mm 或已启用 HSP/HFP 通话模式的蓝牙耳麦。",
    "external": "其他 USB 或外接录音设备。",
    "built-in": "电脑自带的保底输入设备。",
}

_PROVIDERS_BY_ID = {
    descriptor.provider_id: descriptor for descriptor in PROVIDER_DESCRIPTORS
}
_READY_PROVIDERS = tuple(
    descriptor
    for descriptor in PROVIDER_DESCRIPTORS
    if descriptor.availability == "ready"
)

_SERVICE_LABELS = {
    "active": "运行中",
    "activating": "正在启动",
    "deactivating": "正在停止",
    "failed": "启动失败",
    "inactive": "已停止",
    "reloading": "正在重新加载",
    "unknown": "暂时不可用",
}
_SESSION_LABELS = {
    "idle": "等待听写",
    "observing": "正在等待本地修改",
    "recording": "正在录音",
    "starting": "正在打开麦克风",
    "stopping": "正在生成最终文本",
    "unavailable": "控制通道不可用",
    "unknown": "听写状态未知",
}
_STATUS_LABELS = {
    "audio-backpressure": "音频缓冲区已满",
    "capture-start-failed": "麦克风启动失败",
    "data-collection-failed": "可选数据未确认可靠写入，但本次听写已经完成",
    "data-collection-unavailable": "可选数据留存当前不可用，听写仍会继续",
    "final-timeout": "最终识别超时",
    "microphone-unavailable": "没有可用麦克风，请重新连接或调整输入顺序",
    "microphone-policy-invalid": "麦克风顺序无效或不安全，请在设置中保存一个完整顺序",
    "adaptive-correction-failed": "自动纠错未能保存",
    "adaptive-correction-candidate": "已捕获修改，等待你确认后启用",
    "adaptive-correction-conflicted": "已捕获冲突修改，未自动启用",
    "adaptive-correction-learned": "已学习本次修改，后续听写会使用",
    "adaptive-correction-skipped": "本次修改未生成全局纠错规则",
    "recognition-context-invalid": "识别上下文文件无效或不安全",
    "preedit-final-rejected": "当前输入框拒绝了最终文本",
    "preedit-lost": "已失去当前输入框焦点",
    "preedit-rejected": "当前输入框拒绝听写",
    "preedit-unavailable": "当前输入框不支持听写",
    "provider-auth": "云服务身份验证失败",
    "provider-error": "无法连接云端识别服务",
    "recording-limit-warning": "即将达到单次录音时长限制",
    "start-timeout": "麦克风启动超时，请重试",
}

_ADAPTIVE_REASON_LABELS = {
    "active-learned": "已自动启用高置信纠错",
    "active-and-candidates-saved": "已启用明确项，其余已进入候选",
    "active-suppressed": "规则已保留，但因冲突或覆盖关系未发送",
    "candidates-saved": "多处修改已拆分为候选，等待确认",
    "conflict-recorded": "发现同一误识别的不同写法，已暂停自动使用",
    "diff-too-complex": "修改跨度过大，未在输入关键路径执行复杂比对",
    "edit-outside-committed-span": "检测到听写结果之外的修改，未自动推断",
    "explicitly-activated": "候选已由你明确确认",
    "explicit-feedback-activated": "这次明确修改已拆分并启用",
    "explicit-feedback-suppressed": "修改已保留，但安全规则阻止了自动下发",
    "explicit-feedback-insertion-or-deletion": "修改主要是增删内容，未生成全局词汇规则",
    "explicit-feedback-no-change": "两句内容相同，没有需要学习的修改",
    "explicit-feedback-diff-too-complex": "两句差异跨度过大，未生成全局词汇规则",
    "explicit-feedback-unsafe-or-broad-replacement": "修改更像整句润色，未生成全局词汇规则",
    "insertion-or-deletion": "检测到增删内容；未当作全局词汇规则",
    "no-change": "观察期内没有检测到修改",
    "observation-handler-unavailable": "自动学习组件当前不可用",
    "observation-timeout": "修改未在观察期内完成",
    "selection-active": "结束时仍有文字被选中，未自动推断",
    "surrounding-text-unavailable": "当前应用没有提供可信的修改文本",
    "too-many-edits": "修改范围过多，未自动生成规则",
    "unsafe-or-broad-replacement": "修改更像润色或宽泛重写，未生成全局规则",
}

_SETTINGS_CSS = """
.settings-shell {
  background-color: @theme_bg_color;
}

.settings-sidebar-shell {
  background-color: alpha(@theme_base_color, 0.82);
  border-right: 1px solid alpha(@theme_fg_color, 0.10);
}

.settings-sidebar row {
  border-radius: 10px;
  margin: 3px 8px;
  min-height: 40px;
}

.settings-sidebar row:selected {
  background-color: alpha(#1c71d8, 0.14);
}

.page-eyebrow {
  color: #3584e4;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.page-subtitle,
.dim-label {
  color: alpha(@theme_fg_color, 0.68);
}

.settings-card {
  background-color: alpha(@theme_base_color, 0.96);
  border: 1px solid alpha(@theme_fg_color, 0.09);
  border-radius: 18px;
  box-shadow: 0 3px 12px alpha(#000000, 0.07);
}

.hero-card {
  background-image: linear-gradient(135deg, alpha(#1c71d8, 0.14), alpha(#62a0ea, 0.05));
  border-color: alpha(#1c71d8, 0.24);
}

.status-tile,
.metric-tile {
  background-color: alpha(@theme_fg_color, 0.055);
  border-radius: 14px;
}

.status-value {
  font-weight: 700;
}

.metric-value {
  font-size: 1.65rem;
  font-weight: 800;
}

.metric-detail {
  color: alpha(@theme_fg_color, 0.58);
  font-size: 0.78rem;
}

.section-label {
  color: alpha(@theme_fg_color, 0.72);
  font-size: 0.82rem;
  font-weight: 700;
}

.quick-action {
  background-color: alpha(@theme_fg_color, 0.035);
  border: 1px solid alpha(@theme_fg_color, 0.09);
  border-radius: 14px;
  padding: 4px;
}

.quick-action:hover {
  background-color: alpha(#1c71d8, 0.08);
  border-color: alpha(#1c71d8, 0.22);
}

.success-text {
  color: #2ec27e;
}

.warning-text {
  color: #c88800;
}

.error-text {
  color: #e01b24;
}

.soft-badge {
  background-color: alpha(#3584e4, 0.14);
  border-radius: 999px;
  color: #1c71d8;
  font-size: 0.78rem;
  font-weight: 700;
  padding: 5px 10px;
}

.message-banner {
  background-color: alpha(#3584e4, 0.10);
  border-top: 1px solid alpha(@theme_fg_color, 0.10);
}

.message-banner.error {
  background-color: alpha(#e01b24, 0.12);
  color: #c01c28;
}

.rank-badge {
  background-color: alpha(#3584e4, 0.14);
  border-radius: 999px;
  color: #1c71d8;
  font-weight: 700;
  min-height: 30px;
  min-width: 30px;
}

"""


class SettingsWindow(Gtk.ApplicationWindow):
    """A bounded UI that never receives an existing provider key."""

    def __init__(
        self,
        application: Gtk.Application,
        controller: SettingsController | None = None,
        *,
        refresh_service_on_start: bool = True,
        refresh_statistics_on_start: bool = True,
    ) -> None:
        super().__init__(application=application, title="Open Voice Input 设置")
        self.set_default_size(1020, 900)
        self.set_size_request(800, 620)
        self._controller = controller or SettingsController()
        self._service_busy = False
        self._collection_busy = False
        self._statistics_busy = False
        self._statistics_generation = 0
        self._statistics_timeout_id = 0
        self._window_closed = False
        self._key_clear_armed = False
        self._provider_selection = ProviderSelection("volcengine", None)
        self._correction_pairs: list[tuple[str, str]] = []
        self._microphone_priority: list[str] = list(DEFAULT_MICROPHONE_PRIORITY)
        self._data_collection_chooser: Gtk.FileChooserNative | None = None
        self.connect("close-request", self._on_close_request)

        self._css_provider = Gtk.CssProvider()
        self._css_provider.load_from_string(_SETTINGS_CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                self._css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        header = Gtk.HeaderBar()
        header_title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label(label="Open Voice Input", xalign=0)
        title.add_css_class("heading")
        subtitle = Gtk.Label(label="轻量 Linux 语音输入", xalign=0)
        subtitle.add_css_class("dim-label")
        header_title.append(title)
        header_title.append(subtitle)
        header.set_title_widget(header_title)
        self.set_titlebar(header)

        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        shell.add_css_class("settings-shell")
        self.set_child(shell)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        content.set_vexpand(True)
        shell.append(content)

        sidebar_shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        sidebar_shell.add_css_class("settings-sidebar-shell")
        sidebar_shell.set_size_request(196, -1)
        sidebar_brand = Gtk.Label(label="工作台", xalign=0)
        sidebar_brand.add_css_class("title-3")
        sidebar_brand.set_margin_top(18)
        sidebar_brand.set_margin_start(16)
        sidebar_shell.append(sidebar_brand)
        self.settings_sidebar = Gtk.StackSidebar()
        self.settings_sidebar.add_css_class("settings-sidebar")
        self.settings_sidebar.set_vexpand(True)
        sidebar_shell.append(self.settings_sidebar)
        content.append(sidebar_shell)

        self.settings_stack = Gtk.Stack()
        self.settings_stack.set_hexpand(True)
        self.settings_stack.set_vexpand(True)
        self.settings_stack.set_transition_type(
            Gtk.StackTransitionType.SLIDE_LEFT_RIGHT
        )
        self.settings_stack.set_transition_duration(180)
        self.settings_sidebar.set_stack(self.settings_stack)
        content.append(self.settings_stack)

        overview_page = self._new_page(
            "overview",
            "首页",
            "今天的使用概览、最近状态和常用入口都在这里。",
        )
        cloud_page = self._new_page(
            "cloud",
            "云端识别",
            "选择识别服务，并管理对应凭据与发送边界。",
        )
        interaction_page = self._new_page(
            "interaction",
            "快捷键与按住说话",
            "选择点按切换或按住说话；具体按键始终由你或桌面环境决定。",
        )
        vocabulary_page = self._new_page(
            "vocabulary",
            "个人词表",
            "让姓名、术语与中英法混合表达更稳定。",
        )
        corrections_page = self._new_page(
            "corrections",
            "纠错学习",
            "管理明确替换规则，并了解自动学习的范围。",
        )
        microphone_page = self._new_page(
            "microphones",
            "麦克风",
            "由你决定输入设备类别的尝试顺序。",
        )
        collection_page = self._new_page(
            "collection",
            "数据留存",
            "默认关闭；只在你明确同意后保存可复核的数据。",
        )

        hero = self._append_card(overview_page, "hero-card")
        hero_badge = Gtk.Label(label="IBus 原生 · 中文优先 · 轻量 GTK4")
        hero_badge.add_css_class("soft-badge")
        hero_badge.set_halign(Gtk.Align.START)
        hero.append(hero_badge)
        hero_title = Gtk.Label(label="让表达跟上你的思路", xalign=0)
        hero_title.add_css_class("title-1")
        hero.append(hero_title)
        hero_copy = Gtk.Label(
            label=(
                "使用你设置的快捷键开始或结束听写。界面只负责小型本地配置与"
                "状态控制，识别结果直接进入当前光标；打开首页不会启动麦克风。"
            ),
            xalign=0,
            wrap=True,
        )
        hero_copy.add_css_class("page-subtitle")
        hero.append(hero_copy)

        today_card = self._append_card(overview_page)
        today_heading_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        today_heading_copy = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
        )
        today_heading_copy.set_hexpand(True)
        today_title = Gtk.Label(label="今天", xalign=0)
        today_title.add_css_class("title-3")
        today_heading_copy.append(today_title)
        self.statistics_status_label = Gtk.Label(
            label="正在准备本地统计…",
            xalign=0,
            wrap=True,
        )
        self.statistics_status_label.add_css_class("dim-label")
        today_heading_copy.append(self.statistics_status_label)
        today_heading_row.append(today_heading_copy)
        self.refresh_statistics_button = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text="刷新本地使用统计",
        )
        self.refresh_statistics_button.add_css_class("flat")
        self.refresh_statistics_button.connect(
            "clicked", self._on_refresh_dataset_statistics
        )
        today_heading_row.append(self.refresh_statistics_button)
        today_card.append(today_heading_row)

        today_grid = Gtk.Grid(column_spacing=10, column_homogeneous=True)
        self.today_characters_label = self._append_metric_tile(
            today_grid,
            0,
            "今日字数",
            "—",
            "按非空白字符统计",
        )
        self.today_duration_label = self._append_metric_tile(
            today_grid,
            1,
            "今日时长",
            "—",
            "已发布音频",
        )
        self.today_utterances_label = self._append_metric_tile(
            today_grid,
            2,
            "听写次数",
            "—",
            "仅统计已发布记录",
        )
        today_card.append(today_grid)

        cumulative_card = self._append_card(overview_page)
        self._append_card_heading(
            cumulative_card,
            "累计使用",
            "只汇总数据集根目录下不含正文的 usage 索引；不会打开或展示 transcript。",
        )
        cumulative_grid = Gtk.Grid(column_spacing=10, column_homogeneous=True)
        self.total_characters_label = self._append_metric_tile(
            cumulative_grid,
            0,
            "累计字数",
            "—",
            "隐私摘要覆盖范围内",
        )
        self.total_duration_label = self._append_metric_tile(
            cumulative_grid,
            1,
            "累计时长",
            "—",
            "音频内容不会被读取",
        )
        self.total_utterances_label = self._append_metric_tile(
            cumulative_grid,
            2,
            "累计听写",
            "—",
            "不包含暂存记录",
        )
        self.latest_activity_label = self._append_metric_tile(
            cumulative_grid,
            3,
            "最近留存",
            "—",
            "只显示时间，不显示正文",
        )
        cumulative_card.append(cumulative_grid)

        quick_card = self._append_card(overview_page)
        self._append_card_heading(
            quick_card,
            "快捷操作",
            "把常用设置放在手边；录音触发方式和识别服务可在后续版本扩展。",
        )
        quick_grid = Gtk.Grid(
            column_spacing=10,
            row_spacing=10,
            column_homogeneous=True,
        )
        self._append_navigation_action(
            quick_grid,
            0,
            0,
            "format-text-symbolic",
            "完善个人词表",
            "姓名与常用术语",
            "vocabulary",
        )
        self._append_navigation_action(
            quick_grid,
            1,
            0,
            "document-edit-symbolic",
            "管理纠错学习",
            "明确替换与自动学习",
            "corrections",
        )
        self._append_navigation_action(
            quick_grid,
            0,
            1,
            "audio-input-microphone-symbolic",
            "选择麦克风",
            "调整输入设备顺序",
            "microphones",
        )
        self._append_navigation_action(
            quick_grid,
            1,
            1,
            "folder-symbolic",
            "查看数据留存",
            "本地或已挂载目录",
            "collection",
        )
        quick_card.append(quick_grid)

        status_card = self._append_card(overview_page)
        self._append_card_heading(
            status_card,
            "最近状态",
            "这里不显示密钥内容，也不会因为打开设置而启动录音。",
        )
        status_grid = Gtk.Grid(column_spacing=10, column_homogeneous=True)
        self.overview_service_status_label = self._append_status_tile(
            status_grid, 0, "语音服务", "尚未刷新"
        )
        self.overview_key_status_label = self._append_status_tile(
            status_grid, 1, "API Key", "正在读取"
        )
        self.overview_collection_status_label = self._append_status_tile(
            status_grid, 2, "数据留存", "正在读取"
        )
        status_card.append(status_grid)

        service_card = self._append_card(overview_page)
        self._append_card_heading(
            service_card,
            "语音服务",
            "启动是显式操作；停止服务会取消正在进行的听写。",
        )
        self.service_status_label = Gtk.Label(
            label="语音服务：正在检查…", xalign=0, wrap=True
        )
        self.service_status_label.add_css_class("status-value")
        service_card.append(self.service_status_label)

        service_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.start_service_button = Gtk.Button(label="启用并启动")
        self.start_service_button.add_css_class("suggested-action")
        self.start_service_button.connect("clicked", self._on_start_service)
        service_actions.append(self.start_service_button)
        self.stop_service_button = Gtk.Button(label="停用并停止（取消当前听写）")
        self.stop_service_button.add_css_class("destructive-action")
        self.stop_service_button.connect("clicked", self._on_stop_service)
        service_actions.append(self.stop_service_button)
        self.refresh_service_button = Gtk.Button(label="刷新状态")
        self.refresh_service_button.connect("clicked", self._on_refresh_service)
        service_actions.append(self.refresh_service_button)
        service_card.append(service_actions)

        lightweight_card = self._append_card(overview_page)
        self._append_card_heading(
            lightweight_card,
            "轻量意味着什么",
            (
                "设置窗口使用系统 GTK4，不加载浏览器内核，也不随软件捆绑数百兆的"
                "本地 ASR 模型。只有你明确开始听写时，音频才会交给已配置的识别服务。"
            ),
        )

        interaction_card = self._append_card(interaction_page)
        self._append_card_heading(
            interaction_card,
            "交互模式",
            "设置只决定按下与松开时的行为，不会抢占或硬编码任何物理按键。",
        )
        self.toggle_mode_button = Gtk.CheckButton(
            label="点按切换：按一次开始，再按一次结束"
        )
        self.push_to_talk_mode_button = Gtk.CheckButton(
            label="按住说话：按下开始，松开后生成最终文本"
        )
        self.push_to_talk_mode_button.set_group(self.toggle_mode_button)
        self.toggle_mode_button.connect("toggled", self._on_interaction_mode_changed)
        self.push_to_talk_mode_button.connect(
            "toggled", self._on_interaction_mode_changed
        )
        interaction_card.append(self.toggle_mode_button)
        interaction_card.append(self.push_to_talk_mode_button)

        hold_grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        hold_grid.attach(Gtk.Label(label="过短按压取消（毫秒）", xalign=0), 0, 0, 1, 1)
        self.minimum_hold_spin = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=DEFAULT_MINIMUM_HOLD_MILLISECONDS,
                lower=0,
                upper=2000,
                step_increment=10,
                page_increment=100,
                page_size=0,
            ),
            climb_rate=1,
            digits=0,
        )
        hold_grid.attach(self.minimum_hold_spin, 1, 0, 1, 1)
        hold_grid.attach(
            Gtk.Label(label="松开丢失后自动停止（秒）", xalign=0), 0, 1, 1, 1
        )
        self.release_timeout_spin = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=DEFAULT_RELEASE_TIMEOUT_SECONDS,
                lower=5,
                upper=600,
                step_increment=5,
                page_increment=30,
                page_size=0,
            ),
            climb_rate=1,
            digits=0,
        )
        hold_grid.attach(self.release_timeout_spin, 1, 1, 1, 1)
        interaction_card.append(hold_grid)

        self.interaction_boundary_label = Gtk.Label(
            label=(
                "通用桌面快捷键通常只提供一次激活事件，因此 X11 与 Wayland 都可把"
                "自选快捷键绑定到 `murmur-voice-daemon toggle`。按住说话需要所选"
                "桌面、键盘或辅助工具分别发送 `murmur-voice-daemon press` 与 "
                "`murmur-voice-daemon release`；通用 Wayland 快捷键没有可靠的全局"
                "松开事件，本程序不会假装已经获得它，也不会读取全部 /dev/input。"
                "取消操作使用 `murmur-voice-daemon cancel`。"
            ),
            xalign=0,
            wrap=True,
            selectable=True,
        )
        self.interaction_boundary_label.add_css_class("dim-label")
        interaction_card.append(self.interaction_boundary_label)
        self.save_interaction_button = Gtk.Button(label="保存交互模式")
        self.save_interaction_button.add_css_class("suggested-action")
        self.save_interaction_button.set_halign(Gtk.Align.START)
        self.save_interaction_button.connect("clicked", self._on_save_interaction)
        interaction_card.append(self.save_interaction_button)
        provider_card = self._append_card(cloud_page)
        self._append_card_heading(
            provider_card,
            "识别服务与 API Key",
            "服务与密钥作为一次设置原子保存；窗口从不回填或显示已保存的密钥。",
        )

        provider_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        provider_row.append(Gtk.Label(label="识别服务", xalign=0))
        self.provider_combo = Gtk.DropDown.new_from_strings(
            [descriptor.display_name for descriptor in _READY_PROVIDERS]
        )
        self.provider_combo.set_hexpand(True)
        self.provider_combo.connect("notify::selected", self._on_provider_changed)
        provider_row.append(self.provider_combo)
        provider_card.append(provider_row)

        self.provider_description_label = Gtk.Label(xalign=0, wrap=True)
        self.provider_description_label.add_css_class("dim-label")
        provider_card.append(self.provider_description_label)

        self.key_status_label = Gtk.Label(xalign=0, wrap=True)
        self.key_status_label.add_css_class("status-value")
        provider_card.append(self.key_status_label)

        self.remote_audio_notice_label = Gtk.Label(
            label=(
                "只有明确开始听写后，麦克风音频才会发送至你选择的服务并由你的账号"
                "计费；取消听写无法撤回已经发送的音频片段。MiniMax 仅保留为未来"
                "接入点，尚未提供可选择的实现。"
            ),
            xalign=0,
            wrap=True,
        )
        self.remote_audio_notice_label.add_css_class("dim-label")
        provider_card.append(self.remote_audio_notice_label)

        self.key_entry = Gtk.PasswordEntry()
        self.key_entry.set_show_peek_icon(False)
        self.key_entry.set_property("placeholder-text", "粘贴新的 API Key")
        self.key_entry.set_hexpand(True)
        provider_card.append(self.key_entry)

        key_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.save_key_button = Gtk.Button(label="保存服务与新 Key")
        self.save_key_button.add_css_class("suggested-action")
        self.save_key_button.connect("clicked", self._on_save_key)
        key_actions.append(self.save_key_button)
        self.clear_key_button = Gtk.Button(label="清除已保存的 Key…")
        self.clear_key_button.add_css_class("destructive-action")
        self.clear_key_button.connect("clicked", self._on_clear_key)
        key_actions.append(self.clear_key_button)
        provider_card.append(key_actions)

        vocabulary_card = self._append_card(vocabulary_page)
        self._append_card_heading(
            vocabulary_card,
            "每行一个词",
            "适合姓名、产品名、专业术语，以及经常使用的中英法混合表达。",
        )
        self.vocabulary_help_label = Gtk.Label(
            label=(
                "这些明确添加的词会随每次听写请求发送给当前选择的识别服务；不要在这里填写"
                "密码或其他不必要的敏感信息。不同服务对提示词的支持程度可能不同。"
            ),
            xalign=0,
            wrap=True,
        )
        self.vocabulary_help_label.add_css_class("dim-label")
        vocabulary_card.append(self.vocabulary_help_label)

        self.vocabulary_view = Gtk.TextView(
            accepts_tab=False,
            monospace=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        vocabulary_scroll = Gtk.ScrolledWindow()
        vocabulary_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        vocabulary_scroll.set_min_content_height(260)
        vocabulary_scroll.set_child(self.vocabulary_view)
        vocabulary_card.append(vocabulary_scroll)

        self.save_vocabulary_button = Gtk.Button(label="保存个人词表")
        self.save_vocabulary_button.add_css_class("suggested-action")
        self.save_vocabulary_button.set_halign(Gtk.Align.START)
        self.save_vocabulary_button.connect("clicked", self._on_save_vocabulary)
        vocabulary_card.append(self.save_vocabulary_button)

        corrections_card = self._append_card(corrections_page)
        self._append_card_heading(
            corrections_card,
            "明确纠错（可选 · 实验性）",
            "把稳定的误识别映射到你希望得到的标准写法。",
        )

        self.corrections_help_label = Gtk.Label(
            label=(
                "下一次听写会按当前识别服务应用这些信息：火山引擎可接收完整替换映射；"
                "Qwen 与 OpenAI 只会把标准写法作为上下文提示，不保证执行逐项替换。"
                "自动学习会拆分多处"
                "替换：高置信单项可立即启用，中等置信与冲突项只进入本地候选，"
                "不会把整句润色误当成全局规则。"
            ),
            xalign=0,
            wrap=True,
        )
        self.corrections_help_label.add_css_class("dim-label")
        corrections_card.append(self.corrections_help_label)

        correction_inputs = Gtk.Grid(column_spacing=8, row_spacing=6)
        correction_inputs.attach(Gtk.Label(label="经常被识别为", xalign=0), 0, 0, 1, 1)
        correction_inputs.attach(
            Gtk.Label(label="替换成标准写法", xalign=0), 1, 0, 1, 1
        )
        self.correction_wrong_entry = Gtk.Entry(placeholder_text="误识别文本")
        self.correction_wrong_entry.set_max_length(CORRECTION_TEXT_LIMIT)
        self.correction_wrong_entry.set_hexpand(True)
        correction_inputs.attach(self.correction_wrong_entry, 0, 1, 1, 1)
        self.correction_canonical_entry = Gtk.Entry(placeholder_text="你希望得到的文本")
        self.correction_canonical_entry.set_max_length(CORRECTION_TEXT_LIMIT)
        self.correction_canonical_entry.set_hexpand(True)
        correction_inputs.attach(self.correction_canonical_entry, 1, 1, 1, 1)
        self.add_correction_button = Gtk.Button(label="添加")
        self.add_correction_button.connect("clicked", self._on_add_correction)
        correction_inputs.attach(self.add_correction_button, 2, 1, 1, 1)
        corrections_card.append(correction_inputs)

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
        corrections_card.append(self.corrections_scroll)

        self.save_corrections_button = Gtk.Button(label="保存明确纠错")
        self.save_corrections_button.add_css_class("suggested-action")
        self.save_corrections_button.set_halign(Gtk.Align.START)
        self.save_corrections_button.connect("clicked", self._on_save_corrections)
        corrections_card.append(self.save_corrections_button)

        adaptive_card = self._append_card(corrections_page)
        self._append_card_heading(
            adaptive_card,
            "自动学习记录",
            "每次都会给出结果；候选只有在你确认后才会启用。",
        )
        adaptive_grid = Gtk.Grid(column_spacing=10, column_homogeneous=True)
        self.adaptive_active_label = self._append_status_tile(
            adaptive_grid, 0, "已启用", "0"
        )
        self.adaptive_candidate_label = self._append_status_tile(
            adaptive_grid, 1, "待确认", "0"
        )
        self.adaptive_conflicted_label = self._append_status_tile(
            adaptive_grid, 2, "冲突", "0"
        )
        adaptive_card.append(adaptive_grid)
        self.adaptive_recent_label = Gtk.Label(
            label="最近结果：尚无学习记录", xalign=0, wrap=True
        )
        self.adaptive_recent_label.add_css_class("dim-label")
        adaptive_card.append(self.adaptive_recent_label)
        fallback_label = Gtk.Label(
            label=(
                "当前应用无法自动读取修改时，可在这里提交“识别原文”和“修改后整句”。"
                "窗口只保存拆出的短纠错对，不保存输入框周围的其他文字。"
            ),
            xalign=0,
            wrap=True,
        )
        fallback_label.add_css_class("dim-label")
        adaptive_card.append(fallback_label)
        fallback_grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        fallback_grid.attach(Gtk.Label(label="识别原文", xalign=0), 0, 0, 1, 1)
        fallback_grid.attach(Gtk.Label(label="修改后整句", xalign=0), 1, 0, 1, 1)
        self.adaptive_provider_entry = Gtk.Entry(placeholder_text="云端返回的这一句")
        self.adaptive_provider_entry.set_max_length(ADAPTIVE_FEEDBACK_TEXT_LIMIT)
        self.adaptive_provider_entry.set_hexpand(True)
        fallback_grid.attach(self.adaptive_provider_entry, 0, 1, 1, 1)
        self.adaptive_preferred_entry = Gtk.Entry(
            placeholder_text="你最终希望得到的这一句"
        )
        self.adaptive_preferred_entry.set_max_length(ADAPTIVE_FEEDBACK_TEXT_LIMIT)
        self.adaptive_preferred_entry.set_hexpand(True)
        fallback_grid.attach(self.adaptive_preferred_entry, 1, 1, 1, 1)
        self.submit_adaptive_feedback_button = Gtk.Button(label="分析并学习这次修改")
        self.submit_adaptive_feedback_button.connect(
            "clicked", self._on_submit_adaptive_feedback
        )
        fallback_grid.attach(self.submit_adaptive_feedback_button, 2, 1, 1, 1)
        adaptive_card.append(fallback_grid)
        self.adaptive_review_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.adaptive_review_list.add_css_class("boxed-list")
        adaptive_card.append(self.adaptive_review_list)
        self.refresh_adaptive_button = Gtk.Button(label="刷新学习记录")
        self.refresh_adaptive_button.set_halign(Gtk.Align.START)
        self.refresh_adaptive_button.connect("clicked", self._on_refresh_adaptive)
        adaptive_card.append(self.refresh_adaptive_button)

        microphone_card = self._append_card(microphone_page)
        self._append_card_heading(
            microphone_card,
            "输入设备顺序（由你决定）",
            "把你希望优先使用的类别移到上方，然后保存。",
        )

        self.microphone_selection_notice_label = Gtk.Label(
            label=(
                "这是你的当前自定义顺序，并非项目推荐顺序。每次开始新听写前，软件会"
                "重新检查可用输入，并从上到下依次尝试；当前类别不可用时才继续向下。"
                "一次听写从开始到结束固定使用同一个麦克风，不会中途切换。软件不会"
                "移动播放输出，也不会请求 set-default-source；安全恢复录音配置后，"
                "系统音频策略仍可能自行重算默认输入。仅播放的蓝牙 A2DP 不算耳麦"
                "麦克风，软件也不会自动切换蓝牙通话配置。"
            ),
            xalign=0,
            wrap=True,
        )
        self.microphone_selection_notice_label.add_css_class("dim-label")
        microphone_card.append(self.microphone_selection_notice_label)

        self.microphone_priority_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
        )
        self.microphone_priority_list.add_css_class("boxed-list")
        microphone_card.append(self.microphone_priority_list)

        self.save_microphone_priority_button = Gtk.Button(label="保存我的麦克风顺序")
        self.save_microphone_priority_button.add_css_class("suggested-action")
        self.save_microphone_priority_button.set_halign(Gtk.Align.START)
        self.save_microphone_priority_button.connect(
            "clicked", self._on_save_microphone_priority
        )
        microphone_card.append(self.save_microphone_priority_button)

        collection_card = self._append_card(collection_page)
        self._append_card_heading(
            collection_card,
            "个人 ASR 数据留存（可选）",
            "默认关闭。开启后才会把音频与未经人工复核的识别结果写入你选择的目录。",
        )

        self.data_collection_notice_label = Gtk.Label(
            label=(
                "默认关闭。只有在留存已开启、当前识别服务的最终结果已成功确认时，软件才会在所选"
                "绝对 POSIX 路径的 openvoiceinput-dataset-v1 下保存 WAV 与 "
                "provider_final（未经复核的伪标签）。目录可以是本地磁盘，也可以是"
                "操作系统已经挂载的远程文件系统（例如 SSHFS）。本程序不会连接或"
                "挂载远程主机，也不接受 SSH 或 Google Drive URL；请异步把本地或"
                "已挂载目录中的完整记录备份到 Google Drive。spoken_verbatim 与 "
                "preferred_output 会保持为空，直到后续人工复核。这里不会上传数据集，"
                "也不会训练模型。远程挂载断开时，因为没有本地兜底队列，尚未发布的"
                "记录可能丢失。关闭留存会立刻阻止尚未发布的排队记录，已经发布的记录"
                "会继续保留。"
            ),
            xalign=0,
            wrap=True,
        )
        self.data_collection_notice_label.add_css_class("dim-label")
        collection_card.append(self.data_collection_notice_label)

        self.data_collection_check = Gtk.CheckButton(
            label="在所选目录保留 WAV 与未经复核的 provider_final"
        )
        collection_card.append(self.data_collection_check)

        collection_path_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self.data_collection_directory_entry = Gtk.Entry(
            placeholder_text="选择本地或已挂载的绝对路径"
        )
        self.data_collection_directory_entry.set_editable(False)
        self.data_collection_directory_entry.set_hexpand(True)
        collection_path_row.append(self.data_collection_directory_entry)
        self.choose_data_collection_directory_button = Gtk.Button(label="选择文件夹…")
        self.choose_data_collection_directory_button.connect(
            "clicked", self._on_choose_data_collection_directory
        )
        collection_path_row.append(self.choose_data_collection_directory_button)
        collection_card.append(collection_path_row)

        self.save_data_collection_button = Gtk.Button(label="保存数据留存设置")
        self.save_data_collection_button.add_css_class("suggested-action")
        self.save_data_collection_button.set_halign(Gtk.Align.START)
        self.save_data_collection_button.connect(
            "clicked", self._on_save_data_collection
        )
        collection_card.append(self.save_data_collection_button)

        message_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        message_box.add_css_class("message-banner")
        self.message_box = message_box
        self.message_label = Gtk.Label(
            label="设置只会在你明确保存后生效；打开本窗口不会启动录音。",
            xalign=0,
            wrap=True,
            selectable=False,
        )
        self.message_label.set_hexpand(True)
        self.message_label.set_margin_top(10)
        self.message_label.set_margin_bottom(10)
        self.message_label.set_margin_start(16)
        self.message_label.set_margin_end(16)
        message_box.append(self.message_label)
        shell.append(message_box)

        self.settings_stack.set_visible_child_name("overview")

        self._load_local_settings()
        if refresh_statistics_on_start:
            self.refresh_dataset_statistics()
        else:
            self._set_statistics_busy(False)
        if refresh_service_on_start:
            self.refresh_service_status()
        else:
            self._set_service_controls_busy(False)

    def _new_page(self, name: str, title: str, subtitle: str) -> Gtk.Box:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(26)
        content.set_margin_bottom(28)
        content.set_margin_start(28)
        content.set_margin_end(28)
        eyebrow = Gtk.Label(label="OPEN VOICE INPUT", xalign=0)
        eyebrow.add_css_class("page-eyebrow")
        content.append(eyebrow)
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("title-1")
        content.append(heading)
        description = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        description.add_css_class("page-subtitle")
        content.append(description)
        scroll.set_child(content)
        self.settings_stack.add_titled(scroll, name, title)
        return content

    @staticmethod
    def _append_card(parent: Gtk.Box, css_class: str | None = None) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("settings-card")
        if css_class is not None:
            card.add_css_class(css_class)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(18)
        body.set_margin_bottom(18)
        body.set_margin_start(18)
        body.set_margin_end(18)
        card.append(body)
        parent.append(card)
        return body

    @staticmethod
    def _append_card_heading(parent: Gtk.Box, title: str, subtitle: str) -> None:
        heading = Gtk.Label(label=title, xalign=0, wrap=True)
        heading.add_css_class("title-3")
        parent.append(heading)
        description = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        description.add_css_class("dim-label")
        parent.append(description)

    @staticmethod
    def _append_status_tile(
        grid: Gtk.Grid,
        column: int,
        title: str,
        value: str,
    ) -> Gtk.Label:
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        tile.add_css_class("status-tile")
        tile.set_margin_top(2)
        tile.set_margin_bottom(2)
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("dim-label")
        label.set_margin_top(12)
        label.set_margin_start(12)
        label.set_margin_end(12)
        tile.append(label)
        value_label = Gtk.Label(label=value, xalign=0, wrap=True)
        value_label.add_css_class("status-value")
        value_label.set_margin_bottom(12)
        value_label.set_margin_start(12)
        value_label.set_margin_end(12)
        tile.append(value_label)
        grid.attach(tile, column, 0, 1, 1)
        return value_label

    @staticmethod
    def _append_metric_tile(
        grid: Gtk.Grid,
        column: int,
        title: str,
        value: str,
        detail: str,
    ) -> Gtk.Label:
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        tile.add_css_class("metric-tile")
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("section-label")
        title_label.set_margin_top(13)
        title_label.set_margin_start(13)
        title_label.set_margin_end(13)
        tile.append(title_label)
        value_label = Gtk.Label(
            label=value,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
        )
        value_label.add_css_class("metric-value")
        value_label.set_margin_start(13)
        value_label.set_margin_end(13)
        tile.append(value_label)
        detail_label = Gtk.Label(label=detail, xalign=0, wrap=True)
        detail_label.add_css_class("metric-detail")
        detail_label.set_margin_bottom(13)
        detail_label.set_margin_start(13)
        detail_label.set_margin_end(13)
        tile.append(detail_label)
        grid.attach(tile, column, 0, 1, 1)
        return value_label

    def _append_navigation_action(
        self,
        grid: Gtk.Grid,
        column: int,
        row: int,
        icon_name: str,
        title: str,
        detail: str,
        page_name: str,
    ) -> None:
        button = Gtk.Button()
        button.add_css_class("quick-action")
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(10)
        content.set_margin_end(10)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(22)
        content.append(icon)
        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        copy.set_hexpand(True)
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("status-value")
        copy.append(heading)
        description = Gtk.Label(
            label=detail,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
        )
        description.add_css_class("dim-label")
        copy.append(description)
        content.append(copy)
        arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
        content.append(arrow)
        button.set_child(content)
        button.connect("clicked", self._on_navigation_action, page_name)
        grid.attach(button, column, row, 1, 1)

    def _on_navigation_action(self, button: Gtk.Button, page_name: str) -> None:
        del button
        self.settings_stack.set_visible_child_name(page_name)

    def _load_local_settings(self) -> None:
        state = self._controller.key_state()
        self._set_key_state(state)
        provider_loader = getattr(self._controller, "provider_selection", None)
        selection = provider_loader() if callable(provider_loader) else None
        if selection is None:
            selection = ProviderSelection("volcengine", None)
        self._set_provider_selection(selection)
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
        self.refresh_adaptive_learning(silent=True)
        try:
            microphone_policy = self._controller.load_microphone_policy()
        except SettingsError as error:
            self._replace_microphone_priority_rows(DEFAULT_MICROPHONE_PRIORITY)
            self._show_error(str(error))
        else:
            self._replace_microphone_priority_rows(microphone_policy.priority)
        load_interaction = getattr(self._controller, "load_interaction", None)
        if load_interaction is None:
            self._set_interaction_controls(
                DEFAULT_INTERACTION_MODE,
                DEFAULT_MINIMUM_HOLD_MILLISECONDS,
                DEFAULT_RELEASE_TIMEOUT_SECONDS,
            )
        else:
            try:
                interaction = load_interaction()
            except SettingsError as error:
                self._set_interaction_controls(
                    DEFAULT_INTERACTION_MODE,
                    DEFAULT_MINIMUM_HOLD_MILLISECONDS,
                    DEFAULT_RELEASE_TIMEOUT_SECONDS,
                )
                self._show_error(str(error))
            else:
                self._set_interaction_controls(
                    interaction.interaction_mode,
                    interaction.minimum_hold_milliseconds,
                    interaction.release_timeout_seconds,
                )
        try:
            collection = self._controller.load_data_collection()
        except SettingsError as error:
            self.data_collection_check.set_active(False)
            self.data_collection_directory_entry.set_text("")
            self.overview_collection_status_label.set_text("配置不可用")
            self._set_label_tone(
                self.overview_collection_status_label,
                "error-text",
            )
            self._show_error(str(error))
        else:
            self.data_collection_check.set_active(collection.enabled)
            directory = collection.directory
            self.data_collection_directory_entry.set_text(
                str(directory) if directory is not None else ""
            )
            self.overview_collection_status_label.set_text(
                "已开启" if collection.enabled else "已关闭（默认）"
            )
            self._set_label_tone(
                self.overview_collection_status_label,
                "success-text" if collection.enabled else None,
            )

    def refresh_adaptive_learning(self, *, silent: bool = False) -> None:
        loader = getattr(self._controller, "load_adaptive_learning", None)
        if not callable(loader):
            # Keeps third-party/test controllers compatible with the new view.
            self._replace_adaptive_learning(
                AdaptiveLearningSnapshot(
                    statistics={
                        "active": 0,
                        "candidate": 0,
                        "conflicted": 0,
                        "suspended": 0,
                        "archived": 0,
                        "total": 0,
                    },
                    last_result=None,
                    review_entries=(),
                )
            )
            return
        try:
            snapshot = loader()
        except SettingsError as error:
            if not silent:
                self._show_error(str(error))
        except Exception:
            if not silent:
                self._show_error("无法安全读取自动学习记录。")
        else:
            self._replace_adaptive_learning(snapshot)

    def _replace_adaptive_learning(self, snapshot: AdaptiveLearningSnapshot) -> None:
        statistics = snapshot.statistics
        self.adaptive_active_label.set_text(str(statistics.get("active", 0)))
        self.adaptive_candidate_label.set_text(str(statistics.get("candidate", 0)))
        self.adaptive_conflicted_label.set_text(str(statistics.get("conflicted", 0)))
        recent = snapshot.last_result
        if recent is None:
            self.adaptive_recent_label.set_text("最近结果：尚无学习记录")
        else:
            reason = str(recent.get("reason_code", "unknown"))
            explanation = _ADAPTIVE_REASON_LABELS.get(reason, "已记录，等待刷新")
            self.adaptive_recent_label.set_text(f"最近结果：{explanation}")

        child = self.adaptive_review_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.adaptive_review_list.remove(child)
            child = next_child
        for entry in snapshot.review_entries:
            row = Gtk.ListBoxRow()
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            content.set_margin_top(6)
            content.set_margin_bottom(6)
            content.set_margin_start(8)
            content.set_margin_end(8)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text.set_hexpand(True)
            text.append(
                Gtk.Label(
                    label=f"{entry.wrong}  →  {entry.canonical}",
                    xalign=0,
                    wrap=True,
                )
            )
            text.append(
                Gtk.Label(
                    label=f"状态：{entry.state} · 已观察 {entry.support} 次",
                    xalign=0,
                )
            )
            content.append(text)
            confirm = Gtk.Button(label="确认启用")
            confirm.connect(
                "clicked",
                self._on_confirm_adaptive,
                entry.wrong,
                entry.canonical,
            )
            content.append(confirm)
            row.set_child(content)
            self.adaptive_review_list.append(row)

    def _set_key_state(self, state: KeyState) -> None:
        labels = {
            KeyState.MISSING: "尚未配置 API Key。",
            KeyState.READY: "已配置。保存的 Key 永远不会显示在窗口中。",
            KeyState.INVALID: "保存的 API Key 文件无效或不安全。",
        }
        overview_labels = {
            KeyState.MISSING: "未配置",
            KeyState.READY: "已安全配置",
            KeyState.INVALID: "需要修复",
        }
        self.key_status_label.set_text(labels[state])
        self.overview_key_status_label.set_text(overview_labels[state])
        self._set_label_tone(
            self.overview_key_status_label,
            {
                KeyState.MISSING: "warning-text",
                KeyState.READY: "success-text",
                KeyState.INVALID: "error-text",
            }[state],
        )

    def _set_interaction_controls(
        self,
        mode: str,
        minimum_hold_milliseconds: int,
        release_timeout_seconds: int,
    ) -> None:
        push_to_talk = mode == "push_to_talk"
        self.push_to_talk_mode_button.set_active(push_to_talk)
        self.toggle_mode_button.set_active(not push_to_talk)
        self.minimum_hold_spin.set_value(float(minimum_hold_milliseconds))
        self.release_timeout_spin.set_value(float(release_timeout_seconds))
        self._update_interaction_control_sensitivity()

    def _on_interaction_mode_changed(self, button: Gtk.CheckButton) -> None:
        del button
        self._update_interaction_control_sensitivity()

    def _update_interaction_control_sensitivity(self) -> None:
        push_to_talk = self.push_to_talk_mode_button.get_active()
        self.minimum_hold_spin.set_sensitive(push_to_talk)
        self.release_timeout_spin.set_sensitive(push_to_talk)

    def _on_save_interaction(self, button: Gtk.Button) -> None:
        del button
        self.save_interaction()

    def save_interaction(self) -> None:
        mode = (
            "push_to_talk" if self.push_to_talk_mode_button.get_active() else "toggle"
        )
        try:
            saved = self._controller.save_interaction(
                mode,
                self.minimum_hold_spin.get_value_as_int(),
                self.release_timeout_spin.get_value_as_int(),
            )
        except SettingsError as error:
            self._show_error(str(error))
        except Exception:
            self._show_error("无法安全保存快捷键交互模式。")
        else:
            self._set_interaction_controls(
                saved.interaction_mode,
                saved.minimum_hold_milliseconds,
                saved.release_timeout_seconds,
            )
            if saved.interaction_mode == "push_to_talk":
                self._show_message(
                    "已保存按住说话模式。下一次按下会读取新设置；请确认你的按键集成"
                    "能够分别发送 press 与 release。"
                )
            else:
                self._show_message(
                    "已保存点按切换模式。下一次按键会读取新设置，无需重启服务。"
                )

    def _on_save_key(self, button: Gtk.Button) -> None:
        del button
        self.save_key()

    def _selected_provider_id(self) -> str:
        selected = self.provider_combo.get_selected()
        if 0 <= selected < len(_READY_PROVIDERS):
            return _READY_PROVIDERS[selected].provider_id
        return "volcengine"

    def _on_provider_changed(
        self,
        combo: Gtk.DropDown,
        _parameter: object | None = None,
    ) -> None:
        del combo, _parameter
        provider = self._selected_provider_id()
        descriptor = _PROVIDERS_BY_ID.get(provider)
        if descriptor is None or descriptor.availability != "ready":
            self.provider_description_label.set_text("该识别服务当前不可用。")
            return
        model = (
            self._provider_selection.model
            if self._provider_selection.provider == provider
            else None
        )
        model_copy = f" 当前模型：{model}。" if model else ""
        self.provider_description_label.set_text(
            f"{descriptor.description}{model_copy} 更换服务需要同时输入该服务的新 Key。"
        )

    def _set_provider_selection(self, selection: ProviderSelection) -> None:
        descriptor = _PROVIDERS_BY_ID.get(selection.provider)
        if descriptor is None or descriptor.availability != "ready":
            selection = ProviderSelection("volcengine", None)
        self._provider_selection = selection
        selected = next(
            (
                index
                for index, descriptor in enumerate(_READY_PROVIDERS)
                if descriptor.provider_id == selection.provider
            ),
            0,
        )
        self.provider_combo.set_selected(selected)
        self._on_provider_changed(self.provider_combo)

    def save_key(self) -> None:
        self._reset_key_clear_confirmation()
        api_key = self.key_entry.get_text()
        try:
            if not api_key.strip():
                self._show_error("请先输入新的 API Key。")
                return
            provider = self._selected_provider_id()
            model = (
                self._provider_selection.model
                if self._provider_selection.provider == provider
                else None
            )
            saver = getattr(self._controller, "save_provider", None)
            if callable(saver):
                selection = saver(api_key, provider, model)
                self._set_provider_selection(selection)
            else:
                self._controller.save_key(api_key)
        except SettingsError as error:
            self._show_error(str(error))
        except Exception:
            self._show_error("无法安全保存 API Key。")
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
            self.clear_key_button.set_label("确认清除已保存的 Key")
            self._show_message(
                "目前尚未删除任何内容。请先停用并停止语音服务，再次点击“确认清除”"
                "即可永久删除本机保存的 API Key；其他设置不会被删除。"
            )
            return

        self._reset_key_clear_confirmation()
        try:
            removed = self._controller.clear_key()
        except SettingsError as error:
            self._show_error(str(error))
        except Exception:
            self._show_error("无法安全移除已保存的 API Key。")
        else:
            self._set_key_state(KeyState.MISSING)
            if removed:
                self._show_message("本机保存的 API Key 已移除；没有联系任何云服务。")
            else:
                self._show_message("本机原本没有已保存的 API Key；没有联系任何云服务。")
        finally:
            self.key_entry.set_text("")

    def _reset_key_clear_confirmation(self) -> None:
        self._key_clear_armed = False
        self.clear_key_button.set_label("清除已保存的 Key…")

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
            self._show_error("无法安全保存个人词表。")
        else:
            self._show_message(f"已保存 {count} 个词条。{APPLY_NOTICE}")

    def _on_add_correction(self, button: Gtk.Button) -> None:
        del button
        self.add_correction()

    def add_correction(self) -> None:
        wrong = self.correction_wrong_entry.get_text().strip()
        canonical = self.correction_canonical_entry.get_text().strip()
        if not wrong or not canonical:
            self._show_error("请同时填写误识别文本和标准写法。")
            return
        for existing_wrong, existing_canonical in self._correction_pairs:
            if existing_wrong != wrong:
                continue
            if existing_canonical == canonical:
                self._show_error("这条明确纠错已经在列表中。")
            else:
                self._show_error(
                    "同一误识别文本已经对应另一条标准写法；请先移除旧规则再添加。"
                )
            return
        if len(self._correction_pairs) >= CORRECTION_PAIR_LIMIT:
            self._show_error(f"最多只能保存 {CORRECTION_PAIR_LIMIT} 条明确纠错。")
            return
        pair = (wrong, canonical)
        self._correction_pairs.append(pair)
        self._append_correction_row(pair)
        self.correction_wrong_entry.set_text("")
        self.correction_canonical_entry.set_text("")
        self._show_message(
            "已添加到本窗口的待保存列表；点击“保存明确纠错”后才会写入本机。"
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
        text.append(Gtk.Label(label=f"误识别：{wrong}", xalign=0, wrap=True))
        text.append(Gtk.Label(label=f"标准写法：{canonical}", xalign=0, wrap=True))
        content.append(text)
        remove_button = Gtk.Button(label="移除")
        remove_button.connect("clicked", self._on_remove_correction, row)
        content.append(remove_button)
        row.set_child(content)
        self.corrections_list.append(row)

    def _on_remove_correction(self, button: Gtk.Button, row: Gtk.ListBoxRow) -> None:
        del button
        index = row.get_index()
        if index < 0 or index >= len(self._correction_pairs):
            self._show_error("无法安全移除这条纠错。")
            return
        del self._correction_pairs[index]
        self.corrections_list.remove(row)
        self._show_message(
            "已从本窗口的列表移除；点击“保存明确纠错”后才会写入这次更改。"
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
            self._show_error("无法安全保存明确纠错。")
        else:
            self._replace_correction_rows(normalized_pairs)
            self._show_message(f"已保存 {count} 条明确纠错。{APPLY_NOTICE}")

    def _on_refresh_adaptive(self, button: Gtk.Button) -> None:
        del button
        self.refresh_adaptive_learning()

    def _on_confirm_adaptive(
        self,
        button: Gtk.Button,
        wrong: str,
        canonical: str,
    ) -> None:
        del button
        confirmer = getattr(self._controller, "confirm_adaptive_learning", None)
        if not callable(confirmer):
            self._show_error("当前设置控制器不支持确认自动纠错。")
            return
        try:
            active = confirmer(wrong, canonical)
        except SettingsError as error:
            self._show_error(str(error))
            return
        except Exception:
            self._show_error("无法安全确认这条自动纠错。")
            return
        self.refresh_adaptive_learning(silent=True)
        if active:
            self._show_message("已确认并启用；下一次听写会自动读取。")
        else:
            self._show_message("已确认，但因明确规则覆盖或安全冲突暂未发送。")

    def _on_submit_adaptive_feedback(self, button: Gtk.Button) -> None:
        del button
        provider_text = self.adaptive_provider_entry.get_text().strip()
        preferred_text = self.adaptive_preferred_entry.get_text().strip()
        if not provider_text or not preferred_text:
            self._show_error("请同时填写识别原文和修改后整句。")
            return
        submitter = getattr(self._controller, "submit_adaptive_feedback", None)
        if not callable(submitter):
            self._show_error("当前设置控制器不支持显式纠错反馈。")
            return
        try:
            reason = submitter(provider_text, preferred_text)
        except SettingsError as error:
            self._show_error(str(error))
            return
        except Exception:
            self._show_error("无法安全保存这次纠错反馈。")
            return
        self.adaptive_provider_entry.set_text("")
        self.adaptive_preferred_entry.set_text("")
        self.refresh_adaptive_learning(silent=True)
        label = _ADAPTIVE_REASON_LABELS.get(reason, "已分析并安全记录")
        self._show_message(f"{label}；下一次听写会自动读取可用规则。")

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
            rank.add_css_class("rank-badge")
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

            move_up = Gtk.Button(label="上移")
            move_up.set_sensitive(index > 0)
            move_up.connect("clicked", self._on_move_microphone_category, category, -1)
            content.append(move_up)

            move_down = Gtk.Button(label="下移")
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
            self._show_error("无法安全调整麦克风顺序。")
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
            "顺序已在本窗口中调整；点击“保存我的麦克风顺序”后才会用于后续听写。"
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
            self._show_error("无法安全保存麦克风顺序。")
        else:
            self._replace_microphone_priority_rows(policy.priority)
            self._show_message(
                "已保存你的麦克风顺序。下一次听写会重新检查可用输入；一次听写期间"
                "仍会固定使用同一个麦克风。"
            )

    def _on_choose_data_collection_directory(self, button: Gtk.Button) -> None:
        del button
        chooser = Gtk.FileChooserNative.new(
            "选择数据集目录（本地或已挂载）",
            self,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "选择",
            "取消",
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
                self._show_error("请选择本地文件系统路径或已经挂载的目录。")
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
                    "无法安全保存本地数据留存设置。",
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
            self._show_error("无法安全保存本地数据留存设置。")
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
        self.overview_collection_status_label.set_text(
            "已开启" if collection.enabled else "已关闭（默认）"
        )
        self._set_label_tone(
            self.overview_collection_status_label,
            "success-text" if collection.enabled else None,
        )
        if collection.enabled:
            self._show_message(
                "已为所选目录开启 WAV 与未经复核的 provider_final 留存。每次新听写"
                "都会读取此设置；如果使用远程挂载，请保持连接。"
            )
        else:
            self._show_message(
                "本地训练数据留存已关闭。当前或排队中但尚未发布的记录不会写入；"
                "已经发布的记录会继续保留。"
            )
        self._invalidate_and_refresh_dataset_statistics()

    def _on_refresh_dataset_statistics(self, button: Gtk.Button) -> None:
        del button
        self.refresh_dataset_statistics()

    def _invalidate_and_refresh_dataset_statistics(self) -> None:
        self._statistics_generation += 1
        if self._statistics_busy:
            self.statistics_status_label.set_text(
                "留存设置已改变；当前后台读取结束后会自动刷新。"
            )
            return
        self.refresh_dataset_statistics()

    def refresh_dataset_statistics(self) -> None:
        """Load bounded, content-free dataset counters outside the GTK thread."""

        if self._statistics_busy:
            return
        self._statistics_generation += 1
        generation = self._statistics_generation
        self._set_statistics_busy(True)
        self.statistics_status_label.set_text("正在后台读取本地隐私统计摘要…")
        self._set_statistics_tone(None)
        self._statistics_timeout_id = GLib.timeout_add_seconds(
            4,
            self._mark_statistics_slow,
            generation,
        )

        def worker() -> None:
            try:
                statistics = self._controller.load_dataset_statistics()
            except SettingsError:
                statistics = DatasetStatistics("unavailable")
            except Exception:
                statistics = DatasetStatistics("unavailable")
            GLib.idle_add(
                self._finish_dataset_statistics,
                generation,
                statistics,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _mark_statistics_slow(self, generation: int) -> bool:
        self._statistics_timeout_id = 0
        if (
            self._window_closed
            or not self._statistics_busy
            or generation != self._statistics_generation
        ):
            return GLib.SOURCE_REMOVE
        self.statistics_status_label.set_text(
            "存储响应较慢，可能是远程挂载断线；首页仍可正常使用。"
        )
        self._set_statistics_tone("warning-text")
        return GLib.SOURCE_REMOVE

    def _finish_dataset_statistics(
        self,
        generation: int,
        statistics: DatasetStatistics,
    ) -> bool:
        if self._statistics_timeout_id:
            GLib.source_remove(self._statistics_timeout_id)
            self._statistics_timeout_id = 0
        if self._window_closed:
            self._statistics_busy = False
            return GLib.SOURCE_REMOVE
        if generation != self._statistics_generation:
            self._set_statistics_busy(False)
            GLib.idle_add(self.refresh_dataset_statistics)
            return GLib.SOURCE_REMOVE
        self._set_statistics_busy(False)
        self._apply_dataset_statistics(statistics)
        return GLib.SOURCE_REMOVE

    def _set_statistics_busy(self, busy: bool) -> None:
        self._statistics_busy = busy
        self.refresh_statistics_button.set_sensitive(not busy)

    def _apply_dataset_statistics(self, statistics: DatasetStatistics) -> None:
        values = (
            self.today_characters_label,
            self.today_duration_label,
            self.today_utterances_label,
            self.total_characters_label,
            self.total_duration_label,
            self.total_utterances_label,
            self.latest_activity_label,
        )
        if statistics.state == "disabled":
            for label in values:
                label.set_text("—")
            self.statistics_status_label.set_text(
                "数据留存已关闭；首页不会扫描以前选择过的目录。"
            )
            self._set_statistics_tone(None)
            return
        if statistics.state == "unavailable":
            for label in values:
                label.set_text("—")
            self.statistics_status_label.set_text(
                "存储当前不可用；请检查本地目录或远程挂载，普通听写仍可继续。"
            )
            self.overview_collection_status_label.set_text("已开启 · 存储不可用")
            self._set_label_tone(
                self.overview_collection_status_label,
                "warning-text",
            )
            self._set_statistics_tone("warning-text")
            return
        if statistics.state == "unindexed":
            for label in values:
                label.set_text("—")
            self.statistics_status_label.set_text(
                "这是较早版本的数据集，尚无独立 usage 索引；首页不会读取 "
                "record.json 来回填，新版记录会自动加入统计。"
            )
            self.overview_collection_status_label.set_text("已开启 · 等待统计索引")
            self._set_label_tone(
                self.overview_collection_status_label,
                "warning-text",
            )
            self._set_statistics_tone("warning-text")
            return

        self.today_characters_label.set_text(f"{statistics.today_characters:,}")
        self.today_duration_label.set_text(
            self._format_usage_duration(statistics.today_seconds)
        )
        self.today_utterances_label.set_text(f"{statistics.today_utterances:,}")
        self.total_characters_label.set_text(f"{statistics.total_characters:,}")
        self.total_duration_label.set_text(
            self._format_usage_duration(statistics.total_seconds)
        )
        self.total_utterances_label.set_text(f"{statistics.total_utterances:,}")
        if statistics.latest_recorded_at is None:
            self.latest_activity_label.set_text("暂无")
        else:
            self.latest_activity_label.set_text(
                statistics.latest_recorded_at.astimezone().strftime("%m月%d日 %H:%M")
            )
        self.overview_collection_status_label.set_text("已开启 · 存储可用")
        self._set_label_tone(
            self.overview_collection_status_label,
            "success-text",
        )

        skipped = statistics.invalid_summaries
        if statistics.state == "empty":
            message = "还没有可统计的已发布记录；第一次成功留存后会自动出现。"
            tone = None
        elif statistics.state == "limited":
            message = (
                f"发现 {skipped:,} 条旧版或无效记录。为保护正文，首页不会打开它们的 "
                "record.json；新版记录将自动加入统计。"
            )
            tone = "warning-text"
        elif skipped:
            message = (
                f"已汇总 {statistics.total_utterances:,} 条隐私摘要；另有 {skipped:,} 条"
                "旧版或无效记录未纳入，转写正文没有被读取。"
            )
            tone = "warning-text"
        else:
            message = (
                f"已汇总 {statistics.total_utterances:,} 条本地隐私摘要；未读取或展示"
                "任何转写正文。"
            )
            tone = "success-text"
        self.statistics_status_label.set_text(message)
        self._set_statistics_tone(tone)

    def _set_statistics_tone(self, tone: str | None) -> None:
        for css_class in ("success-text", "warning-text", "error-text"):
            self.statistics_status_label.remove_css_class(css_class)
        if tone is not None:
            self.statistics_status_label.add_css_class(tone)

    @staticmethod
    def _set_label_tone(label: Gtk.Label, tone: str | None) -> None:
        for css_class in ("success-text", "warning-text", "error-text"):
            label.remove_css_class(css_class)
        if tone is not None:
            label.add_css_class(tone)

    @staticmethod
    def _format_usage_duration(seconds: float) -> str:
        rounded = max(0, round(seconds))
        if rounded < 60:
            return f"{rounded} 秒"
        hours, remainder = divmod(rounded, 3600)
        minutes = remainder // 60
        if hours:
            return f"{hours} 小时 {minutes} 分"
        return f"{minutes} 分钟"

    def _on_close_request(self, window: Gtk.Window) -> bool:
        del window
        self._window_closed = True
        self._statistics_generation += 1
        if self._statistics_timeout_id:
            GLib.source_remove(self._statistics_timeout_id)
            self._statistics_timeout_id = 0
        chooser = self._data_collection_chooser
        self._data_collection_chooser = None
        if chooser is not None:
            chooser.destroy()
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.remove_provider_for_display(
                display,
                self._css_provider,
            )
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
                    "语音服务操作已安全失败。",
                )
            else:
                GLib.idle_add(self._finish_service_operation, snapshot, None)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_service_operation(
        self, snapshot: ServiceSnapshot | None, error: str | None
    ) -> bool:
        if self._window_closed:
            self._service_busy = False
            return GLib.SOURCE_REMOVE
        self._set_service_controls_busy(False)
        if error is not None:
            self._show_error(error)
        elif snapshot is not None:
            self._set_service_snapshot(snapshot)
        return GLib.SOURCE_REMOVE

    def _set_service_controls_busy(self, busy: bool) -> None:
        self._service_busy = busy
        if busy:
            self.overview_service_status_label.set_text("正在处理…")
        self.clear_key_button.set_sensitive(not busy)
        self.start_service_button.set_sensitive(not busy)
        self.stop_service_button.set_sensitive(not busy)
        self.refresh_service_button.set_sensitive(not busy)

    def _set_service_snapshot(self, snapshot: ServiceSnapshot) -> None:
        service = _SERVICE_LABELS.get(snapshot.active_state, "暂时不可用")
        parts = [f"语音服务：{service}"]
        self.overview_service_status_label.set_text(service)
        self._set_label_tone(
            self.overview_service_status_label,
            {
                "active": "success-text",
                "failed": "error-text",
                "unknown": "warning-text",
            }.get(snapshot.active_state),
        )
        if snapshot.session_state is not None:
            parts.append(_SESSION_LABELS.get(snapshot.session_state, "听写状态未知"))
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
        self.message_box.remove_css_class("error")
        self.message_label.set_text(message)

    def _show_error(self, message: str) -> None:
        self.message_label.add_css_class("error")
        self.message_box.add_css_class("error")
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
