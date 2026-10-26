"""PDF operations with PyMuPDF (fitz): create, merge, split, rotate, watermark."""

from __future__ import annotations

from pathlib import Path

from winbrow.skill_script_utils import (
    clip, emit, ensure_parent, fail, load_spec, read_text_arg, require_module, resolve_path,
)

fitz = require_module("fitz", "PyMuPDF")


def _parse_pages(raw, page_count: int) -> list[int]:
    """Parse 1-based page lists like '1-3,5'. Empty means every page."""
    text = str(raw or "").strip()
    if not text or text.lower() == "all":
        return list(range(page_count))
    pages: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            try:
                first, last = int(start), int(end)
            except ValueError:
                fail(f"Invalid page range '{part}'.")
            pages.extend(range(first - 1, last))
        else:
            try:
                pages.append(int(part) - 1)
            except ValueError:
                fail(f"Invalid page number '{part}'.")
    return [page for page in pages if 0 <= page < page_count]


def create(spec: dict) -> None:
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() != ".pdf":
        output = output.with_suffix(".pdf")
    ensure_parent(output)
    content = read_text_arg(spec)
    if not content:
        fail("Nothing to write. Provide content or content_path.")
    title = str(spec.get("title") or "").strip()
    if title:
        content = f"{title}\n\n{content}"

    document = fitz.open()
    page_width, page_height = fitz.paper_size("a4")
    margin = 54
    rect = fitz.Rect(margin, margin, page_width - margin, page_height - margin)
    # Split the content into page-sized chunks on line boundaries.
    lines = content.splitlines() or [""]
    index = 0
    # Approximate the number of lines that fit; insert_textbox wraps long
    # lines within the rect. Drawing once per page avoids double-rendering.
    line_height = 11 * 1.35
    lines_per_page = max(10, int(rect.height / line_height))
    while index < len(lines):
        page = document.new_page(width=page_width, height=page_height)
        chunk = lines[index:index + lines_per_page]
        index += len(chunk)
        page.insert_textbox(
            rect, "\n".join(chunk), fontsize=11, fontname="helv", lineheight=1.35,
        )
    document.save(str(output))
    document.close()
    emit(True, f"Created PDF: {output}", path=output)


def merge(spec: dict) -> None:
    inputs = spec.get("inputs") or spec.get("input_paths") or []
    if not isinstance(inputs, list) or len(inputs) < 2:
        fail("Merge needs at least two input PDFs in 'inputs'.")
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() != ".pdf":
        output = output.with_suffix(".pdf")
    ensure_parent(output)
    merged = fitz.open()
    for item in inputs:
        path = resolve_path(item)
        if not path.is_file():
            fail(f"Input PDF not found: {path}")
        with fitz.open(str(path)) as source:
            merged.insert_pdf(source)
    merged.save(str(output))
    merged.close()
    emit(True, f"Merged {len(inputs)} PDFs into {output}", path=output)


def split(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    output_dir = resolve_path(spec.get("output_dir") or path.parent)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with fitz.open(str(path)) as source:
        pages = _parse_pages(spec.get("pages"), source.page_count)
        for page_number in pages:
            target = output_dir / f"{path.stem}_page_{page_number + 1}.pdf"
            with fitz.open() as one:
                one.insert_pdf(source, from_page=page_number, to_page=page_number)
                one.save(str(target))
            written.append(str(target))
    emit(True, f"Split {len(written)} page(s) into {output_dir}", paths=written)


def rotate(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    output = resolve_path(spec.get("output_path") or path)
    ensure_parent(output)
    angle = int(spec.get("angle") or 90)
    with fitz.open(str(path)) as document:
        pages = _parse_pages(spec.get("pages"), document.page_count)
        for page_number in pages:
            page = document[page_number]
            page.set_rotation((page.rotation + angle) % 360)
        document.save(str(output))
    emit(True, f"Rotated {len(pages)} page(s) by {angle} degrees", path=output)


def watermark(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    output = resolve_path(spec.get("output_path") or path)
    ensure_parent(output)
    text = str(spec.get("text") or "CONFIDENTIAL")
    with fitz.open(str(path)) as document:
        for page in document:
            rect = page.rect
            page.insert_text(
                fitz.Point(rect.width * 0.16, rect.height * 0.55),
                text,
                fontsize=float(spec.get("font_size") or 48),
                fontname="helv",
                color=(0.7, 0.7, 0.7),
                rotate=45,
                overlay=True,
            )
        document.save(str(output))
    emit(True, f"Added watermark to {output}", path=output)


def encrypt(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    output = resolve_path(spec.get("output_path") or path)
    ensure_parent(output)
    user_pw = str(spec.get("user_password") or "")
    owner_pw = str(spec.get("owner_password") or user_pw)
    if not user_pw:
        fail("A user_password is required for encryption.")
    with fitz.open(str(path)) as document:
        document.save(
            str(output),
            encryption=fitz.PDF_ENCRYPT_AES_256,
            user_pw=user_pw,
            owner_pw=owner_pw,
        )
    emit(True, f"Encrypted PDF: {output}", path=output)


def extract_text(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    with fitz.open(str(path)) as document:
        pages = _parse_pages(spec.get("pages"), document.page_count)
        chunks = []
        for page_number in pages:
            chunks.append(f"\n--- Page {page_number + 1} ---\n{document[page_number].get_text()}")
    emit(True, clip("".join(chunks)))


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "create").strip().lower()
    handlers = {
        "create": create,
        "merge": merge,
        "split": split,
        "rotate": rotate,
        "watermark": watermark,
        "encrypt": encrypt,
        "extract_text": extract_text,
        "read": extract_text,
    }
    handler = handlers.get(operation)
    if handler is None:
        fail(
            f"Unknown PDF operation '{operation}'. Use create, merge, split, "
            "rotate, watermark, encrypt, or extract_text."
        )
    handler(spec)


if __name__ == "__main__":
    main()
