# Session Log

Append-only. The `Stop` hook (`.agent/hooks/post-session.py`) adds one entry per session.
Permanent findings belong in `.agent/knowledge/`; this file is chronological context.

## 2026-10-25 — Knowledge base bootstrap

- **Scope:** Read the `winbrow/` codebase, then build `AGENT.md`, six knowledge files, task memory,
  and the post-session hook.
- **Key findings:**
  - Browser control is raw CDP (HTTP `/json/*` or one consent-mode WebSocket via
    `DevToolsActivePort`), not Playwright/pydoll/Selenium.
  - There are two independent skill systems: brain markdown skills (`winbrow/skills/*/SKILL.md`,
    progressive disclosure, writer-generated) and procedural replay (`tools/skills.json`, approved
    records only, Jaccard ≥ 0.66).
  - Uncommitted work adds the post-session skill writer and its hot-reload path.
  - `WinBrowAgent.run_skill()` is the single permission + dispatch funnel.
  - The current tree is Windows-first: PowerShell/COM/UIA + ctypes, with a cloud-first planner.
- **Verified issue count:** 58 (`05-known-issues.md`; browser 20, desktop 14, routing/generation 14,
  IO/memory/voice 10).
- **Documented decisions:** 12 (`06-recent-changes-and-decisions.md`).
- **Verification:** Source inspection only. No live CDP, UIA, COM, voice, or end-to-end task was run.
  No test suite was executed for this bootstrap; do not claim otherwise.
- **Stale knowledge:** none at bootstrap. `stale.md` is created only by the hook when a source file
  is newer than a knowledge file's `last_verified` date.

## 2026-10-25 10:52 - session

- **Summary:** Bootstrap verification: knowledge files, hook, and settings validated; no stale knowledge.
- **Changed files:** `M tools/skills.json`, `M winbrow/agent.py`, `M winbrow/brain/agent_loop.py`, `M winbrow/brain/skill_registry.py`, `?? .agent/`, `?? AGENT.md`, `?? winbrow/brain/skill_writer.py`, `?? winbrow/tests/`
- **Knowledge:** 6 file(s) present; 0 stale
- **Auto-refresh:** not needed: no stale knowledge

## 2026-10-25 - converted skill library

- **Summary:** Added 43 playbook skills from the six requested sections, 21 bundled helpers, playbook dispatch, the script runner, and the Integrations panel/backend.
- **Changed files:** winbrow/skills/library/**, winbrow/skill_scripts.py, winbrow/skill_script_utils.py, winbrow/integrations.py, winbrow/brain/skill_registry.py, winbrow/brain/agent_loop.py, winbrow/brain/prompt.py, winbrow/agent.py, winbrow/permissions.py, server.py, templates/index.html, winbrow/tests/*
- **Verification:** pytest winbrow/tests -q -> 27 passed; pytest test_agent.py test_brain_m0.py test_syntax.py -q -> 48 passed, 1 pre-existing fixture error; smoke PDF/PPTX/XLSX/EPUB/DOCX/CSV/PNG/GIF; 65 skills, 43 playbooks, metadata ~9,060 chars.
- **Knowledge:** 03 and 06 updated; no stale knowledge.

## 2026-10-26 - scoped subagents

- **Summary:** Added delegate_task, six scoped subagent profiles, automatic delegation by default, filtered skill/tool registries, child-brain execution through the parent permission gate, parallel-safe file analysis, run history, and a Subagents UI panel. Independent research/review candidates are forced into the brain before the browser fast path.
- **Changed files:** winbrow/subagents.py, winbrow/skills/delegate-task/SKILL.md, winbrow/agent.py, winbrow/brain/agent_loop.py, winbrow/brain/prompt.py, winbrow/brain/skill_registry.py, winbrow/permissions.py, server.py, templates/index.html, winbrow/tests/test_subagents.py, docs.
- **Verification:** pytest winbrow/tests -> 37 passed; pytest test_brain_m0.py test_syntax.py -> 48 passed; scripted-gateway child brain test; node UI syntax 0 errors.
- **Limitations:** no live model-provider run; browser subagents are intentionally serialized. Set WINBROW_SUBAGENTS=0 to disable or WINBROW_SUBAGENTS_EXPLICIT=1 to require an explicit request.
