"""
Progressive-disclosure skill registry
=====================================
Claude-Code-style skills for the WinBrow brain:

  winbrow/skills/<folder>/SKILL.md

Every skill is a folder with YAML frontmatter that carries only the metadata the
planner needs on every turn (name, dispatcher key, args, trigger description).
The markdown body below the frontmatter is the full instruction set and is
loaded ONLY when the planner picks that skill. This keeps the cached system
prompt small while still giving the model complete, exact instructions at the
moment it acts.

Three levels:
  1. frontmatter  → parsed once, injected into the cached system prompt
  2. SKILL.md body → loaded when the brain chooses the skill (SKILL DETAIL)
  3. registry tools (MCP, native, learned) → name index in the cached prompt,
     full schema synthesized on demand by build_tool_detail()

Malformed frontmatter raises SkillRegistryError at startup instead of failing
silently in the middle of a task.
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import yaml
except Exception as _yaml_error:  # pragma: no cover - environment guard
    yaml = None
    _YAML_IMPORT_ERROR = _yaml_error
else:
    _YAML_IMPORT_ERROR = None

log = logging.getLogger("winbrow.brain.skills")

# Stable ordering: the hand-written core skills first, meta skills after, then
# learned/generated skills alphabetically.
PREFERRED_ORDER = (
    "browser_navigate",
    "browser_read_page",
    "browser_read_full",
    "browser_interact",
    "browser_wait",
    "browser_upload_file",
    "browser_get_accessibility_tree",
    "find_files",
    "read_document",
    "open_file",
    "open_folder",
    "mcp_filesystem_move_file",
    "create_text_file",
    "run_command",
    "screen_read_text",
    "remember_fact",
    "recall_memory",
    "build_tool",
    "repair_tool",
)

META_DESC_LIMIT = 4000
CORE_SKILL_KEYS = frozenset(PREFERRED_ORDER[:15])
META_SKILL_KEYS = frozenset(PREFERRED_ORDER[15:])
# (core, meta, learned) description budgets. The first ladder rung that fits
# under META_DESC_LIMIT wins, so the core 15 keep the richest triggers.
_DESC_LADDER = (
    (200, 130, 100),
    (180, 120, 100),
    (170, 110, 90),
    (150, 95, 75),
    (120, 80, 60),
)
_VALID_ARG_TYPES = {"string", "bool", "int", "float", "number", "enum", "text"}
_TYPE_ALIASES = {"text": "string", "enum": "string", "number": "float"}


class SkillRegistryError(ValueError):
    """A SKILL.md is missing required frontmatter or is otherwise malformed."""


@dataclass
class ArgDef:
    name: str
    type: str = "string"
    required: bool = False
    default: Any = None
    description: str = ""


@dataclass
class SkillMeta:
    name: str                 # folder / human-facing skill name
    skill: str                # exact JSON dispatcher key
    description: str          # trigger text, metadata level only
    args: list[ArgDef] = field(default_factory=list)
    skill_md_path: str = ""   # absolute path to the full SKILL.md
    folder: str = ""
    source: str = "folder"    # "folder" | "learned"
    body: Optional[str] = None  # generated body for non-file skills


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def _split_frontmatter(text: str, path: Path) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillRegistryError(f"{path}: SKILL.md must start with a --- frontmatter block")
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        raise SkillRegistryError(f"{path}: frontmatter is missing its closing ---")
    return "\n".join(lines[1:end]), "\n".join(lines[end + 1:]).strip()


def _parse_args(raw: Any, path: Path) -> list[ArgDef]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise SkillRegistryError(f"{path}: 'args' must be a list")
    parsed: list[ArgDef] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SkillRegistryError(f"{path}: each arg must be a mapping")
        name = str(item.get("name") or "").strip()
        if not name:
            raise SkillRegistryError(f"{path}: an arg is missing 'name'")
        arg_type = str(item.get("type") or "string").strip().lower()
        if arg_type not in _VALID_ARG_TYPES:
            raise SkillRegistryError(
                f"{path}: arg '{name}' has unsupported type '{arg_type}' "
                f"(expected one of {sorted(_VALID_ARG_TYPES)})"
            )
        arg_type = _TYPE_ALIASES.get(arg_type, arg_type)
        required = item.get("required", False)
        required = bool(required) if isinstance(required, bool) else \
            str(required).strip().lower() == "true"
        parsed.append(ArgDef(
            name=name,
            type=arg_type,
            required=required,
            default=item.get("default"),
            description=str(item.get("description") or "").strip(),
        ))
    return parsed


def _load_skill_file(path: Path, folder_name: str, source: str = "folder") -> SkillMeta:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillRegistryError(f"{path}: cannot read SKILL.md ({exc})") from exc
    frontmatter, body = _split_frontmatter(text, path)
    if yaml is None:
        raise SkillRegistryError(
            f"{path}: PyYAML is required to parse SKILL.md frontmatter "
            f"({_YAML_IMPORT_ERROR})"
        )
    try:
        data = yaml.safe_load(frontmatter)
    except Exception as exc:
        raise SkillRegistryError(f"{path}: invalid YAML frontmatter ({exc})") from exc
    if not isinstance(data, dict):
        raise SkillRegistryError(f"{path}: frontmatter must be a mapping")

    name = str(data.get("name") or "").strip()
    skill = str(data.get("skill") or "").strip()
    description = str(data.get("description") or "").strip()
    if not name:
        raise SkillRegistryError(f"{path}: frontmatter is missing 'name'")
    if not skill:
        raise SkillRegistryError(f"{path}: frontmatter is missing 'skill'")
    if not description:
        raise SkillRegistryError(f"{path}: frontmatter is missing 'description'")
    if name != folder_name:
        raise SkillRegistryError(
            f"{path}: frontmatter name '{name}' does not match folder '{folder_name}'"
        )
    return SkillMeta(
        name=name,
        skill=skill,
        description=" ".join(description.split()),
        args=_parse_args(data.get("args"), path),
        skill_md_path=str(path.resolve()),
        folder=str(path.parent.resolve()),
        source=source,
        body=body or None,
    )


# ---------------------------------------------------------------------------
# A. Load metadata (called once at startup)
# ---------------------------------------------------------------------------

def _default_learned_path(skills_root: Path) -> Path:
    return skills_root.resolve().parent.parent / "tools" / "learned.json"


def load_skill_registry(
    skills_dir: str,
    learned_path: Optional[str] = None,
    sync_learned: bool = True,
) -> dict[str, SkillMeta]:
    """Return ``{dispatcher_key: SkillMeta}`` for every SKILL.md.

    Raises SkillRegistryError on malformed frontmatter so broken skills fail
    loud at startup, not silently mid-task. Missing skills_dir is not fatal:
    the brain falls back to the runtime tool index only.
    """
    root = Path(skills_dir).expanduser().resolve()
    if not root.exists():
        log.warning(f"Skills directory not found: {root}")
        return {}

    registry: dict[str, SkillMeta] = {}
    for path in sorted(root.rglob("SKILL.md")):
        # Skip the generated learned subtree until after the first scan so a
        # curated skill with the same dispatcher key always wins.
        if "learned" in path.relative_to(root).parts[:-2]:
            continue
        meta = _load_skill_file(path, path.parent.name)
        if meta.skill in registry:
            other = registry[meta.skill]
            raise SkillRegistryError(
                f"Duplicate dispatcher key '{meta.skill}': {path} and "
                f"{other.skill_md_path}"
            )
        registry[meta.skill] = meta

    if sync_learned:
        learned_file = Path(learned_path).expanduser() if learned_path \
            else _default_learned_path(root)
        try:
            _sync_learned_skills(learned_file, root, registry)
        except Exception as exc:  # never block startup on learned migration
            log.debug(f"Learned-skill sync skipped: {exc}")

    for path in sorted(root.rglob("SKILL.md")):
        rel_parts = path.relative_to(root).parts
        if len(rel_parts) < 3 or rel_parts[0] != "learned":
            continue
        meta = _load_skill_file(path, path.parent.name, source="learned")
        if meta.skill in registry:
            # A curated folder or an older learned entry already owns the key.
            log.debug(
                f"Skipping generated learned skill '{meta.skill}' at {path}; "
                "the dispatcher key is already registered."
            )
            continue
        registry[meta.skill] = meta

    log.info(f"Loaded {len(registry)} skill definitions from {root}")
    return registry


# ---------------------------------------------------------------------------
# B. Build metadata summary (cached system block)
# ---------------------------------------------------------------------------

def _skill_args_label(args: Iterable[ArgDef]) -> str:
    parts = []
    for arg in args:
        suffix = "" if arg.required else "?"
        parts.append(f"{arg.name}{suffix}: {arg.type}")
    return "{" + ", ".join(parts) + "}" if parts else "{}"


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "..."


def _ordered(registry: dict[str, SkillMeta]) -> list[SkillMeta]:
    rank = {key: index for index, key in enumerate(PREFERRED_ORDER)}
    return sorted(
        registry.values(),
        key=lambda meta: (rank.get(meta.skill, len(rank)), meta.name.lower()),
    )


def _desc_limit(meta: SkillMeta, core: int, meta_limit: int, learned: int) -> int:
    if meta.source == "learned":
        return learned
    if meta.skill in CORE_SKILL_KEYS:
        return core
    return meta_limit


def _render_skills_metadata(
    registry: dict[str, SkillMeta], core: int, meta_limit: int, learned: int,
) -> str:
    lines = ["AVAILABLE SKILLS", "════════════════"]
    for meta in _ordered(registry):
        limit = _desc_limit(meta, core, meta_limit, learned)
        lines.append(f"{meta.skill}  {_skill_args_label(meta.args)}")
        wrapped = textwrap.fill(
            _clip(meta.description, limit),
            width=76,
            initial_indent="  ",
            subsequent_indent="  ",
        )
        lines.append(wrapped)
        lines.append("")
    lines.append(
        "Full instructions for the chosen skill are injected as SKILL DETAIL "
        "on the next turn."
    )
    return "\n".join(lines).strip()


def build_skills_metadata(
    registry: dict[str, SkillMeta], max_chars: int = META_DESC_LIMIT,
) -> str:
    """Compact metadata-only list injected into the cached system prompt.

    Core skills get the richest trigger text; meta and learned skills get a
    shorter line. Budget: under 4,000 characters (~1,000 tokens) whenever the
    registry is the standard skill set.
    """
    if not registry:
        return ""
    text = ""
    for core, meta_limit, learned in _DESC_LADDER:
        text = _render_skills_metadata(registry, core, meta_limit, learned)
        if len(text) <= max_chars:
            return text
    log.warning(
        f"Skill metadata is {len(text)} chars (target <{max_chars}); "
        "shorten SKILL.md descriptions."
    )
    return text


# ---------------------------------------------------------------------------
# C. Load full skill body (called only after the brain picks the skill)
# ---------------------------------------------------------------------------

def load_skill_body(meta: SkillMeta) -> str:
    """Read the markdown below the frontmatter for one chosen skill."""
    if meta.body:
        return meta.body
    if not meta.skill_md_path:
        return ""
    try:
        text = Path(meta.skill_md_path).read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(f"Could not read skill body {meta.skill_md_path}: {exc}")
        return ""
    _, body = _split_frontmatter(text, Path(meta.skill_md_path))
    return body


# ---------------------------------------------------------------------------
# Runtime tool index + detail (MCP, native, learned tools)
# ---------------------------------------------------------------------------

def _tool_group(tool: Any) -> tuple[str, str]:
    """Return (group_key, display_label) for a registry tool."""
    script = str(getattr(tool, "script", "") or "")
    if script.startswith("mcp://"):
        server = script[len("mcp://"):].split("/", 1)[0]
        return f"mcp:{server}", f"mcp_{server}"
    impl = str(getattr(tool, "impl", "") or "").lower()
    if impl == "computer":
        return "computer", "COMPUTER"
    if impl == "browser":
        return "browser", "BROWSER"
    if impl == "shell":
        return "shell", "SHELL"
    if impl == "adapter":
        return "adapter", "APP ADAPTERS"
    if impl == "installer":
        return "installer", "INSTALLER"
    if getattr(tool, "is_learned", False):
        return "learned", "LEARNED"
    return "native", "NATIVE"


def build_tool_index(
    tools: Iterable[Any],
    exclude: Iterable[str] = (),
    full_mcp_names: bool = False,
) -> str:
    """Compact, cache-friendly index of every non-skill registry tool.

    Only names are listed. The full description and arg schema for the chosen
    tool are injected as SKILL DETAIL on the next planning turn, so the cached
    prompt stays small while every MCP/native/learned tool remains discoverable.

    ``full_mcp_names`` spells MCP tools out instead of using the compact
    ``server: suffix`` form. The legacy planner view opts in so older callers
    still see exact names like ``mcp_filesystem_move_file``.
    """
    excluded = set(exclude)
    groups: dict[str, tuple[str, list[str]]] = {}
    for tool in tools or []:
        name = str(getattr(tool, "name", "") or "").strip()
        if not name or name in excluded:
            continue
        key, label = _tool_group(tool)
        if key not in groups:
            groups[key] = (label, [])
        groups[key][1].append(name)

    if not groups:
        return ""

    lines = [
        "OTHER TOOLS",
        "═══════════",
        "Exact dispatcher names. Full instructions load as SKILL DETAIL after "
        "you pick one.",
    ]
    for key in sorted(groups):
        label, names = groups[key]
        names = sorted(set(names))
        if key.startswith("mcp:") and not full_mcp_names:
            server = key.split(":", 1)[1]
            stripped = []
            for name in names:
                stripped.append(name[len(f"mcp_{server}_"):]
                                if name.startswith(f"mcp_{server}_") else name)
            prefix = (
                f"  {label} ({len(names)} tools; prefix each with "
                f"\"{label}_\"): "
            )
            body = ", ".join(stripped)
        else:
            prefix = f"  {label}: "
            body = ", ".join(names)
        # Keep every physical line under the planner's 200-char readability
        # budget, even for catalogs with 100+ MCP tools.
        lines.extend(textwrap.wrap(
            prefix + body, width=180,
            subsequent_indent="    ", break_long_words=False,
            break_on_hyphens=False,
        ) or [prefix.rstrip()])
    return "\n".join(lines)


def build_tool_detail(tool: Any) -> str:
    """Synthesize a SKILL DETAIL body for a registry tool that has no folder."""
    name = str(getattr(tool, "name", "") or "").strip()
    description = str(getattr(tool, "description", "") or "").strip()
    args = list(getattr(tool, "args", []) or [])
    script = str(getattr(tool, "script", "") or "")

    lines = [f"## Tool: {name}"]
    if description:
        lines.append(description)
    lines.extend(["", "## Arguments"])
    if not args:
        lines.append("This tool takes no arguments.")
    else:
        for arg in args:
            arg_name = str(getattr(arg, "name", "") or "")
            kind = str(getattr(arg, "kind", "") or "text")
            default = getattr(arg, "default", None)
            required = default in (None, "")
            instr = str(getattr(arg, "instructions", "") or "").strip()
            marker = "required" if required else f"optional, default={default!r}"
            lines.append(f"- `{arg_name}` ({kind}, {marker}): {instr or 'No description.'}")
    lines.extend([
        "",
        "## Output Shape",
        "Returns a result dict with `success` and `output`; MCP tools may also "
        "return server-specific content.",
        "",
        "## Recovery",
        "Read the failure text, correct the arguments once, and retry. If the "
        "tool is unavailable or keeps failing, choose a different skill or emit "
        "clarify. Follow the self-correct rule.",
    ])
    if script.startswith("mcp://"):
        server = script[len("mcp://"):].split("/", 1)[0]
        lines.insert(1, f"MCP server: {server}")
    return "\n".join(lines)


def resolve_skill_key(name: str, registry: dict[str, SkillMeta]) -> str:
    """Map folder-style or hyphenated names to the exact dispatcher key."""
    raw = str(name or "").strip()
    if not raw:
        return raw
    if raw in registry:
        return raw
    normalized = raw.replace("-", "_")
    if normalized in registry:
        return normalized
    for meta in registry.values():
        if meta.name.replace("-", "_") == normalized:
            return meta.skill
    return raw


# ---------------------------------------------------------------------------
# Learned-tool SKILL.md generation
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:60] or "learned-skill"


def _yaml_quote(value: Any) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def _fold(text: str, width: int = 76) -> str:
    return "\n".join(
        "  " + line for line in textwrap.wrap(" ".join((text or "").split()), width)
    )


def _learned_type(kind: str) -> str:
    kind = (kind or "text").lower()
    return "int" if kind in ("number", "int") else "string"


def render_learned_skill(item: dict) -> str:
    """Render a complete SKILL.md for a learned tool JSON entry."""
    name = str(item.get("name") or "").strip()
    slug = _slugify(name)
    description = str(item.get("description") or f"Learned tool {name}.").strip()
    raw_args = item.get("args") or []
    arg_lines: list[str] = []
    detail_lines: list[str] = []
    for arg in raw_args:
        arg_name = str(arg.get("name") or "").strip()
        if not arg_name:
            continue
        arg_type = _learned_type(str(arg.get("kind") or "text"))
        default = arg.get("default")
        required = default in (None, "")
        instructions = str(arg.get("instructions") or "").strip() or "No description."
        arg_lines.append(f"  - name: {arg_name}")
        arg_lines.append(f"    type: {arg_type}")
        arg_lines.append(f"    required: {'true' if required else 'false'}")
        if not required:
            arg_lines.append(f"    default: {_yaml_quote(default)}")
        arg_lines.append(f"    description: {_yaml_quote(instructions)}")
        detail_lines.append(
            f"- `{arg_name}` ({arg_type}, "
            f"{'required' if required else f'default={default!r}'}): {instructions}"
        )
    args_block = "\n".join(arg_lines) if arg_lines else "args: []"
    if arg_lines:
        args_block = "args:\n" + args_block
    args_detail = "\n".join(detail_lines) if detail_lines else \
        "This learned tool takes no arguments."

    return f"""---
name: {slug}
skill: {name}
{args_block}
description: >
{_fold(description)}
---

## When to Use

Use when GOAL matches this learned capability: {description}

## When NOT to Use

- Do not use it when a built-in skill already covers the same operation.
- Do not use it for a different app, path or target than the one described.
- Do not use it to work around a failure without reading the error first.
- Do not repeat it after a successful run with identical arguments.

## Step Pattern

1. Confirm the request matches this learned tool rather than a generic skill.
2. Call `{name}` with the required arguments.
3. Read the returned output and verify it against GOAL.
4. If a follow-up action is needed, use the path or value returned here.
5. If the tool fails, call repair-tool with its name and the failure text.

## Args Detail

{args_detail}

## Output Shape

Returns a result dict with `success` and `output`. A successful call means the
script completed; it does not by itself prove the wider goal is complete.

## Recovery

If the script errors, call repair-tool with the exact failure. If it succeeds
but returns the wrong result, treat it as a failure and do not repeat it
blindly. If the capability is no longer needed, switch to the matching
built-in skill. Follow the self-correct rule.
"""


def write_learned_skill(tool: Any, skills_dir: Optional[str] = None) -> Optional[str]:
    """Persist a SKILL.md for a learned Tool so it loads on the next startup."""
    name = str(getattr(tool, "name", "") or "").strip()
    if not name:
        return None
    root = Path(skills_dir).expanduser().resolve() if skills_dir \
        else Path(__file__).resolve().parents[1] / "skills"
    item = {
        "name": name,
        "description": str(getattr(tool, "description", "") or "").strip(),
        "args": [
            {
                "name": getattr(arg, "name", ""),
                "kind": getattr(arg, "kind", "text"),
                "instructions": getattr(arg, "instructions", ""),
                "default": getattr(arg, "default", None),
            }
            for arg in (getattr(tool, "args", []) or [])
        ],
    }
    target = root / "learned" / _slugify(name) / "SKILL.md"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_learned_skill(item), encoding="utf-8")
        return str(target)
    except OSError as exc:
        log.debug(f"Could not write learned skill {name}: {exc}")
        return None


def _sync_learned_skills(
    learned_path: Path, skills_root: Path, registry: dict[str, SkillMeta],
) -> None:
    if not learned_path.exists():
        return
    try:
        items = json.loads(learned_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.debug(f"Could not read learned tools {learned_path}: {exc}")
        return
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        if str(item["name"]) in registry:
            continue
        target = skills_root / "learned" / _slugify(str(item["name"])) / "SKILL.md"
        if target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_learned_skill(item), encoding="utf-8")
            log.info(f"Generated learned SKILL.md for '{item['name']}'")
        except OSError as exc:
            log.debug(f"Could not generate learned skill {item['name']}: {exc}")
