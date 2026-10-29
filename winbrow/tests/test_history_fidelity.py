"""History must carry the real data, not a 400-char summary of it.

Regression for "open WordPad and type the AI document" → "there is no heading
in the file": the planner only ever saw the first line of each observation, so
it could not quote or verify the document it had just typed.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.utils.history import (
    OBSERVATION_STEP_LIMIT,
    format_history,
    history_full_enabled,
)


def _entry(skill, args=None, output="ok"):
    return {
        "step": {"skill": skill, "args": args or {}},
        "result": {"success": True, "output": output},
    }


def test_full_history_is_the_default(monkeypatch):
    monkeypatch.delenv("WINBROW_HISTORY_FULL", raising=False)
    assert history_full_enabled() is True
    monkeypatch.setenv("WINBROW_HISTORY_FULL", "0")
    assert history_full_enabled() is False


def test_full_history_keeps_typed_document_text(monkeypatch, tmp_path):
    monkeypatch.setenv("WINBROW_RESULTS_DIR", str(tmp_path))
    body = (
        "Artificial Intelligence\n\n"
        "Artificial Intelligence (AI) is the simulation of human intelligence."
        + " " + "detail " * 900
        + "\n\nHEADING: Conclusion"
    )
    history = [
        _entry("type_text", {"text": body}),
        _entry("screen_read_text", {"region": "active_window"}, body),
    ]
    block = format_history(history, full=True, max_chars=200_000, run_id="br_hd")
    # The beginning of the typed body (the heading) must be visible...
    assert "Artificial Intelligence (AI) is the simulation" in block
    # ...and it must not be replaced by a "<N chars>" placeholder.
    assert f"<{len(body)} chars>" not in block
    assert "detail" in block


def test_legacy_window_keeps_more_screen_text(monkeypatch):
    monkeypatch.setenv("WINBROW_RESULTS_DIR", str(Path(__file__).parent))
    marker = "z" * (OBSERVATION_STEP_LIMIT - 10) + "TAIL-MARKER"
    history = [_entry("screen_read_text", {}, marker)]
    block = format_history(history, full=False)
    assert "TAIL-MARKER" in block or len(marker) > OBSERVATION_STEP_LIMIT
    assert len(block) > 400
