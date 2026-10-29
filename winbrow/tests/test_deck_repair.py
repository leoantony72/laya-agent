"""Deck repair: restore the html-ppt design system before PDF export."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.deck_repair import repair_deck
from winbrow.html_to_pdf import find_browser
from winbrow.skill_scripts import execute_skill_script_tool

BROKEN = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Broken deck</title>
<link rel="stylesheet" href="assets/index.css">
</head>
<body>
<section class="slide title">
<h1>Remote Execution Vulnerability</h1>
<p class="subtitle">What RCE is and how to defend it.</p>
</section>
<section class="slide">
<h2>Agenda</h2>
<ul><li>Definition</li><li>Vectors</li></ul>
</section>
</body>
</html>
"""

THEME = """.tpl-tech-sharing{--bg:#0d1117;--text-1:#e6edf3;--text-2:#8b949e}
.tpl-tech-sharing .slide{background:#0d1117;color:var(--text-1)}
"""


def _broken_deck(tmp_path: Path) -> Path:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "index.html").write_text(BROKEN, encoding="utf-8")
    (folder / "style.css").write_text(THEME, encoding="utf-8")
    return folder / "index.html"


def test_repair_restores_assets_wrapper_and_theme(tmp_path):
    index = _broken_deck(tmp_path)
    report = repair_deck(index)

    assert report["ok"] is True
    assert report["repaired"] is True
    assert report["slides"] == 2
    assert "missing stylesheet: assets/index.css" in report["issues"]

    folder = index.parent
    for rel in (
        "assets/fonts.css",
        "assets/base.css",
        "assets/animations/animations.css",
        "assets/runtime.js",
    ):
        assert (folder / rel).is_file(), rel

    html = index.read_text(encoding="utf-8")
    assert "assets/index.css" not in html
    assert "assets/fonts.css" in html
    assert "assets/base.css" in html
    assert "assets/animations/animations.css" in html
    assert "style.css" in html
    assert "assets/runtime.js" in html
    assert '<div class="deck">' in html
    assert 'class="tpl-tech-sharing"' in html
    assert "data-winbrow-deck-fallback" in html
    assert "class=\"h1\"" in html
    assert "class=\"h2\"" in html


def test_repair_is_idempotent(tmp_path):
    index = _broken_deck(tmp_path)
    repair_deck(index)
    second = repair_deck(index)

    assert second["ok"] is True
    assert second["repaired"] is False
    assert second["repairs"] == []


def test_missing_deck_returns_error(tmp_path):
    report = repair_deck(tmp_path / "nope.html")
    assert report["ok"] is False
    assert report["errors"]


def test_low_contrast_theme_is_warned(tmp_path):
    folder = tmp_path / "low_contrast"
    folder.mkdir()
    (folder / "index.html").write_text(BROKEN, encoding="utf-8")
    (folder / "style.css").write_text(
        ".tpl-tech-sharing{--bg:#ffffff;--text-1:#e6edf3;"
        "--text-2:#f5f5f5;--accent:#ffffff;--accent-ink:#f0f0f0}",
        encoding="utf-8",
    )
    report = repair_deck(folder / "index.html")
    assert report["ok"] is True
    assert any("contrast" in warning.lower() for warning in report["warnings"])


def test_export_repairs_broken_deck_to_one_page_per_slide(tmp_path):
    pytest.importorskip("pypdf")
    if find_browser() is None:
        pytest.skip("Chrome/Edge not installed")
    index = _broken_deck(tmp_path)
    pdf = tmp_path / "broken.pdf"

    result = execute_skill_script_tool(
        "run_skill_script",
        {
            "skill": "html-ppt",
            "script": "render_deck.py",
            "spec": {
                "input": str(index),
                "output": str(pdf),
                "format": "pdf",
                "page": "16:9",
            },
            "timeout": 120,
        },
    )
    assert result.get("success"), result.get("output")

    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    assert len(reader.pages) == 2
    box = reader.pages[0].mediabox
    assert round(float(box.width) / 72, 2) == 13.33
    assert round(float(box.height) / 72, 2) == 7.5

    html = index.read_text(encoding="utf-8")
    assert "assets/base.css" in html
    assert "assets/runtime.js" in html
    assert '<div class="deck">' in html
