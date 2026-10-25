#!/usr/bin/env python
"""WinBrow post-session hook.

Runs at session Stop. It:
  1. Appends one entry to .agent/memory/session-log.md.
  2. Compares each knowledge file's ``last_verified`` date with the mtimes of its
     ``source_files`` and writes .agent/memory/stale.md when something drifted.
  3. If AGENT_API_KEY is set, asks an OpenAI-compatible agent API to refresh at
     most two stale knowledge files. Without the key it skips cleanly.

Design rules:
  - Never fail the session: every path catches exceptions and exits 0.
  - Never create stale.md when nothing is stale.
  - Never overwrite source code. Only files under .agent/knowledge/ are written.
  - Dry run writes nothing and makes no network call.

Usage:
    python .agent/hooks/post-session.py
    python .agent/hooks/post-session.py --dry-run --verbose
    echo '{"summary": "..."}' | python .agent/hooks/post-session.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

HOOK_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOOK_DIR.parents[1]
AGENT_DIR = REPO_ROOT / ".agent"
KNOWLEDGE_DIR = AGENT_DIR / "knowledge"
MEMORY_DIR = AGENT_DIR / "memory"
SESSION_LOG = MEMORY_DIR / "session-log.md"
STALE_FILE = MEMORY_DIR / "stale.md"

DEFAULT_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_API_MODEL = "deepseek/deepseek-v4.1-flash"
MAX_REFRESH_FILES = 2
MAX_SOURCE_CHARS = 60_000
MAX_CHARS_PER_SOURCE = 12_000
MAX_LOG_FILES = 20
API_TIMEOUT_S = 120

FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", re.S)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------- helpers


def _log(verbose: bool, message: str) -> None:
    if verbose:
        print(f"[post-session] {message}", file=sys.stderr)


def _read_text(path: Path, limit: int | None = None) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if limit is not None and len(text) > limit:
        return text[:limit]
    return text


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    data: dict[str, Any] = {}
    sources: list[str] = []
    in_sources = False
    for raw in match.group(1).splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if in_sources:
            stripped = line.strip()
            if stripped.startswith("- "):
                value = stripped[2:].strip().strip("\"'")
                if value:
                    sources.append(value)
                continue
            if line[0] not in " \t":
                in_sources = False
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key in {"last_verified", "knowledge"}:
            data[key] = value
        elif key in {"source_files", "sources"}:
            in_sources = True
    if sources:
        data["source_files"] = sources
    return data, text[match.end():]


def _source_paths(repo_root: Path, raw: str) -> list[Path]:
    """Expand one declared source into concrete files (files or directories)."""
    cleaned = raw.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned in {".git", ".git/"}:
        head = repo_root / ".git" / "HEAD"
        return [head] if head.exists() else []
    target = (repo_root / cleaned).resolve()
    try:
        target.relative_to(repo_root.resolve())
    except ValueError:
        return []
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    if target.name == "skills":
        return sorted(target.rglob("SKILL.md"))
    return sorted(
        path
        for path in target.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".md", ".json", ".txt", ".toml", ".yaml", ".yml", ".bat", ".ps1"}
        and "__pycache__" not in path.parts
    )


def _newest_mtime(repo_root: Path, sources: Iterable[str]) -> tuple[float | None, list[str]]:
    newest: float | None = None
    missing: list[str] = []
    for raw in sources:
        paths = _source_paths(repo_root, raw)
        if not paths:
            missing.append(raw)
            continue
        for path in paths:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
    return newest, missing


def _find_stale(repo_root: Path, knowledge_dir: Path) -> list[dict[str, Any]]:
    stale: list[dict[str, Any]] = []
    if not knowledge_dir.is_dir():
        return stale
    for path in sorted(knowledge_dir.glob("*.md")):
        text = _read_text(path)
        meta, _ = _parse_frontmatter(text)
        reasons: list[str] = []
        if not meta:
            reasons.append("missing YAML frontmatter")
        sources = meta.get("source_files") or []
        last_verified_raw = str(meta.get("last_verified") or "")
        last_verified: date | None = None
        if DATE_RE.match(last_verified_raw):
            try:
                last_verified = date.fromisoformat(last_verified_raw)
            except ValueError:
                last_verified = None
        if last_verified is None:
            reasons.append("missing or invalid last_verified")
        if not sources:
            reasons.append("no source_files declared")
        newest, missing = _newest_mtime(repo_root, sources)
        if missing:
            reasons.append("missing source(s): " + ", ".join(missing[:4]))
        if newest is not None and last_verified is not None:
            newest_date = datetime.fromtimestamp(newest).date()
            if newest_date > last_verified:
                reasons.append(
                    f"source newer than last_verified ({newest_date.isoformat()} > "
                    f"{last_verified.isoformat()})"
                )
        if reasons:
            stale.append({"path": path, "reasons": reasons})
    return stale


def _render_stale(stale: list[dict[str, Any]]) -> str:
    lines = [
        "# Stale Knowledge",
        "",
        f"Generated by `.agent/hooks/post-session.py` at {datetime.now().isoformat(timespec='seconds')}.",
        "",
        "**Do not trust these files without checking source.** AGENT.md must read this list first.",
        "",
        "| Knowledge file | Why it is stale |",
        "|---|---|",
    ]
    for entry in stale:
        rel = entry["path"].relative_to(REPO_ROOT).as_posix()
        reason = "; ".join(entry["reasons"]).replace("|", "\\|")
        lines.append(f"| `{rel}` | {reason} |")
    lines.append("")
    lines.append("Refresh steps: read the changed source files, update the knowledge file, bump")
    lines.append("`last_verified`, then re-run the hook. See `06-recent-changes-and-decisions.md`.")
    lines.append("")
    return "\n".join(lines)


def _collect_source_text(repo_root: Path, sources: Iterable[str], budget: int) -> str:
    chunks: list[str] = []
    used = 0
    for raw in sources:
        for path in _source_paths(repo_root, raw):
            if used >= budget:
                break
            try:
                text = _read_text(path, MAX_CHARS_PER_SOURCE)
            except OSError:
                continue
            if not text:
                continue
            rel = path.relative_to(repo_root).as_posix()
            chunk = f"### {rel}\n{text}\n"
            remaining = budget - used
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            chunks.append(chunk)
            used += len(chunk)
        if used >= budget:
            break
    return "".join(chunks)


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3].rstrip()
    return cleaned


def _set_last_verified(text: str, value: str) -> str:
    if re.search(r"(?m)^last_verified:\s*.*$", text):
        return re.sub(r"(?m)^last_verified:\s*.*$", f"last_verified: {value}", text, count=1)
    if text.startswith("---"):
        return text.replace("---", f"---\nlast_verified: {value}", 1)
    return text


def _refresh_via_api(
    stale: list[dict[str, Any]],
    api_key: str,
    api_url: str,
    api_model: str,
    dry_run: bool,
    verbose: bool,
) -> list[str]:
    """Return human-readable outcomes for each attempted refresh."""
    outcomes: list[str] = []
    for entry in stale[:MAX_REFRESH_FILES]:
        path: Path = entry["path"]
        rel = path.relative_to(REPO_ROOT).as_posix()
        current = _read_text(path)
        meta, _ = _parse_frontmatter(current)
        sources = meta.get("source_files") or []
        source_text = _collect_source_text(REPO_ROOT, sources, MAX_SOURCE_CHARS)
        prompt = (
            "You maintain a repository knowledge base. Update the markdown document below so it "
            "matches the current source. Return the COMPLETE updated markdown file and nothing "
            "else. Preserve the YAML frontmatter, keep the existing structure and line-number "
            "citations accurate, remove claims that are no longer true, and do not invent facts. "
            "Do not use code fences around the whole document.\n\n"
            f"CURRENT DOCUMENT ({rel}):\n{current}\n\n"
            f"CURRENT SOURCE EXCERPTS:\n{source_text}\n"
        )
        payload = {
            "model": api_model,
            "temperature": 0.1,
            "max_tokens": 8000,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a precise code-documentation maintainer. Output only markdown.",
                },
                {"role": "user", "content": prompt},
            ],
        }
        if dry_run:
            outcomes.append(f"{rel}: would refresh via {api_model}")
            _log(verbose, f"dry-run: would call {api_url} for {rel}")
            continue
        request = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/winbrow",
                "X-Title": "WinBrow Knowledge Hook",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as response:
                raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            updated = _strip_fences(str(content or ""))
            if len(updated) < 500 or not updated.lstrip().startswith("---"):
                outcomes.append(f"{rel}: rejected API response (too short or missing frontmatter)")
                continue
            updated = _set_last_verified(updated, date.today().isoformat())
            _atomic_write(path, updated)
            outcomes.append(f"{rel}: refreshed via {api_model}")
            _log(verbose, f"refreshed {rel}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as exc:
            outcomes.append(f"{rel}: refresh failed ({exc})")
            _log(verbose, f"refresh failed for {rel}: {exc}")
    return outcomes


def _git_status() -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return lines[:MAX_LOG_FILES]


def _payload_summary(payload: dict[str, Any]) -> str:
    for key in ("summary", "last_message", "message", "reason", "hook_event_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.strip().split())[:600]
    return "Session ended (no summary provided)."


def _append_session_log(
    summary: str,
    changed: list[str],
    stale_count: int,
    outcomes: list[str],
    dry_run: bool,
    verbose: bool,
) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    changed_text = ", ".join(f"`{item}`" for item in changed) if changed else "none"
    outcome_text = "; ".join(outcomes) if outcomes else "not needed"
    entry = (
        f"\n## {timestamp} - session\n\n"
        f"- **Summary:** {summary}\n"
        f"- **Changed files:** {changed_text}\n"
        f"- **Knowledge:** {len(list(KNOWLEDGE_DIR.glob('*.md')))} file(s) present; "
        f"{stale_count} stale\n"
        f"- **Auto-refresh:** {outcome_text}\n"
    )
    if dry_run:
        print(entry)
        _log(verbose, f"dry-run: would append to {SESSION_LOG}")
        return
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    if not SESSION_LOG.exists():
        _atomic_write(
            SESSION_LOG,
            "# Session Log\n\nAppend-only. The `Stop` hook adds one entry per session.\n",
        )
    with SESSION_LOG.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(entry)


def _read_payload() -> dict[str, Any]:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WinBrow post-session hook")
    parser.add_argument("--dry-run", action="store_true", help="print actions without writing")
    parser.add_argument("--verbose", action="store_true", help="log decisions to stderr")
    parser.add_argument("--no-api", action="store_true", help="skip the agent API refresh")
    args = parser.parse_args(argv)

    try:
        payload = _read_payload()
        summary = _payload_summary(payload)
        changed = _git_status()

        stale = _find_stale(REPO_ROOT, KNOWLEDGE_DIR)
        _log(args.verbose, f"stale knowledge files: {len(stale)}")
        for entry in stale:
            _log(args.verbose, f"  {entry['path'].name}: {'; '.join(entry['reasons'])}")

        outcomes: list[str] = []
        api_key = (os.environ.get("AGENT_API_KEY") or "").strip()
        if stale and not args.no_api:
            if not api_key:
                outcomes.append("skipped: AGENT_API_KEY is not set")
                _log(args.verbose, "AGENT_API_KEY not set; leaving stale.md for the next session")
            else:
                api_url = os.environ.get("AGENT_API_URL", DEFAULT_API_URL)
                api_model = os.environ.get("AGENT_API_MODEL", DEFAULT_API_MODEL)
                outcomes.extend(
                    _refresh_via_api(
                        stale, api_key, api_url, api_model, args.dry_run, args.verbose
                    )
                )
                if not args.dry_run:
                    stale = _find_stale(REPO_ROOT, KNOWLEDGE_DIR)
        elif not stale:
            outcomes.append("not needed: no stale knowledge")

        if stale:
            content = _render_stale(stale)
            if args.dry_run:
                _log(args.verbose, f"dry-run: would write {STALE_FILE}")
                print(content)
            else:
                _atomic_write(STALE_FILE, content)
        elif STALE_FILE.exists() and not args.dry_run:
            try:
                STALE_FILE.unlink()
                _log(args.verbose, "removed stale.md (no stale knowledge)")
            except OSError as exc:
                _log(args.verbose, f"could not remove stale.md: {exc}")

        _append_session_log(
            summary, changed, len(stale), outcomes, args.dry_run, args.verbose
        )
        return 0
    except Exception as exc:  # never fail the session
        print(f"[post-session] non-fatal failure: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
