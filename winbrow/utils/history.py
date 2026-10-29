"""History inspection and compression helpers for the closed-loop planner.

The planner can silently spin on a step that keeps returning the same result,
and long runs can grow the prompt until earlier context is lost. These helpers
compress old steps, summarise dead-end services, detect repetition, and count
recent stuck warnings so the loop can force a clarify instead of spinning.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .clip import save_full_text

FULL_WINDOW = 5
OUTCOME_LIMIT = 120
# Observation output (screen OCR, command results) must survive the legacy
# window too: 400 chars cut off everything after the first lines of a window,
# so the planner could not see its own document content.
RECENT_OUTPUT_LIMIT = 4000
OBSERVATION_SKILLS = frozenset({"screen_read_text"})
OBSERVATION_STEP_LIMIT = 12_000
# Document text must survive the history window long enough for the planner to
# summarize it. Normal tool output stays terse; document reads get a larger,
# newest-first budget so a full read or a few chunks remain visible.
DOCUMENT_READ_SKILLS = frozenset({
    "read_document",
    "mcp_filesystem_read_text_file",
})
DOCUMENT_STEP_LIMIT = 30_000
DOCUMENT_TOTAL_BUDGET = 60_000

# File searches return a one-line header followed by the matching paths. The
# paths are the useful part and must survive compression, otherwise the planner
# has to re-run the search later just to recover the file it already found.
FILE_LIST_SKILLS = frozenset({
    "find_files",
    "find_large_files",
})
FILE_LIST_STEP_LIMIT = 2_000

# Browser page snapshots are the biggest per-step history cost. Keep the last
# three readable and strip page text from everything older; only the newest
# step gets the full decision context.
BROWSER_SKILLS = frozenset({
    "browser_navigate",
    "browser_read_page",
    "browser_read_full",
    "browser_get_accessibility_tree",
    "browser_interact",
    "browser_wait",
    "browser_upload_file",
})
PAGE_TEXT_LIMIT_RECENT = 2000
PAGE_TEXT_LIMIT_CURRENT = 3000
BROWSER_DETAIL_STEPS = 3


# Web research output is the whole point of a search/fetch step: the planner
# must be able to read enough of it to answer without repeating calls. Without
# a dedicated budget, web_fetch fell through to RECENT_OUTPUT_LIMIT (400 chars),
# so every re-fetch showed the same 400-char prefix and the planner looped.
WEB_SEARCH_SKILLS = frozenset({"web_search"})
WEB_FETCH_SKILLS = frozenset({"web_fetch"})
WEB_SEARCH_STEP_LIMIT = 3_000
WEB_SEARCH_TOTAL_BUDGET = 6_000
WEB_FETCH_STEP_LIMIT = 20_000
WEB_FETCH_TOTAL_BUDGET = 30_000


def _skill_of(entry: Any) -> str:
    if isinstance(entry, dict):
        step = entry.get("step")
        if isinstance(step, dict):
            return str(step.get("skill") or "")
        return str(entry.get("skill") or "")
    return str(getattr(entry, "skill", "") or "")


def _args_of(entry: Any) -> dict:
    if isinstance(entry, dict):
        step = entry.get("step")
        if isinstance(step, dict):
            return dict(step.get("args") or {})
        return dict(entry.get("args") or {})
    return dict(getattr(entry, "args", {}) or {})


_ARG_VALUE_LIMIT = 160
_ARG_TOTAL_LIMIT = 600
# Full-history mode must show what was actually typed/written. The old 2k/8k
# caps silently replaced large text args with "<N chars>", so the planner could
# not see the document it had just produced (e.g. type_text content) and
# hallucinated about the file instead of answering from HISTORY.
_FULL_ARG_VALUE_LIMIT = 8000
_FULL_ARG_TOTAL_LIMIT = 32000
_LARGE_TEXT_KEYS = frozenset({
    "content", "text", "body", "data", "oldtext", "newtext",
    "old_text", "new_text",
})

# Full-history defaults. The brain loop asks for full rendering by default;
# WINBROW_HISTORY_FULL=0 restores the legacy compressed window for tests/UI.
HISTORY_KEEP_RECENT = 5
HISTORY_BUDGET_DEFAULT = 240_000
HISTORY_BUDGET_CEILING = 400_000


def history_budget_chars() -> int:
    """Hard history budget, read at call time.

    Formula (user decision): 25% of the context window, in chars, capped at
    400k. Context window comes from WINBROW_CONTEXT_TOKENS; until that is
    known for the deployed model, 240k chars (~60k tokens) is the interim.
    """
    raw = os.environ.get("WINBROW_HISTORY_MAX_CHARS", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    try:
        context_tokens = int(os.environ.get("WINBROW_CONTEXT_TOKENS", "0"))
    except ValueError:
        context_tokens = 0
    if context_tokens > 0:
        return min(HISTORY_BUDGET_CEILING, int(0.25 * context_tokens * 4))
    return HISTORY_BUDGET_DEFAULT


def history_full_enabled() -> bool:
    """Full-fidelity history is the DEFAULT.

    Compressed history (first line only, 120/400-char caps) discards the
    evidence the planner needs to verify its own work and answer follow-ups
    about it. Opt back into it with WINBROW_HISTORY_FULL=0.
    """
    return os.environ.get("WINBROW_HISTORY_FULL", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def _head_tail(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    head = int(limit * 0.6)
    tail = max(0, limit - head)
    return (
        value[:head]
        + f"...<{len(value) - head - tail} chars omitted>..."
        + (value[-tail:] if tail else "")
    )


def _format_args(
    args: Dict[str, Any],
    value_limit: int = _ARG_VALUE_LIMIT,
    total_limit: int = _ARG_TOTAL_LIMIT,
    head_tail: bool = False,
) -> str:
    """Keep history args readable without echoing whole file payloads."""
    if not args:
        return "{}"
    parts: List[str] = []
    for key, value in args.items():
        name = str(key)
        if isinstance(value, str):
            if len(value) <= value_limit:
                parts.append(f"{name}={value!r}")
            elif head_tail:
                # Keep the real text (head + tail) instead of replacing it
                # with "<N chars>". Otherwise full history still hides the
                # document/script the planner just wrote.
                parts.append(
                    f"{name}={_head_tail(value, value_limit)!r}"
                    f"<full length {len(value)} chars>"
                )
            elif name.lower() in _LARGE_TEXT_KEYS:
                parts.append(f"{name}=<{len(value)} chars>")
            else:
                parts.append(
                    f"{name}={value[:value_limit]!r}..."
                    f"<{len(value)} chars>"
                )
        elif isinstance(value, (dict, list)):
            try:
                blob = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                blob = str(value)
            if len(blob) <= value_limit:
                parts.append(f"{name}={blob}")
            else:
                parts.append(f"{name}=<{len(blob)} chars JSON>")
        else:
            parts.append(f"{name}={value!r}")
    text = ", ".join(parts)
    if len(text) > total_limit:
        text = text[:total_limit] + f"...<{len(text)} chars>"
    return "{" + text + "}"


def _output_of(entry: Any) -> str:
    if isinstance(entry, dict):
        result = entry.get("result")
        if isinstance(result, dict):
            return str(result.get("output", ""))
        return str(entry.get("output", ""))
    return str(getattr(entry, "output", "") or "")


def _success_of(entry: Any) -> Any:
    if isinstance(entry, dict):
        result = entry.get("result")
        if isinstance(result, dict):
            return result.get("success", "?")
        return entry.get("success", "?")
    return bool(getattr(entry, "success", False))


def _warning_of(entry: Any) -> str:
    if isinstance(entry, dict):
        result = entry.get("result")
        if isinstance(result, dict):
            return str(result.get("_warning") or result.get("warning") or "")
        return str(entry.get("_warning") or entry.get("warning") or "")
    return str(getattr(entry, "warning", "") or "")


def _url_of(entry: Any) -> str:
    if isinstance(entry, dict):
        result = entry.get("result")
        if isinstance(result, dict) and result.get("url"):
            return str(result["url"])
        step = entry.get("step")
        args = step.get("args") if isinstance(step, dict) else entry.get("args")
        if isinstance(args, dict) and args.get("url"):
            return str(args["url"])
        return ""
    url = getattr(entry, "url", "")
    if url:
        return str(url)
    return str((getattr(entry, "args", {}) or {}).get("url") or "")


def _title_of(entry: Any) -> str:
    if isinstance(entry, dict):
        result = entry.get("result")
        if isinstance(result, dict) and result.get("title"):
            return str(result["title"])
        step = entry.get("step")
        args = step.get("args") if isinstance(step, dict) else entry.get("args")
        if isinstance(args, dict) and args.get("title"):
            return str(args["title"])
        return ""
    return str(getattr(entry, "title", "") or "")


def _compact_browser_outcome(entry: Any) -> str:
    """URL/title only — page text is stripped from older browser steps."""
    parts: List[str] = []
    url = _url_of(entry)
    title = _title_of(entry)
    if url:
        parts.append(f"url={url}")
    if title:
        parts.append(f"title={title}")
    if parts:
        return " ".join(parts)
    return _output_of(entry)[:OUTCOME_LIMIT].split("\n")[0]


def _domain_from_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlsplit(raw).hostname or ""
    except ValueError:
        host = ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or raw


def detect_stuck(history: List[Any], window: int = 2) -> bool:
    """Return True when the last ``window`` steps are the same skill and output.

    Works with both the raw history dicts (``{"step": ..., "result": ...}``)
    and the loop's ``BrainStepResult`` objects.
    """
    if window < 2 or len(history) < window:
        return False
    last = history[-window:]
    return (
        len(set(_skill_of(h) for h in last)) == 1
        and len(set(_output_of(h) for h in last)) == 1
    )


def _args_key(args: Dict[str, Any]) -> str:
    try:
        return json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(args)


def detect_repeated_failure(history: List[Any], window: int = 20) -> str:
    """Return the repeated error when the same step failed twice identically.

    A dependency install can succeed between two failed script calls, so the
    plain consecutive-failure counter resets and the planner loops. This guard
    looks across the recent window and returns the error text for the second
    identical failure. An empty string means no repeat was found.
    """
    seen: set[tuple[str, str, str]] = set()
    for entry in history[-max(2, window):]:
        if _success_of(entry):
            continue
        output = _output_of(entry).strip()
        if not output:
            continue
        # The first line identifies the failure; traceback tails often vary
        # with paths while the underlying error is unchanged.
        first_line = output.splitlines()[0][:300]
        key = (_skill_of(entry), _args_key(_args_of(entry)), first_line)
        if key in seen:
            return output
        seen.add(key)
    return ""


def count_recent_stuck(history: List[Any], window: int = 6) -> int:
    """Count recent steps whose warning says the planner is stuck/repeating."""
    count = 0
    for entry in history[-window:]:
        warning = _warning_of(entry).lower()
        if ("stuck" in warning or "identical" in warning
                or "same result" in warning):
            count += 1
    return count


def _render_full_entry(
    entry: Any, step_no: int, output_limit: Optional[int] = None,
) -> str:
    skill = _skill_of(entry) or "?"
    args = _format_args(
        _args_of(entry), _FULL_ARG_VALUE_LIMIT, _FULL_ARG_TOTAL_LIMIT,
        head_tail=True,
    )
    ok = "✓" if _success_of(entry) else "✗"
    output = _output_of(entry)
    if output_limit is not None and len(output) > output_limit:
        output = (
            _head_tail(output, output_limit)
            + f"\n     [budget: output clipped to {output_limit} chars]"
        )
    warning = _warning_of(entry)
    warning_text = f"\n     ⚠ _warning: {warning}" if warning else ""
    return (
        f"  Step {step_no}: {skill}({args}) → success={ok}\n"
        f"     output={output}{warning_text}"
    )


def _safe_filename_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "skill"))[:40]


def _history_stub(entry: Any, step_no: int, run_id: str = "") -> str:
    """Deterministic stub. Same step always renders the same text."""
    output = _output_of(entry)
    skill = _skill_of(entry) or "result"
    if not output:
        return (
            f"  Step {step_no}: {skill} → [result of step {step_no}, "
            "removed; no output]"
        )
    filename = (
        f"{_safe_filename_part(run_id or 'run')}_step_{step_no}_"
        f"{_safe_filename_part(skill)}.txt"
    )
    from .clip import results_dir
    path = results_dir() / filename
    if not path.exists():
        save_full_text(output, label=skill, filename=filename)
    if not path.exists():
        return (
            f"  Step {step_no}: {skill} → [result of step {step_no}, "
            f"{len(output)} chars, removed]"
        )
    return (
        f"  Step {step_no}: {skill} → [result of step {step_no}, "
        f"{len(output)} chars, removed; full copy at {path}]"
    )


def _record_metrics(
    metrics: Optional[dict],
    rendered: List[str],
    max_chars: int,
    stubbed: int,
    recent_clipped: int,
) -> None:
    if metrics is None:
        return
    size = len("\n".join(rendered))
    metrics.update({
        "chars": size,
        "budget": max_chars,
        "stubbed": stubbed,
        "recent_clipped": recent_clipped,
        "overflow": bool(max_chars > 0 and size > max_chars),
    })


def _format_full_history(
    history: List[Any],
    max_chars: int,
    keep_recent: int,
    run_id: str,
    metrics: Optional[dict] = None,
) -> str:
    keep_recent = max(1, int(keep_recent))
    rendered = [
        _render_full_entry(entry, index + 1)
        for index, entry in enumerate(history)
    ]
    if max_chars <= 0:
        _record_metrics(metrics, rendered, 0, 0, 0)
        return "\n".join(rendered)

    total = sum(len(line) + 1 for line in rendered)
    if total <= max_chars:
        _record_metrics(metrics, rendered, max_chars, 0, 0)
        return "\n".join(rendered)

    # Evict with headroom on real budgets so one addition does not force a
    # cache-breaking rewrite on the very next turn.
    target = int(max_chars * 0.8) if max_chars >= 10_000 else max_chars

    # Old browser snapshots go first: they are stale and re-readable.
    evictable = list(range(max(0, len(history) - keep_recent)))
    if os.environ.get("WINBROW_HISTORY_BROWSER_FIRST", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }:
        evictable.sort(key=lambda i: (0 if _skill_of(history[i]) in BROWSER_SKILLS else 1, i))

    stubbed = 0
    for index in evictable:
        if total <= target:
            break
        stub = _history_stub(history[index], index + 1, run_id=run_id)
        total -= len(rendered[index]) + 1
        total += len(stub) + 1
        rendered[index] = stub
        stubbed += 1

    # If the newest block alone is over budget, clip its oldest results (then
    # any remaining result) rather than sending an over-budget prompt. The
    # allowance ladder keeps the clipping deterministic for a given budget.
    recent_clipped = 0
    if total > max_chars:
        recent_start = max(0, len(history) - keep_recent)
        allowances = [
            max(500, max_chars // keep_recent),
            2000, 500, 200,
        ]
        for allowance in allowances:
            if total <= max_chars:
                break
            for index in range(recent_start, len(history)):
                if total <= max_chars:
                    break
                current = rendered[index]
                new = _render_full_entry(
                    history[index], index + 1, output_limit=allowance)
                if len(new) >= len(current):
                    continue
                total += len(new) - len(current)
                rendered[index] = new
                recent_clipped += 1

    _record_metrics(metrics, rendered, max_chars, stubbed, recent_clipped)
    return "\n".join(rendered)


def format_history(
    history: List[Any],
    full_window: int = FULL_WINDOW,
    *,
    full: bool = False,
    max_chars: Optional[int] = None,
    keep_recent: int = HISTORY_KEEP_RECENT,
    run_id: str = "",
    metrics: Optional[dict] = None,
) -> str:
    """Render history for the planner.

    ``full=False`` keeps the legacy compressed window. ``full=True`` renders
    every step verbatim (subject to the hard char budget), stubbing the oldest
    non-recent results first and saving their full text under logs/results.

    ``metrics`` is filled with chars/budget/stubbed/recent_clipped/overflow
    when supplied; the brain loop forwards it to the trace.
    """
    if not history:
        if metrics is not None:
            metrics.update({"chars": 0, "budget": 0, "stubbed": 0,
                            "recent_clipped": 0, "overflow": False})
        return "  (none — this is the first step)"

    if full:
        return _format_full_history(
            history,
            history_budget_chars() if max_chars is None else int(max_chars),
            keep_recent,
            run_id,
            metrics=metrics,
        )

    full_window = max(1, full_window)
    older_count = max(0, len(history) - full_window)
    lines: List[str] = []

    for i, entry in enumerate(history[:older_count]):
        skill = _skill_of(entry)
        if skill in BROWSER_SKILLS:
            outcome = _compact_browser_outcome(entry)
        elif skill in FILE_LIST_SKILLS:
            outcome = _output_of(entry)[:FILE_LIST_STEP_LIMIT]
        else:
            outcome = _output_of(entry)[:OUTCOME_LIMIT].split("\n")[0]
        warning = _warning_of(entry)
        warning_text = f" ⚠ _warning: {warning[:160]}" if warning else ""
        ok = "✓" if _success_of(entry) else "✗"
        lines.append(
            f"  Step {i + 1}: {_skill_of(entry)} → {ok} {outcome}{warning_text}"
        )

    if older_count:
        lines.append(f"  --- last {full_window} steps in full ---")

    recent = history[-full_window:]
    limits = _recent_output_limits(recent)
    for idx, entry in enumerate(recent):
        i = older_count + idx
        warning = _warning_of(entry)
        warning_text = f"\n     ⚠ _warning: {warning}" if warning else ""
        args = _format_args(_args_of(entry))
        if _skill_of(entry) in BROWSER_SKILLS:
            distance = len(recent) - 1 - idx
            if distance >= BROWSER_DETAIL_STEPS:
                output = _compact_browser_outcome(entry)
            elif distance == 0:
                output = _output_of(entry)[:PAGE_TEXT_LIMIT_CURRENT]
            else:
                output = _output_of(entry)[:PAGE_TEXT_LIMIT_RECENT]
        else:
            output = _output_of(entry)[:limits[idx]]
        lines.append(
            f"  Step {i + 1}: {_skill_of(entry)}({args}) → "
            f"success={_success_of(entry)} "
            f"output={output}"
            f"{warning_text}"
        )

    block = "\n".join(lines)
    if metrics is not None:
        metrics.update({"chars": len(block), "budget": 0, "stubbed": 0,
                        "recent_clipped": 0, "overflow": False})
    return block


def _recent_output_limits(recent: List[Any]) -> List[int]:
    """Per-step output caps with newest-first budgets per output family."""
    def base_limit(skill: str) -> int:
        if skill in FILE_LIST_SKILLS:
            return FILE_LIST_STEP_LIMIT
        if skill in OBSERVATION_SKILLS:
            return OBSERVATION_STEP_LIMIT
        if skill in WEB_SEARCH_SKILLS:
            return WEB_SEARCH_STEP_LIMIT
        if skill in WEB_FETCH_SKILLS:
            return WEB_FETCH_STEP_LIMIT
        return RECENT_OUTPUT_LIMIT

    limits = [base_limit(_skill_of(entry)) for entry in recent]

    def apply_budget(skills: frozenset[str], step_limit: int, total: int) -> None:
        remaining = total
        for idx in range(len(recent) - 1, -1, -1):
            if _skill_of(recent[idx]) not in skills:
                continue
            if remaining <= 0:
                limits[idx] = 0
                continue
            limits[idx] = min(step_limit, remaining)
            remaining -= limits[idx]

    apply_budget(DOCUMENT_READ_SKILLS, DOCUMENT_STEP_LIMIT, DOCUMENT_TOTAL_BUDGET)
    apply_budget(WEB_FETCH_SKILLS, WEB_FETCH_STEP_LIMIT, WEB_FETCH_TOTAL_BUDGET)
    apply_budget(WEB_SEARCH_SKILLS, WEB_SEARCH_STEP_LIMIT, WEB_SEARCH_TOTAL_BUDGET)
    return limits


def extract_tried_services(history: List[Any]) -> str:
    """Summarise which URLs/services have been tried and what happened."""
    tried: Dict[str, str] = {}
    for entry in history:
        if _skill_of(entry) != "browser_navigate":
            continue
        args = _args_of(entry)
        url = str(args.get("url") or _url_of(entry) or "")
        domain = _domain_from_url(url)
        if not domain:
            continue
        success = _success_of(entry)
        warning = _warning_of(entry)
        output = _output_of(entry)
        low_url = url.lower()
        low_output = output.lower()
        if "chrome-error" in low_url or "chrome-error" in low_output:
            status = "ERROR — chrome privacy/cert error"
        elif not success:
            status = f"FAILED — {output[:80]}"
        elif warning:
            status = f"VISITED but: {warning[:80]}"
        else:
            status = "visited"
        tried[domain] = status

    if not tried:
        return ""

    lines = ["ALREADY TRIED:"]
    lines.extend(f"  {domain} → {status}" for domain, status in tried.items())
    lines.append("Do not navigate to these again unless the approach has changed.\n")
    return "\n".join(lines)


def tried_domains(history: List[Any]) -> List[str]:
    """Unique domains the planner has navigated to, in first-seen order."""
    domains: List[str] = []
    for entry in history:
        if _skill_of(entry) != "browser_navigate":
            continue
        args = _args_of(entry)
        domain = _domain_from_url(str(args.get("url") or _url_of(entry) or ""))
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def last_url(history: List[Any]) -> str:
    """Most recent browser URL observed, or '(unknown)'."""
    for entry in reversed(history):
        if not _skill_of(entry).startswith("browser_"):
            continue
        url = _url_of(entry)
        if url:
            return url
    return "(unknown)"
