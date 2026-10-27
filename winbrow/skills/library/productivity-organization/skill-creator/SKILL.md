---
name: skill-creator
skill: playbook_skill_creator
kind: playbook
category: productivity-organization
source: https://github.com/anthropics/skills/tree/main/skills/skill-creator
license: See LICENSE.txt (upstream terms, reference only)
args: []
description: >
  Create, improve, or validate WinBrow skills, playbooks, and bundled scripts.
---

# Skill Creator

## When to Use

Use when the user asks to create a new skill, improve an existing skill, turn a
workflow into a reusable skill, fix a skill that never triggers, or validate a
SKILL.md. Also use before adding a bundled Python helper to a skill.

## When NOT to Use

- Do not use for a one-off task that will not be reused; just do the task.
- Do not use when `build_tool` is the better fit: an atomic PowerShell action
  that no listed tool covers. `build_tool` creates a runnable tool; this skill
  authors guidance/scripts around existing tools.
- Do not duplicate an existing skill. Search the registry first and improve it.

## Two Ways Skills Get Created

1. **Automatic capture** (`skill_writer`): after a successful multi-step run the
   brain may save a learned skill. Do not hand-author over it.
2. **Deliberate authoring** (this skill): the user asks for a skill, or an
   existing one needs a rewrite. Use the steps below.

## Step Pattern

1. **Capture intent.** From the conversation or a short interview, establish:
   - what the skill enables,
   - the exact user phrases that should trigger it,
   - the output/artifact it produces,
   - required tools, files, or packages,
   - what "done" looks like.
2. **Choose the shape.**
   - `kind: tool` — maps to an existing registry tool or a bundled script with
     typed args. Use when the skill is one deterministic action.
   - `kind: playbook` — guidance-only workflow composed from other tools. Use
     for multi-step processes and judgment calls.
   - `build_tool` — a new atomic PowerShell tool for a request no skill covers.
3. **Scaffold.** Prefer the bundled script:

   ```json
   {"name":"weekly-report","kind":"playbook","category":"productivity-organization",
    "skill":"playbook_weekly_report",
    "description":"Use when the user asks for a weekly status report from notes.",
    "with_script":false}
   ```

   `run_skill_script(skill="skill-creator", script="scaffold_skill.py", spec=...)`
   creates the folder and a stub. For a tool skill, pass `"kind":"tool"`.
4. **Write the SKILL.md.** Replace the stubs. See the schema in
   `references/schemas.md` and the rules below.
5. **Validate.**

   ```json
   {"path":"C:\\...\\winbrow\\skills\\library\\productivity-organization\\weekly-report"}
   ```

   `run_skill_script(skill="skill-creator", script="validate_skill.py", spec=...)`
   must report 0 errors before the skill is considered usable.
6. **Test.** Write 2–3 realistic prompts. Run at least one end-to-end through
   the brain and check the observable result:
   - file exists and opens,
   - expected section/row present,
   - tool sequence did not open a browser unnecessarily,
   - failure path reports honestly.
7. **Iterate.** Fix the description (triggering), the step pattern (behavior),
   or the script (determinism). Keep edits targeted.

## Authoring Rules

- **The description is the trigger.** Include what it does and the contexts that
  should activate it. Be a little pushy: name the user phrases and file types.
  Keep playbook descriptions under 95 characters (the registry truncates them).
- **Body under ~500 lines.** Move long tables, schemas, or domain variants into
  `references/` and point to them from the body.
- **Progressive disclosure.** Metadata is always loaded; the body loads only
  when chosen; scripts/references load on demand.
- **One job per skill.** If the workflow has two independent outcomes, split it.
- **Never invent tools.** Use only names that exist in the registry; check
  `TOOLS`/the skill index. For shell work use `run_command`; for files use
  `create_text_file`/`read_document`; for deterministic helpers use
  `run_skill_script`.
- **Verification is part of the skill.** Every skill must say how to prove the
  result (read back, count, re-open, inspect).
- **Recovery is part of the skill.** Name the common failures and the honest
  response; never tell the planner to retry forever.
- **No destructive defaults.** Ask before overwriting, deleting, sending, or
  publishing.

## Frontmatter

```yaml
---
name: weekly-report                 # must equal the folder name
skill: playbook_weekly_report       # dispatcher; playbooks use playbook_*
kind: playbook                      # or tool
category: productivity-organization # required for playbooks in library/
source: https://...                 # optional attribution
license: See LICENSE.txt
args: []                            # tool skills list their typed args
description: >
  Use when the user asks for a weekly status report from notes or tickets.
---
```

Valid categories: `document-processing`, `data-analysis`,
`communication-writing`, `creative-media`, `productivity-organization`,
`collaboration-project-management`.

Tool args use `[{name, type, required, default, description}]`; valid types are
`string`, `text`, `int`, `float`, `number`, `bool`, `enum`.

## Bundled Scripts

Scripts run through `run_skill_script`, which invokes:

```
python <skill>/scripts/<name>.py --spec-json '<json>'
```

The script must print exactly one JSON object on its last line. Use the shared
helpers:

```python
from winbrow.skill_script_utils import emit, fail, load_spec, resolve_path

spec = load_spec()
emit(True, "Done", data={"count": 1}, path="C:\\...")
```

Rules: no shell interpolation of user text, validate paths, bound output, and
give a specific install command when a package is missing.

## Evaluating a Skill

- Use objectively verifiable prompts when possible (file transforms, extraction,
  fixed workflows). For subjective output, ask the user to judge.
- Record the prompt, the tools used, and the observable result; compare before
  and after a change.
- Do not add the upstream skill-creator eval harness (Node, browser viewer,
  background runs); WinBrow has no equivalent runner. A short manual test set is
  the correct scope here.

## Definition of Done

- `validate_skill.py` reports 0 errors.
- One live run produced the expected artifact.
- Trigger description names the real user phrases.
- Failure/recovery sections are specific.
- No duplicate skill, no invented tools, no dead paths.

## Recovery

- Validator reports `name does not match folder`: rename the folder or the
  frontmatter `name`; do not ship the mismatch.
- Validator reports a bad `kind`/`skill`: playbooks need `kind: playbook` and a
  `playbook_*` key; tool skills need an existing dispatcher.
- A skill never triggers: rewrite the description with the user's real phrases
  and file types, then re-validate.
- A bundled script fails on `--spec-json`: use `load_spec()` and `emit()`;
  never parse raw command-line text yourself.
- The skill duplicates an existing one: improve the original instead of adding
  a second folder with the same job.
- The workflow is one atomic action, not a playbook: use `build_tool` and stop
  authoring a multi-step skill.
