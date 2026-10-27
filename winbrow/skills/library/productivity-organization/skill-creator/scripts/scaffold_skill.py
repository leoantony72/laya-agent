"""Scaffold a new WinBrow skill folder.

Spec:
  {
    "name": "weekly-report",
    "kind": "playbook",              // or "tool"
    "category": "productivity-organization",
    "skill": "playbook_weekly_report",
    "description": "Use when ...",
    "with_script": false,
    "overwrite": false,
    "root": "C:\\...\\winbrow\\skills"   // optional, for tests
  }

Playbooks go under library/<category>/<name>; tool skills under <name>.
"""

from __future__ import annotations

import re
from pathlib import Path

from winbrow.skill_script_utils import emit, fail, load_spec, resolve_path

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
VALID_CATEGORIES = {
    "document-processing",
    "data-analysis",
    "communication-writing",
    "creative-media",
    "productivity-organization",
    "collaboration-project-management",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[6]


def _default_script(name: str) -> str:
    return f'''"""Bundled helper for the {name} skill."""

from __future__ import annotations

from winbrow.skill_script_utils import emit, fail, load_spec


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "").strip().lower()
    if operation != "run":
        fail('Unknown operation. Use {"operation": "run"}.')
    # TODO: implement the deterministic part of the workflow.
    emit(True, "Replace this with the real implementation.", data={{"input": spec}})


if __name__ == "__main__":
    main()
'''


def main() -> None:
    spec = load_spec()
    name = str(spec.get("name") or "").strip().lower()
    kind = str(spec.get("kind") or "playbook").strip().lower()
    category = str(spec.get("category") or "").strip().lower()
    skill = str(spec.get("skill") or "").strip()
    description = " ".join(str(spec.get("description") or "").split())
    overwrite = bool(spec.get("overwrite", False))

    if not SLUG_RE.match(name):
        fail("'name' must be a lowercase slug, e.g. 'weekly-report'.")
    if kind not in {"tool", "playbook"}:
        fail("'kind' must be 'tool' or 'playbook'.")
    if kind == "playbook":
        if category not in VALID_CATEGORIES:
            fail(f"'category' must be one of {sorted(VALID_CATEGORIES)}.")
        if not skill:
            skill = "playbook_" + name.replace("-", "_")
        if not skill.startswith("playbook_"):
            fail("Playbook 'skill' must start with playbook_.")
    else:
        if not skill:
            skill = name.replace("-", "_")
        if skill.startswith("playbook_"):
            fail("Tool 'skill' cannot start with playbook_.")
    if not description:
        fail("'description' is required: say when the skill should trigger.")
    if len(description) > 300:
        fail("'description' must be under 300 chars.")

    skills_root = resolve_path(spec.get("root") or _repo_root() / "winbrow" / "skills")
    if kind == "playbook":
        folder = skills_root / "library" / category / name
    else:
        folder = skills_root / name
    skill_md = folder / "SKILL.md"
    if skill_md.exists() and not overwrite:
        fail(f"{skill_md} already exists. Pass overwrite=true after confirming.")

    folder.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"name: {name}\n"
        f"skill: {skill}\n"
        f"kind: {kind}\n"
        + (f"category: {category}\n" if category else "")
        + "args: []\n"
        "description: >\n"
        f"  {description}\n"
        "---\n"
    )
    body = f"""
# {name.replace('-', ' ').title()}

## When to Use

Use when GOAL ...

## When NOT to Use

- ...

## Step Pattern

1. ...
2. ...
3. Verify the result before reporting success.

## Output Shape

Describe what the skill returns or produces.

## Recovery

- ...
"""
    skill_md.write_text(frontmatter + body, encoding="utf-8")

    paths = [str(skill_md)]
    if spec.get("with_script") or kind == "tool":
        scripts = folder / "scripts"
        scripts.mkdir(exist_ok=True)
        script_path = scripts / f"{name.replace('-', '_')}_tool.py"
        if not script_path.exists() or overwrite:
            script_path.write_text(_default_script(name), encoding="utf-8")
        paths.append(str(script_path))

    emit(
        True,
        f"Created {kind} skill at {folder}\n"
        + "\n".join(f"- {path}" for path in paths)
        + "\nNext: fill the TODOs, then validate with validate_skill.py.",
        path=folder,
        paths=paths,
    )


if __name__ == "__main__":
    main()
