"""Approximate PPTX slide renderer (PIL) for visual QA when Office is absent.

It renders the actual shapes/text/images/tables stored in the .pptx so layout
problems are visible. Charts are drawn as simple native-style placeholders;
Office/LibreOffice rendering is preferred when available (see validate.py).
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .measure import font_file, wrap_text

log = logging.getLogger("winbrow.layout.preview")

DPI = 120.0
EMU_PER_INCH = 914400


def _px(emu: int | float) -> float:
    return float(emu) / EMU_PER_INCH * DPI


def _pt(px: float) -> float:
    return float(px) * 72.0 / DPI


def _color(value: Any, default: tuple[int, int, int] = (30, 30, 30)) -> tuple[int, int, int]:
    try:
        rgb = value.rgb
        return (rgb[0], rgb[1], rgb[2])
    except Exception:
        return default


def _fill(shape: Any) -> tuple[int, int, int] | None:
    try:
        if shape.fill.type is None:
            return None
        return _color(shape.fill.fore_color)
    except Exception:
        return None


def _line(shape: Any) -> tuple[int, int, int] | None:
    try:
        return _color(shape.line.color)
    except Exception:
        return None


def _font(font_name: str, size_pt: float, bold: bool, italic: bool) -> ImageFont.FreeTypeFont:
    style = "sans"
    low = (font_name or "").lower()
    if "georgia" in low or "times" in low or "serif" in low:
        style = "serif"
    if bold and italic:
        style = style + "_bold"
    elif bold:
        style = style + "_bold"
    elif italic:
        style = style + "_italic"
    path = font_file(style) or font_file("sans")
    size_px = max(6, int(round(size_pt * DPI / 72.0)))
    try:
        return ImageFont.truetype(path, size_px) if path else ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


def _draw_text_frame(
    draw: ImageDraw.ImageDraw, shape: Any, width_px: int, height_px: int,
) -> None:
    frame = shape.text_frame
    left = _px(shape.left + (frame.margin_left or 0))
    top = _px(shape.top + (frame.margin_top or 0))
    box_w = max(8.0, _px(shape.width) - _px((frame.margin_left or 0) + (frame.margin_right or 0)))
    box_h = max(8.0, _px(shape.height) - _px((frame.margin_top or 0) + (frame.margin_bottom or 0)))
    blocks: list[tuple[str, float, bool, bool, tuple[int, int, int], float, bool]] = []
    for paragraph in frame.paragraphs:
        text = "".join(run.text for run in paragraph.runs) or paragraph.text
        if not text:
            continue
        size = 18.0
        bold = italic = False
        color = (30, 30, 30)
        for run in paragraph.runs:
            if run.font.size:
                size = max(size, run.font.size.pt) if len(paragraph.runs) > 1 else run.font.size.pt
            bold = bold or bool(run.font.bold)
            italic = italic or bool(run.font.italic)
            color = _color(run.font.color, color)
        bullet = False
        try:
            p_pr = paragraph._p.pPr
            if p_pr is not None and p_pr.find(
                "{http://schemas.openxmlformats.org/drawingml/2006/main}buChar"
            ) is not None:
                bullet = True
        except Exception:
            pass
        align = str(paragraph.alignment or "LEFT")
        line_spacing = float(paragraph.line_spacing or 1.12)
        blocks.append((text, size, bold, italic, color, line_spacing, bullet, align))

    total_h = 0.0
    prepared = []
    for text, size, bold, italic, color, spacing, bullet, align in blocks:
        prefix = "• " if bullet else ""
        lines = wrap_text(prefix + text, _pt(box_w), _pt(size), style="sans")
        height = len(lines) * size * DPI / 72.0 * spacing
        prepared.append((lines, size, bold, italic, color, spacing, height, align))
        total_h += height

    y = top
    anchor = str(frame.vertical_anchor or "TOP")
    if "MIDDLE" in anchor:
        y = top + max(0.0, (box_h - total_h) / 2)
    elif "BOTTOM" in anchor:
        y = top + max(0.0, box_h - total_h)
    for lines, size, bold, italic, color, spacing, height, align in prepared:
        font = _font("Segoe UI", _pt(size), bold, italic)
        for line in lines:
            line_w = draw.textlength(line, font=font)
            x = left
            if "CENTER" in align:
                x = left + max(0.0, (box_w - line_w) / 2)
            elif "RIGHT" in align:
                x = left + max(0.0, box_w - line_w)
            draw.text((x, y), line, font=font, fill=color)
            y += size * DPI / 72.0 * spacing


def _draw_picture(draw: ImageDraw.ImageDraw, shape: Any) -> None:
    try:
        with Image.open(BytesIO(shape.image.blob)) as image:
            image = image.convert("RGB")
            target = (max(1, int(_px(shape.width))), max(1, int(_px(shape.height))))
            fitted = image.copy()
            fitted.thumbnail(target, Image.LANCZOS)
            canvas = Image.new("RGB", target, (255, 255, 255))
            canvas.paste(
                fitted,
                ((target[0] - fitted.width) // 2, (target[1] - fitted.height) // 2),
            )
            # Pasted onto the current canvas via the caller's image.
            shape._winbrow_preview = canvas
    except Exception as exc:
        log.debug("preview picture failed: %s", exc)


def _draw_table(draw: ImageDraw.ImageDraw, shape: Any) -> None:
    table = shape.table
    x = _px(shape.left)
    y = _px(shape.top)
    widths = [_px(column.width) for column in table.columns]
    heights = [_px(row.height) for row in table.rows]
    for row_index, row in enumerate(table.rows):
        row_h = heights[row_index] if row_index < len(heights) else 20
        x_cell = x
        for col_index, cell in enumerate(row.cells):
            col_w = widths[col_index] if col_index < len(widths) else 100
            fill = None
            try:
                fill = _color(cell.fill.fore_color)
            except Exception:
                pass
            draw.rectangle(
                (x_cell, y, x_cell + col_w, y + row_h), fill=fill,
                outline=(190, 196, 205), width=1,
            )
            text = cell.text.strip()
            if text:
                size = 12.0
                color = (30, 30, 30)
                for paragraph in cell.text_frame.paragraphs:
                    for run in paragraph.runs:
                        if run.font.size:
                            size = run.font.size.pt
                        color = _color(run.font.color, color)
                        break
                font = _font("Segoe UI", size, False, False)
                lines = wrap_text(text, _pt(max(8, col_w - 8)), size)
                ty = y + 3
                for line in lines:
                    draw.text((x_cell + 4, ty), line, font=font, fill=color)
                    ty += size * DPI / 72.0 * 1.1
            x_cell += col_w
        y += row_h


def _draw_chart(draw: ImageDraw.ImageDraw, shape: Any) -> None:
    x = _px(shape.left)
    y = _px(shape.top)
    w = _px(shape.width)
    h = _px(shape.height)
    fill = (255, 255, 255)
    try:
        from pptx.oxml.ns import qn
        sp_pr = shape.chart._chartSpace.find(qn("c:spPr"))
        if sp_pr is not None:
            color = sp_pr.find(".//" + qn("a:srgbClr"))
            if color is not None:
                value = color.get("val") or ""
                if len(value) == 6:
                    fill = tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        pass
    draw.rectangle((x, y, x + w, y + h), fill=fill, outline=(200, 200, 200))
    try:
        chart = shape.chart
        categories = [str(item) for item in list(chart.plots[0].categories)]
        series = list(chart.series)
        values = [[float(value or 0) for value in list(item.values)] for item in series]
    except Exception:
        categories, series, values = [], [], []
    light_bg = sum(fill) > 380
    label_color = (70, 70, 70) if light_bg else (220, 225, 235)
    draw.text((x + 10, y + 8), "Chart", fill=label_color)
    if not values:
        return
    max_value = max(max(row) for row in values if row) or 1.0
    left, bottom = x + 40, y + h - 28
    width = w - 60
    height = h - 50
    count = max(1, len(values[0]))
    group_w = width / count
    colors = [(56, 189, 248), (167, 139, 250), (245, 158, 11), (16, 185, 129)]
    for s_index, row in enumerate(values):
        color = colors[s_index % len(colors)]
        for index, value in enumerate(row[:count]):
            bar_w = group_w / max(1, len(values)) * 0.7
            bar_h = max(2, height * value / max_value)
            bx = left + index * group_w + s_index * bar_w
            draw.rectangle(
                (bx, bottom - bar_h, bx + bar_w, bottom), fill=color,
            )
    for index, category in enumerate(categories[:count]):
        draw.text(
            (left + index * group_w + 2, bottom + 4),
            category[:12], fill=label_color, font=_font("Segoe UI", 9, False, False),
        )


def render_slide(slide: Any, width: int, height: int) -> Image.Image:
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    try:
        bg = slide.background.fill.fore_color
        draw.rectangle((0, 0, width, height), fill=_color(bg, (255, 255, 255)))
    except Exception:
        pass
    pictures = []
    for shape in slide.shapes:
        try:
            shape_type = str(shape.shape_type)
        except Exception:
            shape_type = ""
        if "PICTURE" in shape_type:
            try:
                _draw_picture(draw, shape)
                pictures.append(shape)
            except Exception:
                pass
            continue
        if getattr(shape, "has_chart", False):
            _draw_chart(draw, shape)
            continue
        if getattr(shape, "has_table", False):
            _draw_table(draw, shape)
            continue
        if "AUTO_SHAPE" in shape_type:
            fill = _fill(shape)
            outline = _line(shape)
            box = (_px(shape.left), _px(shape.top),
                   _px(shape.left + shape.width), _px(shape.top + shape.height))
            try:
                draw.rounded_rectangle(box, radius=6, fill=fill, outline=outline, width=1)
            except Exception:
                draw.rectangle(box, fill=fill, outline=outline, width=1)
        if getattr(shape, "has_text_frame", False) and shape.text.strip():
            _draw_text_frame(draw, shape, width, height)
    for shape in pictures:
        canvas = getattr(shape, "_winbrow_preview", None)
        if canvas is not None:
            image.paste(canvas, (int(_px(shape.left)), int(_px(shape.top))))
    return image


def render_pptx_preview(
    pptx_path: str | Path,
    output_dir: str | Path,
    *,
    width: int = 1600,
    height: int | None = None,
) -> list[str]:
    from pptx import Presentation

    pptx_path = Path(pptx_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    presentation = Presentation(str(pptx_path))
    if height is None:
        ratio = float(presentation.slide_height) / float(presentation.slide_width)
        height = max(600, int(width * ratio))
    written = []
    for index, slide in enumerate(presentation.slides, start=1):
        image = render_slide(slide, width, height)
        target = output_dir / f"{pptx_path.stem}_{index:02d}.png"
        image.save(target)
        written.append(str(target))
    return written
