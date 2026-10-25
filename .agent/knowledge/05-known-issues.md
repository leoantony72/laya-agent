---
knowledge: 05-known-issues
last_verified: 2026-10-25
source_files:
  - winbrow/browser_task.py
  - winbrow/browser_tools.py
  - winbrow/accessibility.py
  - winbrow/computer
  - winbrow/windows.py
  - winbrow/apps.py
  - winbrow/registry.py
  - winbrow/router.py
  - winbrow/ultrafast.py
  - winbrow/generator.py
  - winbrow/llm.py
  - winbrow/model_manager.py
  - winbrow/permissions.py
  - winbrow/mcp.py
  - winbrow/memory
  - winbrow/voice.py
  - winbrow/stt.py
  - winbrow/vision.py
---

# 05 — Known Issues

Verified 2026-10-25 by full-file reads. **58 issues** are recorded: 20 browser, 14 desktop,
14 routing/generation, 10 IO/memory/voice. Severity is an engineering judgement, not a runtime
test result. No tests were run for this document; every item was confirmed by reading the cited
lines.

Fix hints assume you keep the current architecture. Do not "fix" an invariant listed in
`01-system-architecture.md` or `02-browser-automation.md` without checking its callers.

## Browser (B-01 … B-20)

| ID | Sev | Issue | Evidence | Impact / fix hint |
|---|---|---|---|---|
| B-01 | Low | `_cdp_send()` is an empty stub | `browser_task.py:545-548` | Dead code; it returns `None`. Remove it or implement and call it. |
| B-02 | High | `navigate_to()` prefixes `https://` onto `chrome://`/`edge://`/`brave://` URLs | `browser_task.py:977-978` | Browser shortcut routing (`browser_shortcut`, `:1462-1494`) produces `https://chrome://settings/`. Allow internal schemes before prefixing. |
| B-03 | High | Blocking `urllib.request.urlopen` used from async paths | `browser_task.py:213-228`, `:279-285` | Up to 2 s per probe stalls the event loop; `_page_conn` re-probes on every command. Use `asyncio.to_thread` or a cached async probe. |
| B-04 | High | CDP backend prefers HTTP if anything answers on 9222 | `browser_task.py:279-285` | A stale separate-profile window shadows the consent-mode browser the user chose. Detect the intended backend explicitly. |
| B-05 | High | `websockets` is imported outside the `try` and is not in `requirements.txt` | `browser_task.py:327-329`, `:808-809` | `ModuleNotFoundError` escapes `ensure()`; declare the dependency and guard the import. |
| B-06 | Medium | Cancelled CDP calls leak pending futures | `browser_task.py:374-392` | Outer 20 s `wait_for` cancellation leaves `_pending[id]` until a reply. Remove the future in a `finally`. |
| B-07 | Medium | CDP sessions cache is not event-driven; recovery uses `"ession" in err` | `browser_task.py:345-348`, `:482` | Detached targets leave stale `sessionId`s; string matching is brittle. Handle `Target.detachedFromTarget` and match structured error codes. |
| B-08 | High | ~~Page cache is keyed by URL only, no TTL; reads default to cached~~ **partially fixed 2026-10-25** | `browser_task.py` `get_page_state`/`_fetch_page_state`; `browser_tools.py:544-548`, `:570` | Blank snapshots (no title/text/links) are now retried for 3 s and never cached, so a slow SPA load can no longer poison same-URL reads. Same-URL content changes still need `force_refresh`; a TTL/content hash is still missing. |
| B-09 | High | Empty tab list is treated as "agent tab closed"; `_pick_tab` returns `None` | `browser_task.py:613-631`, `:714-738` | A CDP hiccup can blank every subsequent read. Distinguish "no tabs" from "temporary failure", but keep the no-hijack rule. |
| B-10 | Low | `_task_tab_pending` is written but never read | `browser_task.py:568-571`, `:588`, `:982` | Dead state; remove or use it for pending-tab recovery. |
| B-11 | High | Tab/window keystroke tools act on the focused window and interpolate raw text into SendKeys | `browser_task.py:1074-1138`, `:1400-1420` | `close/new/switch/reload/back/forward/find/zoom` can hit the wrong app; `{ } + ^ % ~ ( )` break or alter input. Activate the target window and escape SendKeys metacharacters. |
| B-12 | Medium | URL/type fallbacks overwrite the user's clipboard | `browser_task.py:1154-1169`, `:1864-1875` | `get_current_url` clobbers clipboard and returns `success: True` with `url="Unknown"`; typing sends `{ENTER}`. Save/restore clipboard and report failure honestly. |
| B-13 | High | `browser_upload_file` always uses the first `<input type=file>` | `browser_tools.py:916-930` | Wrong input on multi-upload pages; no visibility/enabled check. Select by proximity to the last click or accept a selector. |
| B-14 | High | File-chooser interception lives only 0.2 s inside the click connection | `browser_task.py:1328-1341` | An asynchronously-opened chooser shows the native dialog, and upload fails. Keep the connection open longer or use `Page.fileChooserOpened` events. |
| B-15 | High | `_pick_element` treats any substring as a perfect match | `browser_tools.py:659-660` | Short labels ("Go" in "google") win; ties keep the earliest DOM element. Require word boundaries and penalize short labels. |
| B-16 | High | File-input shortcut hijacks unrelated "choose/select" instructions | `browser_tools.py:762-770` | "choose Python in the language filter" targets a file input. Require upload-specific verbs and a visible file input. |
| B-17 | Medium | `browser_read_full` appends 40 full-length links before elements, then clips at 9000 chars | `browser_tools.py:594-614` | Link-heavy pages lose the interactive element list; text truncation is silent. Cap per-link length and budget elements separately. |
| B-18 | Low | `accessibility.py` is unreferenced and multiple methods are broken | `accessibility.py:163-179` (wrong control IDs), `:251` (`comtypes.client.wintypes`), `:269` (`search(self)`), `:229-231` (`max_nodes` ignored) | Dead module; do not wire it in without fixing the tree traversal and COM lifecycle. |
| B-19 | Medium | Agent tabs accumulate: `reset_session()`/`end_task()` never close them | `browser_task.py:580-599` | One tab per task. Close only tabs the agent created, and only on explicit cleanup. |
| B-20 | Low | `_A11Y_JS` stamps `data-winbrow-id` permanently; `type_in_element` falls back to the nonexistent `data-wb-idx` | `browser_tools.py:74-78`; `browser_task.py:1829` | DOM pollution grows `window.__winbrowIdSeq`; the fallback selector never matches. Clean attributes or use `data-wb-idx` consistently. |

## Desktop / native control (D-01 … D-14)

| ID | Sev | Issue | Evidence | Impact / fix hint |
|---|---|---|---|---|
| D-01 | High | `GroundingLadder` reads `self.driver.user32`, but `user32` is a module global | `computer/ladder.py:66`, `:93`; `driver.py:50` | `observe()`/`resolve_target()` without an explicit `hwnd` raise `AttributeError`. Use the module global or pass `hwnd` everywhere. |
| D-02 | High | `resolve_app()` returns a sub-threshold match, and the launcher trusts it | `apps.py:509-517`; `windows.py:751`, `:768` | A weak fuzzy match can launch the wrong app, contradicting "never launch a guess". Return `None` below `MATCH_MIN`. |
| D-03 | Medium | `ToolRegistry.available()` scope check is reversed | `registry.py:91` | `ctx.active_app in scope` instead of scope in app (or equality); scoped tools appear/disappear incorrectly. |
| D-04 | High | Script templates are validated before substitution; escaping is ad hoc | `registry.py:58-68`, `:78-80` | A `{{arg}}` in a double-quoted/subexpression context is runtime-injectable and bypasses `validate_script_safety`. Validate the rendered script and use argument passing. |
| D-05 | High | Synchronous atoms run on the event loop | `agent.py:463-464`; `computer/tools.py:104`; `screen.py:211` | `computer_click_text` can reach OCR → `run_powershell_sync` and block for seconds. Route all blocking atoms through `to_thread`/the dispatch map. |
| D-06 | Medium | Negative screen coordinates are rejected | `computer/tools.py:90-92` | Monitors left/above the primary are unclickable. Allow negative coordinates. |
| D-07 | Medium | UIA `invoke()` always returns `False` | `computer/uia.py:206-211` | No UIA pattern invocation; flows must click rect centers. Implement patterns or document the click-only contract. |
| D-08 | Medium | VLM budget is spent after success and is non-atomic; coordinates are not clamped | `computer/vlm.py:147-165` | Failed calls are free/unbounded; concurrent calls exceed the daily cap; off-screen coords can be clicked. Reserve budget before the call and clamp to the capture rect. |
| D-09 | High | After Effects success expression uses `and/or` precedence | `adapters/aftereffects.py:64` | A pre-existing output file reports success even when `aerender` failed. Parenthesize the render check and verify a fresh mtime. |
| D-10 | High | User-controlled text is interpolated into double-quoted PowerShell | `windows.py:588-589`; `computer/screen.py:203-206`; `adapters/paint.py:28-29` | `$`, backticks, `$(...)`, or apostrophes corrupt or inject commands. Single-quote and escape apostrophes. |
| D-11 | Medium | Clipboard paste destroys the user's clipboard | `computer/driver.py:216-219` | Clipboard is not restored. Save/restore around paste, or use SendInput for text. |
| D-12 | Medium | `clean_desktop_to_folder()` bulk-moves every loose desktop file | `windows.py:827-838` | No confirmation in the function; safety depends entirely on the caller's tier. Keep it T2/T3 and preview the file list. |
| D-13 | Medium | Two OCR stacks are duplicated and the `Screen.ocr` path is fully blocking | `computer/screen.py:73-137` vs `computer/tools.py:175-237`; `screen.py:200-211` | Divergent fixes and event-loop stalls. Consolidate on one OCR implementation with an async wrapper. |
| D-14 | Medium | Adapter `required`/`default` metadata is dropped | `adapters/manager.py:34-37`, `:57-60`; `office.py:98-101` | Optional args become required (`office_save_as_pdf.out_path`) or defaults are never injected. Preserve arg metadata. |

## Routing / generation (R-01 … R-14)

| ID | Sev | Issue | Evidence | Impact / fix hint |
|---|---|---|---|---|
| R-01 | Critical | Policy regexes are case-sensitive where they should not be, and miss `shutdown`, `diskpart`, `vssadmin`, `taskkill /f` | `policy.py:18-35`, `:117-124` | `c:\windows` evades `[C-Z]`; scheduled shutdown is not T3. Normalize case and add the missing destructive commands. |
| R-02 | Critical | Generated shutdown/restart scripts are T1 and auto-execute | `generator.py:558-574`; `permissions.py:86-103` | `shutdown /s` / `/r` is not in policy, and `perm.gate("generated_script", ...)` does not pass `tool=`, so the learned-tool T2 floor is skipped. Block or tier them explicitly. |
| R-03 | Medium | `resolvers.is_tool_disabled()` calls undefined `disabled_tools()` | `resolvers.py:73-77` | `NameError` if ever called; `WINBROW_DISABLED_TOOLS` is unimplemented. Add the function or remove the branch. |
| R-04 | High | Ultrafast keyword routing uses broad substrings before file/search branches | `router.py:1555-1574` | `"search for a file"` routes to the browser because of `"search for"`; `"order"` matches `"recorder"`. Use word boundaries and narrow verbs. |
| R-05 | High | Fuzzy matching turns "browser" into the search verb "browse" | `router.py:1583-1600` | "close the browser tab" becomes a web search at 0.96. Exclude the noun "browser" from search-verb correction. |
| R-06 | Critical | `QwenLLM._request()` encodes bytes twice | `llm.py:123-126` | Every local chat/completion/intent call raises `AttributeError`. Pass the bytes directly. |
| R-07 | High | Model checksums are non-real and verification is skipped | `model_manager.py:31`, `:38`, `:86-88` | Corrupt/partial GGUF files are accepted. Use real SHA-256 values or verify size + hash. |
| R-08 | Medium | Small-LLM router default timeout is 25 s (+5 outer) | `small_llm.py:53`, `:185` | A missed route can block voice/UI for ~30 s. Lower the default or make it strictly opt-in per request. |
| R-09 | High | Ultrafast reports success for `submit`, `navigate`, and `scroll` without checking | `ultrafast.py:724-740` | The one-shot path can claim success after a failed action. Propagate the CDP result and verify the page. |
| R-10 | Medium | `WINBROW_DISABLE_LAYA` is ignored by the ultrafast engine | `ultrafast.py:283-286`; `router.py:964` | Browser tools can still load Laya/torch after the kill switch. Check the env in `LayaUltrafastEngine`. |
| R-11 | High | "unlock screen" matches `"lock" in lower` and locks the screen | `router.py:1494` | Simple substring inversion. Match `unlock` before `lock` and require a verb boundary. |
| R-12 | Medium | App-launch branch preempts Windows Settings panes | `router.py:1500-1509` vs `:1657-1676` | "open sound settings" launches the Settings app without selecting the pane. Check `settings` before generic app launch. |
| R-13 | Medium | Generator provider endpoints are mismatched/malformed | `generator.py:235`, `:256-261` (Ollama `/api/generate` called with OpenAI JSON); `:278-284` (Vertex host built from `projects/<id>`) | Ollama requests fail; Vertex URL is not a valid host. Use the right payload per endpoint and a real regional host. |
| R-14 | Low | `NEW_ACTION_MIN_CONFIDENCE` is unused, and Laya tool choices in 0.35–0.60 are discarded | `router.py:35`, `:1025`, `:1129` | Confusing dead configuration; valid mid-confidence choices fall through. Remove or apply consistently. |

## IO / memory / voice (I-01 … I-10)

| ID | Sev | Issue | Evidence | Impact / fix hint |
|---|---|---|---|---|
| I-01 | Medium | `action_key()` truncates every arg to 160 chars before hashing | `permissions.py:112` | Distinct long args sharing a prefix map to one remembered T2 approval. Hash the full value or a longer digest input. |
| I-02 | Medium | An approved pending action can re-authorize identical calls indefinitely | `permissions.py:268-269`, `:293-297` | TTL pruning runs only on new-pending/pending_list. Consume or expire approvals deterministically. |
| I-03 | Medium | MCP `env` is additive over the full parent environment | `mcp.py:147-150` | The header claims explicit-only passthrough, but secrets are inherited by every server. Build a minimal env allowlist. |
| I-04 | Medium | `execute_mcp_tool()` always uses the default hub | `mcp.py:641-645`, `:796` | A hub built with a custom config path can connect but never execute. Pass the hub through or key calls by config. |
| I-05 | Low | MCP tool-name collisions are silently skipped | `mcp.py:708`, `:756` | A server can shadow another server's tool with no warning. Log and namespace deterministically. |
| I-06 | Low | Document cache is keyed by path+mtime+size but never invalidated on delete | `document_tools.py:68` | A deleted/replaced file can serve stale text. Validate existence on hit. |
| I-07 | Medium | FTS5 content table has no sync triggers | `memory/store.py:92-96`, `:158-166` | FTS is write-only mirrored; future updates/deletes will desynchronize. Add triggers or rebuild on mutation. |
| I-08 | High | Voice wake phrase is not enforced before execution | `voice.py:176-184`, `:364-369` | `strip_wake()` returns the full lowercased text when no wake phrase matches, and the loop executes it. Treat "no wake phrase" as ignore in wake mode. |
| I-09 | Medium | Local STT blocks the event loop and `AudioRecorder.stop()` imports numpy unconditionally | `stt.py:196`, `:304` | Whisper runs inline from async `transcribe_best`; missing numpy breaks stop. Use `to_thread` and guard optional imports. |
| I-10 | Low | `vision.py` is unreferenced and broken on construction/use | `vision.py:40`, `:65`, `:106`, `:122-124` | `base_url` property conflict, missing `subprocess`, bytes re-encode. The live vision path is `computer/vlm.py`; delete or repair this module. |

## How to use this list

- Before "fixing" a bug, find every caller with `grep`.
- Preserve the invariants in `01`/`02`; several issues are deliberate trade-offs.
- When an issue is fixed, remove the row here and add a session-log entry. Do not leave a fixed item
  marked as open.
- If you find a new issue, add it with the next free ID and update the count at the top of this file.
- Security-sensitive items (R-01, R-02, D-04, D-10, I-03) should be treated as release blockers for
  any change that expands what the agent can execute.
