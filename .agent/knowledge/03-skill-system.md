---
knowledge: 03-skill-system
last_verified: 2026-10-25
source_files:
  - winbrow/brain/skill_registry.py
  - winbrow/brain/skill_writer.py
  - winbrow/brain/agent_loop.py
  - winbrow/brain/prompt.py
  - winbrow/skill_scripts.py
  - winbrow/skill_script_utils.py
  - winbrow/integrations.py
  - winbrow/subagents.py
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
kind: tool
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

- `skill` — exact dispatcher key; must match a registry tool name, or start
  with `playbook_` when `kind: playbook`.
- `kind` — `tool` (default) or `playbook`. Playbook keys are guidance-only and
  are acknowledged by `agent_loop._run_step` without a tool lookup.
- `category` — converted-library section slug; used for metadata grouping.
- `source`/`license` — provenance only; parsed into `origin`/`license`.
- `description` — trigger text shown in metadata.
- `args` — list of `{name, type, required, default, description}`. Playbooks use
  `args: []`.
- `name` is optional but used as the human-facing/folder name; it must match the
  folder.

Malformed files are logged and skipped per file (`:217-290`); one bad skill does not stop startup.
`yaml` is optional at import (`:35-41`) but is required in practice for frontmatter parsing.

Arg types: `string`, `bool`, `int`, `float`, `number`, `enum`, `text`; aliases normalize
`text→string`, `enum→string`, `number→float` (`:81-82`). Note: the registry's browser tools declare
everything as `kind="text"`, so frontmatter types are documentation, not enforcement.

## 3. Progressive disclosure

1. `build_skills_metadata()` (`skill_registry.py:374-395`) renders **metadata only**:
   dispatcher key, args label, and a clipped description. The budget is now
   **12,000 chars** because the converted library adds 43 playbooks; core skills
   keep the longest descriptions, playbooks use a 95-char budget, and the six
   categories get a header.
2. The metadata is injected into the cached system prompt (`brain/prompt.py`).
3. When the planner chooses a skill, `inject_skill_detail()` loads the full body and adds it as
   `SKILL DETAIL` on the **next** turn (`agent_loop.py:766-770`).
4. A `playbook_*` choice is a no-op acknowledgement; the next turn receives the
   body and executes the concrete tools it names. Never treat a playbook call as
   task completion.
5. `load_skill_body()` reads the markdown below the frontmatter (`skill_registry.py:402-414`).

Ordering: `PREFERRED_ORDER` puts 15 core skills first, then meta skills
(`web_search`, `web_fetch`, memory, build/repair, delegation), then learned
skills alphabetically. `CORE_SKILL_KEYS` = first 15; `META_SKILL_KEYS` = the
rest of `PREFERRED_ORDER`.

Current inventory:

**Core (19):** `browser_accessibility`, `browser_interact`, `browser_navigate`,
`browser_read_full`, `browser_read_page`, `browser_upload_file`, `browser_wait`, `build_tool`,
`create_text_file`, `find_files`, `mcp_filesystem_move_file`, `open_file`, `open_folder`,
`read_document`, `recall_memory`, `remember_fact`, `repair_tool`, `run_command`,
`screen_read_text`.

**Native web access (2):** `web_search` and `web_fetch` (`winbrow/web_tools.py`,
`impl="web"`, T0). No browser is opened. They use DuckDuckGo HTML/Lite search
through a `curl_cffi` TLS/HTTP2-impersonated transport (plain `requests` gets an
HTTP 202 bot challenge) and HTTP GET + Trafilatura main-content extraction
(BeautifulSoup/pdfminer fallback). A `focus` question makes `web_fetch` return
only the most relevant Markdown sections (~6k chars). A search provider failure
is reported honestly; it never falls back to the browser for research.

**Converted playbooks (43):** the six requested sections live under
`winbrow/skills/library/<section>/<name>/SKILL.md`. Six document, five data,
seven writing, ten creative, nine productivity, six collaboration. Every one
uses `kind: playbook` and a unique `playbook_*` key. See
`winbrow/skills/library/README.md` and `PROVENANCE.md`.

**Learned (3):** `open_notepad`, `create_folder_papers`,
`create_folder_and_also_make_a_summ` (the last was captured from a garbled multi-intent request and
is a poor artifact, not a code bug).

**Meta delegation (1):** `delegate_task` (skill folder `delegate-task`) spawns scoped child
planners from `winbrow/subagents.py`. It is a real registry tool (`impl="subagent"`, T1) plus a
SKILL.md body, so the planner sees it in AVAILABLE SKILLS.

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

## 7. Converted skill library and integrations

The 43 converted skills are normal folder skills plus:

- **`run_skill_script`** (`winbrow/skill_scripts.py`, impl `skill_script`, T2):
  runs an allowlisted `.py` helper under `winbrow/skills/library/**/scripts/`.
  The spec travels as `--spec-json`, never through PowerShell. Path traversal,
  symlinks, non-library paths, oversized specs, and timeouts are rejected.
- **`integration_status`** (`winbrow/integrations.py`, impl `integration`, T0):
  checks whether an optional provider is configured.
- **Integrations panel** (`templates/index.html` + `server.py`):
  `GET /api/integrations`, `POST /api/integrations/{id}`,
  `POST /api/integrations/{id}/test`, `DELETE /api/integrations/{id}`,
  `POST /api/integrations/google/oauth`.
- Secrets live in `data/integrations.json` (gitignored), encrypted with Windows
  DPAPI where available, masked on every read, with environment variables taking
  precedence. They are never written into a SKILL.md or planner history.
- Gated skills must call `integration_status` first and emit `clarify` with the
  setup path when unconfigured; never simulate success.

## 8. Subagents

`winbrow/subagents.py` implements scoped child planners:

- `delegate_task` (T1, `impl="subagent"`) accepts one task or a JSON `tasks`
  array (max 4). Delegation is **automatic by default**:
  `AgentBrain.handle` sets `subagents_allowed` via
  `delegation_allowed_for(utterance)` (true unless disabled). Set
  `WINBROW_SUBAGENTS_EXPLICIT=1` to require an explicit user request.
  `_run_step` rejects the tool when disabled/explicit-only. Explicit and
  independent-research candidates are forced into the brain before the browser
  fast path. Profiles: `researcher`, `file-analyst`, `writer`,
  `code-reviewer`, `verifier`, `general`.
- Each child is a real `AgentBrain` with a **filtered skill registry**, a
  **ScopedToolRegistry** view, its own `SubagentSession`, no `ask_user_fn`, no
  `tool_builder`, `enable_skill_writer=False`, and `allowed_skills` enforced in
  `_run_step` before any meta/playbook/tool branch.
- Child actions still call the parent `agent.run_skill`, so the T0–T3 gate is
  unchanged. Browser and write profiles are sequential; only `file-analyst` is
  parallel-safe. Results and step summaries are recorded in
  `data/subagents.json` (last 50) and exposed at `GET /api/subagents`.
- The converted `delegate-task/SKILL.md` is the planner-facing contract.
  Never delegate destructive work as implicit consent; the child still needs
  its own permission approval.

## 9. Adding a new brain skill

1. Pick a dispatcher key that already exists in the registry (or add the tool first).
2. Create `winbrow/skills/<kebab-name>/SKILL.md` with the required frontmatter.
3. Write a **trigger-focused** description (when to use / when not to use), not a summary.
4. Keep the body procedural: exact steps, args, recovery, and verification.
5. Do not duplicate a `PREFERRED_ORDER` key.
6. Restart or let the registry scan; the planner sees it in metadata immediately afterward.
7. If the tool mutates, confirm its permission tier is correct.

## 10. Adding a procedural replay skill

1. Prefer capturing a real successful run; do not hand-write unless necessary.
2. Keep steps ≤ 12 and use only registered tools.
3. Parameterize variable values with `{{name}}`; only `basename|filename`, `lower`, `upper` filters
   are supported.
4. Save as `proposed`, review the steps, then approve.
5. Never store browser workflows as replay skills; they are skipped by design.

## 11. Risks and sharp edges

- **Writer cost:** each eligible task can trigger a model call. Disable with `WINBROW_SKILL_WRITER=0`.
- **Specificity guards are heuristic:** a generic-looking but single-site skill can still pass.
- **No overwrite:** fixing a generated skill means editing/deleting its folder manually; the writer
  will not replace it.
- **Folder-name truncation:** `_slugify` caps at 60 chars; `create-folder-and-also-make-a-summ` is
  evidence that a garbled goal can become a permanent skill name.
- **Metadata budget:** the block is now ~9,626 chars for 69 skills against a
  12,000-char limit. Adding many verbose playbooks will trigger the warning and
  a shorter ladder; measure after every batch.
- **Playbook turn cost:** a playbook costs one extra planner turn because it is
  guidance, not an action. The prompt forbids two playbooks in a row and treats
  a playbook call as non-completion.
- **Script runner surface:** `run_skill_script` executes Python helpers under the
  skill library at T2. It rejects non-library paths, traversal, symlinks, and
  oversized specs, but a malicious helper committed to the library would still
  run. Review helper diffs like code.
- **Integration secrets:** stored under `data/integrations.json`; DPAPI is used
  on Windows, values are masked, and env vars win. Never print a decrypted value
  or put it in SKILL DETAIL.
- **Subagent cost and races:** every child is a full planner loop; a four-task
  call can multiply model cost. Browser profiles share one tab and write
  profiles share files, so they are forced sequential. Only `file-analyst` is
  parallel-safe.
- **Replay blindness:** a replay plan does not re-observe the world; a changed desktop can make a
  stale plan "succeed" or fail halfway.
- **Status gate:** only `approved` records replay. A capture that looks successful but stays
  `proposed` will never run.
- **Two stores:** deleting a brain skill does not delete a replay record, and vice versa.
