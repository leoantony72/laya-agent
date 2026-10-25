"""
Skill Writer — turn a completed task into a reusable SKILL.md
=============================================================
This is a separate brain from the task-execution loop. It receives one
completed task (goal + step history) and answers a single question:

    Is this workflow worth saving as a reusable skill, and if so, what is
    the general version of it?

It never invents a new capability and it never writes a skill for one
specific file, site, person or search query. The output is a ``SKILL.md``
folder that the existing progressive-disclosure registry can load on the
next turn.

The model call uses the same ``ModelGateway`` instance as the rest of the
brain, so no separate provider, key or model configuration is required.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import textwrap
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from .model_gateway import (
    GatewayError,
    GatewayUnavailable,
    ModelGateway,
    extract_json_object,
    get_gateway,
)
from .skill_registry import (
    SkillRegistryError,
    SkillMeta,
    build_skills_metadata,
    load_skill_file,
)

log = logging.getLogger("winbrow.brain.skill_writer")

SKILL_WRITER_MAX_TOKENS = 2000
SKILL_WRITER_TEMPERATURE = 0.1
MIN_REUSABLE_STEPS = 3
MAX_DESCRIPTION_WORDS = 60

_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SNAKE_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_FILENAME_RE = re.compile(
    r"\b[A-Za-z0-9][\w .\-]{0,80}\."
    r"(?:pptx?|docx?|xlsx?|pdf|txt|csv|md|json|png|jpe?g|gif|zip|"
    r"py|js|ts|html?|xml|epub)\b",
    re.I,
)
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/][^\s\"']+|\\\\[^\s\"']+)")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{2,}\b")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Words that describe the TYPE of workflow rather than one instance of it.
# They are allowed in a generated skill name even when they also appear in the
# goal or a URL. Anything else that appears in the candidate name AND in the
# goal/history is treated as a leaked specific value.
_GENERIC_SKILL_TOKENS = frozenset({
    "add", "analyze", "analyse", "app", "application", "attach", "backup",
    "bookmark", "brightness", "browser", "build", "calculate", "capture",
    "channel", "check", "chrome", "clean", "click", "clipboard", "close",
    "command", "comment", "compare", "compress", "configure", "convert",
    "copy", "create", "delete", "desktop", "directory", "discord",
    "document", "download", "downloads", "drag", "drive", "drop", "edge",
    "edit", "email", "enter", "excel", "explorer", "extract", "file",
    "files", "fill", "find", "firefox", "focus", "folder", "follow",
    "form", "format", "from", "gmail", "google", "github", "history",
    "hotkey", "image", "input", "inspect", "install", "interact", "item",
    "keyboard", "launch", "light", "link", "list", "local", "lock", "login",
    "logout", "maximize", "memory", "merge", "message", "minimize",
    "monitor", "mouse", "move", "mute", "navigate", "notepad", "note",
    "notes", "ocr", "office", "online", "open", "organize", "page", "paint",
    "paper", "password", "paste", "pause", "pdf", "photoshop", "play",
    "post", "powerpoint", "preferences", "press", "print", "profile",
    "read", "recall", "record", "remember", "remove", "rename", "report",
    "reset", "resume", "run", "save", "scan", "schedule", "screen",
    "screenshot", "scroll", "search", "select", "send", "service",
    "settings", "share", "sheet", "shortcut", "sign", "site", "skill",
    "slack", "sort", "speak", "split", "spotify", "start", "stop",
    "submit", "subscribe", "summary", "summarize", "switch", "sync",
    "telegram", "text", "theme", "tool", "transcribe", "translate", "type",
    "uninstall", "unlock", "unmute", "update", "upload", "url", "validate",
    "verify", "viewer", "volume", "vscode", "wait", "wallpaper", "web",
    "website", "window", "word", "write", "youtube", "zoom",
})

_SKIP_BRAND_TOKENS = frozenset({
    "http", "https", "www", "com", "org", "net", "app", "apps", "html",
    "index", "products", "search", "watch", "view", "viewer", "en", "us",
    "results", "result", "page", "pages", "user", "users", "profile",
})


SKILL_WRITER_PROMPT = """
IDENTITY
You are the skill architect for winbrow. You analyse completed task
histories and extract reusable workflow patterns. You never write skills for
the task-execution brain — you write them for the next human who asks for the
same TYPE of task.

THE GENERALITY TEST — apply before deciding to save
A skill is worth saving only if all four are true:
  1. REUSABLE: the same sequence of steps would apply to a different
     instance of this task type. "Upload a file to an online viewer" is
     reusable. "Upload LDM seminar.pptx to Aspose" is not.
  2. PARAMETERISED: every instance-specific value (filename, URL, search
     term, folder name) becomes an arg with a clear description. Nothing is
     hardcoded in the step pattern.
  3. NON-TRIVIAL: the task took more than 2 steps and the sequence is not
     obvious. Single-step tasks do not need skills.
  4. STABLE: the sequence will work on the next run without changes. If
     success depended on a page layout that may change, do not save.
If any of the four fail, output should_save=false.

SPECIFICITY RULES — the most important section
These patterns make a skill too specific. Reject any skill that contains
them:
  - A filename, document title or paper name in the skill name, args,
    description or step pattern ("LDM seminar", "attention.pdf",
    "budget_q3.xlsx")
  - A person's name or channel name hardcoded ("PewDiePie", "leoantony72")
  - A specific search query hardcoded ("transformer architecture 2024")
  - A specific website chosen for a non-generic reason ("Aspose because it
    was the first result")
  - A step that only works on one site's exact DOM structure without being
    expressed as a pattern

GOOD skill name:  upload-file-to-online-viewer
BAD skill name:   upload-ldmseminar-to-aspose
GOOD skill name:  open-youtube-channel
BAD skill name:   open-pewdiepie-channel
GOOD skill name:  find-and-move-file
BAD skill name:   move-attention-paper-to-desktop-papers

If any specific value appears anywhere in the draft skill, replace it with
an arg placeholder or reject the skill entirely.

ARGS EXTRACTION RULES
Every value that varied in this specific task becomes an arg. Work through
the history and ask: "if someone asked for the same TYPE of task tomorrow
with different inputs, what would change?" Those are the args.
Common args to extract:
  file_path, file_name, destination_folder, url, search_query, entity_name,
  service_name, channel_handle

Use snake_case arg names. Every arg must have a short description that says
what value belongs there, not the value from this run. Mark an arg required
only when the workflow cannot run without it.

OUTPUT FORMAT — strict JSON, one object:
{
  "should_save": true | false,
  "reason": "...",
  "name": "kebab-case-folder-name",
  "skill_key": "snake_case_dispatcher_key",
  "description": "60 words max, complete sentences...",
  "args": [
    {
      "name": "file_path",
      "type": "string",
      "required": true,
      "default": null,
      "description": "Absolute path to the file to upload."
    }
  ],
  "when_to_use": "...",
  "when_not_to_use": "...",
  "step_pattern": "...",
  "recovery": "..."
}
If should_save is false, all other fields except reason may be empty
strings.

The description is what the planner sees every turn. It must say when to
use the skill and when not to use it, in complete sentences, without any
value from this specific run. The step_pattern must be a numbered sequence
that uses the arg names as placeholders. The recovery must describe what to
do when a step fails, without naming this run's page or file.
""".strip()


SKILL_WRITER_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "should_save": {"type": "boolean"},
        "reason": {"type": "string"},
        "name": {"type": "string"},
        "skill_key": {"type": "string"},
        "description": {"type": "string"},
        "args": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "required": {"type": "boolean"},
                    "default": {
                        "type": ["string", "number", "boolean", "null"]
                    },
                    "description": {"type": "string"},
                },
                "required": ["name", "type", "required", "description"],
            },
        },
        "when_to_use": {"type": "string"},
        "when_not_to_use": {"type": "string"},
        "step_pattern": {"type": "string"},
        "recovery": {"type": "string"},
    },
    "required": [
        "should_save", "reason", "name", "skill_key", "description",
        "args", "when_to_use", "when_not_to_use", "step_pattern", "recovery",
    ],
}


@dataclass
class SkillCandidate:
    should_save: bool
    reason: str
    name: str = ""
    skill_key: str = ""
    description: str = ""
    args: list[dict] = field(default_factory=list)
    when_to_use: str = ""
    when_not_to_use: str = ""
    step_pattern: str = ""
    recovery: str = ""


# ---------------------------------------------------------------------------
# Normalisation and guards
# ---------------------------------------------------------------------------

def _normalise_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return name[:60].strip("-")


def _normalise_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return key[:60].strip("_")


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _ensure_period(text: str) -> str:
    text = " ".join((text or "").split())
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _limit_words(text: str, limit: int = MAX_DESCRIPTION_WORDS) -> str:
    text = _ensure_period(text)
    if len(text.split()) <= limit:
        return text
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s]
    chosen = ""
    for sentence in sentences:
        candidate = f"{chosen} {sentence}".strip()
        if chosen and len(candidate.split()) > limit:
            break
        chosen = candidate
    if not chosen:
        chosen = " ".join(text.split()[:limit])
    if len(chosen.split()) > limit:
        chosen = " ".join(chosen.split()[:limit])
    return _ensure_period(chosen)


def _step_dicts(history: Iterable[Any]) -> list[dict]:
    steps: list[dict] = []
    for entry in history or []:
        if hasattr(entry, "to_dict"):
            try:
                item = entry.to_dict()
            except Exception:
                continue
        elif isinstance(entry, dict):
            item = dict(entry)
        else:
            item = {
                "skill": getattr(entry, "skill", ""),
                "args": getattr(entry, "args", {}) or {},
                "success": getattr(entry, "success", False),
                "output": getattr(entry, "output", ""),
            }
        if not isinstance(item, dict):
            continue
        steps.append(item)
    return steps


def _executed_steps(history: Iterable[Any]) -> list[dict]:
    return [
        step for step in _step_dicts(history)
        if not str(step.get("skill") or "").startswith("_")
    ]


def _successful_step_count(history: Iterable[Any]) -> int:
    return sum(1 for step in _executed_steps(history) if step.get("success"))


def _format_history(history: Iterable[Any], max_chars: int = 12000) -> str:
    lines: list[str] = []
    for index, step in enumerate(_step_dicts(history), 1):
        skill = str(step.get("skill") or "?")
        args = step.get("args") or {}
        try:
            args_text = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            args_text = str(args)
        success = "success" if step.get("success") else "FAILED"
        output = " ".join(str(step.get("output") or "").split())[:500]
        lines.append(
            f"{index}. {skill} args={args_text[:600]} [{success}] "
            f"output={output}"
        )
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[history truncated]"
    return text


def _extract_specific_tokens(text: str) -> set[str]:
    """Return compact forms of filenames/paths that must never be hardcoded."""
    tokens: set[str] = set()
    for match in _FILENAME_RE.finditer(text or ""):
        filename = match.group(0)
        stem = filename.rsplit(".", 1)[0]
        for value in (stem, filename):
            compact = _compact(value)
            if len(compact) >= 4:
                tokens.add(compact)
    for match in _ABS_PATH_RE.finditer(text or ""):
        raw = match.group(0).rstrip("\\/")
        base = re.split(r"[\\/]", raw)[-1]
        for value in (base, base.rsplit(".", 1)[0]):
            compact = _compact(value)
            if len(compact) >= 4:
                tokens.add(compact)
    return tokens


def _url_brand_tokens(text: str) -> set[str]:
    """Specific host/path tokens from URLs, e.g. aspose or pewdiepie."""
    tokens: set[str] = set()
    for match in _URL_RE.finditer(text or ""):
        try:
            parts = urlsplit(match.group(0))
        except ValueError:
            continue
        host = (parts.hostname or "").lower()
        values = re.split(r"[.\-]+", host)
        values.extend(re.split(r"[^A-Za-z0-9]+", parts.path or ""))
        for part in values:
            lowered = part.lower()
            if (
                len(lowered) >= 4
                and lowered not in _GENERIC_SKILL_TOKENS
                and lowered not in _SKIP_BRAND_TOKENS
            ):
                tokens.add(lowered)
    return tokens


def _proper_noun_tokens(goal: str) -> set[str]:
    """Capitalised entity names from the goal (PewDiePie, LDM, Aspose)."""
    tokens: set[str] = set()
    for match in _PROPER_NOUN_RE.finditer(goal or ""):
        token = match.group(0).lower()
        if token not in _GENERIC_SKILL_TOKENS:
            tokens.add(token)
    return tokens


def _candidate_entity_tokens(candidate: SkillCandidate) -> set[str]:
    raw = f"{candidate.name} {candidate.skill_key}"
    return {
        token for token in re.split(r"[^a-z0-9]+", raw.lower())
        if len(token) >= 4
    }


def _specificity_violations(
    candidate: SkillCandidate, goal: str, history_text: str,
) -> list[str]:
    fields = {
        "name": candidate.name,
        "skill_key": candidate.skill_key,
        "description": candidate.description,
        "when_to_use": candidate.when_to_use,
        "when_not_to_use": candidate.when_not_to_use,
        "step_pattern": candidate.step_pattern,
        "recovery": candidate.recovery,
    }
    combined = " ".join(fields.values())
    violations: list[str] = []

    filename = _FILENAME_RE.search(combined)
    if filename:
        violations.append(f"hardcoded filename '{filename.group(0)}'")
    path = _ABS_PATH_RE.search(combined)
    if path:
        violations.append(f"hardcoded path '{path.group(0)[:80]}'")

    compact_combined = _compact(combined)
    for token in sorted(_extract_specific_tokens(history_text)):
        if token in compact_combined:
            violations.append(f"leaked value from this run '{token}'")

    entity_pool = (
        _proper_noun_tokens(goal)
        | _url_brand_tokens(history_text)
        | _extract_specific_tokens(history_text)
    )
    for token in sorted(_candidate_entity_tokens(candidate)):
        if token in _GENERIC_SKILL_TOKENS:
            continue
        if token in entity_pool:
            violations.append(f"hardcoded specific value '{token}'")

    return list(dict.fromkeys(violations))


def _duplicate_reason(
    candidate: SkillCandidate, existing_skill_names: Iterable[str],
) -> str:
    existing = {_compact(str(name)) for name in existing_skill_names or []}
    for value in (candidate.name, candidate.skill_key):
        if _compact(value) in existing:
            return f"skill '{value}' already exists in the registry"
    return ""


def _candidate_from_payload(data: dict) -> SkillCandidate:
    raw_args = data.get("args") or []
    args: list[dict] = []
    if isinstance(raw_args, list):
        for item in raw_args:
            if not isinstance(item, dict):
                continue
            name = _normalise_key(str(item.get("name") or ""))
            if not name:
                continue
            arg_type = str(item.get("type") or "string").strip().lower()
            if arg_type not in {
                "string", "bool", "int", "float", "number", "enum", "text",
            }:
                arg_type = "string"
            required = item.get("required", False)
            if not isinstance(required, bool):
                required = str(required).strip().lower() == "true"
            args.append({
                "name": name,
                "type": arg_type,
                "required": required,
                "default": item.get("default"),
                "description": " ".join(
                    str(item.get("description") or "").split()
                ),
            })
    return SkillCandidate(
        should_save=bool(data.get("should_save", False)),
        reason=" ".join(str(data.get("reason") or "").split()),
        name=_normalise_name(str(data.get("name") or "")),
        skill_key=_normalise_key(str(data.get("skill_key") or "")),
        description=" ".join(str(data.get("description") or "").split()),
        args=args,
        when_to_use=str(data.get("when_to_use") or "").strip(),
        when_not_to_use=str(data.get("when_not_to_use") or "").strip(),
        step_pattern=str(data.get("step_pattern") or "").strip(),
        recovery=str(data.get("recovery") or "").strip(),
    )


def _apply_guards(
    candidate: SkillCandidate,
    goal: str,
    history: Iterable[Any],
    existing_skill_names: Iterable[str],
) -> SkillCandidate:
    if not candidate.should_save:
        return candidate

    if _successful_step_count(history) < MIN_REUSABLE_STEPS:
        return replace(
            candidate,
            should_save=False,
            reason=(
                "task has fewer than three successful steps; "
                "single-step and two-step tasks are not worth a skill"
            ),
        )

    candidate = replace(
        candidate,
        name=_normalise_name(candidate.name or candidate.skill_key),
        skill_key=_normalise_key(candidate.skill_key or candidate.name),
        description=_limit_words(candidate.description),
    )
    if not _KEBAB_RE.match(candidate.name or ""):
        return replace(
            candidate, should_save=False,
            reason="skill name is not valid kebab-case",
        )
    if not _SNAKE_RE.match(candidate.skill_key or ""):
        return replace(
            candidate, should_save=False,
            reason="skill key is not valid snake_case",
        )

    missing = [
        label for label, value in (
            ("description", candidate.description),
            ("when_to_use", candidate.when_to_use),
            ("when_not_to_use", candidate.when_not_to_use),
            ("step_pattern", candidate.step_pattern),
            ("recovery", candidate.recovery),
        )
        if not str(value or "").strip()
    ]
    if missing:
        return replace(
            candidate,
            should_save=False,
            reason="skill is missing required section(s): " + ", ".join(missing),
        )

    duplicate = _duplicate_reason(candidate, existing_skill_names)
    if duplicate:
        return replace(candidate, should_save=False, reason=duplicate)

    history_text = _format_history(history)
    violations = _specificity_violations(candidate, goal, history_text)
    if violations:
        return replace(
            candidate,
            should_save=False,
            reason="specificity check failed: " + "; ".join(violations),
        )
    return candidate


# ---------------------------------------------------------------------------
# Analysis call
# ---------------------------------------------------------------------------

async def analyse_task_for_skill(
    goal: str,
    history: list[dict],
    existing_skill_names: list[str],
    gateway: Optional[ModelGateway] = None,
) -> SkillCandidate:
    """Ask the skill-writer brain whether this completed task is reusable.

    The call uses the existing gateway/model; no separate provider or key is
    configured. A task with fewer than three successful steps is rejected
    locally without spending a model call.
    """
    if _successful_step_count(history) < MIN_REUSABLE_STEPS:
        return SkillCandidate(
            should_save=False,
            reason=(
                "task has fewer than three successful steps; "
                "single-step and two-step tasks are not worth a skill"
            ),
        )

    gw = gateway or get_gateway()
    if gw is None or not getattr(gw, "available", True):
        return SkillCandidate(
            should_save=False,
            reason="no model provider is configured for skill analysis",
        )

    existing = ", ".join(sorted({str(name) for name in existing_skill_names}))
    messages = [
        {"role": "system", "content": SKILL_WRITER_PROMPT},
        {
            "role": "user",
            "content": (
                f"GOAL:\n{goal}\n\n"
                f"COMPLETED TASK HISTORY:\n{_format_history(history)}\n\n"
                f"EXISTING SKILLS (do not duplicate these):\n"
                f"{existing or '(none)'}\n\n"
                "Decide whether this task type is reusable. Return ONLY the "
                "JSON object described in the system prompt."
            ),
        },
    ]

    try:
        response = await gw.chat(
            messages,
            json_schema=SKILL_WRITER_JSON_SCHEMA,
            temperature=SKILL_WRITER_TEMPERATURE,
            max_tokens=SKILL_WRITER_MAX_TOKENS,
        )
    except (GatewayUnavailable, GatewayError) as exc:
        log.warning(f"skill_writer: model call failed — {exc}")
        return SkillCandidate(
            should_save=False,
            reason=f"skill analysis model call failed: {exc}",
        )
    except Exception as exc:
        log.warning(f"skill_writer: model call raised — {exc}")
        return SkillCandidate(
            should_save=False,
            reason=f"skill analysis model call raised: {exc}",
        )

    data = extract_json_object(getattr(response, "content", "") or "")
    if not isinstance(data, dict):
        log.warning("skill_writer: model returned no usable JSON object")
        return SkillCandidate(
            should_save=False,
            reason="skill analysis returned no usable JSON object",
        )

    return _apply_guards(
        _candidate_from_payload(data),
        goal,
        history,
        existing_skill_names,
    )


# ---------------------------------------------------------------------------
# SKILL.md rendering and writing
# ---------------------------------------------------------------------------

def _fold_yaml(text: str, width: int = 76) -> str:
    lines = textwrap.wrap(" ".join((text or "").split()), width=width)
    return "\n".join("  " + line for line in lines)


def _yaml_scalar(value: Any) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def _render_args_frontmatter(args: list[dict]) -> str:
    if not args:
        return "args: []"
    lines = ["args:"]
    for arg in args:
        lines.append(f"  - name: {_yaml_scalar(arg.get('name'))}")
        lines.append(f"    type: {_yaml_scalar(arg.get('type') or 'string')}")
        required = "true" if arg.get("required") else "false"
        lines.append(f"    required: {required}")
        lines.append(f"    default: {json.dumps(arg.get('default'))}")
        lines.append(
            f"    description: {_yaml_scalar(arg.get('description') or '')}"
        )
    return "\n".join(lines)


def _render_args_detail(args: list[dict]) -> str:
    if not args:
        return "This skill takes no arguments."
    lines = []
    for arg in args:
        marker = "required" if arg.get("required") else \
            f"default={arg.get('default')!r}"
        lines.append(
            f"- `{arg.get('name')}` ({arg.get('type') or 'string'}, {marker}): "
            f"{arg.get('description') or 'No description.'}"
        )
    return "\n".join(lines)


def render_skill_md(candidate: SkillCandidate) -> str:
    """Render the complete SKILL.md for a validated candidate."""
    description = _limit_words(candidate.description)
    when_to_use = _ensure_period(candidate.when_to_use)
    when_not = _ensure_period(candidate.when_not_to_use)
    step_pattern = candidate.step_pattern.strip()
    recovery = _ensure_period(candidate.recovery)

    return f"""---
name: {_yaml_scalar(candidate.name)}
skill: {_yaml_scalar(candidate.skill_key)}
{_render_args_frontmatter(candidate.args)}
description: >
{_fold_yaml(description)}
---

## When to Use

{when_to_use}

## When NOT to Use

{when_not}

## Step Pattern

{step_pattern}

## Args Detail

{_render_args_detail(candidate.args)}

## Output Shape

Returns a result dict with `success`, `output`, and any state fields the
chosen tool provides. A successful step means the tool dispatched the action;
it does not by itself prove the wider goal is complete. Verify with the next
read step before declaring the task done.

## Recovery

{recovery}
"""


async def write_skill_to_disk(
    candidate: SkillCandidate,
    skills_dir: str,
) -> str:
    """Write ``skills_dir/<name>/SKILL.md`` and return its absolute path.

    Refuses to overwrite an existing folder so a hand-crafted skill is never
    replaced by a generated one.
    """
    if not candidate.should_save:
        raise ValueError("refusing to write a skill with should_save=False")
    if not _KEBAB_RE.match(candidate.name or ""):
        raise ValueError(f"invalid skill folder name: {candidate.name!r}")
    if not _SNAKE_RE.match(candidate.skill_key or ""):
        raise ValueError(f"invalid skill key: {candidate.skill_key!r}")

    root = Path(skills_dir).expanduser().resolve()
    target_dir = root / candidate.name
    if target_dir.exists():
        raise FileExistsError(
            f"skill folder already exists, refusing to overwrite: {target_dir}"
        )

    target_dir.mkdir(parents=True, exist_ok=False)
    target = target_dir / "SKILL.md"
    content = render_skill_md(candidate)

    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target_dir,
            prefix=".SKILL-", suffix=".md", delete=False,
        ) as handle:
            handle.write(content)
            temp_path = Path(handle.name)
        os.replace(temp_path, target)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            target_dir.rmdir()
        except OSError:
            pass
        raise

    return str(target.resolve())


async def register_skill_in_registry(
    skill_key: str,
    registry: dict[str, SkillMeta],
    skills_dir: str,
) -> None:
    """Hot-reload one generated skill into a live registry dict.

    Parses the SKILL.md from disk, adds it under its dispatcher key, and
    rebuilds the cached metadata block. When ``registry`` is the module-level
    registry in ``agent_loop``, that module's ``SKILLS_METADATA`` is refreshed
    too, so the next planner turn sees the new skill without a restart.
    """
    root = Path(skills_dir).expanduser().resolve()
    meta: Optional[SkillMeta] = None
    for path in sorted(root.rglob("SKILL.md")):
        try:
            parsed = load_skill_file(path)
        except SkillRegistryError as exc:
            log.warning(f"skill_writer: skipping malformed skill {path}: {exc}")
            continue
        if parsed.skill == skill_key:
            meta = parsed
            break

    if meta is None:
        log.warning(
            f"skill_writer: could not find a SKILL.md for key '{skill_key}' "
            f"under {root}"
        )
        return

    existing = registry.get(skill_key)
    if existing is not None and existing.skill_md_path != meta.skill_md_path:
        log.warning(
            f"skill_writer: not replacing existing skill '{skill_key}' at "
            f"{existing.skill_md_path} with {meta.skill_md_path}"
        )
        return

    registry[skill_key] = meta
    metadata = build_skills_metadata(registry)

    # Keep the module-level cache used by older callers in sync as well.
    try:
        from . import agent_loop as _agent_loop

        if getattr(_agent_loop, "SKILL_REGISTRY", None) is registry:
            _agent_loop.SKILLS_METADATA = metadata
    except Exception:
        pass

    log.info(
        f"skill_writer: registered '{skill_key}' from {meta.skill_md_path} "
        f"({len(registry)} skills, {len(metadata)} metadata chars)"
    )
