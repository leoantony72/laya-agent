# Task Checkpoint — Browser Task Regression Fixes

## Goal
Fix the trace `"open chrome and go to x.com and search for onepiece"`:

1. Chrome was launched but browsing happened in Brave.
2. Notepad opened for no reason.
3. After navigating to the x.com search URL, every page read was
   `Title: (untitled)` / `PAGE TEXT: (empty)`.

## Status
DONE — code fixes implemented, regression tests written and passing.

## Decisions
- Blank page snapshots (no title/text/links) are retried for up to 3 s and are
  never cached. A blank snapshot is flagged `_blank` and annotated with a
  "still loading" warning so the planner waits instead of acting.
- Read-only observations (`browser_read_page`, `browser_read_full`,
  `browser_get_accessibility_tree`, `browser_wait`, `screen_read_text`,
  `current_time`) bypass the completed-step repeat guard. Repeating them is how
  the planner refreshes its view; `detect_stuck` still catches true loops.
- `app_focus_or_launch` no longer defaults to Notepad. A missing app name is a
  clear failure.
- Browser names route through `agent._open_requested_browser` →
  `set_preferred_browser` + `launch_with_cdp(guide=False)`, so an explicit
  "open Chrome" launches Chrome *with* CDP instead of letting the next browser
  step attach to the default Brave window.
- The preferred browser is task-scoped: `reset_session()` clears it.

## Files changed
- `winbrow/browser_task.py` — blank-state polling/flagging, cache skip,
  browser preference, `_cdp_browser_hint`, navigate reports browser used.
- `winbrow/browser_tools.py` — `_state_output` returns `text`/`links`, emits
  `_blank` + warning, navigate output names the browser.
- `winbrow/brain/agent_loop.py` — `REPEATABLE_SKILLS`, blank-page annotation,
  warning merge, skill-writer imports (pre-existing uncommitted work).
- `winbrow/agent.py` — `_open_requested_browser`, no Notepad default,
  `open_common_app` browser routing.
- `tools/seed.json` — app default, browser entries for `open_common_app`.
- `test_browser_fixes.py` — 13 regression tests.
- `.agent/knowledge/02`, `.agent/knowledge/05`, `.agent/knowledge/06`.

## Verification (run in this session)
- `python -m py_compile winbrow/{browser_task,browser_tools,brain/agent_loop,agent}.py` → OK.
- `python -m pytest test_browser_fixes.py` → 13 passed.
- `python -m pytest test_m2_adapters.py` → 78 passed, 3 skipped; 1 failure
  pre-existing at HEAD (`test_multistep_browser_routes_to_brain`).
- `python -m pytest test_scenarios.py` → 3 passed (a first run failed, then
  passed; flaky, HEAD also passes).
- `test_m5_accelerators.py::TestEditorSequence` fails identically on a clean
  HEAD worktree (router save-routing), not caused by these changes.

## Next actions
- Live-run the original utterance with a real browser to confirm end-to-end
  (not done: no live CDP run in this session).
- Consider a router fast-path for `"open <browser> and go to <url>"` so the
  planner does not spend steps on app-launch/run-command detours.
- Optional: add a TTL/content hash to the page cache for same-URL SPA updates
  (B-08 is only partially fixed).

## Notes for a fresh session
- Start with `AGENT.md` → `stale.md` (if present) → one knowledge file.
- Do not re-read the whole codebase. Use this checkpoint and targeted `grep`.
- The knowledge-base build history is in `.agent/memory/task-ledger.md` and
  `.agent/knowledge/06-recent-changes-and-decisions.md`.
