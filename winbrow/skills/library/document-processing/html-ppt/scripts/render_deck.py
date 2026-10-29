"""Export an html-ppt deck to PDF (default) or per-slide PNGs.

Spec:
  {"input": "C:\\decks\\talk\\index.html",
   "output": "C:\\decks\\talk\\talk.pdf",       # optional for PDF
   "format": "pdf",                              # pdf | png
   "page": "16:9",                               # 16:9 | 16:10 | 4:3 | letter | a4 | auto
   "repair": true,                               # restore missing deck assets
   "layout_check": true,                         # fail if a slide clips text
   "validate": true,                             # check page size/count/overflow
   "render_dir": "C:\\decks\\talk\\validation",  # optional PNG page renders
   "slides": "all",                              # png only: all | 1 | 8
   "out_dir": "C:\\decks\\talk\\png"}            # png only
"""

from __future__ import annotations

from pathlib import Path

from winbrow.deck_layout import check_deck_layout
from winbrow.deck_repair import repair_deck
from winbrow.html_to_pdf import (
    PAGE_SIZES,
    count_slides,
    html_to_pdf,
    html_to_png,
    resolve_page,
)
from winbrow.skill_script_utils import emit, fail, load_spec, resolve_path


def _validation_error(report: dict) -> str:
    errors = list(report.get("errors") or [])
    if not errors:
        return ""
    return "Deck validation failed:\n" + "\n".join(f"- {e}" for e in errors[:8])


def main() -> None:
    spec = load_spec()
    raw_input = spec.get("input") or spec.get("path") or ""
    source = resolve_path(raw_input)
    if source.is_dir():
        source = source / "index.html"
    if not source.is_file():
        fail(f"Deck HTML not found: {source}")

    repair_report = {"ok": True, "repaired": False, "repairs": []}
    if bool(spec.get("repair", True)):
        repair_report = repair_deck(source, write=True)
        if not repair_report.get("ok"):
            fail(_validation_error(repair_report))

    layout_report = {"ok": True, "slides": 0, "issues": []}
    if bool(spec.get("layout_check", True)):
        try:
            tolerance = int(spec.get("overflow_tolerance") or 4)
        except (TypeError, ValueError):
            tolerance = 4
        layout_report = check_deck_layout(source, tolerance_px=tolerance)
        if not layout_report.get("ok"):
            fail(
                "Deck layout check failed (PDF would clip text):\n"
                + "\n".join(f"- {item}" for item in layout_report["issues"][:10]),
                data={"repair": repair_report, "layout": layout_report},
            )

    fmt = str(spec.get("format") or "pdf").strip().lower()
    html_text = source.read_text(encoding="utf-8", errors="replace")
    page = resolve_page(str(spec.get("page") or "auto"), html_text)

    if fmt == "pdf":
        output = resolve_path(
            spec.get("output") or spec.get("output_path")
            or source.with_suffix(".pdf")
        )
        try:
            result = html_to_pdf(source, output, page=page)
        except Exception as exc:
            fail(f"PDF export failed: {type(exc).__name__}: {exc}")

        validation = None
        if bool(spec.get("validate", True)):
            from winbrow.layout.validate import validate_pdf

            expected = PAGE_SIZES.get(page)
            expected_pt = (
                (expected[0] * 72, expected[1] * 72) if expected else None
            )
            render_dir = spec.get("render_dir")
            validation = validate_pdf(
                result["path"],
                margins=(0, 0, 0, 0),
                expected_size=expected_pt,
                render_dir=str(resolve_path(render_dir)) if render_dir else None,
            )
            problems = list(validation.get("errors") or [])
            slide_count = count_slides(source)
            if slide_count and validation.get("pages") != slide_count:
                problems.append(
                    f"PDF has {validation.get('pages')} page(s) but the deck "
                    f"has {slide_count} slide(s); one slide must equal one page."
                )
            if problems and validation.get("pages", 0) > 0:
                fail(
                    "PDF validation failed:\n"
                    + "\n".join(f"- {item}" for item in problems[:10])
                )

        emit(
            True,
            f"Exported {source.name} to PDF: {result['path']} "
            f"({result['bytes']} bytes, page={result['page']}, "
            f"{result['width_in']}x{result['height_in']}in)",
            path=result["path"],
            data={
                **result,
                "repair": repair_report,
                "layout": layout_report,
                "validation": validation,
            },
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
            data={"repair": repair_report, "layout": layout_report},
        )

    fail(f"Unknown format '{fmt}'. Use 'pdf' or 'png'.")


if __name__ == "__main__":
    main()
