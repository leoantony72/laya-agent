"""Deck layout check: catch text clipped by a fixed 16:9 slide."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.deck_layout import check_deck_layout
from winbrow.deck_repair import repair_deck
from winbrow.html_to_pdf import find_browser

pytestmark = pytest.mark.skipif(
    find_browser() is None, reason="Chrome/Edge not installed"
)

THEME = ".tpl-test{--bg:#0d1117;--text-1:#e6edf3;--text-2:#8b949e}"


def _deck(tmp_path: Path, body: str) -> Path:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "style.css").write_text(THEME, encoding="utf-8")
    index = folder / "index.html"
    index.write_text(
        "<!doctype html><html><head><title>t</title></head><body>"
        f'<section class="slide">{body}</section></body></html>',
        encoding="utf-8",
    )
    repair_deck(index)
    return index


def test_short_slide_passes(tmp_path):
    report = check_deck_layout(_deck(tmp_path, "<h1>Short</h1><p>One line.</p>"))
    assert report["ok"] is True
    assert report["slides"] == 1


def test_overfull_slide_is_reported(tmp_path):
    body = "<h1>Too much</h1>" + "<p>overflow line</p>" * 80
    report = check_deck_layout(_deck(tmp_path, body))
    assert report["ok"] is False
    assert report["slides"] == 1
    assert any("Slide 1" in issue and "overflows" in issue
               for issue in report["issues"])


def test_no_slides_is_not_an_error(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    index = folder / "index.html"
    index.write_text("<html><body><h1>Not a deck</h1></body></html>", encoding="utf-8")
    report = check_deck_layout(index)
    assert report["ok"] is True
    assert report["slides"] == 0
