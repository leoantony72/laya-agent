"""Validate (and optionally repair) an html-ppt deck before PDF export.

Spec:
  {"input": "C:\\decks\\talk\\index.html",
   "repair": true}

Repair restores missing base.css/theme/runtime links, wraps stray slides in
.deck, adds the scoped theme class, and injects semantic fallback CSS. It never
changes slide text. Export runs the same repair automatically.
"""

from __future__ import annotations

from pathlib import Path

from winbrow.deck_repair import repair_deck
from winbrow.skill_script_utils import emit, fail, load_spec, resolve_path


def main() -> None:
    spec = load_spec()
    source = resolve_path(spec.get("input") or spec.get("path") or "")
    if source.is_dir():
        source = source / "index.html"
    if not source.is_file():
        fail(f"Deck HTML not found: {source}")

    report = repair_deck(source, write=bool(spec.get("repair", True)))
    if not report.get("ok"):
        fail(
            "Deck validation failed:\n"
            + "\n".join(f"- {item}" for item in report.get("errors", [])[:10]),
            data=report,
            path=str(source),
        )
    repaired = "repaired" if report.get("repaired") else "already valid"
    emit(
        True,
        f"Deck {repaired}: {source} "
        f"({report.get('slides', 0)} slide(s), "
        f"{report.get('links_checked', 0)} local link(s) checked)",
        path=str(source),
        data=report,
    )


if __name__ == "__main__":
    main()
