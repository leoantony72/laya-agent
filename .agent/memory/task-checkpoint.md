# Task Checkpoint — Converted Skill Library + Integrations

## Goal

Convert the 43 entries from the six requested sections of
`ComposioHQ/awesome-claude-skills` into WinBrow-compatible skills, and add a UI
for the optional services those skills need.

## Status

DONE — all six phases implemented; focused tests passing.

## Scope

- 43 playbooks under `winbrow/skills/library/<section>/<name>/SKILL.md`.
- 21 bundled Python helpers, references, and templates.
- `kind: playbook` support, category-grouped metadata, 12k budget.
- `run_skill_script` (T2, allowlisted) and `integration_status` (T0).
- Integrations panel and backend for 8 providers + dependency status.
- 56 focused tests pass; 69 skill definitions load; metadata ~9,626 chars.

## Key decisions

- Playbooks are guidance-only; a playbook step is acknowledged and the body is
  injected as SKILL DETAIL on the next turn.
- Metadata budget raised from 4,000 to 12,000 chars because 43 entries no longer
  fit. Core descriptions stay longest; playbooks use 95 chars.
- Bundled scripts are invoked through one native runner, not shell-string
  interpolation. Specs travel as JSON; the runner is T2.
- Credentials live in `data/integrations.json`, DPAPI-encrypted on Windows,
  masked on read, env vars win. Gated skills must call `integration_status`
  first.
- Upstream proprietary skills (`docx`, `pdf`, `pptx`, `xlsx`) were rewritten,
  not copied. See `winbrow/skills/library/PROVENANCE.md`.

## Verification run in this session

- `python -m pytest winbrow/tests -q` → 27 passed.
- `python -m pytest test_agent.py test_brain_m0.py test_syntax.py -q` →
  48 passed, 1 pre-existing fixture error (`test_agent.py::test_api` has no
  `cmd` fixture).
- Smoke: PDF, PPTX, XLSX, EPUB, DOCX, CSV, PNG, GIF, and a playbook dispatch.
- `node` compile of both inline scripts → 2 scripts, 0 syntax errors.
- `load_skill_registry` → 69 total, 43 playbooks, including native web_search/web_fetch.

## Subagents (2026-10-26)

- Added `winbrow/subagents.py` and `winbrow/skills/delegate-task/SKILL.md`.
- `delegate_task` (T1) spawns scoped `AgentBrain` children with filtered skill
  and tool registries; child actions still use the parent permission gate.
- Delegation is automatic by default; `WINBROW_SUBAGENTS=0` disables it and
  `WINBROW_SUBAGENTS_EXPLICIT=1` requires an explicit request. Independent
  research/review candidates are forced into the brain before the browser fast
  path.
- Profiles: researcher, file-analyst, writer, code-reviewer, verifier, general.
- Only `file-analyst` is parallel-safe; browser/write profiles are serialized.
- Runs persist to `data/subagents.json` and surface in the **Subagents** panel
  plus `GET /api/subagents`.
- Tests: 37 in `winbrow/tests` pass; 48 core tests (`test_brain_m0`,
  `test_syntax`) pass. Real-model subagent execution was not run (no provider
  key); a scripted gateway covers the child-brain path.

## Web access hierarchy (2026-10-26)

- Root cause: old `web_search` was a seed tool that ran `laya_ultrafast_browser`;
  the router preferred the browser for searches; `_is_browser_intent` treated
  `search` as a browser keyword.
- Added `winbrow/web_tools.py`: native `web_search` (DuckDuckGo HTML API) and
  `web_fetch` (HTTP GET, BeautifulSoup/pdfminer), both T0/no-browser.
- Removed the seed `web_search`; registered web tools in `WinBrowAgent`.
- Added `web-search`/`web-fetch` SKILL.md files; rewrote `browser-navigate` to
  interaction-only.
- Appended the verbatim WEB ACCESS HIERARCHY + WORKED EXAMPLES to the end of
  `SYSTEM_PROMPT_STATIC`, immediately before the AVAILABLE SKILLS block.
- Router now sends search/price/comparison to `web_search` and read/fetch to
  `web_fetch`; booking/checkout/form/login/media/explicit-open still use the
  browser.
- `_is_browser_intent` no longer matches bare research/search/site words.
- Added `.claude/knowledge/session-patterns.md`.
- Tests: `winbrow/tests` 49 pass; core brain/syntax 48 pass. No live network
  search was run.

## Brain-first routing (2026-10-26)

- `WinBrowAgent.execute()` now attempts the brain before replay, MCP shortcuts,
  browser fast path, and router whenever a provider is available.
- `_execute_brain_goal()` uses `handle_browser_task()` for browser-intent
  utterances and `AgentBrain.handle()` for everything else. Tier is
  `brain_first`; planner exceptions are `brain_first_error`.
- Replay is skipped while brain-first is active. Legacy paths are fallbacks
  only for `GatewayUnavailable`/`GatewayError`; a normal brain failure never
  falls through to MCP.
- `WINBROW_BRAIN_FIRST=1` default; `0` restores legacy routing for debugging.
- Tests: `test_brain_first.py` proves MCP shortcuts are bypassed when the brain
  is available and that a planner exception does not fall through to MCP.

## Nebius Token Factory (2026-10-26)

- `winbrow/brain/model_gateway.py` now builds Nebius first in `auto` when
  `NEBIUS_API_KEY` is present; `WINBROW_BRAIN_PROVIDER=nebius` forces it.
- Base URL `https://api.tokenfactory.nebius.com/v1`, default model
  `deepseek-ai/DeepSeek-V4-Flash-0731`; both overridable with
  `WINBROW_NEBIUS_BASE` / `WINBROW_NEBIUS_MODEL`.
- Nebius is OpenAI-compatible; the shared reasoning policy maps to
  `reasoning_effort` (medium structured / none chat / low capped retry).
- Settings UI has a Nebius option; `POST /api/settings` stores the key in
  `NEBIUS_API_KEY` and sets `WINBROW_BRAIN_PROVIDER=nebius`.
- Tests: `test_nebius_gateway.py` (4) covers auto order, env model/base,
  provider preference, and reasoning payload translation.

## Session context + artifacts (2026-10-29)

- `SessionMemory.Turn` now stores `artifacts` (absolute paths created/opened)
  and persists them; older turns are backfilled from stored snippets.
- `reference_hint()` binds follow-ups ("where is the pdf?", "open it") to the
  most recent artifact, flags format mismatches (asked PDF, produced HTML), and
  is injected into the brain prompt. `context_block()` shows artifact paths.
- Fixed `test_brain_m0` wiping the real `default` session via
  `agent.session.reset()`; restored the 12-turn default history.
- Tests: `test_session_context.py` + session/loop/brain suites (27 pass).

## Presentation/PDF pipeline (2026-10-29)

- Root cause of broken deck PDFs: the planner replaced `index.html` and linked
  a non-existent `assets/index.css`, dropping `base.css`, the theme, `.deck`,
  and `runtime.js`. Chrome then printed unstyled HTML (3 pages, light text on
  white).
- Added `winbrow/deck_repair.py`: restores required CSS/theme/runtime, wraps
  stray slides in `.deck`, adds the `tpl-*` body class, normalises h1/h2, and
  injects token-based fallback CSS. Idempotent; never changes slide text.
- Added `winbrow/deck_layout.py`: headless Chrome measures each slide's
  overflow; export fails with the exact slide instead of clipping text.
- `html_to_pdf.py`: `auto` page resolves decks to 16:9 (13.333x7.5in) and
  documents/resumes to A4; print CSS forces one slide per page and
  `print-color-adjust: exact`; added legal/a5 sizes.
- `render_deck.py` repairs + layout-checks + validates page count/size before
  reporting success. `pdf_tool.from_html` defaults to `auto` and repairs decks.
- `layout/pdf.py` adds a `resume` theme; `pdf_tool.create` supports
  `document_kind=resume|cover_letter|official`, A4/Letter portrait, no page
  numbers. Optional `latex` operation compiles via pdflatex/xelatex/lualatex/
  latexmk when installed (not installed on this machine).
- Prompt/SKILL rules now forbid replacing scaffolded HTML, require preserving
  asset links, default decks to 16:9, and send resumes/official docs to
  A4/Letter.
- Verification: 24 PDF/deck tests pass; the real
  `remote-execution-vulnerability` deck exported 12 pages at 960x540 with no
  overflow; a sample resume exported A4 595.28x841.89 with no warnings.

## Next actions

- Install optional packages as needed: `pip install -r
  winbrow/skills/library/requirements-skills.txt`.
- Configure credentials in the Integrations panel before using gated skills.
- Live-smoke the Google OAuth flow and the NotebookLM browser flow.
- Run the full repo suite when time allows; `test_agent.py::test_api` needs a
  fixture and is unrelated.
