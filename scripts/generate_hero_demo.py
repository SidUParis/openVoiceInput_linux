#!/usr/bin/env python3
"""Generate the synthetic README hero animation and social preview.

The assets deliberately use a drawn, generic editor instead of a desktop
capture.  That keeps the demo free of API keys, personal text, audio, host
paths, and third-party artwork while making every frame reproducible.
"""

from __future__ import annotations

import argparse
import math
from functools import lru_cache
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError as error:  # pragma: no cover - exercised by users without Pillow
    raise SystemExit(
        "Pillow is required: install python3-pil or run this script in the "
        "project documentation environment."
    ) from error


REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIRECTORY = REPOSITORY / "docs" / "assets"

WIDTH = 960
HEIGHT = 540
FPS = 12
FRAME_COUNT = 156

SOCIAL_WIDTH = 1200
SOCIAL_HEIGHT = 600

PLUM = "#2E1738"
PLUM_DARK = "#1C1024"
ORANGE = "#E95420"
ORANGE_LIGHT = "#FFF0EA"
BLUE = "#4B6BFB"
BLUE_LIGHT = "#EEF2FF"
GREEN = "#1F9D68"
GREEN_LIGHT = "#EAF8F1"
INK = "#23212B"
MUTED = "#6F6B78"
CANVAS = "#F5F3F7"
PANEL = "#FFFFFF"
BORDER = "#E6E1EA"

REGULAR_FONT_CANDIDATES = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)
BOLD_FONT_CANDIDATES = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)
MONO_FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    Path("/usr/share/fonts/truetype/ubuntu/UbuntuSansMono[wght].ttf"),
)


def first_existing(candidates: Iterable[Path], label: str) -> Path:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    choices = "\n  ".join(str(path) for path in candidates)
    raise SystemExit(f"No {label} font found. Looked for:\n  {choices}")


REGULAR_FONT_PATH = first_existing(REGULAR_FONT_CANDIDATES, "regular")
BOLD_FONT_PATH = first_existing(BOLD_FONT_CANDIDATES, "bold")
MONO_FONT_PATH = first_existing(MONO_FONT_CANDIDATES, "monospace")


def font(
    size: int, *, bold: bool = False, mono: bool = False
) -> ImageFont.FreeTypeFont:
    path = MONO_FONT_PATH if mono else BOLD_FONT_PATH if bold else REGULAR_FONT_PATH
    return ImageFont.truetype(str(path), size=size)


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def ease(amount: float) -> float:
    amount = max(0.0, min(1.0, amount))
    return amount * amount * (3.0 - 2.0 * amount)


@lru_cache(maxsize=8)
def gradient_image(
    width: int,
    height: int,
    top: str,
    bottom: str,
    *,
    horizontal_shift: str | None = None,
) -> Image.Image:
    vertical_mask = Image.linear_gradient("L").resize((width, height))
    result = Image.composite(
        Image.new("RGB", (width, height), bottom),
        Image.new("RGB", (width, height), top),
        vertical_mask,
    )
    if horizontal_shift:
        horizontal_mask = (
            Image.linear_gradient("L").rotate(90, expand=True).resize((width, height))
        )
        horizontal_mask = horizontal_mask.point(lambda value: round(value * 0.14))
        result = Image.composite(
            Image.new("RGB", (width, height), horizontal_shift),
            result,
            horizontal_mask,
        )
    return result


def draw_gradient(
    image: Image.Image,
    top: str,
    bottom: str,
    *,
    horizontal_shift: str | None = None,
) -> None:
    image.paste(
        gradient_image(
            image.width, image.height, top, bottom, horizontal_shift=horizontal_shift
        )
    )


def rounded_shadow(
    image: Image.Image,
    box: tuple[int, int, int, int],
    *,
    radius: int,
    blur: int,
    offset_y: int,
    opacity: int,
) -> None:
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    painter = ImageDraw.Draw(shadow)
    x0, y0, x1, y1 = box
    painter.rounded_rectangle(
        (x0, y0 + offset_y, x1, y1 + offset_y),
        radius=radius,
        fill=(12, 7, 17, opacity),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    image.alpha_composite(shadow)


def text_width(
    draw: ImageDraw.ImageDraw, value: str, text_font: ImageFont.FreeTypeFont
) -> int:
    box = draw.textbbox((0, 0), value, font=text_font)
    return box[2] - box[0]


def pill(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    value: str,
    *,
    background: str,
    foreground: str,
    text_font: ImageFont.FreeTypeFont,
    pad_x: int = 12,
    height: int = 28,
    outline: str | None = None,
) -> tuple[int, int, int, int]:
    x, y = position
    width = text_width(draw, value, text_font) + pad_x * 2
    box = (x, y, x + width, y + height)
    draw.rounded_rectangle(box, radius=height // 2, fill=background, outline=outline)
    text_box = draw.textbbox((0, 0), value, font=text_font)
    baseline_y = y + (height - (text_box[3] - text_box[1])) // 2 - text_box[1]
    draw.text((x + pad_x, baseline_y), value, fill=foreground, font=text_font)
    return box


def microphone_icon(
    draw: ImageDraw.ImageDraw, center: tuple[int, int], colour: str
) -> None:
    x, y = center
    draw.rounded_rectangle(
        (x - 5, y - 10, x + 5, y + 5), radius=5, outline=colour, width=2
    )
    draw.arc((x - 10, y - 3, x + 10, y + 12), 0, 180, fill=colour, width=2)
    draw.line((x, y + 12, x, y + 17), fill=colour, width=2)
    draw.line((x - 5, y + 17, x + 5, y + 17), fill=colour, width=2)


def check_icon(draw: ImageDraw.ImageDraw, center: tuple[int, int], colour: str) -> None:
    x, y = center
    draw.ellipse((x - 9, y - 9, x + 9, y + 9), fill=colour)
    draw.line(
        (x - 4, y, x - 1, y + 4, x + 5, y - 4), fill="white", width=2, joint="curve"
    )


def cursor(
    draw: ImageDraw.ImageDraw, x: int, y: int, height: int, *, visible: bool = True
) -> None:
    if visible:
        draw.rounded_rectangle((x, y, x + 2, y + height), radius=1, fill=BLUE)


def waveform(draw: ImageDraw.ImageDraw, x: int, y: int, phase: float) -> None:
    for index in range(11):
        wave = math.sin(phase * 0.35 + index * 1.17)
        height = 5 + round(abs(wave) * 10)
        colour = ORANGE if index in {4, 5, 6} else "#9B94A4"
        draw.rounded_rectangle(
            (x + index * 7, y - height, x + index * 7 + 3, y + height),
            radius=2,
            fill=colour,
        )


def editor_chrome(image: Image.Image) -> tuple[ImageDraw.ImageDraw, int, int]:
    draw = ImageDraw.Draw(image)
    window = (50, 34, 910, 505)
    rounded_shadow(image, window, radius=20, blur=22, offset_y=12, opacity=64)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(window, radius=20, fill=PANEL, outline="#FFFFFF", width=1)
    # GNOME-style neutral header bar: one menu button, a centred title, and a
    # quiet close control. It intentionally avoids macOS traffic-light dots.
    draw.rounded_rectangle((50, 34, 910, 94), radius=20, fill="#F7F5F8")
    draw.rectangle((50, 70, 910, 94), fill="#F7F5F8")
    draw.line((50, 94, 910, 94), fill="#DED9E2", width=1)
    draw.rounded_rectangle((69, 50, 105, 79), radius=8, fill="#E9E5EB")
    for y in (59, 64, 69):
        draw.line((80, y, 94, y), fill="#645E69", width=2)
    title_font = font(14, bold=True)
    title = "文本编辑器"
    title_x = 480 - text_width(draw, title, title_font) // 2
    draw.text((title_x, 54), title, font=title_font, fill=INK)
    draw.rounded_rectangle((868, 50, 894, 78), radius=8, fill="#E9E5EB")
    draw.line((877, 59, 885, 69), fill="#645E69", width=2)
    draw.line((885, 59, 877, 69), fill="#645E69", width=2)

    draw.rectangle((50, 95, 228, 505), fill="#F3F1F5")
    draw.text((72, 117), "我的笔记", font=font(12, bold=True), fill="#817989")
    draw.rounded_rectangle((64, 145, 214, 183), radius=9, fill="#E5DFE8")
    draw.ellipse((80, 159, 90, 169), fill=ORANGE)
    draw.text((101, 152), "今天", font=font(14, bold=True), fill=PLUM)
    draw.ellipse((80, 205, 90, 215), outline="#AAA3AE", width=1)
    draw.text((101, 198), "术语表", font=font(14), fill="#787180")
    draw.ellipse((80, 245, 90, 255), outline="#AAA3AE", width=1)
    draw.text((101, 238), "项目计划", font=font(14), fill="#787180")

    draw.line((228, 95, 228, 505), fill=BORDER, width=1)
    draw.text((264, 120), "今天的笔记", font=font(29, bold=True), fill=INK)
    draw.text((265, 169), "Linux 原生轻量语音输入", font=font(17), fill=MUTED)
    draw.line((264, 205, 864, 205), fill="#ECE8EF", width=1)

    # A small persistent explanation makes the synthetic nature unambiguous.
    pill(
        draw,
        (714, 51),
        "合成交互演示",
        background="#EDE8F0",
        foreground=PLUM,
        text_font=font(11, bold=True),
        pad_x=11,
        height=27,
    )
    return draw, 264, 250


def phase_progress(frame: int, start: int, end: int) -> float:
    return ease((frame - start) / max(1, end - start))


def draw_key_hint(draw: ImageDraw.ImageDraw, frame: int) -> None:
    listening = 15 <= frame < 61 or 113 <= frame < 146
    pressed = listening
    box = (682, 431, 862, 482)
    fill = ORANGE if pressed else "#F6F3F7"
    outline = ORANGE if pressed else "#D9D2DD"
    foreground = "white" if pressed else PLUM
    draw.rounded_rectangle(box, radius=12, fill=fill, outline=outline, width=2)
    # A generic keycap icon communicates configurability without naming the
    # maintainer's personal shortcut.
    icon_fill = "#FF8B62" if pressed else "#E7E1E9"
    draw.rounded_rectangle((698, 443, 724, 468), radius=6, fill=icon_fill)
    for y in (449, 455):
        for x in (703, 710, 717):
            draw.rounded_rectangle((x, y, x + 4, y + 3), radius=1, fill=foreground)
    draw.rounded_rectangle((704, 461, 720, 464), radius=1, fill=foreground)
    draw.text((735, 439), "自定义快捷键", font=font(12, bold=True), fill=foreground)
    if listening:
        action = "再按一次完成"
    elif 61 <= frame < 113:
        action = "修改后自动学习"
    else:
        action = "按一次开始"
    draw.text((735, 458), action, font=font(10), fill=foreground)


def draw_listening_hud(
    draw: ImageDraw.ImageDraw,
    frame: int,
    label: str,
    action: str = "再按一次完成",
) -> None:
    rounded = (535, 372, 860, 422)
    draw.rounded_rectangle(rounded, radius=15, fill="#282331")
    microphone_icon(draw, (558, 396), "#FF7A4C")
    waveform(draw, 582, 396, float(frame))
    draw.text((670, 383), label, font=font(13, bold=True), fill="#FFFFFF")
    draw.text((670, 402), action, font=font(10), fill="#BDB6C2")


def draw_preedit(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    y: int,
    status: str,
    highlight_term: str | None = None,
    caret_on: bool = True,
) -> None:
    x = 264
    body_font = font(24)
    if highlight_term and highlight_term in text:
        prefix, suffix = text.split(highlight_term, 1)
        draw.text((x, y), prefix, font=body_font, fill=INK)
        prefix_width = text_width(draw, prefix, body_font)
        term_width = text_width(draw, highlight_term, body_font)
        draw.rounded_rectangle(
            (
                x + prefix_width - 3,
                y - 1,
                x + prefix_width + term_width + 3,
                y + 31,
            ),
            radius=5,
            fill=BLUE_LIGHT,
        )
        draw.text((x + prefix_width, y), highlight_term, font=body_font, fill=BLUE)
        draw.text((x + prefix_width + term_width, y), suffix, font=body_font, fill=INK)
    else:
        draw.text((x, y), text, font=body_font, fill=INK)
    width = text_width(draw, text, body_font)
    draw.line((x, y + 34, x + max(8, width), y + 34), fill=BLUE, width=3)
    cursor(draw, x + width + 3, y + 2, 29, visible=caret_on)
    pill(
        draw,
        (264, y + 47),
        status,
        background=BLUE_LIGHT,
        foreground=BLUE,
        text_font=font(11, bold=True),
        pad_x=10,
        height=24,
    )


def draw_committed_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    select_term: str | None = None,
    highlight_term: str | None = None,
    caret_on: bool = False,
) -> None:
    x, y = 264, 250
    body_font = font(24)
    term = select_term or highlight_term
    if term and term in text:
        prefix, suffix = text.split(term, 1)
        prefix_width = text_width(draw, prefix, body_font)
        term_width = text_width(draw, term, body_font)
        if select_term:
            draw.rectangle(
                (
                    x + prefix_width - 1,
                    y - 1,
                    x + prefix_width + term_width + 2,
                    y + 32,
                ),
                fill="#BDD0FF",
            )
        elif highlight_term:
            draw.rounded_rectangle(
                (
                    x + prefix_width - 3,
                    y - 1,
                    x + prefix_width + term_width + 3,
                    y + 32,
                ),
                radius=5,
                fill=GREEN_LIGHT,
            )
        draw.text((x, y), prefix, font=body_font, fill=INK)
        draw.text(
            (x + prefix_width, y),
            term,
            font=body_font,
            fill=GREEN if highlight_term else INK,
        )
        draw.text((x + prefix_width + term_width, y), suffix, font=body_font, fill=INK)
    else:
        draw.text((x, y), text, font=body_font, fill=INK)
    if caret_on:
        cursor(draw, x + text_width(draw, text, body_font) + 3, y + 2, 29)


def draw_final_badge(draw: ImageDraw.ImageDraw, label: str = "文字已提交一次") -> None:
    box = (264, 302, 420, 332)
    draw.rounded_rectangle(box, radius=14, fill=GREEN_LIGHT)
    check_icon(draw, (282, 317), GREEN)
    draw.text((298, 309), label, font=font(11, bold=True), fill=GREEN)


def draw_correction_card(image: Image.Image, amount: float) -> None:
    amount = ease(amount)
    if amount <= 0:
        return
    y_offset = round((1.0 - amount) * 16)
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    card = ImageDraw.Draw(overlay)
    alpha = round(255 * amount)
    shadow_alpha = round(35 * amount)
    card.rounded_rectangle(
        (514, 300 + y_offset, 860, 365 + y_offset),
        radius=14,
        fill=(18, 11, 23, shadow_alpha),
    )
    card.rounded_rectangle(
        (510, 294 + y_offset, 856, 359 + y_offset),
        radius=14,
        fill=(255, 255, 255, alpha),
        outline=(230, 225, 234, alpha),
    )
    card.ellipse(
        (528, 310 + y_offset, 554, 336 + y_offset), fill=(*hex_rgb(ORANGE_LIGHT), alpha)
    )
    card.arc(
        (533, 315 + y_offset, 549, 331 + y_offset),
        start=35,
        end=325,
        fill=(*hex_rgb(ORANGE), alpha),
        width=2,
    )
    card.polygon(
        (
            (547, 314 + y_offset),
            (552, 315 + y_offset),
            (549, 320 + y_offset),
        ),
        fill=(*hex_rgb(ORANGE), alpha),
    )
    card.text(
        (566, 303 + y_offset),
        "术语纠正已学习",
        font=font(13, bold=True),
        fill=(*hex_rgb(INK), alpha),
    )
    card.text(
        (566, 326 + y_offset),
        "bench mark  →  benchmark   ·   下次生效",
        font=font(11),
        fill=(*hex_rgb(MUTED), alpha),
    )
    image.alpha_composite(overlay)


def draw_stepper(draw: ImageDraw.ImageDraw, frame: int) -> None:
    labels = ("快捷键", "实时文字", "提交", "人工修改", "下次命中")
    if frame < 29:
        active = 0
    elif frame < 61:
        active = 1
    elif frame < 76:
        active = 2
    elif frame < 113:
        active = 3
    else:
        active = 4
    start_x = 264
    y = 468
    for index, label in enumerate(labels):
        x = start_x + index * 83
        if index < active:
            circle_fill, line_colour, text_colour = GREEN, GREEN, GREEN
        elif index == active:
            circle_fill, line_colour, text_colour = ORANGE, ORANGE, ORANGE
        else:
            circle_fill, line_colour, text_colour = "#DCD6E0", "#DCD6E0", "#9A939F"
        draw.ellipse((x, y, x + 11, y + 11), fill=circle_fill)
        if index < len(labels) - 1:
            draw.line((x + 13, y + 5, x + 76, y + 5), fill=line_colour, width=2)
        draw.text(
            (x - 1, y + 16), label, font=font(9, bold=index == active), fill=text_colour
        )


def render_frame(frame: int) -> Image.Image:
    image = Image.new("RGBA", (WIDTH, HEIGHT), PLUM)
    draw_gradient(image, "#341B40", "#1B1023", horizontal_shift="#51253A")
    draw, _, _ = editor_chrome(image)

    first_final = "今天整理 bench mark 的实验结果。"
    corrected = "今天整理 benchmark 的实验结果。"
    second_final = "新的 benchmark 结果已写入当前光标。"

    if frame < 15:
        draw.text((264, 250), "▏", font=font(24), fill=BLUE)
        draw.text((264, 294), "准备好后，按你的快捷键开始", font=font(14), fill=MUTED)
    elif frame < 29:
        draw.text((264, 250), "▏", font=font(24), fill=BLUE)
        draw.text((264, 294), "已连接当前输入框", font=font(14, bold=True), fill=ORANGE)
        draw_listening_hud(draw, frame, "正在建立语音会话")
    elif frame < 61:
        partials = (
            "今天",
            "今天整理",
            "今天整理 bench mark",
            "今天整理 bench mark 的实验",
            "今天整理 bench mark 的实验结果",
            first_final,
        )
        progress = (frame - 29) / 32
        index = min(len(partials) - 1, int(progress * len(partials)))
        draw_preedit(
            draw,
            partials[index],
            y=250,
            status="IBus 实时预编辑 · 当前光标",
            caret_on=(frame // 5) % 2 == 0,
        )
        if frame >= 53:
            draw_listening_hud(draw, frame, "完成听写", "等待最终文字")
        else:
            draw_listening_hud(draw, frame, "正在听写 · 中文")
    elif frame < 76:
        draw_committed_line(draw, first_final, caret_on=(frame // 5) % 2 == 0)
        draw_final_badge(draw)
    elif frame < 88:
        draw_committed_line(draw, first_final, select_term="bench mark")
        pill(
            draw,
            (264, 302),
            "5 秒修改窗口 · 同一输入框",
            background=ORANGE_LIGHT,
            foreground=ORANGE,
            text_font=font(11, bold=True),
            pad_x=11,
            height=27,
        )
    elif frame < 98:
        edit_states = (
            "今天整理 benchm 的实验结果。",
            "今天整理 benchmar 的实验结果。",
            corrected,
        )
        index = min(2, int((frame - 88) / 4))
        draw_committed_line(draw, edit_states[index], caret_on=True)
        pill(
            draw,
            (264, 302),
            "只学习这次严格替换：bench mark → benchmark",
            background=ORANGE_LIGHT,
            foreground=ORANGE,
            text_font=font(11, bold=True),
            pad_x=11,
            height=27,
        )
    elif frame < 113:
        draw_committed_line(
            draw,
            corrected,
            highlight_term="benchmark",
            caret_on=(frame // 5) % 2 == 0,
        )
        draw_correction_card(image, phase_progress(frame, 98, 106))
    elif frame < 124:
        draw_committed_line(draw, corrected, highlight_term="benchmark")
        draw_correction_card(image, 1.0)
        draw_listening_hud(draw, frame, "下一次听写")
    elif frame < 146:
        partials = (
            "新的",
            "新的 benchmark",
            "新的 benchmark 结果",
            "新的 benchmark 结果已写入",
            second_final,
        )
        progress = (frame - 124) / 22
        index = min(len(partials) - 1, int(progress * len(partials)))
        draw_preedit(
            draw,
            partials[index],
            y=250,
            status="个人术语命中 · benchmark",
            highlight_term="benchmark",
            caret_on=(frame // 5) % 2 == 0,
        )
        if frame >= 141:
            draw_listening_hud(draw, frame, "完成听写", "等待最终文字")
        else:
            draw_listening_hud(draw, frame, "正在听写 · 中文")
    else:
        draw_committed_line(draw, second_final, highlight_term="benchmark")
        draw_final_badge(draw, "文字已提交")
        pill(
            draw,
            (635, 302),
            "不读剪贴板 · 不模拟粘贴",
            background=GREEN_LIGHT,
            foreground=GREEN,
            text_font=font(11, bold=True),
            pad_x=11,
            height=29,
        )

    draw_key_hint(draw, frame)
    draw_stepper(draw, frame)
    # A quiet playhead makes each encoded GIF frame unique, keeps timing exact,
    # and gives the viewer a visual cue for the loop boundary.
    playhead_x = 50 + round(860 * (frame + 1) / FRAME_COUNT)
    draw.line((50, 503, playhead_x, 503), fill=ORANGE, width=2)
    return image.convert("RGB")


def build_global_palette(frames: list[Image.Image]) -> Image.Image:
    sample_indices = [0, 17, 35, 53, 66, 80, 91, 103, 117, 131, 145, 155]
    swatch = Image.new("RGB", (WIDTH * 4, HEIGHT * 3))
    for slot, frame_index in enumerate(sample_indices):
        swatch.paste(frames[frame_index], ((slot % 4) * WIDTH, (slot // 4) * HEIGHT))
    quantized = swatch.quantize(
        colors=192,
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.NONE,
    )
    palette = Image.new("P", (1, 1))
    palette.putpalette(quantized.getpalette())
    return palette


def save_animation(frames: list[Image.Image], destination: Path) -> None:
    palette = build_global_palette(frames)
    indexed = [
        frame.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)
        for frame in frames
    ]
    indexed[0].save(
        destination,
        format="GIF",
        save_all=True,
        append_images=indexed[1:],
        # GIF stores centiseconds. 80, 80, 90 ms averages 83.3 ms (12 fps).
        duration=[80 if index % 3 != 2 else 90 for index in range(len(indexed))],
        loop=0,
        disposal=1,
        optimize=False,
    )


def save_poster(destination: Path) -> None:
    render_frame(FRAME_COUNT - 1).save(destination, format="PNG", optimize=True)


def draw_social_preview() -> Image.Image:
    image = Image.new("RGBA", (SOCIAL_WIDTH, SOCIAL_HEIGHT), PLUM)
    draw_gradient(image, "#351A41", "#1B1023", horizontal_shift="#672A36")
    draw = ImageDraw.Draw(image)

    # Decorative, deterministic sound bars.
    for index in range(13):
        x = 72 + index * 15
        bar_height = 18 + round(34 * abs(math.sin(index * 0.78 + 0.5)))
        draw.rounded_rectangle(
            (x, 91 - bar_height // 2, x + 7, 91 + bar_height // 2),
            radius=4,
            fill=ORANGE if 4 <= index <= 8 else "#87576E",
        )

    draw.text((72, 137), "Open Voice Input", font=font(63, bold=True), fill="#FFFFFF")
    draw.text(
        (75, 215), "轻量的 Linux 原生语音输入", font=font(34, bold=True), fill="#FFD9CA"
    )
    draw.text(
        (76, 273),
        "在当前光标直接出字 · 不碰剪贴板 · 会记住你的纠正",
        font=font(22),
        fill="#D7CBDD",
    )

    features = (
        ("IBus 原生输入", BLUE, "#EAF0FF"),
        ("个人术语学习", ORANGE, ORANGE_LIGHT),
        ("数据由你掌控", GREEN, GREEN_LIGHT),
    )
    x = 76
    for value, accent, background in features:
        box = pill(
            draw,
            (x, 345),
            value,
            background=background,
            foreground=accent,
            text_font=font(16, bold=True),
            pad_x=17,
            height=39,
        )
        x = box[2] + 14

    draw.text(
        (76, 446),
        "自定义快捷键，说话，再按一次完成。下一次，术语更懂你。",
        font=font(18),
        fill="#FFFFFF",
    )
    draw.text(
        (76, 494),
        "github.com/SidUParis/openVoiceInput_linux",
        font=font(16, mono=True),
        fill="#AFA1B5",
    )

    # A compact caret/preedit motif on the right.
    rounded_shadow(
        image, (792, 94, 1127, 505), radius=24, blur=25, offset_y=14, opacity=80
    )
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((792, 94, 1127, 505), radius=24, fill="#FFFFFF")
    draw.rounded_rectangle((792, 94, 1127, 150), radius=24, fill="#F4F2F6")
    draw.rectangle((792, 124, 1127, 150), fill="#F4F2F6")
    draw.ellipse((817, 111, 839, 133), fill=ORANGE_LIGHT)
    microphone_icon(draw, (828, 122), ORANGE)
    draw.text((852, 111), "实时输入", font=font(14, bold=True), fill=INK)
    draw.rounded_rectangle((1075, 108, 1107, 136), radius=8, fill="#E7E3EA")
    for y in (116, 121, 126):
        draw.ellipse((1090, y, 1093, y + 3), fill="#746D79")
    draw.text((819, 180), "当前光标", font=font(15, bold=True), fill=MUTED)
    draw.text((819, 221), "新的", font=font(24), fill=INK)
    term_font = font(22, bold=True)
    term_width = text_width(draw, "benchmark", term_font)
    draw.rounded_rectangle(
        (819, 260, 819 + term_width + 12, 296), radius=7, fill=BLUE_LIGHT
    )
    draw.text((825, 263), "benchmark", font=term_font, fill=BLUE)
    draw.text((819, 306), "结果写在光标处。", font=font(22), fill=INK)
    draw.line((819, 345, 1056, 345), fill=BLUE, width=4)
    cursor(draw, 1066, 309, 34)
    pill(
        draw,
        (819, 366),
        "IBus 实时预编辑",
        background=BLUE_LIGHT,
        foreground=BLUE,
        text_font=font(13, bold=True),
        pad_x=14,
        height=32,
    )
    pill(
        draw,
        (819, 414),
        "bench mark  →  benchmark",
        background=ORANGE_LIGHT,
        foreground=ORANGE,
        text_font=font(11, bold=True),
        pad_x=12,
        height=34,
    )
    draw.text((819, 465), "人工修正 · 下一次生效", font=font(13), fill=MUTED)
    return image.convert("RGB")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
        help=f"asset output directory (default: {DEFAULT_OUTPUT_DIRECTORY})",
    )
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    output_directory = arguments.output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)

    frames = [render_frame(index) for index in range(FRAME_COUNT)]
    save_animation(frames, output_directory / "hero-demo.gif")
    save_poster(output_directory / "hero-demo-poster.png")
    draw_social_preview().save(
        output_directory / "social-preview.png",
        format="PNG",
        optimize=True,
    )


if __name__ == "__main__":
    main()
