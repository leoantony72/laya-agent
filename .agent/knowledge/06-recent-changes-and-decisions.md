---
knowledge: 06-recent-changes-and-decisions
last_verified: 2026-10-25
source_files:
  - .git
  - winbrow/brain/skill_writer.py
  - winbrow/brain/agent_loop.py
  - winbrow/brain/skill_registry.py
  - winbrow/agent.py
  - winbrow/tests/test_skill_writer.py
  - tools/skills.json
  - winbrow/browser_task.py
  - winbrow/router.py
---

# 06 — Recent Changes and Decisions

Verified 2026-10-25. This file records **why** the code looks the way it does and which surfaces are
most likely to drift. It is the first file to update after a refactor.

## 1. Git state at verification time

- HEAD: `b82cfb2` — `fix: file picker window` (2026-10-22 23:37:50 +0530).
- Previous: `348b213` — `fix: upload file issue, browser click issue`.
- Previous: `58c7e8a` — `fixed: browser tab issue, app opening mismatch, toggle play/pause issue`.

Uncommitted working tree:

```
 M tools/seed.json
 M tools/skills.json
 M winbrow/agent.py
 M winbrow/brain/agent_loop.py
 M winbrow/brain/skill_registry.py
 M winbrow/browser_task.py
 M winbrow/browser_tools.py
?? .agent/
?? test_browser_fixes.py
?? winbrow/brain/skill_writer.py
?? winbrow/tests/
```

The uncommitted work is the **post-session skill writer**:

- New `winbrow/brain/skill_writer.py` (721 lines): analysis prompt, JSON schema, specificity guards,
  atomic `SKILL.md` write, hot reload.
- `brain/agent_loop.py`: `_fire_skill_writer()` and `_maybe_save_skill()`; fired on completed
  actions/done.
- `brain/skill_registry.py`: live metadata rebuild / hot-reload support.
- `winbrow/tests/test_skill_writer.py` (221 lines): guard/write tests.
- `tools/skills.json`: replay records touched by the same work.

Do not assume this work is committed or deployed. If you change the writer, update
`03-skill-system.md` and this file.

## 1a. Browser task regression fixes (2026-10-25)

Trace: `"open chrome and go to x.com and search for onepiece"` opened Notepad,
used Brave after launching Chrome, and then every read of the x.com search page
returned `Title: (untitled)` / `PAGE TEXT: (empty)`.

Causes and fixes:

1. **Poisoned page cache** — `browser_navigate` snapshotted x.com while the SPA
   was still committing (URL set, title/body empty) and `get_page_state` cached
   it by URL. Later reads replayed the blank snapshot forever.
   Fix: `_fetch_page_state` polls up to 3 s for a non-blank snapshot;
   `_state_is_blank` snapshots are flagged `_blank` and never cached/served.
2. **Repeat guard blocked re-reads** — `browser_wait({seconds:3})` was treated as
   an already-completed step and the planner was handed the *old* Home/X result.
   Fix: `REPEATABLE_SKILLS` (reads/waits/time) bypass the completed-step guard.
3. **Notepad default** — `app_focus_or_launch` defaulted `app_name` to
   `"notepad"` in both `agent.py` and `tools/seed.json`, so an omitted argument
   opened Notepad. Fix: empty default; missing name fails with a clear message.
4. **`open_common_app` unknown: chrome** — browsers were not in the enum/map.
   Fix: enum + script entries, and `agent.run_skill` routes browser names to
   `_open_requested_browser`.
5. **Chrome launched, Brave used** — the browser layer attached to whichever
   Chromium had CDP. Fix: `set_preferred_browser()` remembers the requested
   browser (`_browser_order` puts it first, `_devtools_ws_endpoint` refuses to
   attach to a different browser when the requested one is not running), and
   browser app-open steps call `launch_with_cdp(guide=False)`. `reset_session()`
   clears the preference; navigate output now reports the browser used.

Regression tests: `test_browser_fixes.py` (13 tests).
Known pre-existing unrelated failures at HEAD: router tests
`test_multistep_browser_routes_to_brain`, `test_download_with_url_routes_to_tool`,
`TestEditorSequence` (save variants).

## 2. Recency map

| Surface | Last meaningful change | Risk |
|---|---|---|
| Browser page-state cache / readiness | 2026-10-25 (this session) | Medium; see "Browser task regression fixes" below |
| Browser app preference (`set_preferred_browser`) | 2026-10-25 (this session) | Medium; Chrome/Edge/Brave routing |
| Browser upload/tab/click | 2026-10-22 (`b82cfb2`, `348b213`) | **Highest**; actively churning |
| Router / ultrafast | 2026-10-22 (`58c7e8a`) | High; routing regressions common |
| `windows.py` / `apps.py` | 2026-10-22 | High; app launch fixes |
| Skill writer | Uncommitted, 2026-10-25 | High; new and not fully exercised |
| `browser_tools.py` / `browser_task.py` | 2026-10-22 | High; see `05` B-01…B-20 |
| `generator.py` | 2026-10-17 | Medium |
| `llm.py` / `model_manager.py` | 2026-10-19 | Medium; known broken `_request` |
| `router.py` / `resolvers.py` / `small_llm.py` | 2026-10-04 (resolvers/small_llm) | Stale; orphaned option architecture |
| `accessibility.py` | 2026-10-05 | Dead code |
| `vision.py` | Older, unreferenced | Dead/broken |

## 3. Documented decisions (12)

D1. **Raw CDP over a browser framework.** Avoid Playwright/pydoll/Selenium; keep the user's real
browser and logins. Accept hand-rolled transport complexity.

D2. **Two browser transports.** HTTP `/json/*` for a separate/legacy profile; one persistent
consent-mode WebSocket for the user's normal browser. Never open a browser-level socket per page in
consent mode.

D3. **Agent tab isolation.** The agent gets its own tab per task; `_pick_tab()` returns `None`
rather than touching a user tab if that tab disappeared. `end_task()` leaves the tab open for
inspection.

D4. **URL-keyed page cache.** Cheap repeated reads; correctness depends on callers forcing a refresh
after mutations. No TTL.

D5. **Precedence ladder, not a single router.** Replay → MCP shortcuts → browser fast path → router →
brain → generator. Each tier can return early; failures fall through.

D6. **Browser tasks always use the brain loop when available.** Replay is deliberately skipped for
browser intents because live web state invalidates recorded assumptions.

D7. **One permission gate at `run_skill()`.** No tool may bypass it. T3 is never remembered; T2 can
be remembered by exact action key.

D8. **Progressive-disclosure markdown skills.** Metadata only in the system prompt; full body
injected after selection. Skills are instructions, not executable plugins.

D9. **Two independent skill memories.** Brain markdown skills (writer) and procedural replay JSON
(captured plans) are not merged. Browser workflows are excluded from replay.

D10. **Post-session skill writer is asynchronous and conservative.** Minimum 3 successful steps,
specificity guards, no overwrite, hot reload, all exceptions swallowed.

D11. **Cloud-first brain with optional local fallback.** OpenRouter/DeepSeek default; llama.cpp/Qwen
only when explicitly enabled. Reasoning budgets are capped because hidden reasoning consumes
`max_tokens`.

D12. **Windows-native execution.** PowerShell + COM + UIA + ctypes, with app adapters rather than a
generic GUI framework. `policy.py` is a safety net, not a sandbox.

## 4. What changed most recently and why it matters

### 4.1 Skill writer (uncommitted)

The writer was added to turn repeated successful workflows into reusable brain skills. It is
**not** the same as replay memory:

- Writer output: `winbrow/skills/<name>/SKILL.md`, chosen by the planner later.
- Replay output: `tools/skills.json`, executed without the planner.

Consequences:

- A generated skill can change planner behavior immediately (hot reload).
- A bad generated skill is persistent; the writer refuses to overwrite it.
- The writer costs one model call per eligible task; disable with `WINBROW_SKILL_WRITER=0`.

### 4.2 Browser upload/click fixes

Recent commits targeted the Google Drive upload flow: intercept the native chooser during the click,
then set the file input via CDP. The load-bearing details are the 0.2 s connection hold
(`browser_task.py:1338-1340`) and the private `_tabs`/`_pick_tab`/`_page_conn` path in
`browser_upload_file`. Refactors that "clean up" either one tend to break upload.

### 4.3 Tab lifecycle fixes

Recent commits also fixed tab hijacking: the agent tab is exclusive, and a missing agent tab is not
silently replaced by a user tab. Keep that behavior even when reads return empty; surface the
failure instead.

### 4.4 App launch fixes

`windows.focus_or_launch_app()` and `apps.resolve_app()` were touched for app-opening mismatches.
The remaining sub-threshold launch bug (D-02) is a known risk in that area.

## 5. Stale-risk checklist

Mark a knowledge file stale when any of these change:

| Knowledge file | Watch these sources |
|---|---|
| `01-system-architecture.md` | `server.py`, `agent.py` init, `requirements.txt`, `.env.example` |
| `02-browser-automation.md` | `browser_task.py`, `browser_tools.py`, `ultrafast.py`, browser `SKILL.md` |
| `03-skill-system.md` | `brain/skill_registry.py`, `brain/skill_writer.py`, `memory/skills.py`, `skills/` |
| `04-request-data-flow.md` | `agent.py` `execute()`/`run_skill()`, `router.py`, `brain/agent_loop.py`, `permissions.py` |
| `05-known-issues.md` | Any file cited in a row; remove fixed rows |
| `06-recent-changes-and-decisions.md` | Git history, uncommitted files, architectural decisions |

The `Stop` hook compares each file's `last_verified` date with the mtimes of its `source_files`.
A source edited after `last_verified` marks the knowledge file stale.

## 6. Refresh procedure

1. Read `.agent/memory/stale.md`; pick the highest-risk file.
2. Read only the changed source files (use `git diff -- <paths>` first).
3. Update the knowledge file's facts and line numbers.
4. Bump `last_verified` to today.
5. If the change affects a decision, add a new `D<n>` entry here; do not silently rewrite history.
6. Remove the file from `stale.md` (or let the next hook run rebuild the list).
7. Add a session-log entry describing what changed and what was verified.

## 7. Verification honesty

The knowledge base was built by reading source, not by running the product. No live CDP session,
UIA tree, COM automation, voice loop, or end-to-end task was executed for these documents. Treat
runtime claims as "what the code says", not "what was observed". When you run something, record the
exact command and result in the session log.
