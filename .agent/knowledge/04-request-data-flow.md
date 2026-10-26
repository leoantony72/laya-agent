---
knowledge: 04-request-data-flow
last_verified: 2026-10-25
source_files:
  - winbrow/agent.py
  - winbrow/web_tools.py
  - winbrow/router.py
  - winbrow/brain/agent_loop.py
  - winbrow/brain/model_gateway.py
  - winbrow/brain/prompt.py
  - winbrow/brain/normalize.py
  - winbrow/utils/history.py
  - winbrow/permissions.py
  - winbrow/memory/session.py
  - winbrow/memory/store.py
  - winbrow/generator.py
  - winbrow/mcp.py
---

# 04 — Request Data Flow

Verified 2026-10-25. Entry point: `WinBrowAgent.execute()` (`winbrow/agent.py:1033`).

## 1. End-to-end sequence

```
HTTP /api/execute or voice
        │
        ▼
WinBrowAgent.execute()
  1. pending permission yes/no?          (control path)
  2. capture Windows context (thread)
  3. BRAIN FIRST (provider available)    agent.py:_execute_brain_goal
     browser intent → handle_browser_task
     everything else → brain.handle
  4. legacy fallback only if the brain is unavailable or provider fails:
     approved replay skill?
     MCP direct shortcuts
     browser fast path / ultrafast one-shot
     router.route()
  5. record history, session, store
  6. learn corrections, trace
```

`WINBROW_BRAIN_FIRST=0` disables the brain-first path (legacy behavior).
`WINBROW_BRAIN_FIRST` defaults to `1`.

## 2. Step detail

### 2.1 Pending permission replies

If the whole utterance is yes/approve/confirm/allow/proceed/go ahead/do it/sure/yep/ok do it/accept,
the most recent pending action is approved, consumed, and its original utterance is re-executed.
`remember=True` only for T2. No/cancel/deny/reject/don't do it/dont denies and returns a `stop`
result. This runs before the brain.

### 2.2 Brain first

When a model provider is available, every request goes to `_execute_brain_goal()` before replay,
MCP shortcuts, the browser fast path, or the router:

- browser-intent utterances use `handle_browser_task()` (fresh agent tab + browser step budget);
- everything else uses `AgentBrain.handle()`;
- the route tier is `brain_first`; a planner exception is `brain_first_error`;
- a provider failure falls back to legacy routing; a normal brain result (including
  `needs_input`/failure) is returned without an MCP shortcut.

### 2.3 Procedural replay (legacy fallback)

- Runs only when brain-first is disabled or the provider is unavailable.
- `self.skills.find_match(utterance)` (only approved, Jaccard ≥ 0.66).
- Browser intents are skipped and sent to the brain loop instead.
- `_fill_skill_parameters()` maps the new utterance onto the stored `{{parameter}}` templates;
  missing parameters abort replay.
- Steps run through `run_skill()` in order, max 12; first failure stops replay.
- All success → `mark_used`, confidence 1.0, tier `replay`, return immediately.
- Any failure → fall through to normal routing.

### 2.4 Context capture

`get_context()` runs in an executor because process enumeration + COM volume reads take 15–600 ms.
`WindowsContext` fields: `active_app`, `active_title`, `running_apps`, `volume_level`, `is_muted`,
`dark_mode`, `battery_percent`, `battery_plugged`. `to_dict()` caps running apps at 15 and formats
battery as a string.

### 2.5 MCP direct shortcuts (legacy fallback)

Runs only when brain-first is unavailable and the command is not multi-step and not interactive.
Covers:

- dictionary definitions (`define X`, `meaning of X`);
- Wikipedia summary/search;
- git status/log/diff/branch/show;
- current time;
- PDF text/info/search;
- filesystem `read_text_file`.

These bypass the router and brain, return `tier="mcp_shortcut"`, confidence 0.95 (or 0.9 for some
paths). They exist so high-confidence lookups do not spend a planner call.

### 2.5 Browser fast path (`:1383-1430`)

`_is_browser_intent()` (`:117`) and `_is_interactive_task()` classify the
utterance. Research, search, comparison, price lookup, and documentation
reading are **excluded** by design; those go to `web_search`/`web_fetch` or the
brain. Site names only count when paired with an explicit open/go/visit verb.
Multi-step intents bypass this path entirely.

- If the gateway is available and the command is not multi-step: `handle_browser_task()` (`:535`)
  calls `browser.reset_session()`, `brain.handle(...)`, then `browser.end_task()` in `finally`.
  Result tier: `browser_brain`, confidence 1.0.
- If the brain fails/unavailable: `browser.run_ultrafast_task(utterance)`; tier
  `laya_ultrafast`, confidence 0.98.
- All browser tasks use the same observe→act→verify loop; there are no task-type tiers.
- The WEB ACCESS HIERARCHY in `SYSTEM_PROMPT_STATIC` decides whether the brain
  should use web_search, web_fetch, or browser_navigate.

### 2.6 Router (`:1406`)

`WinBrowRouter.route()` (`router.py:979`) returns a `Route`:

```
kind: "tool" | "chat" | "new_action" | "stop" | "uncertain"
tool: Tool | None
args: dict[str, str]
confidence: float
probabilities: dict
latency_ms: float
reason: str
tier: "laya" | "heuristic" | "llm" | "multi_step"
```

Order inside `route()`:

1. Empty → CHAT.
2. Strip conversational prefix, apply learned corrections, autocorrect.
3. **Laya worker** (`apredict`, default 2 s, `LAYA_TIMEOUT_S`): accepted if kind ≠ NEW_ACTION and
   confidence ≥ 0.6.
4. Circuit breaker: 2 consecutive timeouts suppress Laya for 30 s.
5. **Heuristic** route (`_heuristic_route`, `:1340`); returns immediately if not NEW_ACTION.
6. Multi-step → NEW_ACTION, tier `multi_step`, confidence 0.5.
7. Optional Ollama small LLM (`WINBROW_ROUTER_LLM=1`).
8. Fall back to the heuristic NEW_ACTION.

Heuristic branch order: STOP → multi-step → Wikipedia → URL fetch → editor type/save → create file →
URL download → winget → volume → lock → app launch → open folder/file → close folder → play/watch →
browser interaction (booking/checkout/form/login) → web search (web_search) → launch prefix → window action → settings → screenshot → clipboard →
time → network → battery → dark mode → media keys → find files → bare URL (browser_navigate or
web_fetch if read/fetch verb) → NEW_ACTION.

### 2.7 Dispatch (`:1431-1544`)

- `route.kind == "tool"` → `_dispatch_tool_execution()` → `run_skill()`.
- `route.kind == NEW_ACTION`:
  - gateway available → resolve session references, `browser.reset_session()`, `brain.handle()`;
    on success optionally capture a replay skill; on `GatewayUnavailable` → generator; on other
    exceptions → one normalize+re-route, then generator.
  - no gateway and `_is_known_task()` → honest routing-miss error (do not invent a script).
  - otherwise → generator.
- `route.kind == CHAT` → `brain_chat()` or canned replies.
- `route.kind == STOP` → standing-by response.

### 2.8 Recording (`:1549-1606`)

- Append result to `self.history` (ring buffer, max 100).
- Successful opens update `ContextMemory` (`last_opened.py`).
- `session.record_turn()` + `store.record_command()` (best effort; failures logged at debug).
- Successful normalized rewrites persist learned corrections (`brain/normalize.py`).
- `trace.command_event()` appends JSONL (never breaks execution).

## 3. Permission gate

`PermissionManager.gate()` (`permissions.py`) is called once from `run_skill` (`agent.py:362`).

Tiers (`permissions.py:10-14`):

| Tier | Meaning | Behavior |
|---|---|---|
| T0 | Read/sense | Auto-allow |
| T1 | Reversible action | Auto-allow |
| T2 | User-data write / risky | Confirm once, remember the exact action key |
| T3 | System/install/destructive | Confirm every time; never remembered |

Classification:

- Curated `TOOL_TIERS` table (`:47-78`).
- `_default_tier()` substring heuristics (`:86-96`):
  install/shutdown/registry/service/disk → T3;
  save/write/export/delete/close/move/copy/paste/learn/edit/creat/commit → T2;
  screenshot/read/list/status/... → T0; otherwise T1.
- `script_tier()` can lift a rendered script based on content (`policy.py:117-124`).
- Learned tools are floored at T2 (`permissions.py:221-224`).
- `WINBROW_BYPASS_CONFIRM=1` bypasses everything, including T3.
- `WINBROW_AUTO_APPROVE_T2=1` auto-remembers T2.
- Pending TTL is 900 s; TTL pruning runs on new pending/pending_list.
- Confirmation channels: in-process `set_confirm_fn` (voice) or `POST /api/confirm`
  (`server.py:548`).

`action_key()` hashes `(tool, normalized args)`; each arg is lowercased/truncated to 160 chars
(`permissions.py:112`), which can collide for long args sharing a prefix (I-01 in `05`).

## 4. Tool dispatch (`run_skill`, `agent.py:346-526`)

1. Registry lookup; unknown → `{success: False, output: "Unknown tool ..."}`.
2. Render script templates **before** the gate so content-based tiering sees real args.
3. Permission gate.
4. Special cases: `create_text_file` (`files.write_named_text_file`), `volume_set_level`,
   `laya_ultrafast_browser`, `app_focus_or_launch`, `type_text`.
5. `impl` dispatch:
   - `COMPUTER_IMPL` → async override map or `execute_computer_tool`; OCR goes through
     `asyncio.to_thread`.
   - `ADAPTER_IMPL` → `execute_adapter_tool`.
   - `SHELL_IMPL` → `execute_shell_tool`.
   - `DOCUMENT_IMPL` → `asyncio.to_thread(execute_document_tool)`.
   - `WEB_IMPL` → `execute_web_tool_async` (`web_search`/`web_fetch`; never opens the browser).
   - `BROWSER_IMPL` → `execute_browser_tool` (20 s cap).
   - `INSTALLER_IMPL` → `INSTALL_OPS[name]`, then consume the one-shot approval.
   - `MCP_IMPL` → `execute_mcp_tool`.
   - `SKILL_SCRIPTS_IMPL` → allowlisted bundled helper.
   - `INTEGRATION_IMPL` → `integration_status`.
   - `SUBAGENT_IMPL` → `SubagentManager.execute`.
6. Otherwise: render and `run_powershell(rendered)`.

All impls return a dict with at least `success` and `output`; the dispatcher adds `type`/`tool`.

## 5. Brain loop (`brain/agent_loop.py`)

`AgentBrain.handle(utterance, ctx, session, emit=None)` (`:543`):

1. Build system prompt (`SYSTEM_PROMPT_STATIC` + metadata) and user message with context, session
   block, and history.
2. Call the gateway with the planner JSON schema.
3. Parse one of: `action`, `done`, `chat`, `clarify`.
4. `action`: extract steps, inject `SKILL DETAIL` for the chosen skill, execute the first step with
   `_run_step()` (`:1048`), append a `BrainStepResult`, loop.
5. `done`: run `_completion_gap()`; if there is a gap, challenge once ("verify"), otherwise return
   success and fire the skill writer.
6. `clarify`: if `ask_user_fn` exists, ask and append the reply (max 3 rounds); otherwise return
   `needs_input=True`.
7. Stop at `max_steps` (default 20), with a warning when `step_warn_remaining` (default 5) remain.

`_run_step()`:

- `await asyncio.wait_for(executor(skill, args), timeout=STEP_TIMEOUT_S)` (default 120 s).
- Unknown skill/timeout become failed `BrainStepResult`s with a warning, not exceptions.
- `annotate_result()` (`:1292`) adds navigation mismatch warnings, truncation warnings for browser
  trees, and repair hints.

`BrainResult.to_dict()` is the `execution` payload; `BrainStepResult` carries `skill`, `args`,
`success`, `output`, `warning`, `url`, `title`, `elapsed_ms`.

## 6. History and compression (`utils/history.py`)

| Constant | Value | Meaning |
|---|---|---|
| `FULL_WINDOW` | 5 | Last 5 steps rendered in full |
| `RECENT_OUTPUT_LIMIT` | 400 | Output cap for non-browser recent steps |
| `BROWSER_DETAIL_STEPS` | 3 | Last 3 browser steps get detailed page text |
| `PAGE_TEXT_LIMIT_CURRENT` | 3000 | Current browser step page text |
| `PAGE_TEXT_LIMIT_RECENT` | 2000 | Previous detailed browser steps |
| `DOCUMENT_STEP_LIMIT` | 30000 | Per-step document read budget |
| `DOCUMENT_TOTAL_BUDGET` | 60000 | Total document budget across recent steps |

Older steps are compressed to `skill → success output`; older browser steps become
`url=… title=…`. `detect_stuck()` and `count_recent_stuck()` detect repeated identical actions and
warning text so the loop can force a clarify instead of spinning.

## 7. Model gateway (`brain/model_gateway.py`)

- Provider chain is built in `_build_chain()`: Nebius Token Factory → OpenRouter → OpenAI → local,
  depending on keys and `WINBROW_BRAIN_PROVIDER`. `NEBIUS_API_KEY` alone puts Nebius first in
  `auto`; set `WINBROW_BRAIN_PROVIDER=nebius` to force it.
- Nebius uses the OpenAI-compatible base `https://api.tokenfactory.nebius.com/v1` and translates
  the shared reasoning policy to `reasoning_effort` (medium for structured calls, none for chat,
  low for capped retries).
- `chat()` sends OpenAI-compatible requests, retries transient errors, and handles empty
  content caused by reasoning-token exhaustion.
- Reasoning config (`get_reasoning_config`, `:85`): default total 16000 / reasoning 2000; browser
  turns 8000 / 600.
- Empty planner content retries with reasoning 400, then reasoning disabled at 1000 total.
- JSON extraction (`extract_json_object`, `:358`) cleans code fences, finds balanced objects, and
  repairs truncated JSON.
- `GatewayUnavailable` means no provider; callers fall back to heuristics/generator.

## 8. Generator fallback (`generator.py`)

`ScriptGenerator.generate_and_execute()`:

1. Build a prompt with utterance + active app/title + first 10 running apps.
2. Provider order in `auto`: Gemini → OpenAI → explicit endpoint (Ollama/LM Studio) → offline
   synthesizer.
3. Build a `Tool(is_learned=True)`.
4. `validate_script_safety()`.
5. Render with defaults only.
6. Optional tiered gate (`perm.gate("generated_script", ..., script=rendered_script)`).
7. `run_powershell(rendered, timeout=30)`.
8. On success, persist to `tools/learned.json` unless it is a generic no-op wrapper.

The offline synthesizer covers ~30 common system tasks. It is the last resort when no model is
available.

## 9. Memory flow

| Layer | File | Scope |
|---|---|---|
| Session | `memory/session.py` (`tools/session_state.json`) | Turns, entities, clarifications; last 24 turns, 10 entities/kind, 5 sessions |
| Episodic | `memory/store.py` (`data/memory.db`) | Commands, file/folder/app opens, facts, corrections; FTS5 + optional Chroma |
| Replay | `memory/skills.py` (`tools/skills.json`) | Approved multi-step plans |
| Recency | `last_opened.py` (`tools/context_memory.json`) | Last 3 opened files/folders/apps for "that file" |
| Brain skills | `winbrow/skills/` | Markdown instructions |
| Learned tools | `tools/learned.json` | Generated PowerShell tools |

`get_session(id)` constructs a fresh object and reloads JSON on every call; `get_store()` is a
singleton. Chroma failures degrade to FTS/LIKE. FTS is write-only mirrored (no triggers).

## 10. MCP flow

- Specs: `tools/mcp_servers.json`; tool catalogs cached at `tools/mcp_cache.json`.
- `McpHub.connect_with_registry()` starts stdio servers (`subprocess.Popen`, Windows job object) or
  HTTP transports, then registers tools as `mcp_{server}_{tool}` with carrier
  `mcp://{server}/{tool}`.
- `execute_mcp_tool()` lazily (re)connects, retries once, maps `isError` to `success=False`.
- The brain sees MCP tools through the registry; `run_skill` dispatches `MCP_IMPL`.
- Preload servers connect at startup; the rest connect on use.

## 11. Response contract

`execute()` returns:

```
{
  "utterance": str,
  "route": {"kind", "tool_name", "args", "confidence", "latency_ms",
            "probabilities", "tier"},
  "context": {...},
  "execution": {"type", "success", "output", ...},
  "total_elapsed_ms": float,
  "normalize": {...} | absent,
  "session_resolutions": {...} | absent
}
```

`/api/execute` wraps this in the HTTP response; the WebSocket broadcasts progress events. Failures
should be returned as `success: False` with an `output` message, not raised across the API boundary.

## 12. Where to change what

| Change | Touch |
|---|---|
| New intent phrase | `router._heuristic_route()` + a test case |
| New tool | `registry.Tool`, `get_*_tools()`, `run_skill` impl branch, `permissions.TOOL_TIERS`, `SKILL.md` |
| New permission rule | `permissions.TOOL_TIERS` or `_default_tier`, and `policy.script_tier` if content-based |
| New planner behavior | `brain/prompt.py` rules + `brain/agent_loop.py` parse/complete logic |
| New browser primitive | `browser_tools.py` + `browser_task.py` + `SKILL.md` + `history.BROWSER_SKILLS` |
| New memory type | `memory/store.py` schema + `record_*` + retrieval |
| New provider | `brain/model_gateway._build_chain()` |
| New app adapter | `adapters/<app>.py` `OPS` + manager import |
