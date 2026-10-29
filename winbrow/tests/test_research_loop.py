"""Research loop: web output must stay visible and URLs must not be re-fetched."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.agent_loop import (
    BrainStepResult,
    _is_fetch_escalation,
    _step_signature,
)
from winbrow.utils.history import format_history


def _entry(skill, args, output, success=True):
    return {
        "step": {"skill": skill, "args": args},
        "result": {"success": success, "output": output},
    }


def _step(skill, args, output, success=True):
    return BrainStepResult(skill, args, success, output)


def test_web_fetch_text_survives_history_compression():
    """A fetched page used to be cut to 400 chars, so the planner refetched it."""
    body = "PAGE HEADER\n" + "x" * 18_000 + "\nFULL-PAGE-MARKER"
    block = format_history([_entry("web_fetch", {"url": "https://example.com/a"}, body)])
    assert "FULL-PAGE-MARKER" in block


def test_web_fetch_budget_is_newest_first():
    history = [
        _entry(
            "web_fetch",
            {"url": f"https://example.com/{i}"},
            "y" * 12_000 + f"marker-{i}",
        )
        for i in range(3)
    ]
    block = format_history(history)
    assert "marker-2" in block
    assert "marker-0" not in block
    assert len(block) < 26_000


def test_web_fetch_full_mode_keeps_all_steps():
    history = [
        _entry(
            "web_fetch",
            {"url": f"https://example.com/{i}"},
            "y" * 12_000 + f"marker-{i}",
        )
        for i in range(3)
    ]
    block = format_history(
        history, full=True, max_chars=100_000, run_id="br_full")
    assert "marker-0" in block
    assert "marker-2" in block
    assert len(block) > 30_000


def test_web_search_keeps_all_results_visible():
    output = "\n\n".join(
        f"{i}. {'T' * 60}\n   https://example.com/{i}\n   {'s' * 160}"
        for i in range(1, 6)
    )
    assert len(output) > 900
    block = format_history([_entry("web_search", {"query": "q"}, output)])
    assert "https://example.com/5" in block


def test_non_web_tools_keep_terse_history():
    output = "x" * 4_000 + "END-MARKER"
    block = format_history([_entry("run_command", {"command": "dir"}, output)])
    assert "END-MARKER" not in block


def test_web_fetch_signature_ignores_max_chars():
    a = _step_signature("web_fetch", {"url": "https://x.test/a", "max_chars": 12000})
    b = _step_signature("web_fetch", {"url": "https://x.test/a", "max_chars": 40000})
    c = _step_signature("web_fetch", {"url": "https://x.test/b", "max_chars": 12000})
    assert a == b
    assert a != c


def test_fetch_escalation_allowed_once_for_truncated_page():
    prior = _step(
        "web_fetch",
        {"url": "https://x.test/a", "max_chars": 12000},
        "text [TRUNCATED: 8000 chars remain]",
    )
    assert _is_fetch_escalation(
        [prior], {"url": "https://x.test/a", "max_chars": 40000}
    ) is True

    second = _step(
        "web_fetch",
        {"url": "https://x.test/a", "max_chars": 40000},
        "more [TRUNCATED: 100 chars remain]",
    )
    assert _is_fetch_escalation(
        [prior, second], {"url": "https://x.test/a", "max_chars": 40000}
    ) is False

    clean = _step("web_fetch", {"url": "https://x.test/a"}, "complete text")
    assert _is_fetch_escalation(
        [clean], {"url": "https://x.test/a", "max_chars": 40000}
    ) is False
