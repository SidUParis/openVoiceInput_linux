#!/usr/bin/env python3
"""A small GTK client for visually testing inline IBus preedit text."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402


APPLICATION_ID = "org.murmur.IME.PreeditDemo"


class PreeditDemoApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APPLICATION_ID)

    def do_activate(self) -> None:
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("Murmur IME - Inline Preedit Demo")
        window.set_default_size(680, 430)

        page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_top=22,
            margin_bottom=22,
            margin_start=22,
            margin_end=22,
        )

        title = Gtk.Label(label="Murmur IME 光标处实时文字演示", xalign=0)
        title.add_css_class("title-2")
        page.append(title)

        instructions = Gtk.Label(
            label=(
                "切换到 Murmur IME，然后把光标留在下面的输入框中。"
                "在另一个终端运行 scripts/send_preedit_demo.py："
                "草稿应以 preedit 形式在光标处逐步变化，最终文本才会成为已提交正文。"
            ),
            xalign=0,
            wrap=True,
        )
        page.append(instructions)

        entry_label = Gtk.Label(label="单行输入框", xalign=0)
        page.append(entry_label)

        entry = Gtk.Entry()
        entry.set_placeholder_text("点击这里并保持光标，然后运行发送器……")
        page.append(entry)

        committed = Gtk.Label(label="已提交文本：<空>", xalign=0, wrap=True)
        committed.add_css_class("dim-label")
        page.append(committed)

        text_label = Gtk.Label(label="多行输入框（也可以在这里测试）", xalign=0)
        page.append(text_label)

        text_view = Gtk.TextView()
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_top_margin(10)
        text_view.set_bottom_margin(10)
        text_view.set_left_margin(10)
        text_view.set_right_margin(10)

        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_min_content_height(130)
        scroller.set_child(text_view)
        page.append(scroller)

        clear_button = Gtk.Button(label="清空已提交文本")
        clear_button.set_halign(Gtk.Align.START)
        page.append(clear_button)

        def update_committed_text(widget: Gtk.Entry) -> None:
            text = widget.get_text()
            committed.set_text(f"已提交文本：{text}" if text else "已提交文本：<空>")

        def clear_text(_button: Gtk.Button) -> None:
            entry.set_text("")
            text_view.get_buffer().set_text("")
            entry.grab_focus()

        entry.connect("changed", update_committed_text)
        clear_button.connect("clicked", clear_text)

        window.set_child(page)
        window.present()

        def focus_entry_once() -> bool:
            entry.grab_focus()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(focus_entry_once)


def main() -> int:
    app = PreeditDemoApplication()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
