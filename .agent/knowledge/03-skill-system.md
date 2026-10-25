---
knowledge: 03-skill-system
last_verified: 2026-10-25
source_files:
  - winbrow/brain/skill_registry.py
  - winbrow/brain/skill_writer.py
  - winbrow/brain/agent_loop.py
  - winbrow/brain/prompt.py
  - winbrow/memory/skills.py
  - winbrow/agent.py
  - winbrow/skills
  - tools/skills.json
  - tools/learned.json
---

# 03 — Skill System

Verified 2026-10-25. There are **two independent skill systems** plus a third related concept
(learned tools). Do not conflate them.

## 1. Three related but separate things

| System | Storage | Purpose | Who uses it |
|---|---|---|---|
| **Brain skills** | `winbrow/skills/<name>/SKILL.md` | Markdown instructions for the planner; progressive disclosure | `brain/skill_registry.py`, `brain/prompt.py` |
| **Procedural replay** | `tools/skills.json` | Captured multi-step plans replayed without the planner | `memory/skills.py`, `agent.execute()` |
| **Learned tools** | `tools/learned.json` + generated `SKILL.md` | New PowerShell tools created by the generator | `registry.py`, `generator.py`, `skill_registry.write_learned_skill` |

A learned tool gets a **brain skill wrapper** so the planner can choose it, but the executable
definition lives in `tools/learned.json`.

## 2. Brain skill format

Each curated skill is `winbrow/skills/<folder>/SKILL.md` with YAML frontmatter:

```yaml
---
name: browser-upload-file
skill: browser_upload_file
description: >
  Upload a local file to a website...
args:
  - name: file_path
    type: string
    required: true
    description: Absolute path to the file.
---
# Browser: Upload a File
...full instructions...
```

Required frontmatter fields (`skill_registry._load_skill_file`, `:160-202`):

- `skill` — exact dispatcher key; must match a registry tool name.
- `description` — trigger text shown in metadata.
- `args` — list of `{name, type, required, default, description}`.
- `name` is optional but used as the human-facing/folder name.

Malformed files are logged and skipped per file (`:217-290`); one bad skill does not stop startup.
`yaml` is optional at import (`:35-41`) but is required in practice for frontmatter parsing.

Arg types: `string`, `bool`, `int`, `float`, `number`, `enum`, `text`; aliases normalize
`text→string`, `enum→string`, `number→float` (`:81-82`). Note: the registry's browser tools declare
everything as `kind="text"`, so frontmatter types are documentation, not enforcement.

## 3. Progressive disclosure

1. `build_skills_metadata()` (`skill_registry.py:374-395`) renders **metadata only**:
   dispatcher key, args label, and a clipped description. Budget target: under 4,000 chars
   (`META_DESC_LIMIT`), using a ladder of description lengths (`_DESC_LADDER`, `:74-80`).
2. The metadata is injected into the cached system prompt (`brain/prompt.py`).
3. When the planner chooses a skill, `inject_skill_detail()` loads the full body and adds it as
   `SKILL DETAIL` on the **next** turn (`agent_loop.py:766-770`).
4. `load_skill_body()` reads the markdown below the frontmatter (`skill_registry.py:402-414`).

Ordering: `PREFERRED_ORDER` (`:47-67`) puts 15 core skills first, then meta skills, then learned
skills alphabetically. `CORE_SKILL_KEYS` = first 15; `META_SKILL_KEYS` = last 4.

Current inventory:

**Curated (19):** `browser_accessibility`, `browser_interact`, `browser_navigate`,
`browser_read_full`, `browser_read_page`, `browser_upload_file`, `browser_wait`, `build_tool`,
`create_text_file`, `find_files`, `mcp_filesystem_move_file`, `open_file`, `open_folder`,
`read_document`, `recall_memory`, `remember_fact`, `repair_tool`, `run_command`,
`screen_read_text`.

**Learned (3):** `open_notepad`, `create_folder_papers`,
`create_folder_and_also_make_a_summ` (the last was captured from a garbled multi-intent request and
is a poor artifact, not a code bug).

## 4. Skill writer lifecycle

The writer turns a **successful multi-step brain run** into a reusable `SKILL.md`.

### Trigger

`AgentBrain._fire_skill_writer()` (`agent_loop.py:477-502`):

- Skipped when `WINBROW_SKILL_WRITER=0`.
- Requires at least **3 successful steps** whose skill name does not start with `_`
  (`MIN_REUSABLE_STEPS = 3`, `skill_writer.py:50`).
- Runs as a background `asyncio` task so the user gets the task result first.
- Fired on `done` (`:754`) and on a completed action (`:695`).

### Analysis

`analyse_task_for_skill()` (`skill_writer.py:566-645`):

- Skips when there are fewer than 3 successful executed steps.
- Skips when no model provider is configured.
- Sends the goal, formatted history, and existing skill names to the gateway with a JSON schema
  (`SKILL_WRITER_JSON_SCHEMA`), `temperature=0.1`, `max_tokens=2000`.
- Returns a `SkillCandidate`; any model/JSON failure becomes `should_save=False` with a reason.

### Guards (`_apply_guards`, `:494-560`)

A candidate is rejected when:

- fewer than 3 successful steps;
- the name is not kebab-case or the key is not snake_case;
- the description exceeds 60 words (`MAX_DESCRIPTION_WORDS`);
- a required section is missing;
- the name/key duplicates an existing skill;
- it contains task-specific tokens: filenames, document titles, people, sites, or search queries
  (`_specificity_violations`).

The prompt explicitly requires generic names (e.g. `upload-file-to-online-viewer`, not
`upload-ldmseminar-to-aspose`) and replacing specific values with args.

### Write

`write_skill_to_disk()` (`:734-783`):

- Refuses `should_save=False`.
- Validates the folder name and skill key.
- **Refuses to overwrite an existing folder** (`:754`). A hand-crafted skill is never replaced.
- Renders the full markdown and writes via a temporary file + `os.replace` (atomic).

### Hot reload

`register_skill_in_registry()` (`:785-839`):

- Re-parses the new `SKILL.md` from disk.
- Refuses to replace an existing skill whose path differs.
- Adds the `SkillMeta` to the live registry.
- Rebuilds metadata and updates the module-level `SKILLS_METADATA` when the registry is the live
  agent loop's registry.

So the next planner turn can choose the skill **without a restart**.

## 5. Procedural replay (`tools/skills.json`)

Implemented in `winbrow/memory/skills.py`.

### Record shape

```
{
  "id": "...",
  "name": "...",
  "utterance": "...",
  "description": "...",
  "triggers": [...],
  "parameters": [{"name": "...", "description": "..."}],
  "steps": [{"skill": "...", "args": {"...": "{{param}}"}}],
  "status": "proposed" | "approved",
  "created": 0.0,
  "uses": 0,
  "last_used": 0.0,
  "source": "..."
}
```

Constants: `MAX_STEPS = 12`, `MATCH_THRESHOLD = 0.66` (`skills.py:40-41`). Storage defaults to
`tools/skills.json`; `WINBROW_SKILLS_DIR` overrides the directory (`:361-362`).

### Capture

`WinBrowAgent._capture_skill_candidate()` (`agent.py:622-666`) runs after a successful brain
`action` (`agent.py:1459-1460`). Brain captures are saved with `status="proposed"`, so they are
**not** replayable until approved (`skills.py:410-427`).

### Matching

`SkillMemory.find_match()` (`skills.py:455-474`):

- Computes Jaccard token overlap on stopword-filtered tokens.
- Only `status == "approved"` records are considered.
- Returns the best record at or above `0.66`, else `None`.

### Replay

In `execute()` before routing (`agent.py:1087-1167`):

- Browser intents are deliberately **skipped** (`:1098-1100`) because live web state changes; they
  always use observe→act→verify.
- Parameters are filled by `_fill_skill_parameters()`; missing values fall through to normal
  planning.
- Steps replay through `run_skill()` (`:1116-1131`), capped at 12.
- All steps successful → `mark_used`, confidence `1.0`, tier `replay`. Any failure stops replay and
  falls through to routing.

### Approval

`SkillMemory.approve()` (`skills.py:476+`), exposed as `POST /api/skills/approve`
(`server.py:518`). `POST /api/skills/delete` removes records (`server.py:509`).

Current `tools/skills.json`: 14 records, 2 approved, 12 proposed, all `uses=0` as of 2026-10-24.

## 6. Learned tools

- Generated by `generator.py` and persisted to `tools/learned.json` (`registry.save_learned`).
- `skill_registry.write_learned_skill()` (`:661-687`) renders a `SKILL.md` wrapper.
- `_sync_learned_skills()` (`:691-716`) regenerates wrappers from `tools/learned.json` on registry
  scan.
- Learned tools have `impl=None` (script-backed) and are gated at least T2 by
  `permissions.py:221-224` when invoked through `run_skill`.

## 7. Adding a new brain skill

1. Pick a dispatcher key that already exists in the registry (or add the tool first).
2. Create `winbrow/skills/<kebab-name>/SKILL.md` with the required frontmatter.
3. Write a **trigger-focused** description (when to use / when not to use), not a summary.
4. Keep the body procedural: exact steps, args, recovery, and verification.
5. Do not duplicate a `PREFERRED_ORDER` key.
6. Restart or let the registry scan; the planner sees it in metadata immediately afterward.
7. If the tool mutates, confirm its permission tier is correct.

## 8. Adding a procedural replay skill

1. Prefer capturing a real successful run; do not hand-write unless necessary.
2. Keep steps ≤ 12 and use only registered tools.
3. Parameterize variable values with `{{name}}`; only `basename|filename`, `lower`, `upper` filters
   are supported.
4. Save as `proposed`, review the steps, then approve.
5. Never store browser workflows as replay skills; they are skipped by design.

## 9. Risks and sharp edges

- **Writer cost:** each eligible task can trigger a model call. Disable with `WINBROW_SKILL_WRITER=0`.
- **Specificity guards are heuristic:** a generic-looking but single-site skill can still pass.
- **No overwrite:** fixing a generated skill means editing/deleting its folder manually; the writer
  will not replace it.
- **Folder-name truncation:** `_slugify` caps at 60 chars; `create-folder-and-also-make-a-summ` is
  evidence that a garbled goal can become a permanent skill name.
- **Metadata budget:** too many verbose descriptions push the block over 4,000 chars and trigger a
  warning; the ladder clips at sentence boundaries.
- **Replay blindness:** a replay plan does not re-observe the world; a changed desktop can make a
  stale plan "succeed" or fail halfway.
- **Status gate:** only `approved` records replay. A capture that looks successful but stays
  `proposed` will never run.
- **Two stores:** deleting a brain skill does not delete a replay record, and vice versa.
