"""Create and edit PowerPoint .pptx decks with python-pptx."""

from __future__ import annotations

from pathlib import Path

from winbrow.skill_script_utils import (
    clip, emit, ensure_parent, fail, load_spec, require_module, resolve_path,
)
from winbrow.image_search import find_image as find_web_image
from winbrow.image_search import search_and_download
from winbrow.layout.pptx import build_deck
from winbrow.layout.validate import validate_pptx

pptx = require_module("pptx", "python-pptx")
Presentation = pptx.Presentation

from pptx.util import Inches, Pt


def _inches(value, default=None):
    try:
        return Inches(float(value))
    except (TypeError, ValueError):
        return default


def _slide_by_number(deck, number):
    try:
        index = int(number) - 1
    except (TypeError, ValueError):
        return None
    if 0 <= index < len(deck.slides):
        return deck.slides[index]
    return None


def _add_content_slide(deck, spec: dict) -> None:
    title = str(spec.get("title") or "").strip()
    bullets = spec.get("bullets") or []
    if isinstance(bullets, str):
        bullets = [line.strip(" -") for line in bullets.splitlines() if line.strip()]
    layout = deck.slide_layouts[1]
    slide = deck.slides.add_slide(layout)
    slide.shapes.title.text = title
    body = slide.placeholders[1].text_frame
    body.text = ""
    for index, bullet in enumerate(bullets):
        paragraph = body.paragraphs[0] if index == 0 else body.add_paragraph()
        paragraph.text = str(bullet)
        paragraph.level = 0
    notes = str(spec.get("notes") or "").strip()
    if notes:
        slide.notes_slide.notes_text_frame.text = notes


def _resolve_slide_images(slides: list, output: Path, spec: dict) -> None:
    assets_dir = output.parent / f"{output.stem}_assets"
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        kind = str(slide.get("type") or "").strip().lower()
        items = slide.get("images")
        if not items and kind in {"image", "images"}:
            items = [{
                "path": slide.get("image_path") or slide.get("path"),
                "query": slide.get("query") or slide.get("image_query"),
                "caption": slide.get("caption") or "",
                "credit": slide.get("credit") or "",
            }]
        if not items:
            continue
        resolved = []
        for item in items:
            if isinstance(item, str):
                item = {"query": item}
            if not isinstance(item, dict):
                continue
            raw_path = item.get("path") or item.get("image_path")
            query = str(item.get("query") or "").strip()
            if raw_path:
                path = Path(str(raw_path))
                if not path.is_absolute():
                    path = (output.parent / path).resolve()
                if path.is_file():
                    item["path"] = str(path)
                    resolved.append(item)
                    continue
            if query:
                try:
                    found = find_web_image(query, assets_dir, min_width=900)
                except Exception as exc:
                    if spec.get("allow_missing_images"):
                        continue
                    fail(f"Could not find a reusable image for '{query}': {exc}")
                item["path"] = found["path"]
                item["credit"] = item.get("credit") or found.get("credit", "")
                item["caption"] = item.get("caption") or found.get("title", query)
                resolved.append(item)
                continue
            if not spec.get("allow_missing_images"):
                fail("An image needs a local 'path' or a web 'query'.")
        slide["images"] = resolved


def create(spec: dict) -> None:
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() != ".pptx":
        output = output.with_suffix(".pptx")
    ensure_parent(output)

    slides = spec.get("slides")
    if not isinstance(slides, list):
        slides = []
    if not slides:
        title = str(spec.get("title") or "").strip()
        slides = [{
            "type": "title",
            "title": title or "Presentation",
            "subtitle": str(spec.get("subtitle") or ""),
        }]
    _resolve_slide_images(slides, output, spec)

    deck_spec = dict(spec)
    deck_spec["slides"] = slides
    try:
        builder, _pending = build_deck(deck_spec)
        builder.save(output)
    except Exception as exc:
        fail(f"Could not create presentation: {type(exc).__name__}: {exc}")

    validation = None
    if spec.get("validate", True):
        render_dir = spec.get("render_dir") or output.parent / f"{output.stem}_slides"
        validation = validate_pptx(
            output, aspect=str(spec.get("aspect") or "16:9"), render_dir=render_dir,
        )
        if validation and not validation["ok"] and spec.get("strict"):
            fail(
                "Presentation was generated but failed layout validation:\n"
                + "\n".join(validation["errors"][:8]),
                data={"slides": len(builder.prs.slides), "validation": validation},
                path=output,
            )

    message = f"Created presentation: {output} ({len(builder.prs.slides)} slides)"
    if validation:
        message += (
            f"; validation: {len(validation['errors'])} error(s), "
            f"{len(validation['warnings'])} warning(s); "
            f"{len(validation['rendered'])} slide preview(s)"
        )
    emit(
        True,
        message,
        path=output,
        count=len(builder.prs.slides),
        data={
            "slides": len(builder.prs.slides),
            "slide_size": [round(builder.width, 3), round(builder.height, 3)],
            "validation": validation,
        },
    )


def add_slide(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("output_path") or "")
    if not path.is_file():
        fail(f"Presentation not found: {path}")
    deck = Presentation(str(path))
    _add_content_slide(deck, spec)
    deck.save(str(path))
    emit(True, f"Added a slide to {path}", path=path)


def read(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"Presentation not found: {path}")
    deck = Presentation(str(path))
    chunks: list[str] = []
    for index, slide in enumerate(deck.slides, start=1):
        chunks.append(f"\n--- Slide {index} ---")
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text.strip():
                chunks.append(shape.text.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    chunks.append(" | ".join(cell.text.strip() for cell in row.cells))
        try:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                chunks.append(f"[notes] {notes}")
        except Exception:
            pass
    emit(True, clip("\n".join(chunks)), path=path)


def add_table(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("output_path") or "")
    if not path.is_file():
        fail(f"Presentation not found: {path}")
    deck = Presentation(str(path))
    slide = _slide_by_number(deck, spec.get("slide")) or (
        deck.slides[-1] if len(deck.slides) else None
    )
    if slide is None:
        fail("The presentation has no slides; create one first.")
    rows = spec.get("rows") or []
    if not isinstance(rows, list) or not rows:
        headers = spec.get("headers") or []
        if not headers:
            fail("Provide rows (list of lists) or headers for the table.")
        rows = [headers]
    rows = [row for row in rows if isinstance(row, list)]
    if not rows:
        fail("Table rows must be lists of cell values.")
    columns = max(len(row) for row in rows)
    if columns <= 0:
        fail("Table rows are empty.")
    shape = slide.shapes.add_table(
        rows=len(rows), cols=columns,
        left=_inches(spec.get("left"), Inches(0.7)),
        top=_inches(spec.get("top"), Inches(1.5)),
        width=_inches(spec.get("width"), Inches(11.9)),
        height=_inches(spec.get("height"), Inches(0.4 * len(rows))),
    ).table
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row[:columns]):
            cell = shape.cell(row_index, column_index)
            cell.text = str(value)
            if row_index == 0 and spec.get("header_row", True):
                for paragraph in cell.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.bold = True
    deck.save(str(path))
    emit(True, f"Added a {len(rows)}x{columns} table to {path}", path=path)


def add_image(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("output_path") or "")
    if not path.is_file():
        fail(f"Presentation not found: {path}")
    image_path = resolve_path(spec.get("image_path") or spec.get("image") or "")
    if not image_path.is_file():
        fail(f"Image not found: {image_path}")
    deck = Presentation(str(path))
    slide = _slide_by_number(deck, spec.get("slide")) or (
        deck.slides[-1] if len(deck.slides) else None
    )
    if slide is None:
        fail("The presentation has no slides; create one first.")
    try:
        slide.shapes.add_picture(
            str(image_path),
            _inches(spec.get("left"), Inches(0.7)),
            _inches(spec.get("top"), Inches(1.5)),
            width=_inches(spec.get("width")),
            height=_inches(spec.get("height")),
        )
    except Exception as exc:
        fail(f"Could not insert image: {type(exc).__name__}: {exc}")
    deck.save(str(path))
    emit(True, f"Inserted image into {path}", path=path)


def set_notes(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("output_path") or "")
    if not path.is_file():
        fail(f"Presentation not found: {path}")
    notes = str(spec.get("notes") or "").strip()
    if not notes:
        fail("Provide 'notes' text.")
    deck = Presentation(str(path))
    slide = _slide_by_number(deck, spec.get("slide")) or (
        deck.slides[-1] if len(deck.slides) else None
    )
    if slide is None:
        fail("The presentation has no slides.")
    slide.notes_slide.notes_text_frame.text = notes
    deck.save(str(path))
    emit(True, f"Updated speaker notes in {path}", path=path)


def delete_slide(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("output_path") or "")
    if not path.is_file():
        fail(f"Presentation not found: {path}")
    deck = Presentation(str(path))
    slide = _slide_by_number(deck, spec.get("slide"))
    if slide is None:
        fail("Provide a valid 1-based 'slide' number to delete.")
    slide_id_list = deck.slides._sldIdLst
    for element in list(slide_id_list):
        if str(element.get("id")) == str(slide.slide_id):
            slide_id_list.remove(element)
            break
    deck.save(str(path))
    emit(True, f"Deleted slide {spec.get('slide')} from {path}", path=path)


def find_image_op(spec: dict) -> None:
    query = str(spec.get("query") or "").strip()
    if not query:
        fail("A search 'query' is required.")
    output_dir = resolve_path(spec.get("output_dir") or (Path.cwd() / "images"))
    try:
        count = max(1, min(int(spec.get("count") or 3), 8))
    except (TypeError, ValueError):
        count = 3
    results = search_and_download(query, output_dir, count=count)
    if not results:
        fail(
            f"No reusable image found for '{query}'. Use a local image path "
            "or a diagram instead; do not insert a placeholder."
        )
    emit(
        True,
        f"Downloaded {len(results)} licensed image(s) for '{query}' to {output_dir}\n"
        + "\n".join(f"- {item['path']} | {item['credit']}" for item in results),
        path=output_dir,
        paths=[item["path"] for item in results],
        data={"images": results},
    )


def validate_op(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"Presentation not found: {path}")
    aspect = str(spec.get("aspect") or "").strip() or None
    render_dir = spec.get("render_dir") or path.parent / f"{path.stem}_slides"
    result = validate_pptx(path, aspect=aspect, render_dir=render_dir)
    summary = (
        f"{path.name}: {result['slides']} slide(s), "
        f"{result['slide_size'][0]}x{result['slide_size'][1]}in, "
        f"{len(result['errors'])} error(s), {len(result['warnings'])} warning(s), "
        f"{len(result['rendered'])} slide preview(s)."
    )
    if result["errors"]:
        summary += "\nErrors:\n" + "\n".join(f"- {item}" for item in result["errors"][:10])
    if result["warnings"]:
        summary += "\nWarnings:\n" + "\n".join(f"- {item}" for item in result["warnings"][:8])
    emit(result["ok"], summary, path=path, data=result)


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "create").strip().lower()
    handlers = {
        "create": create,
        "add_slide": add_slide,
        "add_table": add_table,
        "add_image": add_image,
        "set_notes": set_notes,
        "delete_slide": delete_slide,
        "read": read,
        "find_image": find_image_op,
        "validate": validate_op,
    }
    handler = handlers.get(operation)
    if handler is None:
        fail(
            f"Unknown pptx operation '{operation}'. Use create, add_slide, "
            "add_table, add_image, set_notes, delete_slide, read, find_image, "
            "or validate."
        )
    handler(spec)


if __name__ == "__main__":
    main()
