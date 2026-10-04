"""
Argument Resolvers — Compute and Validate, Never Parse-to-Decide
=================================================================
This module is the only place that turns model-chosen option ids into real
argument values. Rules:

- Option lists are built from the machine (known folders, files on disk,
  running/installed apps) or from contiguous token spans of the utterance.
- `validate_option_id` rejects any id that was not offered. Unknown ids
  never reach a script.
- Resolvers map ids/values to concrete paths and clamp numbers; they never
  read the sentence to pick a tool.

Text-arg sourcing per tool (static config, not phrase matching):
  FOLDER_ARGS: (tool, arg) pairs resolved from folder option lists.
  FILE_ARGS:   (tool, arg) pairs resolved from file option lists.
  APP_ARGS:    (tool, arg) pairs resolved from app option lists.
  Remaining text args resolve from span choices.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from . import files as filebox

log = logging.getLogger("winbrow.resolvers")

MAX_OPTIONS = 25
MAX_SPANS = 40
MAX_SPAN_TOKENS = 6

FOLDER_ARGS = {
    ("open_folder", "folder"),
    ("close_folder_window", "folder"),
    ("take_screenshot", "location"),
}

FILE_ARGS = {
    ("open_file", "target"),
    ("set_wallpaper", "image"),
    ("find_files", "search_term"),
}

APP_ARGS = {
    ("open_file", "app"),
    ("app_focus_or_launch", "app_name"),
    ("kill_unresponsive_process", "target"),
    ("open_common_app", "app"),
}

# Tools that change state: read back before running, require explicit yes.
CONFIRM_TOOLS = frozenset({
    "organize_desktop",
    "clean_temp_files",
    "clean_downloads",
    "kill_unresponsive_process",
    "shutdown_timer",
})

# Extra safety: tools disabled entirely unless explicitly enabled.
# Comma-separated names in WINBROW_DISABLED_TOOLS, e.g. "shutdown_timer".
RISKY_DEFAULT_OFF = frozenset({"shutdown_timer"})


def enabled_tools() -> set[str]:
    raw = os.environ.get("WINBROW_ENABLED_TOOLS", "")
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def is_tool_disabled(tool_name: str) -> bool:
    """Explicit disable list wins; risky-off-by-default tools need opt-in."""
    name = (tool_name or "").lower()
    if name in disabled_tools():
        return True
    if name in RISKY_DEFAULT_OFF and name not in enabled_tools():
        return True
    return False


def requires_confirmation(tool_name: str, is_learned: bool = False) -> bool:
    """State-changing tools (and all generated tools) need an explicit yes."""
    if is_learned:
        return True
    return (tool_name or "").lower() in CONFIRM_TOOLS


# -- option builders ---------------------------------------------------

def folder_options(memory=None) -> list[dict[str, Any]]:
    """[{id, label, path}] from known folders + last-opened folder."""
    opts = [
        {"id": f"folder:{nick}", "label": f"{nick} ({path})", "path": path}
        for nick, path in sorted(filebox.known_folder_paths().items())
    ]
    if memory and getattr(memory, "last_folder", None):
        last = memory.last_folder
        if last and all(o["path"] != last for o in opts):
            opts.append({"id": "folder:@last", "label": f"last folder ({last})", "path": last})
    return opts[:MAX_OPTIONS]


def file_options(memory=None, exts=None, limit: int = MAX_OPTIONS) -> list[dict[str, Any]]:
    """[{id, label, path}] from the last-opened folder, else Downloads."""
    roots: list[str] = []
    if memory and getattr(memory, "last_folder", None) and memory.last_folder:
        roots.append(memory.last_folder)
    dl = filebox._special_folder("downloads")
    if dl:
        roots.append(dl)
    opts: list[dict[str, Any]] = []
    for root in roots:
        for f in filebox.list_files(root, exts=exts, limit=limit):
            if all(o["path"] != f["path"] for o in opts):
                opts.append({"id": f["id"], "label": f"{f['name']} ({f['path']})", "path": f["path"]})
            if len(opts) >= limit:
                return opts
    return opts


def app_options(running: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """[{id, label, name}] from running processes, then installed apps."""
    opts: list[dict[str, Any]] = []
    for name in (running or filebox.running_apps_simple()):
        if all(o["name"] != name for o in opts):
            opts.append({"id": f"app:{name}", "label": name, "name": name})
        if len(opts) >= MAX_OPTIONS:
            return opts
    for name in filebox.installed_apps(limit=MAX_OPTIONS):
        if all(o["name"].lower() != str(name).lower() for o in opts):
            opts.append({"id": f"app:{name}", "label": str(name), "name": str(name)})
        if len(opts) >= MAX_OPTIONS:
            break
    return opts


def span_options(utterance: str) -> list[dict[str, Any]]:
    """Candidate contiguous token spans: [{id, label, value}].

    Built mechanically (all n-grams up to MAX_SPAN_TOKENS, capped); the
    model chooses one. No filtering by meaning — that is the model's job.
    """
    import re as _re
    tokens = _re.findall(r"[A-Za-z0-9]+(?:[._\-][A-Za-z0-9]+)*", utterance or "")
    opts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for size in range(1, min(MAX_SPAN_TOKENS, len(tokens)) + 1):
        for i in range(len(tokens) - size + 1):
            value = " ".join(tokens[i:i + size])
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            opts.append({"id": f"span:{i}:{i + size}", "label": value, "value": value})
            if len(opts) >= MAX_SPANS:
                return opts
    return opts


# -- validation + resolution --------------------------------------------

def validate_option_id(option_id: str, offered: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return the offered option with this id, else None (reject unknowns)."""
    for opt in offered:
        if opt.get("id") == option_id:
            return opt
    log.warning(f"Rejecting unoffered option id: {option_id!r}")
    return None


def resolve_folder(value: str) -> Optional[str]:
    """Nickname, ~/path, absolute path, or on-disk name → directory or None."""
    ref = (value or "").strip().strip('"').strip("'")
    if not ref:
        return None
    known = filebox.known_folder_paths()
    if ref.lower() in known:
        return known[ref.lower()]
    if ref.startswith("~"):
        cand = os.path.join(filebox.user_home(), ref[1:].lstrip("\\/"))
        return cand if os.path.isdir(cand) else None
    if os.path.isdir(ref):
        return os.path.abspath(ref)
    from .router import _folder_exists_on_disk  # local import: router owns the scanner
    found = _folder_exists_on_disk(ref)
    return found if found and os.path.isdir(found) else None


def resolve_file(value: str, search_roots: Optional[list[str]] = None) -> Optional[str]:
    """Filename or path → existing file path or None."""
    ref = (value or "").strip().strip('"').strip("'")
    if not ref:
        return None
    if ref.startswith("~"):
        cand = os.path.join(filebox.user_home(), ref[1:].lstrip("\\/"))
        return cand if os.path.isfile(cand) else None
    return filebox.find_file_by_name(ref, search_roots=search_roots)


def clamp_volume(value: str) -> Optional[float]:
    """Free text → 0–100 volume level or None (delegates to windows parser)."""
    from .windows import _parse_volume_level
    try:
        parsed = _parse_volume_level(value)
    except Exception:
        return None
    if parsed is None:
        return None
    return max(0.0, min(100.0, float(parsed)))


def preview_action(tool_name: str, args: dict[str, str]) -> str:
    """One-paragraph read-back of what a state-changing run will do."""
    shown = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items()) or "no arguments"
    return (
        f"This will run '{tool_name}' with {shown}. "
        f"Reply yes to confirm, or anything else to cancel."
    )
