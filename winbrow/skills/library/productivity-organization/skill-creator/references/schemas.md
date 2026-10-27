# WinBrow Skill Schemas

## Folder Layout

```
winbrow/skills/
├── <tool-skill>/SKILL.md                 # kind: tool (core/learned)
├── learned/<tool-skill>/SKILL.md         # auto-captured skills
└── library/<category>/<playbook>/SKILL.md
    ├── scripts/                          # run_skill_script helpers
    ├── references/                       # optional docs, loaded on demand
    └── assets/ or templates/             # optional files used in output
```

The folder name must equal frontmatter `name` (lowercase slug).

## Frontmatter Fields

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Lowercase slug; must match the folder name |
| `skill` | yes | Dispatcher key. Playbooks: `playbook_<slug>` |
| `kind` | yes | `tool` or `playbook` |
| `category` | playbooks | One of the six library categories (or `other`) |
| `args` | recommended | `[]` for playbooks; typed list for tool skills |
| `description` | yes | Trigger text; playbooks stay under 95 chars |
| `source` | optional | Upstream URL/attribution |
| `license` | optional | License note or reference |

### Tool Args

```yaml
args:
  - name: path
    type: string        # string | text | int | float | number | bool | enum
    required: true
    description: Full path to the source file.
  - name: format
    type: enum
    required: false
    default: pdf
    description: Output format.
```

`text` is an alias for `string`; `enum` values live in the skill body.

## Tool Skill Example

```yaml
---
name: weekly-report
skill: weekly_report
kind: tool
args:
  - name: notes_path
    type: string
    required: true
    description: Notes file used to build the report.
description: Build a weekly status report from a notes file and save it as docx.
---
```

The dispatcher name must exist in the registry. If the action is new, create it
with `build_tool` or add a script and call it from a playbook instead.

## Playbook Example

```yaml
---
name: weekly-report
skill: playbook_weekly_report
kind: playbook
category: productivity-organization
args: []
description: >
  Build a weekly status report from notes, tickets, or chat logs.
---
```

## Script Contract

`run_skill_script(skill, script, spec)` runs:

```
python winbrow/skills/**/<skill>/scripts/<script> --spec-json '<json object>'
```

The script:
1. calls `load_spec()`;
2. validates inputs and resolves paths;
3. does work with standard libraries or an optional package;
4. prints exactly one JSON object as its last line via `emit()` or `fail()`.

```python
from winbrow.skill_script_utils import emit, fail, load_spec, resolve_path

def main():
    spec = load_spec()
    path = resolve_path(spec.get("path") or "")
    if not path.is_file():
        fail(f"File not found: {path}")
    emit(True, "Parsed 12 rows", data={"rows": 12}, path=path)

if __name__ == "__main__":
    main()
```

Allowed output keys: `ok`/`success`, `message`/`output`, `data`, `path`,
`paths`, `files`, `count`, plus extra keys. Keep messages short; the runner caps
output at 12k chars and the script at 90 s by default.

## Trigger Description Rules

- Start with "Use when..." or a direct verb.
- Name user phrases and file extensions.
- Include the output ("...and save it as .docx").
- Distinguish from the nearest existing skill.
- Do not put the whole workflow in the description; the body has that.

## Validation Checklist

- `name` == folder, lowercase slug.
- `kind` valid; playbook `skill` starts with `playbook_`.
- `category` valid for library playbooks.
- `description` non-empty, under 300 chars (95 for playbooks).
- `args` is a list; types valid; descriptions present.
- Body has When to Use, a step pattern, verification, and recovery.
- Scripts use `--spec-json`/`load_spec` and emit one JSON object.
- No `TODO`/`TBD` left in a shipped skill.
