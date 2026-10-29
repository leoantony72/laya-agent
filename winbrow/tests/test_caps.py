"""Structural checks for the agreed result caps."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_tool_cap_constants():
    from winbrow import document_tools, mcp, shell, skill_scripts, web_tools
    assert shell._MAX_OUTPUT_CHARS == 100_000
    assert shell._INLINE_COMMAND_CHARS == 12_000
    assert shell._MAX_COMMAND_CHARS == 30_000
    assert skill_scripts.MAX_OUTPUT_CHARS == 60_000
    assert mcp.MAX_OUTPUT_CHARS == 100_000
    assert document_tools.DEFAULT_MAX_CHARS == 60_000
    assert document_tools.MAX_OUTPUT_CHARS == 100_000
    assert web_tools.DEFAULT_FETCH_CHARS == 40_000
    assert web_tools.MAX_FETCH_CHARS == 100_000
    assert web_tools.DEFAULT_SEARCH_COUNT == 8


def test_screen_ocr_clip_is_bounded(monkeypatch):
    from winbrow.computer.tools import _ocr_output
    body = "line\n" * 5000
    out = _ocr_output(body)
    assert "OCR extracted" in out
    assert len(out) < len(body)
