"""PDF page policy: slide decks are 16:9, paper documents are A4/Letter."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.html_to_pdf import (
    PAGE_SIZES,
    _page_css,
    detect_document_kind,
    resolve_page,
)

DECK_HTML = "<html><body><div class='deck'><section class='slide'>x</section></div></body></html>"
RESUME_HTML = "<html><head><title>Resume</title></head><body><h1>Jane Doe</h1></body></html>"
LETTER_HTML = "<html><body><h1>Cover Letter</h1><p>Dear hiring manager</p></body></html>"


def test_slide_size_matches_powerpoint_widescreen():
    assert PAGE_SIZES["16:9"] == (13.333, 7.5)
    ratio = PAGE_SIZES["16:9"][0] / PAGE_SIZES["16:9"][1]
    assert abs(ratio - 16 / 9) < 0.001


def test_deck_auto_uses_16_9():
    assert detect_document_kind(DECK_HTML) == "presentation"
    assert resolve_page("auto", DECK_HTML) == "16:9"
    assert resolve_page(None, DECK_HTML) == "16:9"


def test_resume_and_letter_auto_use_paper():
    assert detect_document_kind(RESUME_HTML) == "resume"
    assert resolve_page("auto", RESUME_HTML) == "a4"
    assert resolve_page("auto", LETTER_HTML) == "a4"
    assert resolve_page("letter", RESUME_HTML) == "letter"


def test_explicit_page_wins_for_documents_but_not_deck_default():
    assert resolve_page("letter", RESUME_HTML) == "letter"
    assert resolve_page("a4", DECK_HTML) == "a4"  # explicit paper version


def test_deck_print_css_paginates_and_preserves_colors():
    css = _page_css("16:9", deck=True)
    assert "13.333in 7.5in" in css
    assert "print-color-adjust: exact" in css
    assert ".slide" in css
    assert "page-break-after: always" in css
    assert ".notes" in css


def test_document_print_css_does_not_force_slide_height():
    css = _page_css("a4", deck=False)
    assert "8.27in 11.69in" in css
    assert "page-break-after" not in css
    assert "height: 11.69in !important" not in css


def _load_pdf_tool():
    path = ROOT / "winbrow" / "skills" / "library" / "document-processing" / "pdf" / "scripts" / "pdf_tool.py"
    spec = importlib.util.spec_from_file_location("wb_pdf_tool_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_resume_kind_forces_paper_theme_and_no_page_numbers():
    pdf_tool = _load_pdf_tool()
    theme = pdf_tool._theme_from_spec({
        "document_kind": "resume",
        "page_size": "16:9",  # must be corrected to paper
    })
    assert theme.name == "resume"
    assert theme.page_size == "a4"
    assert theme.page_numbers is False
    assert theme.orientation == "portrait"


def test_cover_letter_can_use_letter():
    pdf_tool = _load_pdf_tool()
    theme = pdf_tool._theme_from_spec({
        "document_kind": "cover_letter",
        "page_size": "letter",
    })
    assert theme.name == "resume"
    assert theme.page_size == "letter"
    width, height = theme.page_format()
    assert (round(width), round(height)) == (612, 792)
