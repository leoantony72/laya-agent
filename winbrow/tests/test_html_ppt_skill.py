"""html-ppt skill: scaffold, asset verification, and PDF export."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.skill_registry import load_skill_registry, load_skill_body
from winbrow.html_to_pdf import find_browser
from winbrow.skill_scripts import execute_skill_script_tool


def _run(skill: str, script: str, spec: dict) -> dict:
    result = execute_skill_script_tool(
        "run_skill_script",
        {"skill": skill, "script": script, "spec": spec, "timeout": 120},
    )
    assert result.get("success"), result.get("output")
    return result


@pytest.fixture(scope="module")
def deck(tmp_path_factory) -> Path:
    parent = tmp_path_factory.mktemp("html-ppt")
    result = _run("html-ppt", "new_deck.py", {
        "name": "starter",
        "output_parent": str(parent),
        "template": "deck",
    })
    return Path(result["paths"][0])


def test_html_ppt_is_registered():
    registry = load_skill_registry(str(ROOT / "winbrow" / "skills"), sync_learned=False)
    meta = registry.get("playbook_html_ppt")
    assert meta is not None
    assert meta.kind == "playbook"
    assert meta.category == "document-processing"
    assert len(load_skill_body(meta)) > 2000


def test_scaffold_copies_assets_and_verifies_references(deck):
    assert deck.is_file()
    folder = deck.parent
    assert (folder / "assets" / "base.css").is_file()
    assert (folder / "assets" / "runtime.js").is_file()
    assert (folder / "assets" / "themes").is_dir()
    html = deck.read_text(encoding="utf-8")
    assert 'assets/base.css' in html
    assert 'assets/runtime.js' in html
    assert "../../../assets/" not in html
    assert "../../assets/" not in html


def test_export_pdf_16_9(deck, tmp_path):
    pytest.importorskip("pypdf")
    if find_browser() is None:
        pytest.skip("Chrome/Edge not installed")
    pdf = tmp_path / "starter.pdf"
    result = _run("html-ppt", "render_deck.py", {
        "input": str(deck),
        "output": str(pdf),
        "format": "pdf",
        "page": "16:9",
    })
    assert Path(result["path"]) == pdf
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    assert len(reader.pages) >= 3
    box = reader.pages[0].mediabox
    assert round(float(box.width) / 72, 2) == 13.33
    assert round(float(box.height) / 72, 2) == 7.5


def test_pdf_skill_from_html(tmp_path):
    pytest.importorskip("pypdf")
    if find_browser() is None:
        pytest.skip("Chrome/Edge not installed")
    source = tmp_path / "simple.html"
    source.write_text(
        "<!doctype html><html><body><h1>Hello</h1><p>PDF export.</p></body></html>",
        encoding="utf-8",
    )
    pdf = tmp_path / "simple.pdf"
    _run("pdf", "pdf_tool.py", {
        "operation": "from_html",
        "path": str(source),
        "output_path": str(pdf),
        "page": "16:9",
    })
    from pypdf import PdfReader

    assert len(PdfReader(str(pdf)).pages) == 1
