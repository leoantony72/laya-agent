"""Anthropic-derived skills: registry wiring and bundled helper smoke tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.skill_registry import load_skill_registry, load_skill_body
from winbrow.skill_scripts import execute_skill_script_tool


def _run(skill: str, script: str, spec: dict) -> dict:
    result = execute_skill_script_tool(
        "run_skill_script",
        {"skill": skill, "script": script, "spec": spec, "timeout": 90},
    )
    assert result.get("success"), result.get("output")
    return result


@pytest.fixture(scope="module")
def registry():
    return load_skill_registry(str(ROOT / "winbrow" / "skills"), sync_learned=False)


def test_anthropic_skills_are_registered(registry):
    expected = {
        "playbook_docx": "document-processing",
        "playbook_pdf": "document-processing",
        "playbook_pptx": "document-processing",
        "playbook_algorithmic_art": "creative-media",
        "playbook_skill_creator": "productivity-organization",
    }
    for key, category in expected.items():
        meta = registry.get(key)
        assert meta is not None, key
        assert meta.kind == "playbook"
        assert meta.category == category
        assert len(load_skill_body(meta)) > 1000


def test_every_skill_validates():
    result = _run("skill-creator", "validate_skill.py", {"all": True})
    assert result["data"]["errors"] == []


def test_docx_create_inspect_and_comment(tmp_path):
    pytest.importorskip("docx")
    doc = tmp_path / "report.docx"
    _run("docx", "docx_tool.py", {
        "operation": "create",
        "output_path": str(doc),
        "title": "Quarterly Report",
        "toc": True,
        "page_numbers": True,
        "header": "Acme",
        "content": "# Summary\n\nRevenue grew 18%.\n\n## Risks\n\n- Supply chain",
        "tables": [{"headers": ["Metric", "Value"], "rows": [["Revenue", "$4.2M"]]}],
    })
    inspected = _run("docx", "docx_tool.py", {
        "operation": "inspect", "path": str(doc),
    })
    assert inspected["data"]["tables"] == 1
    assert any("Summary" in item["text"] for item in inspected["data"]["headings"])

    _run("docx", "docx_tool.py", {
        "operation": "comment",
        "path": str(doc),
        "anchor": "Revenue grew",
        "comment": "Add the source.",
    })
    inspected = _run("docx", "docx_tool.py", {
        "operation": "inspect", "path": str(doc),
    })
    assert inspected["data"]["comments"] == 1


def test_pdf_create_metadata_and_tables(tmp_path):
    pytest.importorskip("fpdf")
    pytest.importorskip("pdfplumber")
    from fpdf import FPDF

    pdf = tmp_path / "table.pdf"
    writer = FPDF()
    writer.add_page()
    writer.set_font("helvetica", size=12)
    writer.cell(60, 10, "Metric", border=1)
    writer.cell(60, 10, "Value", border=1)
    writer.ln()
    writer.cell(60, 10, "Revenue", border=1)
    writer.cell(60, 10, "$4.2M", border=1)
    writer.output(str(pdf))

    meta = _run("pdf", "pdf_tool.py", {"operation": "metadata", "path": str(pdf)})
    assert meta["data"]["pages"] == 1

    tables = _run("pdf", "pdf_tool.py", {"operation": "tables", "path": str(pdf)})
    assert tables["data"]["count"] == 1
    assert tables["data"]["tables"][0]["rows"][0] == ["Metric", "Value"]


def test_pptx_create_table_notes_and_read(tmp_path):
    pytest.importorskip("pptx")
    deck = tmp_path / "deck.pptx"
    _run("pptx", "pptx_tool.py", {
        "operation": "create",
        "output_path": str(deck),
        "title": "Q3 Review",
        "slides": [{"title": "Revenue", "bullets": ["$4.2M"]}],
    })
    _run("pptx", "pptx_tool.py", {
        "operation": "add_table",
        "path": str(deck),
        "slide": 1,
        "rows": [["Metric", "Value"], ["Revenue", "$4.2M"]],
    })
    _run("pptx", "pptx_tool.py", {
        "operation": "set_notes",
        "path": str(deck),
        "slide": 1,
        "notes": "Pause for questions.",
    })
    result = _run("pptx", "pptx_tool.py", {"operation": "read", "path": str(deck)})
    assert "Metric | Value" in result["output"]
    assert "Pause for questions." in result["output"]


def test_algorithmic_art_scaffold(tmp_path):
    out = tmp_path / "art"
    result = _run("algorithmic-art", "scaffold_art.py", {
        "output_dir": str(out),
        "title": "Test Art",
        "philosophy": "A test philosophy.",
    })
    assert (out / "index.html").is_file()
    assert (out / "sketch.js").is_file()
    assert (out / "philosophy.md").read_text(encoding="utf-8").strip() == "A test philosophy."
    assert "Test Art" in (out / "index.html").read_text(encoding="utf-8")
    assert len(result["paths"]) == 3


def test_skill_creator_scaffold_and_validate(tmp_path):
    skills_root = tmp_path / "skills"
    _run("skill-creator", "scaffold_skill.py", {
        "root": str(skills_root),
        "name": "weekly-report",
        "kind": "playbook",
        "category": "productivity-organization",
        "description": "Use when the user asks for a weekly status report.",
    })
    folder = (
        skills_root / "library" / "productivity-organization" / "weekly-report"
    )
    assert (folder / "SKILL.md").is_file()
    result = _run("skill-creator", "validate_skill.py", {"path": str(folder)})
    assert result["data"]["errors"] == []
