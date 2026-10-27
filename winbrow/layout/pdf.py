"""
Measured PDF layout engine (fpdf2).

Every block is measured before it is drawn, so pagination, table rows, and
image placement are based on actual page geometry rather than hard-coded
coordinates. Used by the PDF skill's ``create`` operation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fpdf import FPDF
from PIL import Image

from .measure import font_file, image_fit

log = logging.getLogger("winbrow.layout.pdf")

IMAGE_RE = re.compile(r"^!\[(.*?)\]\((.+?)\)\s*$")

PAGE_SIZES: dict[str, tuple[float, float]] = {
    "a4": (595.28, 841.89),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
}

THEMES: dict[str, dict[str, Any]] = {
    "corporate": {
        "font": "sans", "body_size": 10.5, "body_spacing": 1.38,
        "heading_sizes": (20.0, 14.5, 12.0),
        "text": (24, 32, 45), "muted": (95, 105, 120),
        "accent": (29, 78, 216), "table_header": (29, 78, 216),
        "table_header_text": (255, 255, 255), "table_row": (244, 247, 252),
        "code_bg": (244, 246, 248), "quote_bg": (245, 248, 255),
    },
    "academic": {
        "font": "serif", "body_size": 11.0, "body_spacing": 1.45,
        "heading_sizes": (20.0, 14.0, 12.0),
        "text": (20, 20, 20), "muted": (100, 100, 100),
        "accent": (31, 58, 95), "table_header": (31, 58, 95),
        "table_header_text": (255, 255, 255), "table_row": (247, 247, 247),
        "code_bg": (245, 245, 245), "quote_bg": (248, 248, 245),
    },
    "minimal": {
        "font": "sans", "body_size": 10.5, "body_spacing": 1.40,
        "heading_sizes": (22.0, 15.0, 12.0),
        "text": (17, 17, 17), "muted": (110, 110, 110),
        "accent": (17, 17, 17), "table_header": (17, 17, 17),
        "table_header_text": (255, 255, 255), "table_row": (246, 246, 246),
        "code_bg": (244, 244, 244), "quote_bg": (248, 248, 248),
    },
    "editorial": {
        "font": "serif", "body_size": 11.0, "body_spacing": 1.45,
        "heading_sizes": (22.0, 15.0, 12.0),
        "text": (20, 20, 19), "muted": (107, 104, 98),
        "accent": (217, 119, 87), "table_header": (20, 20, 19),
        "table_header_text": (255, 255, 255), "table_row": (250, 249, 245),
        "code_bg": (246, 244, 240), "quote_bg": (250, 247, 242),
    },
}


@dataclass
class PdfTheme:
    name: str = "corporate"
    page_size: str = "a4"
    orientation: str = "portrait"
    margins: tuple[float, float, float, float] = (56.0, 56.0, 56.0, 56.0)
    title: str = ""
    author: str = ""
    header: str = ""
    footer: str = ""
    page_numbers: bool = True
    header_on_first: bool = False
    body_size: float | None = None
    body_spacing: float | None = None
    heading_sizes: tuple[float, ...] | None = None
    colors: dict[str, Any] = field(default_factory=dict)

    @property
    def palette(self) -> dict[str, Any]:
        base = dict(THEMES.get(self.name, THEMES["corporate"]))
        base.update(self.colors or {})
        if self.body_size:
            base["body_size"] = float(self.body_size)
        if self.body_spacing:
            base["body_spacing"] = float(self.body_spacing)
        if self.heading_sizes:
            base["heading_sizes"] = tuple(self.heading_sizes)
        return base

    def page_format(self) -> tuple[float, float]:
        width, height = PAGE_SIZES.get(self.page_size.lower(), PAGE_SIZES["a4"])
        if self.orientation.lower() in {"landscape", "horizontal"}:
            width, height = height, width
        return width, height


def parse_blocks(content: str, base_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """Parse Markdown-ish text into measured layout blocks."""
    base = Path(base_dir) if base_dir else None
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []
    code: list[str] = []
    in_code = False
    table_rows: list[list[str]] = []

    def flush_paragraph() -> None:
        if paragraph:
            text = " ".join(line.strip() for line in paragraph).strip()
            if text:
                blocks.append({"kind": "paragraph", "text": text})
            paragraph.clear()

    def flush_table() -> None:
        nonlocal table_rows
        if not table_rows:
            return
        rows = [row for row in table_rows if any(cell.strip() for cell in row)]
        table_rows = []
        if not rows:
            return
        # Drop a Markdown separator row (|---|---|).
        if len(rows) >= 2 and all(
            set(cell.strip()) <= set("-: ") for cell in rows[1]
        ):
            rows = [rows[0]] + rows[2:]
        blocks.append({
            "kind": "table",
            "headers": [cell.strip() for cell in rows[0]],
            "rows": [[cell.strip() for cell in row] for row in rows[1:]],
        })

    for raw in str(content or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if in_code:
            if stripped.startswith("```"):
                blocks.append({"kind": "code", "text": "\n".join(code)})
                code = []
                in_code = False
            else:
                code.append(line)
            continue
        if stripped.startswith("```"):
            flush_paragraph()
            flush_table()
            in_code = True
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            flush_paragraph()
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            table_rows.append(cells)
            continue
        flush_table()
        if not stripped:
            flush_paragraph()
            continue
        if stripped.startswith("#"):
            flush_paragraph()
            level = min(3, len(stripped) - len(stripped.lstrip("#")))
            text = stripped[level:].strip()
            if text:
                blocks.append({"kind": "heading", "text": text, "level": level})
            continue
        if stripped.startswith(("- ", "* ")):
            flush_paragraph()
            blocks.append({"kind": "bullet", "text": stripped[2:].strip()})
            continue
        if len(stripped) > 2 and stripped[0].isdigit() and stripped[1:3] == ". ":
            flush_paragraph()
            blocks.append({"kind": "numbered", "text": stripped[3:].strip()})
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            blocks.append({"kind": "quote", "text": stripped.lstrip("> ").strip()})
            continue
        image_match = IMAGE_RE.match(stripped)
        if image_match:
            flush_paragraph()
            caption, raw_path = image_match.group(1).strip(), image_match.group(2).strip()
            path = Path(raw_path)
            if not path.is_absolute() and base is not None:
                path = (base / path).resolve()
            blocks.append({
                "kind": "image", "path": str(path), "caption": caption,
            })
            continue
        paragraph.append(stripped)

    flush_paragraph()
    flush_table()
    if in_code and code:
        blocks.append({"kind": "code", "text": "\n".join(code)})
    return blocks


class PdfReport(FPDF):
    def __init__(self, theme: PdfTheme):
        self.theme = theme
        self.palette = theme.palette
        width, height = theme.page_format()
        super().__init__(unit="pt", format=(width, height))
        self.set_margins(0, 0, 0)
        self.set_auto_page_break(False)
        self.alias_nb_pages()
        self._register_fonts()
        self.left, self.right, self.top, self.bottom = theme.margins
        self.header_h = 28.0 if (theme.header or theme.title) else 0.0
        self.footer_h = 26.0 if (theme.footer or theme.page_numbers or theme.author) else 0.0
        self.content_top = self.top + self.header_h
        self.content_bottom = height - self.bottom - self.footer_h
        if theme.title:
            self.set_title(theme.title)
        if theme.author:
            self.set_author(theme.author)

    # ------------------------------------------------------------------ fonts
    def _register_fonts(self) -> None:
        family = self.palette.get("font", "sans")
        base = "Serif" if family == "serif" else "Sans"
        regular = font_file("serif" if family == "serif" else "sans")
        bold = font_file("serif_bold" if family == "serif" else "sans_bold")
        italic = font_file("serif_italic" if family == "serif" else "sans_italic")
        mono = font_file("mono")
        mono_bold = font_file("mono_bold")
        try:
            if regular:
                self.add_font(base, "", regular)
            if bold:
                self.add_font(base, "B", bold)
            if italic:
                self.add_font(base, "I", italic)
            if mono:
                self.add_font("Mono", "", mono)
            if mono_bold:
                self.add_font("Mono", "B", mono_bold)
            self.family = base if regular else "helvetica"
            self.mono_family = "Mono" if mono else "courier"
        except Exception as exc:
            log.warning("PDF font registration failed (%s); using Helvetica", exc)
            self.family = "helvetica"
            self.mono_family = "courier"

    @property
    def content_width(self) -> float:
        return self.w - self.left - self.right

    @property
    def content_height(self) -> float:
        return self.content_bottom - self.content_top

    def _font(self, size: float, *, style: str = "", mono: bool = False) -> None:
        family = self.mono_family if mono else self.family
        self.set_font(family, style, size)

    def _wrap(self, text: str, width: float, size: float, *, bold: bool = False,
              italic: bool = False, mono: bool = False) -> list[str]:
        style = ("B" if bold else "") + ("I" if italic else "")
        self._font(size, style=style, mono=mono)
        try:
            lines = self.multi_cell(
                max(10.0, width), size * 1.25, str(text or ""),
                dry_run=True, output="LINES",
            )
        except TypeError:
            lines = self.multi_cell(
                max(10.0, width), size * 1.25, str(text or ""), split_only=True,
            )
        except Exception:
            lines = [str(text or "")]
        return [line for line in (lines or [""])] or [""]

    # ------------------------------------------------------------------ page chrome
    def header(self) -> None:
        if self.page_no() == 1 and not self.theme.header_on_first:
            return
        if self.header_h <= 0:
            return
        self._font(8, mono=False)
        self.set_text_color(*self.palette["muted"])
        text = self.theme.header or self.theme.title
        if text:
            self.set_xy(self.left, self.top * 0.42)
            self.cell(self.content_width, 10, text[:160])
        y = self.content_top - 6
        self.set_draw_color(*self.palette["muted"])
        self.set_line_width(0.4)
        self.line(self.left, y, self.w - self.right, y)

    def footer(self) -> None:
        if self.footer_h <= 0:
            return
        y = self.h - self.bottom * 0.55
        self.set_draw_color(*self.palette["muted"])
        self.set_line_width(0.4)
        self.line(self.left, y - 8, self.w - self.right, y - 8)
        self._font(8)
        self.set_text_color(*self.palette["muted"])
        left_text = self.theme.footer or self.theme.author
        if left_text:
            self.set_xy(self.left, y)
            self.cell(self.content_width * 0.75, 10, left_text[:120])
        if self.theme.page_numbers:
            label = f"Page {self.page_no()} of {{nb}}"
            self.set_xy(self.w - self.right - 100, y)
            self.cell(100, 10, label, align="R")

    def add_page(self, orientation: str = "", format: Any = None, same: bool = False) -> None:
        super().add_page(orientation=orientation, format=format, same=same)
        self.set_y(self.content_top)

    def ensure_space(self, height: float) -> None:
        if self.get_y() + height > self.content_bottom and self.get_y() > self.content_top + 1:
            self.add_page()

    # ------------------------------------------------------------------ blocks
    def render(self, blocks: list[dict[str, Any]]) -> int:
        self.add_page()
        for index, block in enumerate(blocks):
            kind = str(block.get("kind") or "paragraph").lower()
            if kind == "pagebreak":
                self.add_page()
                continue
            if kind == "spacer":
                self.set_y(min(self.content_bottom, self.get_y() + float(block.get("height") or 10)))
                continue
            if kind == "heading":
                height = self._heading_height(block)
                next_height = 0.0
                if index + 1 < len(blocks):
                    next_kind = str(blocks[index + 1].get("kind") or "").lower()
                    if next_kind in {"paragraph", "bullet", "numbered"}:
                        size = float(self.palette["body_size"])
                        next_height = size * float(self.palette["body_spacing"]) * 2.6
                    elif next_kind == "table":
                        next_height = 54.0
                    elif next_kind == "image":
                        next_height = 90.0
                    elif next_kind != "pagebreak" and next_kind != "spacer":
                        next_height = min(
                            self._block_height(blocks[index + 1]), 110.0,
                        )
                if self.get_y() + height + next_height > self.content_bottom:
                    self.add_page()
                else:
                    self.ensure_space(height)
                self._draw_heading(block)
            elif kind in {"bullet", "numbered"}:
                self._draw_bullet(block, numbered=(kind == "numbered"))
            elif kind == "table":
                self._draw_table(block)
            elif kind == "image":
                self._draw_image(block)
            elif kind == "code":
                self._draw_code(block)
            elif kind == "quote":
                self._draw_quote(block)
            else:
                self._draw_paragraph(block)
        return self.page_no()

    def _heading_height(self, block: dict[str, Any]) -> float:
        level = min(3, max(1, int(block.get("level") or 1)))
        size = self.palette["heading_sizes"][level - 1]
        lines = self._wrap(block.get("text", ""), self.content_width, size, bold=True)
        return len(lines) * size * 1.2 + (14 if level == 1 else 10 if level == 2 else 7)

    def _draw_heading(self, block: dict[str, Any]) -> None:
        level = min(3, max(1, int(block.get("level") or 1)))
        size = self.palette["heading_sizes"][level - 1]
        lines = self._wrap(block.get("text", ""), self.content_width, size, bold=True)
        self.ensure_space(len(lines) * size * 1.2)
        self._font(size, style="B")
        self.set_text_color(*self.palette["text"])
        x = self.left
        for line in lines:
            self.set_xy(x, self.get_y())
            self.cell(self.content_width, size * 1.2, line)
            self.set_y(self.get_y() + size * 1.2)
        if level == 1:
            self.set_draw_color(*self.palette["accent"])
            self.set_line_width(1.2)
            self.line(self.left, self.get_y() + 2, self.left + 54, self.get_y() + 2)
            self.set_y(self.get_y() + 10)
        else:
            self.set_y(self.get_y() + (5 if level == 2 else 3))

    def _block_height(self, block: dict[str, Any]) -> float:
        kind = str(block.get("kind") or "paragraph").lower()
        if kind == "heading":
            return self._heading_height(block)
        if kind == "paragraph":
            size = self.palette["body_size"]
            spacing = self.palette["body_spacing"]
            lines = self._wrap(block.get("text", ""), self.content_width, size)
            return len(lines) * size * spacing + 6
        if kind in {"bullet", "numbered"}:
            size = self.palette["body_size"]
            spacing = self.palette["body_spacing"]
            lines = self._wrap(block.get("text", ""), self.content_width - 18, size)
            return len(lines) * size * spacing + 4
        if kind == "code":
            size = 8.5
            lines = self._wrap(block.get("text", ""), self.content_width - 16, size, mono=True)
            return len(lines) * size * 1.35 + 16
        if kind == "quote":
            size = self.palette["body_size"]
            lines = self._wrap(block.get("text", ""), self.content_width - 26, size, italic=True)
            return len(lines) * size * self.palette["body_spacing"] + 18
        if kind == "image":
            try:
                with Image.open(block.get("path")) as image:
                    width, height = image.size
            except Exception:
                return 40
            box_w = self.content_width
            box_h = self.content_height * 0.55
            fit_w, fit_h, _, _ = image_fit(width, height, box_w, box_h)
            caption = 18 if block.get("caption") else 0
            credit = 12 if block.get("credit") else 0
            return fit_h + caption + credit + 10
        return 0

    def _draw_paragraph(self, block: dict[str, Any]) -> None:
        size = float(block.get("size") or self.palette["body_size"])
        spacing = self.palette["body_spacing"]
        text = str(block.get("text") or "")
        align = str(block.get("align") or "left").lower()
        color = self.palette["muted"] if block.get("muted") else self.palette["text"]
        width = self.content_width
        lines = self._wrap(text, width, size, italic=bool(block.get("italic")))
        line_h = size * spacing
        self._font(size, style="I" if block.get("italic") else "")
        self.set_text_color(*color)
        remaining = list(lines)
        while remaining:
            if self.content_bottom - self.get_y() < line_h:
                self.add_page()
            available = self.content_bottom - self.get_y()
            take = max(1, int(available // line_h))
            chunk, remaining = remaining[:take], remaining[take:]
            for line in chunk:
                self.set_xy(self.left, self.get_y())
                self.cell(width, line_h, line, align=align[0].upper())
                self.set_y(self.get_y() + line_h)
            if remaining:
                self.add_page()
        self.set_y(self.get_y() + 6)

    def _draw_bullet(self, block: dict[str, Any], *, numbered: bool) -> None:
        size = self.palette["body_size"]
        spacing = self.palette["body_spacing"]
        text = str(block.get("text") or "")
        marker = str(block.get("marker") or "•")
        indent = 16.0
        line_h = size * spacing
        lines = self._wrap(text, self.content_width - indent, size)
        self._font(size)
        self.set_text_color(*self.palette["text"])
        first_line = True
        remaining = list(lines)
        while remaining:
            if self.content_bottom - self.get_y() < line_h:
                self.add_page()
            available = self.content_bottom - self.get_y()
            take = max(1, int(available // line_h))
            chunk, remaining = remaining[:take], remaining[take:]
            for line in chunk:
                y = self.get_y()
                if first_line:
                    if numbered:
                        self.set_xy(self.left, y)
                        self.cell(indent, line_h, marker)
                    else:
                        self.set_xy(self.left + 3, y)
                        self.cell(indent, line_h, "•")
                    first_line = False
                self.set_xy(self.left + indent, y)
                self.cell(self.content_width - indent, line_h, line)
                self.set_y(y + line_h)
            if remaining:
                self.add_page()
        self.set_y(self.get_y() + 4)

    def _draw_code(self, block: dict[str, Any]) -> None:
        size = 8.5
        text = str(block.get("text") or "")
        lines = self._wrap(text, self.content_width - 16, size, mono=True)
        line_h = size * 1.35
        self._font(size, mono=True)
        self.set_text_color(35, 35, 35)
        remaining = list(lines)
        while remaining:
            available = self.content_bottom - self.get_y()
            take = max(1, int((available - 14) // line_h))
            chunk, remaining = remaining[:take], remaining[take:]
            height = len(chunk) * line_h + 14
            if height > available:
                self.add_page()
                available = self.content_bottom - self.get_y()
                take = max(1, int((available - 14) // line_h))
                chunk, remaining = chunk[:take], chunk[take:] + remaining
                height = len(chunk) * line_h + 14
            y = self.get_y()
            self.set_fill_color(*self.palette["code_bg"])
            self.set_draw_color(215, 215, 215)
            self.set_line_width(0.4)
            self.rect(self.left, y, self.content_width, height, style="DF")
            line_y = y + 7
            for line in chunk:
                self.set_xy(self.left + 8, line_y)
                self.cell(self.content_width - 16, line_h, line)
                line_y += line_h
            self.set_y(y + height + (8 if not remaining else 0))
            if remaining:
                self.add_page()
        self.set_y(self.get_y() + 8)

    def _draw_quote(self, block: dict[str, Any]) -> None:
        size = self.palette["body_size"]
        spacing = self.palette["body_spacing"]
        text = str(block.get("text") or "")
        lines = self._wrap(text, self.content_width - 30, size, italic=True)
        line_h = size * spacing
        self._font(size, style="I")
        self.set_text_color(*self.palette["text"])
        remaining = list(lines)
        while remaining:
            available = self.content_bottom - self.get_y()
            take = max(1, int((available - 18) // line_h))
            chunk, remaining = remaining[:take], remaining[take:]
            height = len(chunk) * line_h + 18
            if height > available:
                self.add_page()
                available = self.content_bottom - self.get_y()
                take = max(1, int((available - 18) // line_h))
                chunk, remaining = chunk[:take], chunk[take:] + remaining
                height = len(chunk) * line_h + 18
            y = self.get_y()
            self.set_fill_color(*self.palette["quote_bg"])
            self.rect(self.left, y, self.content_width, height, style="F")
            self.set_fill_color(*self.palette["accent"])
            self.rect(self.left, y, 3, height, style="F")
            line_y = y + 9
            for line in chunk:
                self.set_xy(self.left + 14, line_y)
                self.cell(self.content_width - 30, line_h, line)
                line_y += line_h
            self.set_y(y + height + (8 if not remaining else 0))
            if remaining:
                self.add_page()
        self.set_y(self.get_y() + 8)

    def _draw_image(self, block: dict[str, Any]) -> None:
        path = Path(str(block.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(f"Image not found: {path}")
        with Image.open(path) as image:
            width, height = image.size
        box_w = self.content_width
        box_h = self.content_height * 0.55
        fit_w, fit_h, _, _ = image_fit(width, height, box_w, box_h)
        caption = str(block.get("caption") or "").strip()
        credit = str(block.get("credit") or "").strip()
        caption_lines = self._wrap(caption, self.content_width - 20, 8.5, italic=True) if caption else []
        credit_lines = self._wrap(credit, self.content_width - 20, 7.5) if credit else []
        total = fit_h + len(caption_lines) * 11 + len(credit_lines) * 9 + (6 if caption or credit else 0)
        self.ensure_space(total + 10)
        x = self.left + (self.content_width - fit_w) / 2
        y = self.get_y()
        self.image(str(path), x=x, y=y, w=fit_w, h=fit_h)
        self.set_draw_color(208, 208, 208)
        self.set_line_width(0.5)
        self.rect(x, y, fit_w, fit_h)
        y += fit_h + 4
        if caption_lines:
            self._font(8.5, style="I")
            self.set_text_color(*self.palette["muted"])
            for line in caption_lines:
                self.set_xy(self.left + 10, y)
                self.cell(self.content_width - 20, 11, line, align="C")
                y += 11
        if credit_lines:
            self._font(7.5)
            self.set_text_color(*self.palette["muted"])
            for line in credit_lines:
                self.set_xy(self.left + 10, y)
                self.cell(self.content_width - 20, 9, line, align="C")
                y += 9
        self.set_y(y + 10)

    def _draw_table(self, block: dict[str, Any]) -> None:
        headers = [str(cell) for cell in (block.get("headers") or [])]
        rows = [[str(cell) for cell in row] for row in (block.get("rows") or [])]
        if not headers and rows:
            headers, rows = rows[0], rows[1:]
        if not headers and not rows:
            return
        columns = max([len(headers)] + [len(row) for row in rows]) if rows else len(headers)
        headers += [""] * (columns - len(headers))
        rows = [row + [""] * (columns - len(row)) for row in rows]

        size = 8.5
        line_h = 11.0
        pad_x, pad_y = 5.0, 4.0
        widths = self._table_widths(headers, rows, columns, size, pad_x)
        total_w = sum(widths)

        def cell_lines(text: str, width: float) -> list[str]:
            return self._wrap(text, max(12.0, width - 2 * pad_x), size)

        header_lines = [cell_lines(text, widths[i]) for i, text in enumerate(headers)]
        header_h = max(len(lines) for lines in header_lines) * line_h + 2 * pad_y

        if self.get_y() + header_h + line_h > self.content_bottom:
            self.add_page()

        y = self.get_y()
        self._draw_table_header(y, headers, widths, header_lines, total_w, header_h, line_h, pad_x, pad_y)
        y += header_h

        row_index = 0
        while row_index < len(rows):
            row = rows[row_index]
            lines_per_cell = [cell_lines(text, widths[i]) for i, text in enumerate(row)]
            remaining = list(lines_per_cell)
            first_chunk = True
            while any(remaining):
                available = self.content_bottom - y
                if available < 2 * pad_y + line_h:
                    self.add_page()
                    y = self.get_y()
                    self._draw_table_header(
                        y, headers, widths, header_lines, total_w, header_h,
                        line_h, pad_x, pad_y,
                    )
                    y += header_h
                    available = self.content_bottom - y
                    if available < 2 * pad_y + line_h:
                        raise RuntimeError(
                            "A table header/row does not fit on the page; "
                            "reduce the content or margins."
                        )
                max_lines = max(1, int((available - 2 * pad_y) // line_h))
                chunk = [lines[:max_lines] for lines in remaining]
                remaining = [lines[max_lines:] for lines in remaining]
                row_h = max(len(lines) for lines in chunk) * line_h + 2 * pad_y
                self._draw_table_row(
                    y, row, widths, chunk, total_w, row_h, line_h, pad_x, pad_y,
                    alternate=(row_index % 2 == 1),
                )
                y += row_h
                if any(remaining):
                    self.add_page()
                    y = self.get_y()
                    self._draw_table_header(
                        y, headers, widths, header_lines, total_w, header_h,
                        line_h, pad_x, pad_y,
                    )
                    y += header_h
                first_chunk = False
            row_index += 1
        self.set_y(y + 8)

    def _table_widths(self, headers: list[str], rows: list[list[str]], columns: int,
                      size: float, pad_x: float) -> list[float]:
        self._font(size)
        natural: list[float] = []
        for index in range(columns):
            values = [headers[index]] + [row[index] for row in rows[:40]]
            widest = max((self.get_string_width(value) for value in values), default=0)
            natural.append(min(widest + 2 * pad_x + 2, self.content_width * 0.42))
        minimum = 34.0
        natural = [max(minimum, value) for value in natural]
        total = sum(natural)
        if total <= self.content_width:
            extra = self.content_width - total
            natural = [value + extra * value / total for value in natural]
        else:
            natural = [value * self.content_width / total for value in natural]
        return natural

    def _draw_table_header(self, y: float, headers: list[str], widths: list[float],
                           header_lines: list[list[str]], total_w: float,
                           header_h: float, line_h: float, pad_x: float, pad_y: float) -> None:
        x = self.left
        self.set_fill_color(*self.palette["table_header"])
        self.set_draw_color(255, 255, 255)
        self.set_line_width(0.6)
        self.rect(x, y, total_w, header_h, style="F")
        self._font(8.5, style="B")
        self.set_text_color(*self.palette["table_header_text"])
        for index, width in enumerate(widths):
            cell_y = y + pad_y
            for line in header_lines[index]:
                self.set_xy(x + pad_x, cell_y)
                self.cell(width - 2 * pad_x, line_h, line)
                cell_y += line_h
            x += width
        self.set_draw_color(255, 255, 255)
        x = self.left
        for width in widths:
            self.line(x, y, x, y + header_h)
            x += width

    def _draw_table_row(self, y: float, row: list[str], widths: list[float],
                        chunk: list[list[str]], total_w: float, row_h: float,
                        line_h: float, pad_x: float, pad_y: float,
                        *, alternate: bool) -> None:
        if alternate:
            self.set_fill_color(*self.palette["table_row"])
            self.rect(self.left, y, total_w, row_h, style="F")
        self._font(8.5)
        self.set_text_color(*self.palette["text"])
        x = self.left
        for index, width in enumerate(widths):
            cell_y = y + pad_y
            for line in chunk[index]:
                self.set_xy(x + pad_x, cell_y)
                self.cell(width - 2 * pad_x, line_h, line)
                cell_y += line_h
            x += width
        self.set_draw_color(205, 210, 220)
        self.set_line_width(0.5)
        self.rect(self.left, y, total_w, row_h)
        x = self.left
        for width in widths:
            self.line(x, y, x, y + row_h)
            x += width
            self.line(x, y, x, y + row_h)


def render_pdf(blocks: list[dict[str, Any]], theme: PdfTheme, output: Path) -> dict:
    report = PdfReport(theme)
    pages = report.render(blocks)
    output.parent.mkdir(parents=True, exist_ok=True)
    report.output(str(output))
    return {"pages": pages, "path": str(output), "width": report.w, "height": report.h}
