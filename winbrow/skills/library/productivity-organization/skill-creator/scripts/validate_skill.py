"""Validate a WinBrow SKILL.md (frontmatter + basic structure).

Spec:
  {"path": "C:\\...\\SKILL.md"}      validate one skill
  {"path": "C:\\...\\my-skill"}      folder containing SKILL.md
  {"all": true}                      validate every skill under winbrow/skills

Prints one JSON object. Errors make the process exit non-zero.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from winbrow.skill_script_utils import emit, fail, load_spec, resolve_path

try:
    import yaml
except Exception:
    yaml = None

VALID_KINDS = {"tool", "playbook"}
VALID_TYPES = {"string", "bool", "int", "float", "number", "enum", "text"}
VALID_CATEGORIES = {
    "document-processing",
    "data-analysis",
    "communication-writing",
    "creative-media",
    "productivity-organization",
    "collaboration-project-management",
    "other",
}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[6]


def validate_one(skill_md: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not skill_md.is_file():
        return [f"{skill_md}: file not found"], warnings
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return [f"{skill_md}: must start with a --- frontmatter block and body"], warnings
    if yaml is None:
        return ["PyYAML is required to validate skills (pip install pyyaml)"], warnings
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except Exception as exc:
        return [f"{skill_md}: invalid YAML ({exc})"], warnings
    if not isinstance(meta, dict):
        return [f"{skill_md}: frontmatter must be a mapping"], warnings
    body = match.group(2).strip()

    name = str(meta.get("name") or "").strip()
    skill = str(meta.get("skill") or "").strip()
    kind = str(meta.get("kind") or "tool").strip().lower()
    category = str(meta.get("category") or "").strip().lower()
    description = str(meta.get("description") or "").strip()
    folder = skill_md.parent.name

    if not name:
        errors.append(f"{skill_md}: missing 'name'")
    elif not SLUG_RE.match(name):
        errors.append(f"{skill_md}: 'name' must be a lowercase slug, got '{name}'")
    elif name != folder:
        errors.append(f"{skill_md}: name '{name}' does not match folder '{folder}'")
    if not skill:
        errors.append(f"{skill_md}: missing 'skill' dispatcher key")
    if kind not in VALID_KINDS:
        errors.append(f"{skill_md}: kind '{kind}' must be one of {sorted(VALID_KINDS)}")
    if kind == "playbook" and not skill.startswith("playbook_"):
        errors.append(f"{skill_md}: playbooks need skill='playbook_<slug>'")
    if kind == "tool" and skill.startswith("playbook_"):
        errors.append(f"{skill_md}: tool skills cannot use a playbook_ dispatcher")
    if category and category not in VALID_CATEGORIES:
        errors.append(f"{skill_md}: unknown category '{category}'")
    if kind == "playbook" and not category:
        warnings.append(f"{skill_md}: playbook has no category; it will appear under OTHER")
    if not description:
        errors.append(f"{skill_md}: missing 'description' (the trigger text)")
    elif len(description) > 300:
        warnings.append(f"{skill_md}: description is {len(description)} chars; keep it under 300")
    elif kind == "playbook" and len(description) > 95:
        warnings.append(
            f"{skill_md}: playbook description is {len(description)} chars; "
            "the registry truncates it to 95"
        )

    args = meta.get("args")
    if args is None:
        warnings.append(f"{skill_md}: no 'args' list (use args: [] for playbooks)")
    elif not isinstance(args, list):
        errors.append(f"{skill_md}: 'args' must be a list")
    else:
        for index, arg in enumerate(args):
            if not isinstance(arg, dict):
                errors.append(f"{skill_md}: args[{index}] must be a mapping")
                continue
            arg_name = str(arg.get("name") or "").strip()
            if not arg_name:
                errors.append(f"{skill_md}: args[{index}] is missing 'name'")
            arg_type = str(arg.get("type") or "string").strip().lower()
            if arg_type not in VALID_TYPES:
                errors.append(
                    f"{skill_md}: args[{index}] type '{arg_type}' is not one of "
                    f"{sorted(VALID_TYPES)}"
                )
            if not str(arg.get("description") or "").strip():
                warnings.append(f"{skill_md}: args[{index}] has no description")

    if len(body) < 80:
        errors.append(f"{skill_md}: body is too short to guide the planner")
    if "## When to Use" not in body:
        warnings.append(f"{skill_md}: body has no '## When to Use' section")
    if "TODO" in body or "TBD" in body:
        warnings.append(f"{skill_md}: body contains TODO/TBD")
    if len(body.splitlines()) > 500:
        warnings.append(f"{skill_md}: body is over 500 lines; consider references/")

    scripts_dir = skill_md.parent / "scripts"
    if scripts_dir.is_dir():
        for script in sorted(scripts_dir.glob("*.py")):
            source = script.read_text(encoding="utf-8", errors="replace")
            if "load_spec" not in source and "--spec-json" not in source:
                warnings.append(
                    f"{script}: does not use load_spec/--spec-json; "
                    "run_skill_script cannot call it"
                )
    return errors, warnings


def main() -> None:
    spec = load_spec()
    targets: list[Path] = []
    if spec.get("all"):
        root = Path(spec.get("root") or _repo_root() / "winbrow" / "skills")
        targets = sorted(root.rglob("SKILL.md"))
    else:
        raw = spec.get("path") or spec.get("skill_md") or ""
        target = resolve_path(raw)
        targets = [target / "SKILL.md" if target.is_dir() else target]

    if not targets:
        fail("No SKILL.md found to validate.")

    errors: list[str] = []
    warnings: list[str] = []
    for skill_md in targets:
        item_errors, item_warnings = validate_one(skill_md)
        errors.extend(item_errors)
        warnings.extend(item_warnings)

    summary = f"Validated {len(targets)} skill(s): {len(errors)} error(s), {len(warnings)} warning(s)."
    if warnings:
        summary += "\nWarnings:\n" + "\n".join(f"- {item}" for item in warnings[:40])
    if errors:
        summary += "\nErrors:\n" + "\n".join(f"- {item}" for item in errors[:40])
    emit(
        not errors,
        summary,
        data={
            "checked": len(targets),
            "errors": errors,
            "warnings": warnings,
        },
    )


if __name__ == "__main__":
    main()
