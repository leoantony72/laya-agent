"""Render posters and static art to PNG or PDF with Pillow."""

from __future__ import annotations

import os
from pathlib import Path

from winbrow.skill_script_utils import emit, ensure_parent, fail, load_spec, require_module, resolve_path

Image = require_module("PIL.Image", "Pillow")
ImageDraw = require_module("PIL.ImageDraw", "Pillow")
ImageFont = require_module("PIL.ImageFont", "Pillow")

_FONT_CANDIDATES = {
    "serif": ["georgia.ttf", "times.ttf", "cambria.ttc"],
    "sans": ["arial.ttf", "segoeui.ttf", "calibri.ttf"],
    "mono": ["consola.ttf", "cour.ttf"],
    "display": ["impact.ttf", "georgiab.ttf", "arialbd.ttf"],
}


def _font(name: str, size: int):
    filename = str(name or "sans")
    candidates = _FONT_CANDIDATES.get(filename.lower(), [filename])
    for candidate in candidates:
        path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / candidate
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=int(size))
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    words = str(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        width = draw.textbbox((0, 0), candidate, font=font)[2]
        if current and width > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _draw_text(draw, element: dict) -> None:
    text = str(element.get("text") or "")
    if not text:
        return
    size = int(element.get("size") or element.get("font_size") or 48)
    font = _font(str(element.get("font") or "sans"), size)
    x = int(element.get("x") or 0)
    y = int(element.get("y") or 0)
    color = str(element.get("color") or "#111111")
    max_width = int(element.get("max_width") or 0)
    lines = _wrap(draw, text, font, max_width) if max_width else text.splitlines()
    line_height = int(size * float(element.get("line_height") or 1.25))
    for index, line in enumerate(lines):
        draw.text((x, y + index * line_height), line, font=font, fill=color)


def _draw_element(draw, element: dict, canvas) -> None:
    kind = str(element.get("type") or "text").lower()
    if kind == "text":
        _draw_text(draw, element)
    elif kind == "rect":
        draw.rectangle(
            [int(element.get("x", 0)), int(element.get("y", 0)),
             int(element.get("x", 0)) + int(element.get("w", 0)),
             int(element.get("y", 0)) + int(element.get("h", 0))],
            fill=element.get("fill"), outline=element.get("outline"),
            width=int(element.get("width") or 1),
        )
    elif kind == "ellipse":
        draw.ellipse(
            [int(element.get("x", 0)), int(element.get("y", 0)),
             int(element.get("x", 0)) + int(element.get("w", 0)),
             int(element.get("y", 0)) + int(element.get("h", 0))],
            fill=element.get("fill"), outline=element.get("outline"),
            width=int(element.get("width") or 1),
        )
    elif kind == "line":
        draw.line(
            [int(element.get("x1", 0)), int(element.get("y1", 0)),
             int(element.get("x2", 0)), int(element.get("y2", 0))],
            fill=element.get("fill") or "#111111",
            width=int(element.get("width") or 1),
        )
    elif kind == "image":
        source = resolve_path(element.get("path") or "")
        if not source.is_file():
            raise FileNotFoundError(f"Image not found: {source}")
        image = Image.open(source).convert("RGBA")
        width = int(element.get("w") or image.width)
        height = int(element.get("h") or image.height)
        image = image.resize((width, height), Image.LANCZOS)
        canvas.alpha_composite(image, (int(element.get("x", 0)), int(element.get("y", 0))))


def render(spec: dict) -> None:
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() not in {".png", ".jpg", ".jpeg", ".pdf"}:
        output = output.with_suffix(".png")
    ensure_parent(output)

    width = int(spec.get("width") or 1200)
    height = int(spec.get("height") or 1600)
    pages = spec.get("pages")
    if not isinstance(pages, list) or not pages:
        pages = [{
            "background": spec.get("background") or "#ffffff",
            "elements": spec.get("elements") or [],
        }]

    rendered = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        canvas = Image.new("RGBA", (width, height), page.get("background") or "#ffffff")
        draw = ImageDraw.Draw(canvas)
        for element in page.get("elements") or []:
            if isinstance(element, dict):
                _draw_element(draw, element, canvas)
        rendered.append(canvas.convert("RGB"))
    if not rendered:
        fail("No pages to render.")

    if output.suffix.lower() == ".pdf":
        rendered[0].save(str(output), "PDF", resolution=150.0, save_all=True,
                         append_images=rendered[1:])
    elif len(rendered) == 1:
        rendered[0].save(str(output), quality=95)
    else:
        output_dir = resolve_path(spec.get("output_dir") or output.parent)
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index, page in enumerate(rendered, start=1):
            target = output_dir / f"{output.stem}_{index}{output.suffix}"
            page.save(str(target), quality=95)
            paths.append(str(target))
        emit(True, f"Rendered {len(paths)} page images.", paths=paths)
        return
    emit(True, f"Rendered {output}", path=output, count=len(rendered))


def main() -> None:
    render(load_spec())


if __name__ == "__main__":
    main()
