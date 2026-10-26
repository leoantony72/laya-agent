"""Build a standards-compliant EPUB 3 file from Markdown or chapter input."""

from __future__ import annotations

import html
import re
import uuid
import zipfile
from pathlib import Path

from winbrow.skill_script_utils import (
    emit, ensure_parent, fail, load_spec, read_text_arg, resolve_path,
)


def _split_chapters(content: str) -> list[dict]:
    chapters: list[dict] = []
    current_title = "Chapter 1"
    current: list[str] = []
    for line in content.splitlines():
        if line.startswith("# "):
            if current:
                chapters.append({"title": current_title, "content": "\n".join(current)})
            current_title = line[2:].strip() or f"Chapter {len(chapters) + 1}"
            current = []
        elif line.startswith("## "):
            current.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
        else:
            current.append(line)
    if current or not chapters:
        chapters.append({"title": current_title, "content": "\n".join(current)})
    return chapters


def _paragraphs_to_html(content: str) -> str:
    parts: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("<h2>"):
            parts.append(line)
        elif line.startswith(("- ", "* ")):
            parts.append(f"<li>{html.escape(line[2:].strip())}</li>")
        elif line.startswith("1. "):
            parts.append(f"<li>{html.escape(line[3:].strip())}</li>")
        else:
            parts.append(f"<p>{html.escape(line)}</p>")
    rendered = "\n".join(parts)
    rendered = re.sub(r"(<li>.*?</li>\n?)+", lambda match: f"<ul>{match.group(0)}</ul>", rendered, flags=re.S)
    return rendered


def build(spec: dict) -> None:
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() != ".epub":
        output = output.with_suffix(".epub")
    ensure_parent(output)

    title = str(spec.get("title") or "Untitled").strip()
    author = str(spec.get("author") or "WinBrow").strip()
    language = str(spec.get("language") or "en").strip()
    chapters = spec.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        content = read_text_arg(spec)
        if not content:
            fail("Provide content, content_path, or a chapters list.")
        chapters = _split_chapters(content)

    book_id = f"urn:uuid:{uuid.uuid4()}"
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine: list[str] = []
    chapter_files: list[tuple[str, str, str]] = []
    for index, chapter in enumerate(chapters, start=1):
        if not isinstance(chapter, dict):
            continue
        chapter_title = str(chapter.get("title") or f"Chapter {index}")
        chapter_html = _paragraphs_to_html(str(chapter.get("content") or ""))
        filename = f"chapter{index}.xhtml"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" lang="' + html.escape(language) + '">\n'
            f"<head><title>{html.escape(chapter_title)}</title>"
            '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
            f"<body><h1>{html.escape(chapter_title)}</h1>\n{chapter_html}</body></html>"
        )
        chapter_files.append((filename, chapter_title, body))
        manifest.append(
            f'<item id="chapter{index}" href="{filename}" media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="chapter{index}"/>')

    nav_items = "\n".join(
        f'<li><a href="{filename}">{html.escape(chapter_title)}</a></li>'
        for filename, chapter_title, _ in chapter_files
    )
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" lang="' + html.escape(language) + '">\n'
        f"<head><title>{html.escape(title)}</title></head>\n"
        '<body><nav epub:type="toc" id="toc"><h1>Contents</h1>'
        f"<ol>{nav_items}</ol></nav></body></html>"
    )
    content_opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'<dc:identifier id="bookid">{book_id}</dc:identifier>\n'
        f"<dc:title>{html.escape(title)}</dc:title>\n"
        f"<dc:creator>{html.escape(author)}</dc:creator>\n"
        f"<dc:language>{html.escape(language)}</dc:language>\n"
        '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>\n'
        "</metadata>\n<manifest>\n" + "\n".join(manifest) + "\n</manifest>\n"
        '<spine toc="nav">\n' + "\n".join(spine) + "\n</spine>\n</package>"
    )
    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    css = (
        "body { font-family: Georgia, serif; line-height: 1.55; margin: 6%; }\n"
        "h1, h2 { font-family: Helvetica, Arial, sans-serif; }\n"
        "ul { margin-left: 1.2em; }\n"
    )

    try:
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(
                "mimetype", "application/epub+zip",
                compress_type=zipfile.ZIP_STORED,
            )
            archive.writestr("META-INF/container.xml", container, zipfile.ZIP_DEFLATED)
            archive.writestr("OEBPS/content.opf", content_opf, zipfile.ZIP_DEFLATED)
            archive.writestr("OEBPS/nav.xhtml", nav, zipfile.ZIP_DEFLATED)
            archive.writestr("OEBPS/style.css", css, zipfile.ZIP_DEFLATED)
            for filename, _, body in chapter_files:
                archive.writestr(f"OEBPS/{filename}", body, zipfile.ZIP_DEFLATED)
    except OSError as exc:
        fail(f"Could not write EPUB: {exc}")

    emit(True, f"Created EPUB: {output}", path=output, count=len(chapter_files))


def main() -> None:
    build(load_spec())


if __name__ == "__main__":
    main()
