---
knowledge: 02-browser-automation
last_verified: 2026-10-25
source_files:
  - winbrow/browser_task.py
  - winbrow/browser_tools.py
  - winbrow/ultrafast.py
  - winbrow/accessibility.py
  - winbrow/skills/browser-navigate/SKILL.md
  - winbrow/skills/browser-read-page/SKILL.md
  - winbrow/skills/browser-read-full/SKILL.md
  - winbrow/skills/browser-accessibility/SKILL.md
  - winbrow/skills/browser-interact/SKILL.md
  - winbrow/skills/browser-upload-file/SKILL.md
  - winbrow/skills/browser-wait/SKILL.md
  - winbrow/permissions.py
---

# 02 — Browser Automation

Verified 2026-10-25 against `browser_task.py` (2015 lines), `browser_tools.py` (1018),
`ultrafast.py` (1222), `accessibility.py` (337).

## 1. Module roles

| Module | Role | Used by |
|---|---|---|
| `browser_task.py` | Transport/controller: browser detection, CDP backends, tabs, JS, clicks, screenshots, upload primitives, PowerShell fallback | `agent.py`, `ultrafast.py`, `browser_tools.py` |
| `browser_tools.py` | Brain-facing primitives: 7 tools + result rendering + deterministic element matching | `agent.run_skill` via `impl="browser"` |
| `ultrafast.py` | Per-step DOM/Laya browser loop; one-shot fallback when the brain is unavailable | `browser_tools`, `browser_task.run_ultrafast_task`, `agent.py` |
| `accessibility.py` | Standalone UIAutomation foreground tree for desktop (not browser DOM) | **Nothing** — currently unreferenced |

The brain uses **`browser_tools.py`**; `browser_task.py` is its backend. `browser_task.py` is never
imported by the brain directly.

## 2. CDP backends

Port is hard-coded: `_CDP_PORT = 9222` (`browser_task.py:57`).

### 2.1 HTTP backend (legacy / separate profile)

- Discovery: `GET http://127.0.0.1:9222/json/version`; a 404 still means "server up" in consent mode
  (`_cdp_status`, `:213-228`).
- Tabs: `GET /json/list` filtered to `type == "page"` (`:682-712`).
- New tab: `PUT /json/new?url` with GET retry on 404/405 (Chrome ≥119 requires PUT; `:740-754`).
- Activate: `GET /json/activate/{id}` (`:786-797`).
- Commands: a fresh page WebSocket per `_page_conn` call (`:799-816`), wrapped in `_HttpPageConn`
  (`:440-463`). One in-flight command per socket; no event multiplexing.

### 2.2 Consent-mode persistent WebSocket

- Discovery: read `<User Data>/DevToolsActivePort` (line 1 = port, line 2 = path), liveness-check
  with `socket.create_connection(..., 0.4)`, return `ws://127.0.0.1:{port}{path}` (`:249-276`).
- `_BrowserCDP.ensure()` connects once with `ping_interval=None, open_timeout=30` and waits for the
  user's single "Allow" prompt (`:314-336`). The client is a process singleton (`:412`).
- `_reader_loop` resolves futures by `id`; events without an `id` are discarded (`:338-360`).
- `call(method, params, session_id, timeout=15)` allocates ids and awaits a future (`:362-392`).
- `attach(target_id)` issues `Target.attachToTarget {flatten: true}` and caches the `sessionId`
  (`:394-406`); `invalidate` drops it (`:408-409`).
- `_WsPageConn.call` attaches then multiplexes; it retries once when the error text contains
  `"ession"` after invalidating (`:472-486`).

**Critical invariant:** never open a new browser-level WebSocket per page in consent mode. Every new
socket triggers another "Allow" prompt and `/json` returns 404. Pages are multiplexed through one
socket.

## 3. Agent tab lifecycle

| Method | Behavior |
|---|---|
| `reset_session()` (`:580-591`) | Clears `_agent_tab_id`, `_task_tab_pending`, `_agent_tab_created`, `_last_page_state`, `_last_page_state_url`. **Does not close the tab.** |
| `begin_task()` / `end_task()` (`:593-599`) | Aliases for `reset_session()` |
| `get_or_create_agent_tab()` (`:613-631`) | Reuses the remembered tab if still present; otherwise opens `about:blank`; sets `_agent_tab_created` |
| `_navigate_tab(tab_id, url)` (`:633-662`) | Re-lists tabs, waits up to 6×0.2 s for a newly-created target, activates, `Page.navigate` (5 s), sleeps 0.5 s |
| `_pick_tab(tabs)` (`:714-738`) | Agent tab wins and is exclusive; if `_agent_tab_id` is set but missing, returns `None` (never hijacks a user tab); otherwise `_current_tab_id`, then first non-internal tab |

`reset_session()` is called at the start of every brain browser task (`agent.py:558`, `:1452`) and
inside `run_ultrafast_task` (`browser_task.py:1999`). `end_task()` is called in a `finally`
(`agent.py:563-564`). The tab is intentionally left open for inspection.

`_task_tab_pending` is written at `:571`, `:588`, `:982` but never read (B-10 in
`05-known-issues.md`).

## 4. Page-state cache

`get_page_state(force_refresh=False)` (`browser_task.py:1267-1301`):

1. Compute the current URL (`_get_current_url`).
2. If the URL equals `_last_page_state_url`, `force_refresh` is false, **and the
   cached snapshot is not blank**, return a shallow copy with `_cached: True`.
3. Otherwise run `_PAGE_STATE_JS` through `Runtime.evaluate`
   (`_fetch_page_state`), falling back to `read_page_text`.
4. Store `_last_page_state` and `_last_page_state_url` **only when the snapshot
   has a title, text, or links**.

Blank snapshots (`_state_is_blank`: no title, no text, no links) are a page that
has not rendered yet — typically right after navigating a slow SPA such as
x.com. `_fetch_page_state` polls the page for up to `_PAGE_READY_WAIT_S` (3 s,
0.4 s interval) while the snapshot stays blank and the URL is http(s)/file.
It returns the blank snapshot with `_blank: True` if the page never fills in.
`browser_tools._state_output` forwards `_blank` and adds a warning telling the
planner to wait and re-read. A blank snapshot is **never cached**, so the next
read re-queries the DOM instead of replaying "(untitled)/(empty)" forever.

The key is still **URL only**, with **no TTL**. `browser_read_page`
(`browser_tools.py:544-548`) and `_read_full` (`:570`) use
`force_refresh=False`; navigate/interact/wait force a refresh. A same-URL SPA
update is invisible unless the caller forces it (B-08, mitigated for the
blank-page case only).

`reset_session()` clears the cache, but recreating the agent tab or `new_tab=True`
does not necessarily clear it before the next same-URL read.

## 5. Brain-facing tools

All seven are `script=""`, `impl="browser"`, `speak="result"`, and every arg is `kind="text"`.
Dispatch is `execute_browser_tool()` (`browser_tools.py:971-1018`) with a hard 20 s
`asyncio.wait_for` (`_BROWSER_OP_TIMEOUT_S = 20.0`, `:38`).

| Registry name | Args (default / clamp) | Mutates? | Notes |
|---|---|---|---|
| `browser_navigate` (`:125`) | `url` required; `new_tab` `"false"` | Yes | Ensures CDP, navigates agent tab, sleeps 1.0 s |
| `browser_read_page` (`:155`) | `url` `""`; `max_chars` 5000 (1000–5000) | No | Uses cached state; returns `success/output/url/title/text/links/method`; adds `_blank` + `_warning` when the page has not rendered |
| `browser_read_full` (`:184`) | `target` `""`; `max_text_chars` 3000 (1000–5000); `max_tree_chars` 3000 (1000–6000) | Reads, but stamps DOM attributes | Parallel page state + a11y harvest |
| `browser_get_accessibility_tree` (`:220`) | `target` `""`; `max_chars` 6000 (1000–12000) | Reads, stamps DOM | Final clip is 9000, so 9001–12000 is undeliverable |
| `browser_upload_file` (`:252`) | `file_path` required (absolute) | Yes | CDP `DOM.setFileInputFiles`; no state read |
| `browser_interact` (`:275`) | `instruction`; `selector` `""`; `value` `""` | Yes | Click/type/submit/upload |
| `browser_wait` (`:310`) | `seconds` 1 (0–10) | No | Sleep + forced re-read |

### 5.1 `browser_read_full`

`_read_full` (`:560-630`) runs `_page_state(browser, max_text, force_refresh=False)` and
`browser.execute_js(_A11Y_JS)` concurrently with `asyncio.gather` (`:570-572`). It then joins:

- page text,
- up to `_MAX_LINKS = 40` links,
- interactive elements rendered by `_render_elements` (`:394-420`),

and clips the whole blob to `_MAX_STATE_CHARS = 9000` (`:614`). Because links are appended before
elements, a link-heavy page can lose the element list. `_truncated_text` is set but only
`_truncated_tree` produces a warning in `annotate_result` (B-17).

### 5.2 Deterministic element matching

`_pick_element` (`:638-670`):

1. Strip action prefixes (`_ACTION_PREFIX_RE`, `:633-635`) and role words.
2. Tokenize labels (length > 1).
3. Score exact substring `label in text` as 1.0; otherwise token overlap
   `|intersection| / |label_tokens|`, accepted at ≥ 0.6.
4. Return the best element if score ≥ 0.6.

This is fast but over-eager for short labels ("Go" inside "google") (B-15). `_interact` (`:725-830`)
prefers an explicit selector, then a file-input shortcut, then `_pick_element`, and only then builds
`LayaUltrafastEngine` and calls `harvest_elements()` → `decide_with_laya()` →
`make_typed_decision()` → `execute_step()`.

### 5.3 Upload flow

Intended sequence (comment at `browser_tools.py:844-858`):

1. Navigate to the target site (e.g. Google Drive).
2. Click the upload trigger with `browser_interact`.
3. Call `browser_upload_file` with an absolute path.
4. Wait/read to verify.

The click side must intercept the native file chooser:

- `click_element` locates the element, scrolls it into view, computes the center (`:1743-1807`).
- `click_at` calls `Page.bringToFront`, enables
  `Page.setInterceptFileChooserDialog {enabled: true}`, dispatches mouse events, and sleeps **0.2 s
  while the page connection is still open** (`:1307-1341`). That 0.2 s is load-bearing.

The upload side (`browser_tools.py:859-955`):

1. Validate `os.path.isfile`.
2. Resolve the private controller API `_tabs` / `_pick_tab` / `_page_conn`.
3. `DOM.getDocument {depth:1}` (10 s), take `root.nodeId`.
4. `DOM.querySelectorAll "input[type='file']"` (10 s).
5. `DOM.setFileInputFiles {nodeId: node_ids[0], files:[path]}` (15 s).
6. Return success/path/method. No event dispatch, no wait, no state read.

Fallback when the private API is absent: `browser.set_file_input("input[type='file']", path)`
(`:881`), which uses `Runtime.evaluate` → `objectId` → `DOM.setFileInputFiles` and dispatches
`input`/`change` (`browser_task.py:1879-1919`).

Limitations: only the **first** file input is used (B-13), and the chooser interception window is
tiny (B-14). The 10 MB Aspose limit exists only in `skills/browser-upload-file/SKILL.md:31,53-55`;
no code enforces it.

## 6. Ultrafast browser engine

`LayaUltrafastEngine` (`ultrafast.py:270`) is a per-step typed-decision loop:

1. `harvest_elements()` injects `_DOM_HARVESTER_JS` and collects visible interactive elements
   (links, buttons, inputs, selects, textareas, ARIA roles, contenteditable, tabindex), capped at
   40 (`:129-218`, `:291-313`).
2. It stamps `data-wb-idx="N"` and builds a selector (`#id` if present, else `[data-wb-idx="N"]`)
   (`:165`, `:181-185`).
3. `decide_with_laya()` offers Laya `el_<index>` for only the first 30 elements and actions
   `click/type/scroll/wait/finish` (`:315-446`).
4. `make_typed_decision()` is the heuristic fallback for flights/search/forms/click/extract
   (`:448-639`).
5. `execute_step()` performs type/click/submit/navigate/scroll/wait/extract/finish through
   `BrowserController` (`:641-832`).
6. `run_task()` loops, with YouTube play/watch special handling and loop protection (`:1009-1222`).

It is used by `browser_tools._interact` when deterministic matching fails, and by
`browser_task.run_ultrafast_task` for the no-brain fallback. It reports success for `submit`,
`navigate`, and `scroll` without checking the result (B-09 in `05`).

## 7. Browser constants

- CDP port: `9222`.
- Consent mode: `DevToolsActivePort`; project docstring references Chromium 144+.
- Separate profile: `%LOCALAPPDATA%\WinBrow\cdp_profile_<browser>` when
  `WINBROW_BROWSER_MODE=separate`.
- Browser order: an explicitly requested browser first (`set_preferred_browser`), else the OS default,
  then Brave → Chrome → Edge. The preference is set from the goal text (`note_browser_from_text`,
  e.g. "open chrome and …" / "in brave") and from browser app-open steps
  (`agent._open_requested_browser` → `launch_with_cdp(guide=False)`); `reset_session()` clears it.
  If the requested browser is not running it is launched with CDP instead of silently attaching to
  another running Chromium.
- Screenshot fallback path: `C:\Users\Public\winbrow_screenshot.png`.
- Inspect pages: `brave://inspect/#remote-debugging`,
  `chrome://inspect/#remote-debugging`, `edge://inspect/#remote-debugging`.
- Upload selector: `input[type='file']`.
- A11y attribute: `data-winbrow-id`; sequence `window.__winbrowIdSeq`.
- Laya attribute: `data-wb-idx`.

## 8. Invariants

1. One browser-level WebSocket in consent mode; pages are multiplexed.
2. Never fall back to a user tab when an agent tab is remembered.
3. Keep the 0.2 s sleep inside the click connection for file chooser interception.
4. Upload must be preceded by the trigger click that creates the file input.
5. Page cache is URL-keyed; force a refresh after any mutation. Blank snapshots are never cached and
   are retried for up to 3 s before being reported.
6. `reset_session()` is the authoritative task reset; `end_task()` must not close the tab.
7. Every browser operation is capped at 20 s; the brain step is capped at 120 s.
8. Tool names must match `SKILL.md` frontmatter, `BROWSER_IMPL`, and `history.BROWSER_SKILLS`.
9. Reads are T0; navigate/interact/wait/upload are T1 (`permissions.py:61-64`).
10. `browser_wait` is clamped to 0–10 s; the final state render is capped at 9000 chars.

## 9. Known browser issues

See `05-known-issues.md` IDs **B-01 … B-20**. Highest-impact:

- `_cdp_send` is an empty stub (`browser_task.py:545-548`).
- `navigate_to` prefixes `https://` onto `chrome://` shortcuts (`:977-978`).
- Blocking `urllib` probes run on the event loop (`:213-228`, `:279-285`).
- `websockets` is undeclared and imported outside the guard (`:327-329`, `:808-809`).
- `_pick_element` trusts short substring matches (`browser_tools.py:659-660`).
- `accessibility.py` is unreferenced and has broken methods (`:163-179`, `:251`, `:269`).
