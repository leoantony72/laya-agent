"""Aggregator for brain_run / brain_step / brain_plan trace rows."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.brain_metrics import aggregate, main


def _write(path: Path) -> None:
    rows = [
        {"type": "brain_run", "run_id": "br_a", "outcome": "action",
         "success": True, "complete": True, "steps": 2,
         "total_ms": 1200, "provider": "nebius", "model": "m"},
        {"type": "brain_plan", "run_id": "br_a", "step_index": 0,
         "attempt": 1, "input_tokens": 1000, "output_tokens": 50,
         "cache_read": 600, "prompt_chars": 4000, "provider": "nebius"},
        {"type": "brain_plan", "run_id": "br_a", "step_index": 1,
         "attempt": 2, "input_tokens": 2000, "output_tokens": 60,
         "cache_read": 1000, "prompt_chars": 8000, "provider": "nebius"},
        {"type": "brain_step", "run_id": "br_a", "step_index": 1,
         "skill": "web_fetch", "success": True, "timed_out": False},
        {"type": "brain_step", "run_id": "br_a", "step_index": 2,
         "skill": "read_document", "success": True, "timed_out": True},
        {"type": "brain_step", "run_id": "br_a", "step_index": 2,
         "skill": "repair_tool", "success": True},
        {"type": "brain_skip", "run_id": "br_a", "step_index": 2,
         "skill": "read_document"},
        {"type": "brain_plan", "run_id": "br_a", "step_index": 2,
         "attempt": 1, "input_tokens": 0, "estimated": True,
         "history_chars": 9000, "history_overflow": True},
        {"type": "brain_run", "run_id": "br_b", "outcome": "action",
         "success": False, "complete": False, "steps": 1,
         "total_ms": 500, "provider": "nebius"},
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_aggregate_groups_runs_and_computes_ratios(tmp_path):
    path = tmp_path / "trace.jsonl"
    _write(path)
    result = aggregate([str(path)])
    assert result["count"] == 2
    run = next(r for r in result["runs"] if r["run_id"] == "br_a")
    assert run["steps"] == 2
    assert run["skips"] == 1
    assert run["plans"] == 3
    assert run["parse_retries"] == 1
    assert run["timeouts"] == 1
    assert run["repairs"] == 1
    assert run["budget_overflows"] == 1
    assert run["estimated_tokens"] == 1
    assert run["peak_history_chars"] == 9000
    assert run["input_tokens"] == 3000
    assert run["cache_read"] == 1600
    assert run["cache_ratio"] == 0.533
    assert run["prompt_chars"] == 8000


def test_trace_path_is_read_at_call_time(tmp_path, monkeypatch):
    from winbrow.trace import trace_path
    target = tmp_path / "custom.jsonl"
    monkeypatch.setenv("WINBROW_TRACE_PATH", str(target))
    assert trace_path() == target


def test_eval_case_loader_refuses_unsafe_by_default():
    from scripts.eval_brain import _load_cases
    cases = _load_cases(ROOT / "eval_cases.jsonl", include_unsafe=False)
    assert cases
    assert all(not case.get("unsafe") for case in cases)
    all_cases = _load_cases(ROOT / "eval_cases.jsonl", include_unsafe=True)
    assert len(all_cases) > len(cases)
    unsafe = {case["request"] for case in all_cases if case.get("unsafe")}
    assert "show my wifi password" in unsafe
    assert "clean temp files" in unsafe


def test_cli_json_output(tmp_path, capsys):
    path = tmp_path / "trace.jsonl"
    _write(path)
    assert main([str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["runs"] == 2
    assert payload["totals"]["completed"] == 1
