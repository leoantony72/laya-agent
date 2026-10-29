"""Run the brain loop against eval_cases.jsonl (safe by default).

Unsafe cases are refused unless ``--include-unsafe`` is passed. Even then,
``--live`` is required for a skill outside the read-only allowlist; without it
the executor returns an ``[eval-blocked]`` result and nothing touches the
machine.

    python -m scripts.eval_brain --runs 1 --out logs/eval.json
    python -m scripts.eval_brain --include-unsafe --live   # opt-in, risky

The runner writes trace rows to ``logs/eval_<timestamp>.jsonl``. Aggregate
them with ``python -m scripts.brain_metrics <trace>``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_CASES = ROOT / "eval_cases.jsonl"

SAFE_SKILLS = frozenset({
    "current_time", "network_status", "system_spec_info", "system_specs",
    "system_uptime", "system_performance", "battery_health",
    "battery_status", "public_ip", "web_search", "web_fetch",
    "read_document", "find_files", "find_large_files", "list_directory",
    "recall_memory", "mcp_wikipedia_search_wikipedia",
    "mcp_wikipedia_get_summary", "mcp_dictionary_define",
    "mcp_arxiv_search_papers", "mcp_crossref_get_work", "mcp_fetch_fetch",
    "mcp_open_meteo_openmeteo_search_locations",
    "mcp_open_meteo_openmeteo_get_forecast",
})


def _load_cases(path: Path, include_unsafe: bool) -> List[dict]:
    cases: List[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("unsafe") and not include_unsafe:
            continue
        cases.append(row)
    return cases


def _load_env_file() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and (key not in os.environ or not os.environ[key]):
            os.environ[key] = value


def _configure_env(trace_path: Path, permissions_dir: Path) -> None:
    os.environ["WINBROW_BRAIN_FIRST"] = "1"
    os.environ["WINBROW_AUTO_APPROVE_T2"] = "0"
    os.environ["WINBROW_TRACE_PATH"] = str(trace_path)
    os.environ["WINBROW_PERMISSIONS_DIR"] = str(permissions_dir)
    os.environ.setdefault("WINBROW_TTS", "0")
    # Benchmark runs should not mutate the skill library.
    os.environ["WINBROW_SKILL_WRITER"] = "0"


def _run_one(agent: Any, request: str, session_id: str) -> dict:
    result = asyncio.run(agent.execute(request, session_id=session_id))
    execution = result.get("execution") or {}
    output = str(execution.get("output") or "")
    steps = execution.get("steps") or []
    blocked = output.startswith("[eval-blocked]")
    return {
        "request": request,
        "success": bool(execution.get("success")) and not blocked,
        "complete": bool(execution.get("complete")),
        "blocked": blocked,
        "steps": len(steps),
        "kind": str(execution.get("kind") or execution.get("type") or ""),
        "output": output[:300],
        "total_elapsed_ms": result.get("total_elapsed_ms", 0),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="eval_brain")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--out", default="")
    parser.add_argument("--trace", default="")
    parser.add_argument("--include-unsafe", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    trace_path = Path(args.trace or ROOT / "logs" / f"eval_{stamp}.jsonl")
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    permissions_dir = Path(tempfile.mkdtemp(prefix="winbrow_eval_perms_"))
    _load_env_file()
    _configure_env(trace_path, permissions_dir)

    from winbrow.agent import WinBrowAgent
    from winbrow.windows import WindowsContext

    agent = WinBrowAgent()
    try:
        from winbrow.memory.skills import reset_skills
        reset_skills().clear()
    except Exception:
        pass
    agent.get_context = lambda: WindowsContext()
    original_run_skill = agent.run_skill
    calls: List[tuple] = []

    async def guarded_run_skill(tool_name, tool_args, **kwargs):
        calls.append((tool_name, tool_args))
        if not args.live and tool_name not in SAFE_SKILLS:
            return {
                "success": False,
                "output": (
                    f"[eval-blocked] '{tool_name}' is outside the read-only "
                    "allowlist; rerun with --live to execute it."
                ),
            }
        return await original_run_skill(tool_name, tool_args, **kwargs)

    agent.run_skill = guarded_run_skill
    agent.brain.executor = guarded_run_skill

    cases = _load_cases(Path(args.cases), args.include_unsafe)
    if args.limit:
        cases = cases[: args.limit]

    rows: List[dict] = []
    for run_index in range(max(1, args.runs)):
        for case_index, case in enumerate(cases):
            request = str(case.get("request") or "")
            calls.clear()
            row = _run_one(
                agent, request,
                session_id=f"eval_{stamp}_{run_index}_{case_index}",
            )
            row["run"] = run_index + 1
            row["expected"] = case.get("expected") or []
            row["tool_calls"] = [name for name, _ in calls]
            rows.append(row)
            print(
                f"[{run_index + 1}/{args.runs}] "
                f"{'OK  ' if row['success'] else 'FAIL'} "
                f"steps={row['steps']:2} {request[:70]}"
            )

    summary = {
        "generated": stamp,
        "trace": str(trace_path),
        "live": bool(args.live),
        "include_unsafe": bool(args.include_unsafe),
        "runs": max(1, args.runs),
        "cases": len(cases),
        "completed": sum(1 for r in rows if r["success"] and r["complete"]),
        "success": sum(1 for r in rows if r["success"]),
        "blocked": sum(1 for r in rows if r["blocked"]),
        "rows": rows,
    }
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"\n{summary['success']}/{len(rows)} succeeded, "
        f"{summary['completed']}/{len(rows)} complete, "
        f"{summary['blocked']} blocked. Trace: {trace_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
