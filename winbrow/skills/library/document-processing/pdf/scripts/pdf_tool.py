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
from winbrow.image_search import find_image as find_web_image
from winbrow.image_search import search_and_download
from winbrow.layout.pdf import PAGE_SIZES, PdfTheme, parse_blocks, render_pdf
from winbrow.layout.validate import validate_pdf
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


def _rgb(value, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if value is None:
        return default
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(max(0, min(255, int(part))) for part in value)  # type: ignore[return-value]
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return default
    try:
        return tuple(int(text[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return default


def _theme_from_spec(spec: dict) -> PdfTheme:
    from winbrow.layout.pdf import THEMES

    kind = str(spec.get("document_kind") or spec.get("kind") or "").strip().lower()
    paper_kind = kind in {"resume", "cv", "cover_letter", "cover-letter", "official", "memo"}
    name = str(spec.get("theme") or "").strip().lower()
    if not name:
        name = "resume" if paper_kind else "corporate"
    if name not in THEMES:
        name = "resume" if paper_kind else "corporate"
    base = THEMES[name]
    raw_margins = spec.get("margins")
    if isinstance(raw_margins, dict):
        margins = (
            float(raw_margins.get("left", 56)),
            float(raw_margins.get("top", 56)),
            float(raw_margins.get("right", 56)),
            float(raw_margins.get("bottom", 56)),
        )
    elif isinstance(raw_margins, (list, tuple)) and len(raw_margins) == 4:
        margins = tuple(float(value) for value in raw_margins)  # type: ignore[assignment]
    else:
        margins = (50.0, 50.0, 50.0, 50.0) if paper_kind else (56.0, 56.0, 56.0, 56.0)
    colors = {}
    for key, value in (spec.get("colors") or {}).items():
        if key in base:
            colors[key] = _rgb(value, base[key])
    page_size = str(spec.get("page_size") or "a4").strip().lower()
    # Resumes, cover letters and official documents are paper, never slides.
    if paper_kind and page_size in {"16:9", "16:10", "4:3"}:
        page_size = "a4"
    page_numbers = spec.get("page_numbers")
    if page_numbers is None:
        page_numbers = False if paper_kind else True
    return PdfTheme(
        name=name,
        page_size=page_size,
        orientation=str(spec.get("orientation") or "portrait").strip().lower(),
        margins=margins,
        title=str(spec.get("title") or "").strip(),
        author=str(spec.get("author") or "").strip(),
        header=str(spec.get("header") or "").strip(),
        footer=str(spec.get("footer") or "").strip(),
        page_numbers=bool(page_numbers),
        header_on_first=bool(spec.get("header_on_first", False)),
        body_size=spec.get("body_size"),
        body_spacing=spec.get("body_spacing"),
        heading_sizes=tuple(spec["heading_sizes"]) if spec.get("heading_sizes") else None,
        colors=colors,
    )


def _resolve_image_block(block: dict, *, base_dir: Path, assets_dir: Path,
                         spec: dict) -> None:
    raw_path = block.get("path") or block.get("image_path")
    query = str(block.get("query") or "").strip()
    if raw_path:
        path = Path(str(raw_path))
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        if not path.is_file():
            if not query:
                fail(f"Image not found: {path}")
        else:
            block["path"] = str(path)
            return
    if query:
        try:
            found = find_web_image(query, assets_dir, min_width=900)
        except Exception as exc:
            if spec.get("allow_missing_images"):
                block["path"] = ""
                block["credit"] = ""
                return
            fail(f"Could not find a reusable image for '{query}': {exc}")
        block["path"] = found["path"]
        block["credit"] = block.get("credit") or found.get("credit", "")
        block["caption"] = block.get("caption") or found.get("title", query)
        return
    fail("An image block needs a local 'path' or a web 'query'.")


def _blocks_from_spec(spec: dict, output: Path) -> tuple[list[dict], PdfTheme]:
    theme = _theme_from_spec(spec)
    content = read_text_arg(spec)
    raw_blocks = spec.get("blocks")
    if isinstance(raw_blocks, list) and raw_blocks:
        blocks = [dict(block) for block in raw_blocks if isinstance(block, dict)]
        base_dir = output.parent
    else:
        if not content and not (spec.get("images") or spec.get("image_query")):
            fail("Nothing to write. Provide content, blocks, or images.")
        base_dir = output.parent
        if spec.get("content_path"):
            base_dir = resolve_path(spec["content_path"]).parent
        blocks = parse_blocks(content, base_dir=base_dir)
    title = str(spec.get("title") or "").strip()
    if title:
        first = blocks[0] if blocks else None
        if not (
            first and str(first.get("kind") or "").lower() == "heading"
            and str(first.get("text") or "").strip().lower() == title.lower()
        ):
            blocks.insert(0, {"kind": "heading", "text": title, "level": 1})
    subtitle = str(spec.get("subtitle") or "").strip()
    if subtitle:
        insert_at = 1 if blocks and blocks[0].get("kind") == "heading" else 0
        blocks.insert(insert_at, {
            "kind": "paragraph", "text": subtitle,
            "align": "left" if theme.name == "resume" else "center",
            "muted": True, "size": max(10.0, float(theme.palette["body_size"]) + 1.5),
        })

    assets_dir = output.parent / f"{output.stem}_assets"
    image_blocks: list[dict] = []
    for block in blocks:
        if str(block.get("kind") or "").lower() == "image":
            _resolve_image_block(block, base_dir=base_dir, assets_dir=assets_dir, spec=spec)

    images = spec.get("images")
    if isinstance(images, list):
        for item in images:
            if isinstance(item, str):
                item = {"query": item}
            if not isinstance(item, dict):
                continue
            block = {
                "kind": "image",
                "path": item.get("path") or item.get("image_path"),
                "query": item.get("query"),
                "caption": item.get("caption") or "",
                "credit": item.get("credit") or "",
            }
            _resolve_image_block(block, base_dir=base_dir, assets_dir=assets_dir, spec=spec)
            if block.get("path"):
                image_blocks.append(block)
    query = str(spec.get("image_query") or "").strip()
    if query:
        block = {"kind": "image", "query": query,
                 "caption": spec.get("image_caption") or "",
                 "credit": spec.get("image_credit") or ""}
        _resolve_image_block(block, base_dir=base_dir, assets_dir=assets_dir, spec=spec)
        if block.get("path"):
            image_blocks.append(block)

    position = str(spec.get("image_position") or "end").strip().lower()
    if image_blocks:
        if position == "start":
            blocks = image_blocks + blocks
        else:
            blocks = blocks + image_blocks
    return blocks, theme


def create(spec: dict) -> None:
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() != ".pdf":
        output = output.with_suffix(".pdf")
    ensure_parent(output)
    blocks, theme = _blocks_from_spec(spec, output)
    if not blocks:
        fail("No content blocks to render.")
    try:
        result = render_pdf(blocks, theme, output)
    except Exception as exc:
        fail(f"Could not create PDF: {type(exc).__name__}: {exc}")

    validation = None
    if spec.get("validate", True):
        render_dir = spec.get("render_dir") or output.parent / f"{output.stem}_validation"
        validation = validate_pdf(
            output,
            margins=theme.margins,
            expected_size=theme.page_format(),
            render_dir=render_dir,
        )
        if validation and not validation["ok"] and spec.get("strict"):
            fail(
                "PDF was generated but failed layout validation:\n"
                + "\n".join(validation["errors"][:8]),
                data={"pages": result["pages"], "validation": validation},
                path=output,
            )

    message = f"Created PDF: {output} ({result['pages']} page(s))"
    if validation:
        message += (
            f"; validation: {len(validation['errors'])} error(s), "
            f"{len(validation['warnings'])} warning(s)"
        )
    emit(
        True,
        message,
        path=output,
        data={
            "pages": result["pages"],
            "page_size": [round(result["width"], 2), round(result["height"], 2)],
            "validation": validation,
        },
    )


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


def metadata(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    reader = _readable_reader(path)
    meta = reader.metadata or {}
    data = {
        "pages": len(reader.pages),
        "encrypted": bool(reader.is_encrypted),
        "title": str(meta.get("/Title") or ""),
        "author": str(meta.get("/Author") or ""),
        "subject": str(meta.get("/Subject") or ""),
        "creator": str(meta.get("/Creator") or ""),
        "producer": str(meta.get("/Producer") or ""),
        "file_size": path.stat().st_size,
    }
    emit(
        True,
        f"{path.name}: {data['pages']} page(s), "
        f"title={data['title'] or '(none)'}, encrypted={data['encrypted']}",
        data=data,
        path=path,
    )


def tables(spec: dict) -> None:
    """Extract tables with pdfplumber; returns rows grouped by page."""
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    pdfplumber = require_module("pdfplumber", "pdfplumber")
    try:
        pdf = pdfplumber.open(str(path))
    except Exception as exc:
        fail(f"Could not open PDF: {type(exc).__name__}: {exc}")
    with pdf:
        pages = _parse_pages(spec.get("pages"), len(pdf.pages))
        if not pages:
            fail("No pages selected.")
        found = []
        for page_number in pages:
            try:
                extracted = pdf.pages[page_number].extract_tables() or []
            except Exception as exc:
                fail(f"Table extraction failed on page {page_number + 1}: "
                     f"{type(exc).__name__}: {exc}")
            for table_index, table in enumerate(extracted, start=1):
                found.append({
                    "page": page_number + 1,
                    "table": table_index,
                    "rows": table,
                })
    summary = "\n".join(
        f"Page {item['page']} table {item['table']}: {len(item['rows'])} row(s)"
        for item in found
    ) or "No tables found."
    emit(True, clip(summary), data={"tables": found, "count": len(found)}, path=path)


def fields(spec: dict) -> None:
    """List AcroForm fields (name, type, current value, choices)."""
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    reader = _readable_reader(path)
    try:
        raw = reader.get_fields() or {}
    except Exception as exc:
        fail(f"Could not read form fields: {type(exc).__name__}: {exc}")
    items = []
    for name, field in raw.items():
        items.append({
            "name": str(name),
            "type": str(field.get("/FT") or ""),
            "value": str(field.get("/V") or ""),
            "choices": [str(choice) for choice in (field.get("/Opt") or [])],
            "required": bool(field.get("/Ff", 0) & 2) if field.get("/Ff") else False,
        })
    if not items:
        emit(True, "This PDF has no fillable AcroForm fields; treat it as a flat PDF.",
             data={"fields": []}, path=path)
    summary = "\n".join(
        f"- {item['name']} ({item['type'] or 'field'}) = {item['value'] or '(empty)'}"
        for item in items
    )
    emit(True, clip(f"{len(items)} field(s):\n{summary}"),
         data={"fields": items, "count": len(items)}, path=path)


def fill_form(spec: dict) -> None:
    """Fill AcroForm fields and write a new PDF (optionally flatten)."""
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    values = spec.get("fields")
    if not isinstance(values, dict) or not values:
        fail("Provide fields as a JSON object: {\"FieldName\": \"value\"}.")
    output = resolve_path(
        spec.get("output_path")
        or path.with_name(f"{path.stem}_filled{path.suffix or '.pdf'}")
    )
    ensure_parent(output)
    reader = _readable_reader(path)
    writer = PdfWriter()
    try:
        writer.clone_document_from_reader(reader)
    except Exception:
        for page in reader.pages:
            writer.add_page(page)
    flatten = bool(spec.get("flatten", False))
    clean = {}
    for key, value in values.items():
        if isinstance(value, bool):
            clean[str(key)] = "/Yes" if value else "/Off"
        else:
            clean[str(key)] = str(value)
    try:
        writer.set_need_appearances_writer(True)
        writer.update_page_form_field_values(
            None, clean,
            auto_regenerate=False, flatten=flatten,
        )
    except Exception as exc:
        fail(f"Could not fill form fields: {type(exc).__name__}: {exc}")
    _write_pdf(writer, output)
    emit(
        True,
        f"Filled {len(values)} field(s) into {output}"
        + (" (flattened)" if flatten else ""),
        path=output,
        count=len(values),
    )


def images(spec: dict) -> None:
    """Extract embedded images to a folder."""
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    output_dir = resolve_path(
        spec.get("output_dir") or (path.parent / f"{path.stem}_images")
    )
    ensure_parent(output_dir / "placeholder")
    reader = _readable_reader(path)
    pages = _parse_pages(spec.get("pages"), len(reader.pages))
    written = []
    for page_number in pages:
        try:
            page_images = list(reader.pages[page_number].images)
        except Exception:
            continue
        for index, image in enumerate(page_images, start=1):
            suffix = Path(str(image.name)).suffix or ".png"
            destination = output_dir / f"page{page_number + 1}_{index}{suffix}"
            try:
                destination.write_bytes(image.data)
                written.append(str(destination))
            except Exception:
                continue
    if not written:
        emit(True, "No embedded images found.", data={"files": []}, path=output_dir)
    emit(True, f"Extracted {len(written)} image(s) to {output_dir}",
         data={"files": written}, paths=written, path=output_dir)


def latex_op(spec: dict) -> None:
    """Compile a LaTeX resume/cover letter/official document when TeX exists."""
    from winbrow.layout.latex import LatexUnavailable, render_latex

    source = read_text_arg(spec) or str(
        spec.get("latex") or spec.get("source") or ""
    )
    if not source.strip():
        fail("Provide LaTeX source in 'content', 'latex', or 'content_path'.")
    output = resolve_path(
        spec.get("output_path") or spec.get("path") or "document.pdf"
    )
    if output.suffix.lower() != ".pdf":
        output = output.with_suffix(".pdf")
    ensure_parent(output)
    try:
        result = render_latex(source, output, timeout=int(spec.get("timeout") or 120))
    except LatexUnavailable as exc:
        fail(str(exc))
    except Exception as exc:
        fail(f"LaTeX compile failed: {type(exc).__name__}: {exc}")

    validation = None
    if bool(spec.get("validate", True)):
        expected = None
        page_size = str(spec.get("page_size") or "").strip().lower()
        if page_size in PAGE_SIZES:
            raw = PAGE_SIZES[page_size]
            expected = raw if isinstance(raw, tuple) else None
        validation = validate_pdf(
            result["path"], margins=(0, 0, 0, 0), expected_size=expected,
        )
        if validation.get("errors") and validation.get("pages", 0) > 0:
            fail(
                "LaTeX PDF validation failed:\n"
                + "\n".join(f"- {item}" for item in validation["errors"][:8])
            )
    emit(
        True,
        f"Compiled LaTeX to PDF: {result['path']} "
        f"({result['bytes']} bytes, engine={result['engine']})",
        path=result["path"],
        data={**result, "validation": validation},
    )


def from_html(spec: dict) -> None:
    """Print a local HTML page or deck to PDF with headless Chrome/Edge."""
    from winbrow.deck_repair import repair_deck
    from winbrow.html_to_pdf import PAGE_SIZES, detect_document_kind, html_to_pdf

    source = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if source.is_dir():
        source = source / "index.html"
    if not source.is_file():
        fail(f"HTML file not found: {source}")

    html_text = source.read_text(encoding="utf-8", errors="replace")
    kind = detect_document_kind(html_text)
    repair_report = {"ok": True, "repaired": False, "repairs": []}
    if kind == "presentation" and bool(spec.get("repair", True)):
        repair_report = repair_deck(source, write=True)
        if not repair_report.get("ok"):
            fail(
                "Deck needs repair but unresolved references remain:\n"
                + "\n".join(f"- {e}" for e in repair_report["errors"][:8])
            )

    output = resolve_path(
        spec.get("output_path") or source.with_suffix(".pdf")
    )
    # auto -> 16:9 for a slide deck, A4 for a document/resume.
    page = str(spec.get("page") or "auto").strip().lower()
    try:
        result = html_to_pdf(source, output, page=page)
    except Exception as exc:
        fail(f"HTML→PDF export failed: {type(exc).__name__}: {exc}")

    validation = None
    if bool(spec.get("validate", True)):
        from winbrow.layout.validate import validate_pdf

        expected = PAGE_SIZES.get(result["page"])
        expected_pt = (expected[0] * 72, expected[1] * 72) if expected else None
        validation = validate_pdf(
            result["path"],
            margins=(0, 0, 0, 0),
            expected_size=expected_pt,
        )
        problems = list(validation.get("errors") or [])
        if problems and validation.get("pages", 0) > 0:
            fail(
                "PDF validation failed:\n"
                + "\n".join(f"- {item}" for item in problems[:10])
            )

    emit(
        True,
        f"Exported HTML to PDF: {result['path']} "
        f"({result['bytes']} bytes, page={result['page']}, "
        f"{result['width_in']}x{result['height_in']}in)",
        path=result["path"],
        data={**result, "repair": repair_report, "validation": validation},
    )


def find_image_op(spec: dict) -> None:
    query = str(spec.get("query") or "").strip()
    if not query:
        fail("A search 'query' is required.")
    output_dir = resolve_path(
        spec.get("output_dir") or (Path.cwd() / "images")
    )
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
        + "\n".join(
            f"- {item['path']} | {item['credit']}" for item in results
        ),
        path=output_dir,
        paths=[item["path"] for item in results],
        data={"images": results},
    )


def validate_op(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"PDF not found: {path}")
    margins = (56.0, 56.0, 56.0, 56.0)
    raw = spec.get("margins")
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        margins = tuple(float(value) for value in raw)  # type: ignore[assignment]
    expected = None
    if spec.get("page_size"):
        page = str(spec["page_size"]).lower()
        width, height = PAGE_SIZES.get(page, (0, 0))
        if width and height:
            expected = (width, height)
            if str(spec.get("orientation") or "portrait").lower() in {"landscape", "horizontal"}:
                expected = (height, width)
    render_dir = spec.get("render_dir") or path.parent / f"{path.stem}_validation"
    result = validate_pdf(
        path, margins=margins, expected_size=expected, render_dir=render_dir,
    )
    summary = (
        f"{path.name}: {result['pages']} page(s), "
        f"{len(result['errors'])} error(s), {len(result['warnings'])} warning(s), "
        f"{len(result['rendered'])} page(s) rendered."
    )
    if result["errors"]:
        summary += "\nErrors:\n" + "\n".join(f"- {item}" for item in result["errors"][:10])
    if result["warnings"]:
        summary += "\nWarnings:\n" + "\n".join(f"- {item}" for item in result["warnings"][:10])
    emit(result["ok"], summary, path=path, data=result)


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
        "metadata": metadata,
        "tables": tables,
        "fields": fields,
        "fill_form": fill_form,
        "images": images,
        "from_html": from_html,
        "latex": latex_op,
        "find_image": find_image_op,
        "validate": validate_op,
    }
    handler = handlers.get(operation)
    if handler is None:
        fail(
            f"Unknown PDF operation '{operation}'. Use create, merge, split, "
            "rotate, watermark, encrypt, extract_text, metadata, tables, "
            "fields, fill_form, images, from_html, latex, find_image, or validate."
        )
    handler(spec)


if __name__ == "__main__":
    main()
