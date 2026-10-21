"""History inspection and compression helpers for the closed-loop planner.

The planner can silently spin on a step that keeps returning the same result,
and long runs can grow the prompt until earlier context is lost. These helpers
compress old steps, summarise dead-end services, detect repetition, and count
recent stuck warnings so the loop can force a clarify instead of spinning.
"""

from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import urlsplit

FULL_WINDOW = 5
OUTCOME_LIMIT = 120
RECENT_OUTPUT_LIMIT = 400


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
        outcome = _output_of(entry)[:OUTCOME_LIMIT].split("\n")[0]
        warning = _warning_of(entry)
        warning_text = f" ⚠ _warning: {warning[:160]}" if warning else ""
        ok = "✓" if _success_of(entry) else "✗"
        lines.append(
            f"  Step {i + 1}: {_skill_of(entry)} → {ok} {outcome}{warning_text}"
        )

    if older_count:
        lines.append(f"  --- last {full_window} steps in full ---")

    for i, entry in enumerate(history[-full_window:], start=older_count):
        warning = _warning_of(entry)
        warning_text = f"\n     ⚠ _warning: {warning}" if warning else ""
        args = _args_of(entry)
        lines.append(
            f"  Step {i + 1}: {_skill_of(entry)}({args}) → "
            f"success={_success_of(entry)} "
            f"output={_output_of(entry)[:RECENT_OUTPUT_LIMIT]}"
            f"{warning_text}"
        )

    return "\n".join(lines)


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
