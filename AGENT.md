# How to Use This File — Read First

This file is the durable entry point for the **WinBrow** repository. It is a map, not a
substitute for source. Read it once per session, then open only the knowledge file that
matches the task.

## Read order (3-file budget)

1. **This file.**
2. **`.agent/memory/stale.md`** — if it exists, read it before trusting any knowledge file.
3. **One** file from `.agent/knowledge/` that matches the task. Do not read all six.
4. For active work only: `.agent/memory/task-checkpoint.md`, then `.agent/memory/task-ledger.md`.

Typical sessions need at most three files: `AGENT.md` + `stale.md` + one knowledge file.
Task memory is separate from permanent knowledge and may be deleted/reset at any time.

## Trust rules

- Knowledge files are compressed snapshots verified on **2026-10-25**. Code moves; they can drift.
- **Never trust a knowledge file listed in `.agent/memory/stale.md`.** Treat it as a hint only.
  Verify every claim against source before changing code or answering a "how does it work" question.
- If a knowledge file conflicts with source, **source wins**. Fix the knowledge file in the same session.
- If `stale.md` lists a file, read the relevant source modules directly and update the knowledge file.
- Never claim tests passed unless you actually ran them in this session. `pytest` is not proof of
  runtime behavior for the browser/desktop paths.
- Never invent a tool, flag, file, or environment variable. Grep the repo first.

## Context budget and resume protocol

| Zone | Context used | Action |
|---|---|---|
| Green | < 50% | Read one knowledge file + targeted source. Work normally. |
| Amber | 50–70% | Stop broad exploration. Use `grep`/`glob`, not whole-file reads. Update the checkpoint. |
| Red | > 70% | Write a handoff in `task-checkpoint.md`, update `task-ledger.md`, then summarize and stop. |

At every milestone:

1. Update `.agent/memory/task-checkpoint.md` with goal, current milestone, decisions, next actions.
2. Add/refresh one row in `.agent/memory/task-ledger.md` (status, files, verification, result).
3. If you changed source behavior, update the matching knowledge file and its `last_verified` date.
4. Add a session-log entry at the end (the `Stop` hook does this automatically).

If a session is resumed, read `task-checkpoint.md` first. Do **not** re-read the whole codebase;
trust the checkpoint and only verify the files named in "Next actions".

## Knowledge files (read on demand)

@.agent/knowledge/01-system-architecture.md
@.agent/knowledge/02-browser-automation.md
@.agent/knowledge/03-skill-system.md
@.agent/knowledge/04-request-data-flow.md
@.agent/knowledge/05-known-issues.md
@.agent/knowledge/06-recent-changes-and-decisions.md

| File | Open when you need… |
|---|---|
| `01-system-architecture.md` | Repo map, entry points, startup order, dependency/config facts |
| `02-browser-automation.md` | CDP backends, agent tab, page cache, browser tools, upload flow |
| `03-skill-system.md` | `SKILL.md` format, progressive disclosure, skill writer, replay memory |
| `04-request-data-flow.md` | End-to-end routing, permissions, dispatch, brain loop, history, memory |
| `05-known-issues.md` | 58 verified bugs/risks with `file:line` and mitigations |
| `06-recent-changes-and-decisions.md` | Git state, uncommitted work, architectural decisions, refresh steps |

---

# WinBrow — Agent Guide

WinBrow is a Windows desktop + browser control agent. A user types or speaks a goal; the
system routes it through cheap deterministic paths first, then a cloud planner (the "brain"),
then a generated-PowerShell fallback. Browser control is **raw Chrome DevTools Protocol (CDP)**,
not Playwright/pydoll/Selenium.

## What this project is not

The following assumptions appear in some task templates but **do not exist in this repo**:

- No `pydoll`, Playwright, or Selenium dependency. Browser control is CDP over port `9222` or a
  consent-mode `DevToolsActivePort` WebSocket.
- No `winbrow/browser/controller.py` module. The controller is `winbrow/browser_task.py`.
- `screen_read_text` is **not** a mandatory executor step for desktop actions. It exists in
  `winbrow/computer/tools.py` and is used only when a skill explicitly calls it.
- There is no 3-step history window. `history.FULL_WINDOW = 5`; browser steps get extra detail
  for the last 3 (`BROWSER_DETAIL_STEPS = 3`).
- There is no single "System 1 / System 2" class. The split is a precedence chain in
  `WinBrowAgent.execute()`.

## Golden rules

1. **All tool execution goes through `WinBrowAgent.run_skill()`** (`winbrow/agent.py:346`).
   It is the single permission gate and the single dispatch point. Do not call atom functions
   directly from new routes.
2. **Permissions are tiered T0–T3** (`winbrow/permissions.py`). T3 is never remembered. T2 can
   be remembered. `WINBROW_BYPASS_CONFIRM=1` disables the gate entirely — never set it in tests
   that assert safety.
3. **Two skill systems exist and must not be confused:**
   - Brain skills: `winbrow/skills/*/SKILL.md`, progressive disclosure, generated by the skill writer.
   - Procedural replay: `tools/skills.json`, Jaccard matching, only `approved` records replay.
4. **The browser agent tab is isolated.** `_pick_tab()` returns `None` rather than hijacking a
   user tab when the remembered agent tab is gone. Do not "fix" this into a fallback.
5. **`reset_session()` clears references and cache but leaves the tab open** on purpose. The user
   inspects the result after the task.
6. **Page state is cached by URL only, with no TTL.** Pass `force_refresh=True` after any mutation.
7. **Policy regexes are a safety net, not a sandbox.** Generated scripts run with
   `-ExecutionPolicy Bypass`. Treat `policy.py` gaps as real risk (see `05-known-issues.md`).
8. **Do not add a new tool without**: a `Tool`/`ArgSpec` definition, a tier in
   `permissions.TOOL_TIERS` (or a clear default), a dispatcher branch in `run_skill`, and a
   matching `SKILL.md` if the brain should see it.
9. **PowerShell quoting is the most common bug source.** Always single-quote and escape embedded
   apostrophes; never interpolate user text into a double-quoted PowerShell string.
10. **Tests:** `pytest` files exist under `winbrow/tests/` and repo root. They do not cover live
    CDP/UIA. Record exactly what you ran.

## Fast orientation

| Question | Look at |
|---|---|
| Where does a request enter? | `server.py` (`/api/execute`), `run.py`/`start.bat`, `winbrow/cli.py` |
| What orchestrates a command? | `winbrow/agent.py` → `WinBrowAgent.execute()` (`:1033`) |
| What plans multi-step work? | `winbrow/brain/agent_loop.py` → `AgentBrain.handle()` (`:543`) |
| What talks to the LLM? | `winbrow/brain/model_gateway.py` |
| What prompts the planner? | `winbrow/brain/prompt.py` (`SYSTEM_PROMPT_STATIC`, `build_loop_messages`) |
| What routes one-shot commands? | `winbrow/router.py` → `WinBrowRouter.route()` (`:979`) |
| What controls the browser? | `winbrow/browser_task.py` (`BrowserController`) |
| What exposes browser tools? | `winbrow/browser_tools.py` (`get_browser_tools`, `execute_browser_tool`) |
| What controls the desktop? | `winbrow/computer/*`, `winbrow/windows.py`, `winbrow/adapters/*` |
| What stores memory? | `winbrow/memory/{store,session,skills}.py`, `winbrow/last_opened.py` |
| What gates dangerous actions? | `winbrow/permissions.py`, `winbrow/policy.py` |
| What generates new tools? | `winbrow/generator.py` |
| What writes new skills? | `winbrow/brain/skill_writer.py` |

## Repository map

```
server.py                 FastAPI app, global WinBrowAgent, REST + WebSocket
run.py / start.bat        Launchers
winbrow/
  agent.py                Orchestrator: replay → browser → router → brain → generator
  router.py               Laya + heuristics + optional Ollama router
  ultrafast.py            Per-step DOM/Laya browser engine (one-shot fallback)
  generator.py            LLM-authored PowerShell tools + offline synthesizer
  browser_task.py         CDP transport, tab lifecycle, page cache, screenshots
  browser_tools.py        Seven brain-facing browser tools
  accessibility.py        Standalone UIA tree (currently unreferenced)
  computer/               Native input, UIA, OCR, VLM grounding, atoms
  adapters/               App-specific compound ops (Notepad, Office, Paint, PS, AE, VS Code)
  brain/                  Planner loop, model gateway, prompts, skill registry/writer
  memory/                 SQLite episodic store, session memory, replay skills
  permissions.py          T0–T3 gate, pending confirmations, allowlist
  policy.py               Hard-block regexes + script tiering
  registry.py             Tool/ArgSpec model, seed + learned tools
  mcp.py                  MCP client bridge (stdio + HTTP)
  voice.py/stt.py/tts.py  Push-to-talk/wake loop, Whisper, SAPI
  skills/                 19 curated SKILL.md + learned/
tools/                    seed.json, learned.json, skills.json, permissions.json,
                          session_state.json, mcp_servers.json, mcp_cache.json
data/                     memory.db, chroma/, observe/
```

## Runtime facts that bite

- Python **3.10+** syntax (`X | None`, built-in generics). Dev environment seen: 3.13.
- `psutil` is a **hard** import in `winbrow/windows.py:23`; `websockets` is used by
  `browser_task.py` but is **not** in `requirements.txt`.
- `server.py` constructs `WinBrowAgent()` at import (`:74`). Import failures in agent
  dependencies break the whole server.
- `server.py` does not pass `ask_user_fn` to the agent, so clarify returns `needs_input=True`
  rather than blocking for an interactive answer.
- The brain is cloud-first (`OPENROUTER_API_KEY`). Local llama.cpp/Qwen starts only when
  `WINBROW_LOCAL_LLM=1` or `WINBROW_BRAIN_PROVIDER=local|qwen`.
- Default brain model: `deepseek/deepseek-v4.1-flash`; planner `max_tokens=16000`,
  `temperature=1`; browser-heavy turns use 8000 total / 600 reasoning.
- `agent_loop.MAX_STEPS_DEFAULT=20`, `STEP_TIMEOUT_S=120`, `STEP_WARN_REMAINING=5`.
- `history.FULL_WINDOW=5`, `RECENT_OUTPUT_LIMIT=400`, `BROWSER_DETAIL_STEPS=3`.
- SQLite memory lives at `data/memory.db`; procedural replay at `tools/skills.json`.

## Change checklist

Before editing:

- [ ] Read the relevant knowledge file and the actual source file.
- [ ] Check `.agent/memory/stale.md`; if the file is listed, verify source first.
- [ ] Grep for every caller of the function you are changing.

After editing:

- [ ] Update the matching `SKILL.md` if a tool's args/behavior changed.
- [ ] Update `permissions.TOOL_TIERS` if the new action is not T1.
- [ ] Run the narrowest useful test/syntax check and record the exact command.
- [ ] Update the knowledge file and bump `last_verified`.
- [ ] Update `task-checkpoint.md` and `task-ledger.md`.

## Definition of done

- The change works through `run_skill()` (not a side door).
- Permission tier is correct and T3 is not remembered.
- Errors return `{success: False, output: "..."}` instead of raising across the API boundary.
- Knowledge files and task memory reflect reality; no stale claim is left unmarked.
