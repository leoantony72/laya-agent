"""Conversion lint: all 46 playbooks exist, parse, and use WinBrow conventions."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.skill_registry import build_skills_metadata, load_skill_registry

LIBRARY = ROOT / "winbrow" / "skills" / "library"

EXPECTED = {
    "docx", "pdf", "pptx", "xlsx", "markdown-to-epub", "legal-workflows",
    "csv-summarizer", "deep-research", "postgres", "recursive-research",
    "root-cause-tracing", "article-extractor", "brainstorming",
    "content-research-writer", "family-history", "meeting-insights",
    "notebooklm", "twitter-optimizer", "anydesign", "canvas-design", "imagen",
    "image-enhancer", "slack-gif", "theme-factory", "video-downloader",
    "youtube-transcript", "swiftui-design", "pixelbin-media", "file-organizer",
    "invoice-organizer", "kaizen", "n8n-workflows", "raffle-winner",
    "solo-founder", "tailored-resume", "ship-learn-next", "tapestry",
    "git-pushing", "google-workspace", "mercury", "outline",
    "review-implementing", "test-fixing", "algorithmic-art", "skill-creator",
    "html-ppt",
}

BANNED = (
    "Claude", "/mnt/", "~/.claude", "allowed-tools", "WebFetch", "subagent",
    "Task tool", "artifacts/",
)

SCRIPTS = {
    "docx": "docx_tool.py",
    "pdf": "pdf_tool.py",
    "pptx": "pptx_tool.py",
    "xlsx": "xlsx_tool.py",
    "markdown-to-epub": "build_epub.py",
    "csv-summarizer": "profile_csv.py",
    "deep-research": "research_gemini.py",
    "postgres": "postgres_query.py",
    "meeting-insights": "transcript_metrics.py",
    "canvas-design": "render_canvas.py",
    "imagen": "imagen.py",
    "image-enhancer": "enhance_image.py",
    "slack-gif": "gif_builder.py",
    "video-downloader": "download_video.py",
    "youtube-transcript": "fetch_transcript.py",
    "file-organizer": "find_duplicates.py",
    "invoice-organizer": "organize_invoices.py",
    "raffle-winner": "pick_winners.py",
    "google-workspace": "google_api.py",
    "outline": "outline_api.py",
    "pixelbin-media": "pixelbin.py",
    "algorithmic-art": "scaffold_art.py",
    "skill-creator": "validate_skill.py",
    "html-ppt": "new_deck.py",
}


def _body(text: str) -> str:
    parts = text.split("---", 2)
    return parts[-1] if len(parts) == 3 else text


def test_all_expected_skills_exist_and_parse():
    registry = load_skill_registry(str(ROOT / "winbrow" / "skills"), sync_learned=False)
    playbooks = {meta.name for meta in registry.values() if meta.kind == "playbook"}
    assert playbooks == EXPECTED
    assert len(playbooks) == 46


def test_each_skill_has_required_sections_and_clean_body():
    for name in sorted(EXPECTED):
        path = next(LIBRARY.rglob(f"{name}/SKILL.md"))
        text = path.read_text(encoding="utf-8")
        body = _body(text)
        assert "## When to Use" in body, name
        assert "## When NOT to Use" in body, name
        assert "## Step Pattern" in body, name
        assert "## Recovery" in body, name
        for token in BANNED:
            assert token.lower() not in body.lower(), f"{name}: banned token {token}"
        assert "args: []" in text, name
        assert re.search(r"^kind: playbook$", text, re.M), name
        assert re.search(r"^category: ", text, re.M), name


def test_descriptions_and_metadata_budget():
    registry = load_skill_registry(str(ROOT / "winbrow" / "skills"), sync_learned=False)
    for meta in registry.values():
        if meta.kind != "playbook":
            continue
        assert len(meta.description.split()) <= 60, meta.name
        assert meta.description.endswith("."), meta.name
        assert meta.skill.startswith("playbook_"), meta.name
    metadata = build_skills_metadata(registry)
    assert len(metadata) < 12000
    assert "..." not in metadata


def test_script_references_exist():
    for skill, script in SCRIPTS.items():
        matches = list(LIBRARY.rglob(f"{skill}/scripts/{script}"))
        assert len(matches) == 1, f"{skill}/{script} found {len(matches)} times"
        text = next(LIBRARY.rglob(f"{skill}/SKILL.md")).read_text(encoding="utf-8")
        assert f'"{script}"' in text or script in text, skill


def test_no_unresolved_placeholders():
    for path in LIBRARY.rglob("SKILL.md"):
        text = path.read_text(encoding="utf-8")
        assert "{{" not in text, path
        assert "TODO" not in text, path
