"""Shared text measurement and wrapping for PDF and PPTX layout engines."""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

FONT_DIR = Path("C:/Windows/Fonts")
FALLBACK_FONTS = {
    "sans": ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"),
    "sans_bold": ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"),
    "sans_italic": ("segoeuii.ttf", "ariali.ttf", "DejaVuSans-Oblique.ttf"),
    "serif": ("georgia.ttf", "times.ttf", "DejaVuSerif.ttf"),
    "serif_bold": ("georgiab.ttf", "timesbd.ttf", "DejaVuSerif-Bold.ttf"),
    "serif_italic": ("georgiai.ttf", "timesi.ttf", "DejaVuSerif-Italic.ttf"),
    "mono": ("consola.ttf", "cour.ttf", "DejaVuSansMono.ttf"),
    "mono_bold": ("consolab.ttf", "courbd.ttf", "DejaVuSansMono-Bold.ttf"),
}


@lru_cache(maxsize=64)
def font_file(style: str = "sans") -> str | None:
    for name in FALLBACK_FONTS.get(style, FALLBACK_FONTS["sans"]):
        path = FONT_DIR / name
        if path.is_file():
            return str(path)
    return None


@lru_cache(maxsize=256)
def _font(style: str, size: int) -> ImageFont.FreeTypeFont:
    path = font_file(style)
    if path:
        return ImageFont.truetype(path, max(1, int(round(size))))
    return ImageFont.load_default()


@lru_cache(maxsize=8192)
def text_width(text: str, size: float, style: str = "sans") -> float:
    if not text:
        return 0.0
    return float(_font(style, int(round(size))).getlength(text))


def _break_word(word: str, max_width: float, size: float, style: str) -> list[str]:
    if text_width(word, size, style) <= max_width:
        return [word]
    pieces: list[str] = []
    current = ""
    for char in word:
        candidate = current + char
        if current and text_width(candidate, size, style) > max_width:
            pieces.append(current)
            current = char
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def wrap_text(
    text: str,
    max_width: float,
    size: float,
    *,
    style: str = "sans",
) -> list[str]:
    """Wrap a paragraph to ``max_width``; breaks long URLs/words by character."""
    lines: list[str] = []
    for paragraph in str(text or "").replace("\r\n", "\n").split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            if not word:
                continue
            candidate = word if not current else f"{current} {word}"
            if text_width(candidate, size, style) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            if text_width(word, size, style) <= max_width:
                current = word
            else:
                chunks = _break_word(word, max_width, size, style)
                lines.extend(chunks[:-1])
                current = chunks[-1] if chunks else ""
        if current:
            lines.append(current)
    return lines or [""]


def line_height(size: float, spacing: float = 1.25) -> float:
    return float(size) * float(spacing)


def text_height(text: str, width: float, size: float, *, spacing: float = 1.25,
                style: str = "sans") -> float:
    return len(wrap_text(text, width, size, style=style)) * line_height(size, spacing)


def fit_font_size(
    text: str,
    width: float,
    height: float,
    *,
    max_size: float,
    min_size: float,
    spacing: float = 1.25,
    style: str = "sans",
    step: float = 0.5,
) -> tuple[float, list[str]]:
    """Largest font size whose wrapped text fits in the box."""
    size = float(max_size)
    while size >= min_size:
        lines = wrap_text(text, width, size, style=style)
        if len(lines) * line_height(size, spacing) <= height:
            return size, lines
        size -= step
    return float(min_size), wrap_text(text, width, min_size, style=style)


def image_fit(
    image_width: int,
    image_height: int,
    box_width: float,
    box_height: float,
    *,
    mode: str = "contain",
) -> tuple[float, float, float, float]:
    """Return (width, height, crop_x_fraction, crop_y_fraction).

    ``contain`` fits inside; ``cover`` fills and reports how much to crop.
    """
    if image_width <= 0 or image_height <= 0:
        return box_width, box_height, 0.0, 0.0
    scale_contain = min(box_width / image_width, box_height / image_height)
    if mode == "cover":
        scale = max(box_width / image_width, box_height / image_height)
        width = image_width * scale
        height = image_height * scale
        crop_x = max(0.0, 1.0 - box_width / width)
        crop_y = max(0.0, 1.0 - box_height / height)
        return box_width, box_height, crop_x / 2, crop_y / 2
    width = image_width * scale_contain
    height = image_height * scale_contain
    return width, height, 0.0, 0.0


def pt_to_emu(points: float) -> int:
    return int(round(points * 12700))


def emu_to_pt(emu: int) -> float:
    return float(emu) / 12700.0


def inches_to_pt(inches: float) -> float:
    return float(inches) * 72.0


def pt_to_inches(points: float) -> float:
    return float(points) / 72.0


def safe_div(value: float, divisor: float, default: float = 0.0) -> float:
    return value / divisor if divisor else default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def ceil_div(value: float, divisor: float) -> int:
    return int(math.ceil(safe_div(value, divisor, 1.0)))
