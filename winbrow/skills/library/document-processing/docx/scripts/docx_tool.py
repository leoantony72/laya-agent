"""Create, edit, comment on, and inspect Word .docx files with python-docx."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from winbrow.skill_script_utils import (
    clip, emit, ensure_parent, fail, load_spec, read_text_arg, require_module,
    resolve_path,
)

docx = require_module("docx", "python-docx")
Document = docx.Document

from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Mm


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def _add_markdown(document, content: str) -> None:
    for raw in content.splitlines():
        line = raw.rstrip()
        if not line.strip():
            document.add_paragraph("")
            continue
        if line.startswith("### "):
            document.add_heading(line[4:].strip(), level=3)
        elif line.startswith("## "):
            document.add_heading(line[3:].strip(), level=2)
        elif line.startswith("# "):
            document.add_heading(line[2:].strip(), level=1)
        elif line.lstrip().startswith(("- ", "* ")):
            document.add_paragraph(line.lstrip()[2:].strip(), style="List Bullet")
        elif line.lstrip().startswith("1. "):
            document.add_paragraph(line.lstrip()[3:].strip(), style="List Number")
        else:
            document.add_paragraph(line)


def _add_tables(document, tables: list) -> None:
    for table_spec in tables or []:
        if not isinstance(table_spec, dict):
            continue
        headers = [str(value) for value in table_spec.get("headers") or []]
        rows = table_spec.get("rows") or []
        if not headers and not rows:
            continue
        columns = max([len(headers)] + [len(row) for row in rows if isinstance(row, list)])
        if columns <= 0:
            continue
        table = document.add_table(rows=1, cols=columns)
        table.style = "Table Grid"
        for index, header in enumerate(headers[:columns]):
            table.rows[0].cells[index].text = header
        for row in rows:
            if not isinstance(row, list):
                continue
            cells = table.add_row().cells
            for index, value in enumerate(row[:columns]):
                cells[index].text = str(value)


def _add_field(paragraph, code: str, placeholder: str = "") -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = code
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = placeholder or "Update field (F9) to render"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for element in (begin, instr, separate, text, end):
        run._r.append(element)


def _apply_page_setup(document, spec: dict) -> None:
    size = str(spec.get("page_size") or "").strip().lower()
    orientation = str(spec.get("orientation") or "").strip().lower()
    for section in document.sections:
        if size == "letter":
            section.page_width, section.page_height = Inches(8.5), Inches(11)
        elif size == "a4":
            section.page_width, section.page_height = Mm(210), Mm(297)
        if orientation == "landscape":
            section.orientation = WD_ORIENT.LANDSCAPE
            if section.page_width < section.page_height:
                section.page_width, section.page_height = section.page_height, section.page_width
        elif orientation == "portrait":
            section.orientation = WD_ORIENT.PORTRAIT
            if section.page_width > section.page_height:
                section.page_width, section.page_height = section.page_height, section.page_width


def _insert_toc(document) -> None:
    heading = document.add_paragraph("Contents", style="Heading 1")
    body = document.element.body
    body.insert(0, heading._p)
    field_paragraph = document.add_paragraph()
    _add_field(field_paragraph, 'TOC \\o "1-3" \\h \\z \\u', "Right-click → Update Field")
    body.insert(1, field_paragraph._p)


def _add_page_numbers(document) -> None:
    for section in document.sections:
        footer = section.footer
        paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _add_field(paragraph, "PAGE", "1")


def _set_header_footer(document, spec: dict) -> None:
    header = str(spec.get("header") or "").strip()
    footer = str(spec.get("footer") or "").strip()
    for section in document.sections:
        if header:
            paragraph = (
                section.header.paragraphs[0]
                if section.header.paragraphs else section.header.add_paragraph()
            )
            paragraph.text = header
        if footer:
            paragraph = (
                section.footer.paragraphs[0]
                if section.footer.paragraphs else section.footer.add_paragraph()
            )
            paragraph.text = footer


def _iter_paragraphs(document) -> Iterator:
    yield from document.paragraphs
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs


def _run_format(run) -> str:
    rpr = run._r.rPr
    return rpr.xml if rpr is not None else ""


def _plain_text_run(run) -> bool:
    allowed = {qn("w:rPr"), qn("w:t")}
    return len(run._r) > 0 and all(child.tag in allowed for child in run._r)


def _merge_paragraph_runs(paragraph) -> int:
    runs = list(paragraph.runs)
    changed = 0
    index = 1
    while index < len(runs):
        previous, current = runs[index - 1], runs[index]
        if (
            _plain_text_run(previous) and _plain_text_run(current)
            and _run_format(previous) == _run_format(current)
        ):
            previous.text = (previous.text or "") + (current.text or "")
            current._r.getparent().remove(current._r)
            runs.pop(index)
            changed += 1
        else:
            index += 1
    return changed


def _find_paragraph(document, anchor: str):
    target = (anchor or "").strip().lower()
    if not target:
        return next((p for p in _iter_paragraphs(document) if p.text.strip()), None)
    for paragraph in _iter_paragraphs(document):
        if target in paragraph.text.lower():
            return paragraph
    return None


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def create(spec: dict) -> None:
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() != ".docx":
        output = output.with_suffix(".docx")
    ensure_parent(output)

    document = Document()
    _apply_page_setup(document, spec)
    title = str(spec.get("title") or "").strip()
    if title:
        heading = document.add_heading(title, level=0)
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle = str(spec.get("subtitle") or "").strip()
    if subtitle:
        paragraph = document.add_paragraph(subtitle)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

    content = read_text_arg(spec)
    if content:
        _add_markdown(document, content)
    _add_tables(document, spec.get("tables") or [])

    if spec.get("toc"):
        _insert_toc(document)
    if spec.get("page_numbers"):
        _add_page_numbers(document)
    _set_header_footer(document, spec)

    document.save(str(output))
    emit(True, f"Created Word document: {output}", path=output)


def append(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("output_path") or "")
    if not path.is_file():
        fail(f"Document not found: {path}")
    document = Document(str(path))
    if spec.get("page_break"):
        document.add_page_break()
    content = read_text_arg(spec)
    if not content and not spec.get("tables"):
        fail("Nothing to append. Provide content, content_path, or tables.")
    if content:
        _add_markdown(document, content)
    _add_tables(document, spec.get("tables") or [])
    _set_header_footer(document, spec)
    document.save(str(path))
    emit(True, f"Updated Word document: {path}", path=path)


def replace(spec: dict) -> None:
    path = resolve_path(spec.get("path") or "")
    if not path.is_file():
        fail(f"Document not found: {path}")
    old = str(spec.get("old") or "")
    new = str(spec.get("new") or "")
    if not old:
        fail("The 'old' text is required for replace.")
    document = Document(str(path))
    count = 0
    for paragraph in _iter_paragraphs(document):
        if old in paragraph.text:
            paragraph.text = paragraph.text.replace(old, new)
            count += 1
    document.save(str(path))
    emit(True, f"Replaced {count} occurrence(s) in {path}", path=path, count=count)


def comment(spec: dict) -> None:
    path = resolve_path(spec.get("path") or "")
    if not path.is_file():
        fail(f"Document not found: {path}")
    text = str(spec.get("comment") or spec.get("text") or "").strip()
    if not text:
        fail("A 'comment' string is required.")
    document = Document(str(path))
    anchor = str(spec.get("anchor") or spec.get("anchor_text") or "").strip()
    paragraph = _find_paragraph(document, anchor)
    if paragraph is None:
        fail(f"Could not find anchor text '{anchor}' in {path}.")
    if not paragraph.runs:
        fail("The anchor paragraph has no editable runs; comment on different text.")
    document.add_comment(
        paragraph.runs,
        text=text,
        author=str(spec.get("author") or "WinBrow"),
        initials=str(spec.get("initials") or "WB"),
    )
    document.save(str(path))
    emit(True, f"Added a comment anchored to '{anchor or 'first paragraph'}' in {path}",
         path=path)


def toc(spec: dict) -> None:
    path = resolve_path(spec.get("path") or "")
    if not path.is_file():
        fail(f"Document not found: {path}")
    document = Document(str(path))
    _insert_toc(document)
    document.save(str(path))
    emit(True, "Inserted a Table of Contents field. Update it in Word with F9.", path=path)


def page_numbers(spec: dict) -> None:
    path = resolve_path(spec.get("path") or "")
    if not path.is_file():
        fail(f"Document not found: {path}")
    document = Document(str(path))
    _add_page_numbers(document)
    document.save(str(path))
    emit(True, "Added centered page numbers to every section footer.", path=path)


def header_footer(spec: dict) -> None:
    path = resolve_path(spec.get("path") or "")
    if not path.is_file():
        fail(f"Document not found: {path}")
    if not (spec.get("header") or spec.get("footer")):
        fail("Provide header and/or footer text.")
    document = Document(str(path))
    _set_header_footer(document, spec)
    document.save(str(path))
    emit(True, "Updated header/footer text.", path=path)


def image(spec: dict) -> None:
    path = resolve_path(spec.get("path") or "")
    if not path.is_file():
        fail(f"Document not found: {path}")
    image_path = resolve_path(spec.get("image_path") or spec.get("image") or "")
    if not image_path.is_file():
        fail(f"Image not found: {image_path}")
    document = Document(str(path))
    width = spec.get("width_inches")
    kwargs = {"width": Inches(float(width))} if width not in (None, "") else {}
    try:
        document.add_picture(str(image_path), **kwargs)
    except Exception as exc:
        fail(f"Could not insert image: {type(exc).__name__}: {exc}")
    caption = str(spec.get("caption") or "").strip()
    if caption:
        paragraph = document.add_paragraph(caption)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if "Caption" in [style.name for style in document.styles]:
            paragraph.style = "Caption"
    document.save(str(path))
    emit(True, f"Inserted image into {path}", path=path)


def merge_runs(spec: dict) -> None:
    path = resolve_path(spec.get("path") or "")
    if not path.is_file():
        fail(f"Document not found: {path}")
    document = Document(str(path))
    changed = sum(_merge_paragraph_runs(p) for p in _iter_paragraphs(document))
    document.save(str(path))
    emit(True, f"Merged {changed} fragmented run(s) in {path}", path=path, count=changed)


def inspect(spec: dict) -> None:
    path = resolve_path(spec.get("path") or "")
    if not path.is_file():
        fail(f"Document not found: {path}")
    document = Document(str(path))
    headings = [
        {"style": paragraph.style.name if paragraph.style else "", "text": paragraph.text}
        for paragraph in document.paragraphs
        if paragraph.text.strip() and paragraph.style
        and paragraph.style.name.lower().startswith("heading")
    ]
    comments = 0
    try:
        comments = len(document.comments)
    except Exception:
        pass
    data = {
        "headings": headings[:60],
        "paragraphs": len(document.paragraphs),
        "tables": len(document.tables),
        "sections": len(document.sections),
        "comments": comments,
        "page_width_in": round(document.sections[0].page_width.inches, 2),
        "page_height_in": round(document.sections[0].page_height.inches, 2),
    }
    emit(
        True,
        clip(
            f"{path}\nheadings={len(headings)} paragraphs={data['paragraphs']} "
            f"tables={data['tables']} comments={comments}"
        ),
        data=data,
        path=path,
    )


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "create").strip().lower()
    handlers = {
        "create": create,
        "append": append,
        "replace": replace,
        "comment": comment,
        "toc": toc,
        "page_numbers": page_numbers,
        "header_footer": header_footer,
        "image": image,
        "merge_runs": merge_runs,
        "inspect": inspect,
    }
    handler = handlers.get(operation)
    if handler is None:
        fail(
            f"Unknown docx operation '{operation}'. Use create, append, replace, "
            "comment, toc, page_numbers, header_footer, image, merge_runs, or inspect."
        )
    handler(spec)


if __name__ == "__main__":
    main()
