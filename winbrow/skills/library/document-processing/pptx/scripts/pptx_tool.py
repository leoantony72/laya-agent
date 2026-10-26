"""Create and edit PowerPoint .pptx decks with python-pptx."""

from __future__ import annotations

from winbrow.skill_script_utils import (
    clip, emit, ensure_parent, fail, load_spec, require_module, resolve_path,
)

pptx = require_module("pptx", "python-pptx")
Presentation = pptx.Presentation


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


def create(spec: dict) -> None:
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() != ".pptx":
        output = output.with_suffix(".pptx")
    ensure_parent(output)

    deck = Presentation()
    title = str(spec.get("title") or "").strip()
    subtitle = str(spec.get("subtitle") or "").strip()
    if title:
        slide = deck.slides.add_slide(deck.slide_layouts[0])
        slide.shapes.title.text = title
        if subtitle:
            slide.placeholders[1].text = subtitle

    slides = spec.get("slides") or []
    if not isinstance(slides, list) or not slides:
        slides = [{"title": "Overview", "bullets": []}]
    for item in slides:
        if isinstance(item, dict):
            _add_content_slide(deck, item)
        elif isinstance(item, str):
            _add_content_slide(deck, {"title": item, "bullets": []})

    deck.save(str(output))
    emit(True, f"Created presentation: {output}", path=output, count=len(slides))


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
    emit(True, clip("\n".join(chunks)))


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "create").strip().lower()
    handlers = {"create": create, "add_slide": add_slide, "read": read}
    handler = handlers.get(operation)
    if handler is None:
        fail(f"Unknown pptx operation '{operation}'. Use create, add_slide, or read.")
    handler(spec)


if __name__ == "__main__":
    main()
