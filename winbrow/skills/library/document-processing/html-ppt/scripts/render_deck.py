"""Export an html-ppt deck to PDF (default) or per-slide PNGs.

Spec:
  {"input": "C:\\decks\\talk\\index.html",
   "output": "C:\\decks\\talk\\talk.pdf",       # optional for PDF
   "format": "pdf",                              # pdf | png
   "page": "16:9",                               # 16:9 | 16:10 | 4:3 | letter | a4 | auto
   "slides": "all",                              # png only: all | 1 | 8
   "out_dir": "C:\\decks\\talk\\png"}            # png only
"""

from __future__ import annotations

from pathlib import Path

from winbrow.html_to_pdf import count_slides, html_to_pdf, html_to_png
from winbrow.skill_script_utils import emit, fail, load_spec, resolve_path


def main() -> None:
    spec = load_spec()
    raw_input = spec.get("input") or spec.get("path") or ""
    source = resolve_path(raw_input)
    if source.is_dir():
        source = source / "index.html"
    if not source.is_file():
        fail(f"Deck HTML not found: {source}")

    fmt = str(spec.get("format") or "pdf").strip().lower()
    page = str(spec.get("page") or "16:9").strip().lower()

    if fmt == "pdf":
        output = resolve_path(
            spec.get("output") or spec.get("output_path")
            or source.with_suffix(".pdf")
        )
        try:
            result = html_to_pdf(source, output, page=page)
        except Exception as exc:
            fail(f"PDF export failed: {type(exc).__name__}: {exc}")
        emit(
            True,
            f"Exported {source.name} to PDF: {result['path']} "
            f"({result['bytes']} bytes, page={result['page']})",
            path=result["path"],
            data=result,
        )

    if fmt == "png":
        raw_slides = str(spec.get("slides") or "all").strip().lower()
        if raw_slides in {"all", ""}:
            total = count_slides(source)
        else:
            try:
                total = int(raw_slides)
            except ValueError:
                fail("'slides' must be a positive integer or 'all'.")
        if total < 1:
            fail("No <section class=\"slide\"> elements found; nothing to render.")
        out_dir = resolve_path(
            spec.get("out_dir") or spec.get("output")
            or (source.parent / f"{source.stem}-png")
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        try:
            for index in range(1, total + 1):
                target = out_dir / f"{source.stem}_{index:02d}.png"
                html_to_png(source, target, slide=index)
                written.append(str(target))
        except Exception as exc:
            fail(f"PNG export failed after {len(written)} slide(s): "
                 f"{type(exc).__name__}: {exc}")
        emit(
            True,
            f"Rendered {len(written)} slide(s) from {source.name} to {out_dir}",
            path=out_dir,
            paths=written,
        )

    fail(f"Unknown format '{fmt}'. Use 'pdf' or 'png'.")


if __name__ == "__main__":
    main()
