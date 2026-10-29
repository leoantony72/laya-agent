"""Full-history rendering, hard budget, and deterministic stubs."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.utils.history import (
    detect_stuck,
    format_history,
    history_budget_chars,
)


def _entry(skill, output, args=None, success=True):
    return {
        "step": {"skill": skill, "args": args or {}},
        "result": {"success": success, "output": output},
    }


def _results(tmp_path, monkeypatch):
    target = tmp_path / "results"
    monkeypatch.setenv("WINBROW_RESULTS_DIR", str(target))
    return target


def test_under_budget_renders_every_step_verbatim(tmp_path, monkeypatch):
    _results(tmp_path, monkeypatch)
    history = [
        _entry("read_document", f"body-{i} " + "x" * 200)
        for i in range(4)
    ]
    block = format_history(
        history, full=True, max_chars=100_000, run_id="br_test")
    for i in range(4):
        assert f"body-{i}" in block
    assert "removed" not in block


def test_over_budget_stubs_oldest_first_and_saves_full_copy(
    tmp_path, monkeypatch
):
    target = _results(tmp_path, monkeypatch)
    history = [
        _entry("read_document", f"marker-{i} " + "x" * 500)
        for i in range(7)
    ]
    block = format_history(
        history, full=True, max_chars=1200, keep_recent=2,
        run_id="br_test",
    )
    assert "marker-6" in block
    assert "marker-0" not in block
    assert "full copy at" in block
    files = list(target.glob("*.txt"))
    assert files
    assert any("marker-0" in f.read_text(encoding="utf-8") for f in files)


def test_recent_block_is_clipped_when_it_alone_exceeds_budget(
    tmp_path, monkeypatch
):
    _results(tmp_path, monkeypatch)
    history = [
        _entry("read_document", f"marker-{i} " + "x" * 2000)
        for i in range(5)
    ]
    block = format_history(
        history, full=True, max_chars=3000, keep_recent=5, run_id="br_recent")
    assert "[budget: output clipped" in block
    assert len(block) <= 3000


def test_metrics_and_eviction_headroom(tmp_path, monkeypatch):
    _results(tmp_path, monkeypatch)
    history = [
        _entry("read_document", f"marker-{i} " + "x" * 1500)
        for i in range(10)
    ]
    metrics = {}
    block = format_history(
        history, full=True, max_chars=10_000, keep_recent=3,
        run_id="br_metrics", metrics=metrics,
    )
    assert metrics["budget"] == 10_000
    assert metrics["stubbed"] > 0
    assert metrics["chars"] == len(block)
    assert metrics["chars"] <= 10_000
    assert metrics["overflow"] is False


def test_stub_is_stable_across_calls(tmp_path, monkeypatch):
    _results(tmp_path, monkeypatch)
    history = [
        _entry("web_fetch", f"page-{i} " + "y" * 500)
        for i in range(6)
    ]
    first = format_history(
        history, full=True, max_chars=900, keep_recent=2, run_id="br_same")
    second = format_history(
        history, full=True, max_chars=900, keep_recent=2, run_id="br_same")
    assert first == second


def test_browser_snapshots_are_evicted_first(tmp_path, monkeypatch):
    _results(tmp_path, monkeypatch)
    history = [
        _entry("browser_read_page", "browser-" + "b" * 500),
        _entry("read_document", "document-" + "d" * 500),
        _entry("read_document", "recent-" + "r" * 500),
        _entry("read_document", "newest-" + "n" * 500),
    ]
    block = format_history(
        history, full=True, max_chars=2000, keep_recent=2, run_id="br_b")
    assert "browser-" not in block or "removed" in block
    # The browser step is stubbed before the older document step.
    assert "Step 1: browser_read_page → [result" in block
    assert "Step 2: read_document(" in block


def test_detection_helpers_read_objects_not_rendered_stubs(
    tmp_path, monkeypatch
):
    _results(tmp_path, monkeypatch)
    history = [
        _entry("web_fetch", "same output")
        for _ in range(3)
    ]
    format_history(
        history, full=True, max_chars=100, keep_recent=1, run_id="br_d")
    assert detect_stuck(history) is True


def test_budget_formula(tmp_path, monkeypatch):
    monkeypatch.delenv("WINBROW_HISTORY_MAX_CHARS", raising=False)
    monkeypatch.setenv("WINBROW_CONTEXT_TOKENS", "128000")
    assert history_budget_chars() == 128_000
    monkeypatch.setenv("WINBROW_CONTEXT_TOKENS", "200000")
    assert history_budget_chars() == 200_000
    monkeypatch.setenv("WINBROW_CONTEXT_TOKENS", "1000000")
    assert history_budget_chars() == 400_000
    monkeypatch.delenv("WINBROW_CONTEXT_TOKENS")
    assert history_budget_chars() == 240_000
    monkeypatch.setenv("WINBROW_HISTORY_MAX_CHARS", "1234")
    assert history_budget_chars() == 1234
