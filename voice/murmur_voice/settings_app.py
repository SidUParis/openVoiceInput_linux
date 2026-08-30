"""Small native GTK4 settings window for voice-provider onboarding."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

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
    "adaptive-correction-learned": "已学习本次修改，后续听写会使用",
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

_SETTINGS_CSS = """
.settings-shell {
  background-color: @theme_bg_color;
}

.settings-sidebar-shell {
  background-color: alpha(@theme_base_color, 0.72);
  border-right: 1px solid alpha(@theme_fg_color, 0.10);
}

.settings-sidebar row {
  border-radius: 10px;
  margin: 3px 8px;
  min-height: 40px;
}

.settings-sidebar row:selected {
  background-color: alpha(#3584e4, 0.16);
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
  background-color: alpha(@theme_base_color, 0.94);
  border: 1px solid alpha(@theme_fg_color, 0.10);
  border-radius: 16px;
  box-shadow: 0 2px 8px alpha(#000000, 0.08);
}

.hero-card {
  background-color: alpha(#3584e4, 0.10);
  border-color: alpha(#3584e4, 0.30);
}

.status-tile {
  background-color: alpha(@theme_fg_color, 0.055);
  border-radius: 12px;
}

.status-value {
  font-weight: 700;
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
    ) -> None:
        super().__init__(application=application, title="Open Voice Input 设置")
        self.set_default_size(900, 720)
        self.set_size_request(720, 560)
        self._controller = controller or SettingsController()
        self._service_busy = False
        self._collection_busy = False
        self._window_closed = False
        self._key_clear_armed = False
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
        sidebar_shell.set_size_request(188, -1)
        sidebar_brand = Gtk.Label(label="设置", xalign=0)
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
            "概览与服务",
            "快速确认运行状态，并显式启动或停止语音服务。",
        )
        cloud_page = self._new_page(
            "cloud",
            "云端识别",
            "管理火山引擎凭据与发送边界。",
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
        hero_badge = Gtk.Label(label="轻量 · 原生 GTK4 · 不捆绑本地大模型")
        hero_badge.add_css_class("soft-badge")
        hero_badge.set_halign(Gtk.Align.START)
        hero.append(hero_badge)
        hero_title = Gtk.Label(label="把声音直接写进当前输入框", xalign=0)
        hero_title.add_css_class("title-1")
        hero.append(hero_title)
        hero_copy = Gtk.Label(
            label=(
                "使用你设置的快捷键开始或结束听写。界面只负责小型本地配置与"
                "状态控制，识别由你配置的云服务完成。"
            ),
            xalign=0,
            wrap=True,
        )
        hero_copy.add_css_class("page-subtitle")
        hero.append(hero_copy)

        status_card = self._append_card(overview_page)
        self._append_card_heading(
            status_card,
            "当前状态",
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

        provider_card = self._append_card(cloud_page)
        self._append_card_heading(
            provider_card,
            "火山引擎 API Key",
            "密钥只写入本机私有配置；窗口从不回填或显示已保存的内容。",
        )

        self.key_status_label = Gtk.Label(xalign=0, wrap=True)
        self.key_status_label.add_css_class("status-value")
        provider_card.append(self.key_status_label)

        self.remote_audio_notice_label = Gtk.Label(
            label=(
                "只有明确开始听写后，麦克风音频才会流式发送至火山引擎并由你的账号"
                "计费；取消听写无法撤回已经发送的音频片段。"
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
        self.save_key_button = Gtk.Button(label="保存新 Key")
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
                "这些明确添加的词会随每次听写请求发送给火山引擎；不要在这里填写"
                "密码或其他不必要的敏感信息。"
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
                "每个已保存的纠错对都会随听写请求发送给火山引擎。听写结束后的 5 秒"
                "观察窗口只会学习边界明确的单处替换；含糊或冲突的修改不会启用。"
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
                "默认关闭。只有在留存已开启、火山引擎最终结果已成功确认时，软件才会在所选"
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
            self.overview_collection_status_label.set_text("关闭（读取失败）")
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

    def _on_save_key(self, button: Gtk.Button) -> None:
        del button
        self.save_key()

    def save_key(self) -> None:
        self._reset_key_clear_confirmation()
        api_key = self.key_entry.get_text()
        try:
            if not api_key.strip():
                self._show_error("请先输入新的 API Key。")
                return
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

    def _on_close_request(self, window: Gtk.Window) -> bool:
        del window
        self._window_closed = True
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
