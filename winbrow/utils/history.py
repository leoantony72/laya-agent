"""History inspection and compression helpers for the closed-loop planner.

The planner can silently spin on a step that keeps returning the same result,
and long runs can grow the prompt until earlier context is lost. These helpers
compress old steps, summarise dead-end services, detect repetition, and count
recent stuck warnings so the loop can force a clarify instead of spinning.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.parse import urlsplit

FULL_WINDOW = 5
OUTCOME_LIMIT = 120
RECENT_OUTPUT_LIMIT = 400
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
_LARGE_TEXT_KEYS = frozenset({
    "content", "text", "body", "data", "oldtext", "newtext",
    "old_text", "new_text",
})


def _format_args(args: Dict[str, Any]) -> str:
    """Keep history args readable without echoing whole file payloads."""
    if not args:
        return "{}"
    parts: List[str] = []
    for key, value in args.items():
        name = str(key)
        if isinstance(value, str):
            if len(value) <= _ARG_VALUE_LIMIT:
                parts.append(f"{name}={value!r}")
            elif name.lower() in _LARGE_TEXT_KEYS:
                parts.append(f"{name}=<{len(value)} chars>")
            else:
                parts.append(
                    f"{name}={value[:_ARG_VALUE_LIMIT]!r}..."
                    f"<{len(value)} chars>"
                )
        elif isinstance(value, (dict, list)):
            try:
                blob = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                blob = str(value)
            if len(blob) <= _ARG_VALUE_LIMIT:
                parts.append(f"{name}={blob}")
            else:
                parts.append(f"{name}=<{len(blob)} chars JSON>")
        else:
            parts.append(f"{name}={value!r}")
    text = ", ".join(parts)
    if len(text) > _ARG_TOTAL_LIMIT:
        text = text[:_ARG_TOTAL_LIMIT] + f"...<{len(text)} chars>"
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


def count_recent_stuck(history: List[Any], window: int = 6) -> int:
    """Count recent steps whose warning says the planner is stuck/repeating."""
    count = 0
    for entry in history[-window:]:
        warning = _warning_of(entry).lower()
        if ("stuck" in warning or "identical" in warning
                or "same result" in warning):
            count += 1
    return count


def format_history(history: List[Any], full_window: int = FULL_WINDOW) -> str:
    """Render history with old steps compressed and the last few in detail."""
    if not history:
        return "  (none — this is the first step)"

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

    return "\n".join(lines)


def _recent_output_limits(recent: List[Any]) -> List[int]:
    """Per-step output caps, giving document reads a newest-first budget."""
    limits = [
        FILE_LIST_STEP_LIMIT if _skill_of(entry) in FILE_LIST_SKILLS
        else RECENT_OUTPUT_LIMIT
        for entry in recent
    ]
    remaining = DOCUMENT_TOTAL_BUDGET
    for idx in range(len(recent) - 1, -1, -1):
        if _skill_of(recent[idx]) not in DOCUMENT_READ_SKILLS or remaining <= 0:
            continue
        limits[idx] = min(DOCUMENT_STEP_LIMIT, remaining)
        remaining -= limits[idx]
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
