"""Aggregate brain_run / brain_step / brain_plan trace rows.

Usage:
    python -m scripts.brain_metrics logs/trace.jsonl
    python -m scripts.brain_metrics logs/trace.jsonl --json
    python -m scripts.brain_metrics logs/baseline.jsonl --run-id br_abc123

The runtime never imports this module. Trace rows store hashes and sizes, not
tool output, so this is safe to share.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _iter_rows(paths: Iterable[str]) -> Iterable[dict]:
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row


def aggregate(paths: Iterable[str], run_id: Optional[str] = None) -> Dict[str, Any]:
    runs: Dict[str, dict] = {}
    for row in _iter_rows(paths):
        kind = str(row.get("type") or "")
        if not kind.startswith("brain_"):
            continue
        rid = str(row.get("run_id") or "")
        if run_id and rid != run_id:
            continue
        if not rid:
            continue
        run = runs.setdefault(rid, {
            "run_id": rid,
            "outcome": "",
            "success": False,
            "complete": False,
            "steps": 0,
            "skips": 0,
            "plans": 0,
            "parse_retries": 0,
            "timeouts": 0,
            "repairs": 0,
            "budget_overflows": 0,
            "estimated_tokens": 0,
            "peak_history_chars": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read": 0,
            "prompt_chars": 0,
            "elapsed_ms": 0.0,
            "providers": set(),
            "by_provider": {},
            "ttft_rows": 0,
        })
        if kind == "brain_run":
            run["outcome"] = str(row.get("outcome") or "")
            run["success"] = bool(row.get("success"))
            run["complete"] = bool(row.get("complete"))
            run["steps"] = max(run["steps"], int(row.get("steps") or 0))
            run["elapsed_ms"] = max(run["elapsed_ms"], float(row.get("total_ms") or 0.0))
            if row.get("provider"):
                run["providers"].add(str(row["provider"]))
        elif kind == "brain_step":
            run["steps"] = max(run["steps"], int(row.get("step_index") or 0))
            if row.get("timed_out"):
                run["timeouts"] += 1
            if str(row.get("skill") or "") in {"repair_tool", "build_tool"}:
                run["repairs"] += 1
        elif kind == "brain_skip":
            run["skips"] += 1
        elif kind == "brain_plan":
            run["plans"] += 1
            attempt = int(row.get("attempt") or 1)
            if attempt > 1:
                run["parse_retries"] += 1
            if row.get("estimated"):
                run["estimated_tokens"] += 1
            if row.get("history_overflow"):
                run["budget_overflows"] += 1
            run["peak_history_chars"] = max(
                run["peak_history_chars"],
                int(row.get("history_chars") or 0),
            )
            run["input_tokens"] += int(row.get("input_tokens") or 0)
            run["output_tokens"] += int(row.get("output_tokens") or 0)
            run["cache_read"] += int(row.get("cache_read") or 0)
            run["prompt_chars"] = max(
                run["prompt_chars"], int(row.get("prompt_chars") or 0))
            provider = str(row.get("provider") or "unknown")
            run["providers"].add(provider)
            bucket = run["by_provider"].setdefault(
                provider, {"input_tokens": 0, "cache_read": 0})
            bucket["input_tokens"] += int(row.get("input_tokens") or 0)
            bucket["cache_read"] += int(row.get("cache_read") or 0)

    rows: List[dict] = []
    for run in runs.values():
        input_tokens = run["input_tokens"]
        run["cache_ratio"] = (
            round(run["cache_read"] / input_tokens, 3) if input_tokens else 0.0
        )
        run["uncached_tokens"] = max(0, input_tokens - run["cache_read"])
        run["providers"] = sorted(run["providers"])
        for bucket in run["by_provider"].values():
            bucket["cache_ratio"] = (
                round(bucket["cache_read"] / bucket["input_tokens"], 3)
                if bucket["input_tokens"] else 0.0
            )
        run["duplicate_rate"] = (
            round(run["skips"] / (run["steps"] + run["skips"]), 3)
            if (run["steps"] + run["skips"]) else 0.0
        )
        rows.append(run)
    rows.sort(key=lambda item: item["run_id"])
    return {"runs": rows, "count": len(rows)}


def _totals(rows: List[dict]) -> dict:
    def total(name: str) -> float:
        return sum(float(row.get(name) or 0) for row in rows)
    steps = total("steps")
    skips = total("skips")
    tokens = total("input_tokens")
    return {
        "runs": len(rows),
        "completed": sum(1 for row in rows if row["complete"] and row["success"]),
        "steps": int(steps),
        "skips": int(skips),
        "duplicate_rate": round(skips / (steps + skips), 3) if (steps + skips) else 0.0,
        "parse_retries": int(total("parse_retries")),
        "timeouts": int(total("timeouts")),
        "repairs": int(total("repairs")),
        "budget_overflows": int(total("budget_overflows")),
        "estimated_tokens": int(total("estimated_tokens")),
        "peak_history_chars": int(
            max([row["peak_history_chars"] for row in rows] or [0])),
        "input_tokens": int(tokens),
        "output_tokens": int(total("output_tokens")),
        "cache_read": int(total("cache_read")),
        "cache_ratio": round(total("cache_read") / tokens, 3) if tokens else 0.0,
        "elapsed_ms": round(total("elapsed_ms"), 1),
        "peak_prompt_chars": int(max([row["prompt_chars"] for row in rows] or [0])),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="brain_metrics")
    parser.add_argument("paths", nargs="+", help="trace JSONL file(s)")
    parser.add_argument("--run-id", default="", help="only this run id")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    result = aggregate(args.paths, run_id=args.run_id or None)
    result["totals"] = _totals(result["runs"])
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    header = (
        "run_id       outcome       ok steps skips dup  plans retry tout rep "
        "ovf in_tok cache%  ms"
    )
    print(header)
    for row in result["runs"]:
        print(
            f"{row['run_id'][:12]:12} {row['outcome'][:12]:12} "
            f"{int(row['success'] and row['complete'])}  "
            f"{row['steps']:4} {row['skips']:5} {row['duplicate_rate']:.2f} "
            f"{row['plans']:5} {row['parse_retries']:5} {row['timeouts']:4} "
            f"{row['repairs']:3} {row['budget_overflows']:3} "
            f"{row['input_tokens']:6} {row['cache_ratio'] * 100:6.1f} "
            f"{row['elapsed_ms']:7.0f}"
        )
    totals = result["totals"]
    print(
        f"\nTOTAL: runs={totals['runs']} "
        f"complete={totals['completed']} steps={totals['steps']} "
        f"dup={totals['duplicate_rate']:.2f} retries={totals['parse_retries']} "
        f"timeouts={totals['timeouts']} repairs={totals['repairs']} "
        f"budget_overflows={totals['budget_overflows']} "
        f"cache={totals['cache_ratio'] * 100:.1f}% "
        f"peak_prompt={totals['peak_prompt_chars']} chars "
        f"peak_history={totals['peak_history_chars']} chars"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
