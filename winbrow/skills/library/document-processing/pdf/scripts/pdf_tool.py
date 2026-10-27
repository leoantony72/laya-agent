"""PDF operations with pure-Python libraries: create, merge, split, rotate,
watermark, encrypt, and extract text.

PyMuPDF is intentionally not used. Its native DLL can be blocked by Windows
Smart App Control, which makes the package look missing even when it is
installed. ``pypdf`` and ``fpdf2`` are pure Python.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from winbrow.pdf_utils import PdfError, open_reader
from winbrow.skill_script_utils import (
    clip, emit, ensure_parent, fail, load_spec, read_text_arg, require_module,
    resolve_path,
)

pypdf = require_module("pypdf", "pypdf")
PdfReader = pypdf.PdfReader
PdfWriter = pypdf.PdfWriter
fpdf = require_module("fpdf", "fpdf2")
FPDF = fpdf.FPDF

_UNICODE_FAMILY = "WinBrowUnicode"


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


def _is_cp1252(text: str) -> bool:
    try:
        text.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def _unicode_font_path() -> Path | None:
    root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in ("segoeui.ttf", "arial.ttf", "calibri.ttf"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _set_font(pdf, text: str, size: float, *, bold: bool = False) -> bool:
    """Select a font for ``text``; return True when the text must be sanitized."""
    if _is_cp1252(text):
        pdf.set_font("helvetica", style="B" if bold else "", size=size)
        return False
    font_path = _unicode_font_path()
    if font_path is not None:
        if _UNICODE_FAMILY not in pdf.fonts:
            pdf.add_font(_UNICODE_FAMILY, "", str(font_path))
        # The same TTF is registered for the regular style only. Use size to
        # distinguish a title rather than asking fpdf2 for a missing bold face.
        pdf.set_font(_UNICODE_FAMILY, style="", size=size)
        return False
    pdf.set_font("helvetica", style="B" if bold else "", size=size)
    return True


def _cp1252(text: str) -> str:
    return text.encode("cp1252", errors="replace").decode("cp1252")


def _write_pdf(writer, output: Path) -> None:
    ensure_parent(output)
    temporary = output.with_name(output.name + ".tmp")
    try:
        with open(temporary, "wb") as handle:
            writer.write(handle)
        os.replace(temporary, output)
    except Exception as exc:
        fail(f"Could not write PDF: {type(exc).__name__}: {exc}")
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _readable_reader(path: Path):
    try:
        return open_reader(path)
    except PdfError as exc:
        fail(str(exc))


def create(spec: dict) -> None:
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() != ".pdf":
        output = output.with_suffix(".pdf")
    ensure_parent(output)
    content = read_text_arg(spec)
    if not content:
        fail("Nothing to write. Provide content or content_path.")
    title = " ".join(str(spec.get("title") or "").splitlines()).strip()

    pdf = FPDF(unit="pt", format="A4")
    pdf.set_margins(54, 54, 54)
    pdf.set_auto_page_break(auto=True, margin=54)
    pdf.add_page()

    # A title is rendered larger; body text follows. fpdf2 adds pages
    # automatically when multi_cell reaches the bottom margin.
    if title:
        sanitize = _set_font(pdf, title, 18, bold=True)
        title_text = _cp1252(title) if sanitize else title
        pdf.multi_cell(0, 24, title_text, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(8)

    sanitize = _set_font(pdf, content, 11)
    for line in content.splitlines():
        line = line.replace("\t", "    ")
        if sanitize:
            line = _cp1252(line)
        if line.strip():
            pdf.multi_cell(0, 15, line, new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.ln(15)

    try:
        pdf.output(str(output))
    except Exception as exc:
        fail(f"Could not create PDF: {type(exc).__name__}: {exc}")
    emit(True, f"Created PDF: {output}", path=output)


def merge(spec: dict) -> None:
    inputs = spec.get("inputs") or spec.get("input_paths") or []
    if not isinstance(inputs, list) or len(inputs) < 2:
        fail("Merge needs at least two input PDFs in 'inputs'.")
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() != ".pdf":
        output = output.with_suffix(".pdf")
    writer = PdfWriter()
    for item in inputs:
        path = resolve_path(item)
        if not path.is_file():
            fail(f"Input PDF not found: {path}")
        reader = _readable_reader(path)
        for page in reader.pages:
            writer.add_page(page)
    _write_pdf(writer, output)
    emit(True, f"Merged {len(inputs)} PDFs into {output}", path=output)


def split(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    output_dir = resolve_path(spec.get("output_dir") or path.parent)
    output_dir.mkdir(parents=True, exist_ok=True)
    reader = _readable_reader(path)
    pages = _parse_pages(spec.get("pages"), len(reader.pages))
    if not pages:
        fail("No pages selected.")
    written: list[str] = []
    for page_number in pages:
        target = output_dir / f"{path.stem}_page_{page_number + 1}.pdf"
        writer = PdfWriter()
        writer.add_page(reader.pages[page_number])
        _write_pdf(writer, target)
        written.append(str(target))
    emit(True, f"Split {len(written)} page(s) into {output_dir}", paths=written)


def rotate(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    output = resolve_path(spec.get("output_path") or path)
    try:
        angle = int(spec.get("angle") or 90) % 360
    except (TypeError, ValueError):
        fail("The rotation angle must be a whole number.")
    if angle % 90:
        fail("The rotation angle must be a multiple of 90 degrees.")
    reader = _readable_reader(path)
    pages = _parse_pages(spec.get("pages"), len(reader.pages))
    if not pages:
        fail("No pages selected.")
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    for page_number in pages:
        writer.pages[page_number].rotate(angle)
    _write_pdf(writer, output)
    emit(True, f"Rotated {len(pages)} page(s) by {angle} degrees", path=output)


def _watermark_overlay(width: float, height: float, text: str, font_size: float):
    pdf = FPDF(unit="pt", format=(width, height))
    pdf.set_margins(0, 0, 0)
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    sanitize = _set_font(pdf, text, font_size)
    if sanitize:
        text = _cp1252(text)
    pdf.set_text_color(180, 180, 180)
    text_width = pdf.get_string_width(text)
    x = max(0.0, (width - text_width) / 2)
    y = height / 2
    with pdf.rotation(45, x=x + text_width / 2, y=y):
        pdf.text(x, y, text)
    return PdfReader(io.BytesIO(bytes(pdf.output()))).pages[0]


def watermark(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    output = resolve_path(spec.get("output_path") or path)
    text = str(spec.get("text") or "CONFIDENTIAL")
    try:
        font_size = float(spec.get("font_size") or 48)
    except (TypeError, ValueError):
        font_size = 48.0
    reader = _readable_reader(path)
    pages = _parse_pages(spec.get("pages"), len(reader.pages))
    if not pages:
        fail("No pages selected.")
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    try:
        for page_number in pages:
            page = writer.pages[page_number]
            overlay = _watermark_overlay(
                float(page.mediabox.width),
                float(page.mediabox.height),
                text,
                font_size,
            )
            page.merge_page(overlay)
    except Exception as exc:
        fail(f"Could not add watermark: {type(exc).__name__}: {exc}")
    _write_pdf(writer, output)
    emit(True, f"Added watermark to {output}", path=output)


def encrypt(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    output = resolve_path(spec.get("output_path") or path)
    user_pw = str(spec.get("user_password") or "")
    owner_pw = str(spec.get("owner_password") or user_pw)
    if not user_pw:
        fail("A user_password is required for encryption.")
    reader = _readable_reader(path)
    if reader.is_encrypted:
        fail("Input PDF is already encrypted.")
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    try:
        writer.encrypt(user_pw, owner_pw, algorithm="AES-256")
    except Exception as exc:
        fail(f"Could not encrypt PDF: {type(exc).__name__}: {exc}")
    _write_pdf(writer, output)
    emit(True, f"Encrypted PDF: {output}", path=output)


def extract_text(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    reader = _readable_reader(path)
    pages = _parse_pages(spec.get("pages"), len(reader.pages))
    if not pages:
        fail("No pages selected.")
    chunks = []
    try:
        for page_number in pages:
            text = reader.pages[page_number].extract_text() or ""
            chunks.append(f"\n--- Page {page_number + 1} ---\n{text}")
    except Exception as exc:
        fail(f"Could not extract PDF text: {type(exc).__name__}: {exc}")
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
