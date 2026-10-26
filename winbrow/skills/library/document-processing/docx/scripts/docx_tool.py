"""Create or edit Word .docx files with python-docx."""

from __future__ import annotations

from pathlib import Path

from winbrow.skill_script_utils import (
    emit, ensure_parent, fail, load_spec, read_text_arg, require_module, resolve_path,
)

docx = require_module("docx", "python-docx")
Document = docx.Document
WD_ALIGN_PARAGRAPH = docx.enum.text.WD_ALIGN_PARAGRAPH


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


def create(spec: dict) -> None:
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() != ".docx":
        output = output.with_suffix(".docx")
    ensure_parent(output)

    document = Document()
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
    if not content:
        fail("Nothing to append. Provide content or content_path.")
    _add_markdown(document, content)
    _add_tables(document, spec.get("tables") or [])
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
    for paragraph in document.paragraphs:
        if old in paragraph.text:
            paragraph.text = paragraph.text.replace(old, new)
            count += 1
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                if old in cell.text:
                    cell.text = cell.text.replace(old, new)
                    count += 1
    document.save(str(path))
    emit(True, f"Replaced {count} occurrence(s) in {path}", path=path, count=count)


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "create").strip().lower()
    if operation == "create":
        create(spec)
    elif operation == "append":
        append(spec)
    elif operation == "replace":
        replace(spec)
    else:
        fail(f"Unknown docx operation '{operation}'. Use create, append, or replace.")


if __name__ == "__main__":
    main()
