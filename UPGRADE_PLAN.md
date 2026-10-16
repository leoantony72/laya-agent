# WinBrow v3 — Complete Desktop Automation Upgrade Plan

> Target: a system that understands follow-up context, plans multi-step tasks, and
> controls **any** application on Windows (Photoshop, After Effects, Paint, Notepad,
> VSCode, installers, everything — not just the browser and OS panels).

---

## 0. Current System Analysis (evidence-based)

### 0.1 What exists and works well — keep

| Component | File | Why it's good |
|---|---|---|
| Persistent PowerShell host | `winbrow/pshost.py` | Sub-second execution; avoids 1–3s cold spawn |
| CDP browser engine | `winbrow/browser_task.py`, `winbrow/ultrafast.py` | DOM harvesting + typed decisions (Laya) in 25–45ms/step; YouTube/flight flows work |
| Tool registry (42 seed tools) | `winbrow/registry.py`, `tools/seed.json` | Safe render/escape paths, learned-tool persistence |
| Pattern-based safety policy | `winbrow/policy.py` | Blocks format/system-wipe/bcdedit; confirmation heuristic |
| JSONL trace | `winbrow/trace.py` | Tracking of tier/tool/latency/success per command |
| Windows context capture | `winbrow/windows.py` | Active app, running apps, volume, battery — 2s TTL cache |
| Accessibility tree module | `winbrow/accessibility.py` | UIAutomation polling — **built but not wired into the agent loop** |
| FastAPI server + UI + voice | `server.py`, `winbrow/voice.py` | Voice STT (whisper.cpp), HUD UI, WebSocket live status |

### 0.2 The 6 structural gaps (why it feels "not intelligent")

1. **No multi-turn session memory.**
   `winbrow/last_opened.py` stores exactly 3 slots (`last_file`, `last_folder`, `last_app`) + 20 history events. `server.py` history is an in-memory ring buffer of 100 dicts and is never fed back into routing. `WORKING.md:626` admits: *"No multi-turn dialogue state: follow-ups re-prompt rather than filling slots from history."*
   → "open that file again", "and make it louder", "do the same thing but dark mode" cannot work.

2. **SemanticMemory is dead code.**
   `winbrow/memory.py` implements SQLite + ChromaDB episodic memory with `record_file_open/search_by_content/get_context_summary` — **it is never imported by `agent.py` or `server.py`**. It has zero consumers outside its own `__main__`.

3. **The "intelligence" (LLM) is disconnected.**
   `winbrow/llm.py` (`QwenLLM.extract_intent/resolve_reference`) is only called from `voice.py`. The main `server.py → agent.py → router.py` path never uses it. The 0.8B model would barely help anyway on this box.

4. **Routing is keyword soup.**
   `router.py` is ~1,200 lines of keyword/regex/typo heuristics, duplicated in `agent.py` (`BROWSER_KEYWORDS`, `_ULTRAFAST_TRIGGERS`, `_is_known_task`) and `ultrafast.py` (`_PLAY_INTENT_KW`). Three overlapping keyword banks = drift and misroutes. Laya exists but on this CPU it always times out (`WORKING.md:623`), so heuristics carry everything — and heuristics can only handle exactly-templated commands.

5. **No GUI control for native apps.**
   Browser gets a full perceive→decide→act loop. Everything else gets a one-shot PowerShell template. There is no click/type/key/scroll driver, no dialog handling, no screen-state verification. PyAutoGUI is in `requirements.txt` but unused by `winbrow/`.

6. **No perception beyond DOM.**
   `accessibility.py` (UIA) and `vision.py` (Qwen2.5-VL) exist but are not in the execution loop. So Photoshop/AE/Paint (canvas apps with weak UIA) are out of reach entirely.

### 0.3 Hardware reality check (decisive constraint)

| Resource | Measured | Consequence |
|---|---|---|
| CPU | Intel Pentium G3240, 2 cores @ 3.1GHz (Haswell, AVX2) | ~1.3 GFLOPs effective; Llama 7B ≈ 1–2 tok/s; unusable for agent loop |
| RAM | 5.9 GB total, **1.8 GB free** | 7B Q4 GGUF needs ~4.5 GB → OOM. 0.5B–1.5B Q4 fits (~0.4–1 GB) |
| GPU | Intel HD (no CUDA, no BM) | No VLM, no OmniParser locally |
| Findings | Laya always >5s cap | System 1 must remain heuristic-first; Laya is optional accelerator |

**Conclusion:** a capable local "brain" is impossible on this machine. The plan must be
**model-gateway-based**: cloud API by default (the generator tier already supports
OpenAI/Gemini/Groq/Ollama/LM Studio — this pattern generalizes), with a tiny local model
only for cheap jobs (summaries, reference resolution) if configured.

---

## 1. Technique survey — how existing systems solve this (analytical)

| Approach | Systems using it | Mechanics | Strengths | Weaknesses | Verdict for us |
|---|---|---|---|---|---|
| **Screenshot → VLM → coordinates (pixel agent)** | Anthropic computer-use agents, OpenAI Operator, UI-TARS, Claude/GPT-4o GUI | Capture → multimodal LLM selects pixel coords → click | Works on *anything* incl. games/canvas | 5–15s per step, expensive, VLM required, hallucination | **Fallback only** — needs cloud VLM; too slow for default path |
| **Accessibility tree agent** | Windows agent benchmarks, iOS/macOS agents | Parse UIA/AX tree → LLM picks element by name/ID | Structured, fast, cheap tokens, coordinates from bounding rect | Electron/canvas apps lose detail; some apps no UIA | **Primary native-app grounding** (already half-built in `accessibility.py`) |
| **DOM harvesting + typed decisions** | browser-use, your `ultrafast.py` | JS-injected element index → indexed choices | Sub-50ms decisions, no vision | Browser-only | **Keep as-is for browser** |
| **App scripting APIs (COM/CLI/JSX)** | Photoshop COM, AE ExtendScript, VSCode CLI, Office COM | Drive the app's own automation surface | 100% reliable, no UI race conditions, can batch | Per-app effort, only where APIs exist | **Highest priority adapters** for Photoshop/AE/VSCode/Office |
| **Keyboard shortcuts + clipboard as AX bus** | Keymapping agents, Photoshop power users | Focus → send chord → verify by title/clipboard | Ron when UIA dies (PS panels, Paint canvas) | Blind unless verified | **Middle rung of the grounding ladder** |
| **Command generator + persisted skills** | Open Interpreter, your `generator.py` | LLM writes script → policy check → execute → cache | Grows capability surface unboundedly | Only shell-level; no GUI state feedback | **Generalize into procedural memory** (parameterized *plans*, not just scripts) |
| **ReAct plan→act→verify loop with memory** | Claude Code, Devin, SWE agents, Semantic Kernel AutoGen | LLM plans; executes via tools; observes result; verifies; retries | Context-aware, error-recovering, generalizes | Needs a capable model; needs tight tool loop | **The System 2 core of v3** |

**Design synthesis.** Use a *grounding ladder* (API adapter → UIA → keyboard → OCR/VLM)
with the cheapest reliable rung first, a *skill library* for instant known commands,
and an *LLM agent loop* for everything else. Memory gets its own first-class subsystem.

---

## 2. Target architecture (v3)

```
                      ┌────────────────────────────────────────────────────────┐
   Text / Voice / UI  │                 SESSION ORCHESTRATOR                    │
 ────────────────────►│  (agent.py refactor)                                    │
                      │  • session working memory load/save                      │
                      │  • contextualize(utterance) → resolve refs via memory    │
                      └───────┬───────────────────────────┬────────────────────┘
                              │                           │
              ┌───────────────▼──────────┐   ┌────────────▼──────────────────┐
              │ SYSTEM 1 — FAST PATH      │   │ SYSTEM 2 — AGENT BRAIN         │
              │ (router.py, slimmed)      │   │ (NEW brain/ package)           │
              │ exact intents <300ms      │   │ plan → act → observe → verify  │
              │ volume/lock/launch/folder  │   │ LLM model_gateway (cloud-first) │
              └───────────────┬──────────┘   └────────────┬──────────────────┘
                              │                           │
                        ┌─────▼─────────────────────────────▼──────┐
                        │              SKILL EXECUTION              │
                        │ registry.py tools  ·  adapters/ per app   │
                        │ installer.py (winget)  ·  browser engine  │
                        └─────┬─────────────────────────────────────┘
                              │ every action gated by policy.py v2 (tiers)
                        ┌─────▼─────────────────────────────────────┐
                        │        GROUNDING LADDER (perception)      │
                        │ 1. app adapter (COM/CLI/JSX)              │
                        │ 2. UIA accessibility tree (uia.py)        │
                        │ 3. keyboard chords + clipboard            │
                        │ 4. Windows OCR on mss screenshot          │
                        │ 5. cloud VLM on screenshot (opt-in)       │
                        │ 6. DOM harvesting (browser path)          │
                        └─────┬─────────────────────────────────────┘
                              │ results/state diffs
                        ┌─────▼─────────────────────────────────────┐
                        │                  MEMORY                    │
                        │ working/ (session)  episodic/ (sqlite+vec) │
                        │ semantic/ facts+prefs  procedural/ skills  │
                        └───────────────────────────────────────────┘
```

### 2.1 Subsystem contracts (interfaces to build against)

```python
# brain/model_gateway.py
class ModelGateway:
    async def chat(messages, tools=None, json_schema=None, priority="smart") -> Msg
    # Provider chain (first configured wins):
    #   1. OpenRouter (OpenAI-compatible) — DEFAULT: deepseek/deepseek-chat (V3.x)
    #   2. Other cloud keys (OpenAI / Gemini / Groq) if configured
    #   3. Local: LM Studio / Ollama (user-opted)
    #   4. Tiny local GGUF (0.5B) — summaries/reference resolution only
    # With cloud brain, llama.cpp server + Qwen GGUF download are dropped:
    # the only local models left are whisper STT (~140MB) + wake/VAD (~15MB).

# computer/driver.py  (NEW — native app control)
class ComputerDriver:
    async def click(x,y, button="left", double=False); move(x,y); drag(x1,y1,x2,y2)
    async def key(combo: str)          # "ctrl+s", "shift+f5"
    async def type_text(text: str)     # unicode SendInput
    async def paste(text: str)         # clipboard + ctrl+v
    async def scroll(amount, x=None, y=None)
    async def screenshot() -> np.ndarray
    async def find_window/app; focus_window; wait_for_window(title_re, timeout)
    async def close_window(pid_or_hwnd); list_windows()

# computer/uia.py  (promote accessibility.py)
class UIA: async def tree(hwnd) -> UIElement tree; async def find(query) -> rect+invoke
# supports click-by-name, menu selection, dialog buttons — no pixels needed

# computer/ladder.py
class GroundingLadder:  # tries rungs in order, returns (view, method) for the brain
    async def observe(window) -> View: DOM? UIA? OCR? VLM?

# memory/…  (see §3)
# adapters/… (see §4)  installer.py (see §5)  policy.py v2 (see §6)
```

---

## 3. Memory architecture (4 layers, replaces current 3-slot memory)

| Layer | Purpose | Implementation | Lifespan |
|---|---|---|---|
| **Working** | Current conversation: turns, resolved entities ("that file" → `D:\...\x.png`), pending plan state | `memory/session.py`: rolling turns + entity registry + summarization (LLM when >8 turns) | Session; saved to disk, resumable |
| **Episodic** | "What did we do": command → route → output → screenshot hash | `memory/store.py`: SQLite (FTS5) + ChromaDB embeddings (activate the *dead* `memory.py` code — fix imports and dedupe) | Forever, queryable |
| **Semantic** | User facts/preferences: "Chrome is my default browser", "Save exports to D:\Exports", app quirks | `memory/store.py` same DB, kind="fact"; written when user states a preference (explicit or detected patterns) | Forever |
| **Procedural** | Learned skills: parameterized plans ("open in photoshop and desaturate") — replay without LLM | `memory/skills.py`: success-plan capture from agent loop, YAML/JSON form, replaces `tools/learned.json` PS-scripts-only approach | Forever |

**Key flows enabled:**
- Follow-ups: `contextualize(utterance)` rewrites "open that file but larger" using working memory entities before routing.
- "What did I do yesterday?" → episodic FTS/vector query → answer via LLM.
- "Remember my exports folder is D:\Exports" → semantic fact, auto-injected into future PS/AE/PSD save operations.
- "Do the same thing you did for last tuesday's file" → episodic similarity query → replay plan with new parameters.

**Retention/race rules:** all writes are best-effort and never block execution (existing philosophy); entity slots validated against disk (path exists) and foreground window state; summarization runs off the event loop.

---

## 4. App adapters — the rung-1 grounding tier (new package `winbrow/adapters/`)

Adapter = know-how bundle per app: launch, focus, grounded ops, read state, verify. The registry exposes adapters as tools so both System 1 and System 2 can call them.

| App | Rung | Automation surface | Example capabilities |
|---|---|---|---|
| **VS Code** | CLI + UIA | `code <path>`, `code --install-extension`, `code --command workbench.action.toggleHighlight`, command palette via key+type, `settings.json` edits, integrated terminal | open folder/file, install extension, run tasks, edit settings, search across files |
| **Photoshop** | COM + JSX | `Photoshop.Application` COM: open/save-as/export, batch, `app.doScript` (JSX), Actions panel playback; UIA fallback for dialogs | open png, desaturate+autosave, batch resize folder, export-layers, place image into doc |
| **After Effects** | JSX + Render Engine | `afterfx.exe -r script.jsx` / `-s "app.project..."`, `aerender.exe -project -comp -output`, CEP panel for UI ops | open project, render comp to mp4, batch replace footage, create comps from folder |
| **Notepad** | UIA + keys | UIA for menu bar + find/replace dialogs, SendInput for text | open, type, save-as (handle dialog), find-replace, encode switch |
| **Paint** | UIA + canvas mouse | UIA for ribbon/menus; canvas ops via mouse/keyboard w/ verify-state-by-screenshot diff | open image, crop (select→copy→paste new), scale, invert colors, save copy |
| **Office (W/E/P)** | COM | Word/Excel/PowerPoint COM (voyage-hard, high value) | open doc, edit cell, save as PDF |
| **Any app** | keyboard+UIA+OCR | generic adapter: focus → observe(UIA/OCR) → act | "click Save", "type X in the search box", "install the offer in the app" |

Adapter contract:

```python
class AppAdapter(Protocol):
    app_id: str; match_names: tuple[str, ...]      # "photoshop", "photoshop.exe", "Adobe Photoshop 2025"
    def launch(self, target: str = "") -> Result   # and focus if running
    async def available(self, ctx) -> bool         # installed? COM registered?
    ops: dict[str, SkillOp]                        # op name → (args schema, impl, verify)
    def read_state(self, hwnd) -> dict             # title, doc name, dirty flag, progress
```

The `ops` dictionaries seed the skill registry — so "export to png in photoshop 50%" routes in <300ms once learned, and the brain composes novel flows from the same atoms when needed.

---

## 5. Install / uninstall (new `winbrow/installer.py`)

Order of preference:
1. **winget** (`winget search/install/upgrade/uninstall`, `--silent --accept-package-agreements --accept-source-agreements`) — native, packages thousands of apps
2. **Direct download install** (MSI/EXE) — only when winget misses; requires explicit per-call user confirmation (T3), downloaded from verified URLs, policy-checked (no `Invoke-WebRequest ... | iex` without confirmation)
3. **Store/MSIX** — `Add-AppxPackage` via double-click handoff (UAC is surfaced to the user, never bypassed)
4. Uninstall = `winget uninstall` / controlled `Remove-AppxPackage`

UAC rules: never attempt silent elevation; raise consent prompts to the user via UI + voice ("I need admin approval to install 7-Zip — confirm UAC dialog"). Installer progress parsed → streamed to UI; failures → suggestion retry with different source.

---

## 6. Safety model v2 — tiered permissions (extends `policy.py`)

| Tier | Examples | Gate |
|---|---|---|
| T0 readable | screenshot, list windows, read UIA, get clipboard, network status | auto |
| T1 reversible | open app/file/folder, volume, focus window, type into focused field | auto |
| T2 user-data | file writes outside temp, delete, move bulk, close windows, paste into app | **one-tap confirm**; remembered allowlist gets stored per-choice |
| T3 system | installs, uninstalls, registry HKLM writes, service state changes, shutdown | **every-time confirm** + blocklist regex from existing `BLOCKED_PATTERNS` |

Every path (System 1 tools, brain actions, generated scripts) funnels through the single `policy.gate(action_tier, context)` — no exceptions (today only generator.py calls the policy).

The brain cannot execute raw PowerShell — only skill atoms + declared ComputerDriver ops, each individually tiered. Escapes to shell remain possible but always T2+ and policy-checked (this retroactively solves the "LLM writes PS and runs it" hazard of the existing generator).

---

## 7. File-by-file modification plan

### New packages

```
winbrow/
├── brain/
│   ├── orchestrator.py        # session-aware execute(); merged router fast-path + brain loop
│   ├── agent_loop.py          # plan → act → observe → verify, budgeted steps, cancelable, replay capture
│   ├── model_gateway.py       # cloud-first LLM + tools/json-schema everywhere (from generator._call_* generalization)
│   └── prompts.py             # planner/actioner/verifier/summarizer prompt templates
├── memory/
│   ├── session.py             # working memory + entity slots + contextualizer
│   ├── store.py               # SQLite(FTS5)+Chroma episodic + semantic facts (absorbs memory.py, dedupes last_opened.py)
│   └── skills.py              # procedural capture/replay (success-plan → YAML)
├── computer/
│   ├── driver.py              # pyautogui/ctypes SendInput, clipboard, window mgmt
│   ├── uia.py                 # promote accessibility.py; element find/invoke APIs
│   ├── screen.py              # mss capture, Windows OCR (WinRT), diff-based change verify
│   └── ladder.py              # grounding escalation DOM→UIA→keys→OCR→VLM
├── adapters/
│   ├── base.py, registry.py   # adapter discovery/matching by process name/title
│   ├── vscode.py  photoshop.py  aftereffects.py  notepad.py  paint.py
│   ├── office.py  generic.py
├── installer.py               # winget + direct installer + UAC consent flow
└── permissions.py             # tier table + confirmation UX hooks (UI/voice)
```

### Modifications to existing files

| File | Change |
|---|---|
| `agent.py` | Slim to orchestrator shell: build session, contextualize, delegate to `brain/orchestrator.py`. Remove keyword banks (`BROWSER_KEYWORDS`, `_ULTRAFAST_TRIGGERS`, `_is_known_task`) — they move behind one `IntentClassifier` used by both tiers. |
| `router.py` | Keep exact-intent fast paths only (volume/lock/launch/folders/window). New intent-kind `AGENT` (was NEW_ACTION) → brain. Delete legacy `_fallback_route` duplication. |
| `registry.py` | Skills-as-data: every adapter op + ComputerDriver op registers as a `Tool` with args schema + verify hook, so System 1 and brain share one action space. Learned tools migrate to `memory/skills.py`. |
| `llm.py` | Becomes thin: delegates to `brain/model_gateway.py` (keeps `FakeLLM` for tests). Replace the hardcoded 0.8B path with gateway priority chain. |
| `policy.py` | Add tiers, allowlist store, per-action gate; keep existing regexes as T3 blocklist. Generator/brain/static tools all call it. |
| `generator.py` | Retain as "write a *skill atom*" fallback (PS one-shot) but only called from the brain; persist into procedural memory w/ parameters, not just raw strings. |
| `server.py` | New endpoints: `GET/POST /api/session` (list/switch), `POST /api/confirm` (permission flow), `GET /api/memories?q=`, WS events get `plan/step/verify` frames. Startup wires gateway + memory + adapters. |
| `voice.py` | Feed STT through the same orchestrator (drops its private LLM path); adds spoken confirmation prompts. |
| `accessibility.py` | Thin wrapper re-export from `computer/uia.py` (keeps `observe` CLI working). |
| `ultrafast.py` | Unchanged (browser path stays); but wins a generic `run_goal_on_page(goal)` shared with brain via `browser_task.py`. |
| `last_opened.py` | Superseded by `memory/session.py` (kept temporarily as adapter for trace compat, then deleted). |
| `memory.py` | Merged into `memory/store.py` (SQLite+Chroma actually used this time). |
| Dead code | Root `agent.py`/`run.py` legacy prototype, `test_syntax*.py` scratch files → delete. |
| `requirements.txt` | `pyyaml`, `onnxruntime` (already present) pinned; optional `[cloud]` extra: `openai`, `google-genai`, `groq`. chromadb/uiautomation/comtypes/mss/sounddevice noted (currently missing from requirements but used!). |

---

## 8. System 2 agent loop semantics (the core new behavior)

```
context = { session turns, entities, live UIA tree (≤60 nodes), active window,
            clipboard, recent episodic hits, available skill atoms }
plan   = gateway.chat(planner_prompt, tools=skill_defs, json_schema=PlanSchema)
for step in plan.steps:                       # budget: e.g. 25 steps
    result   = skill_exec(step)               # tool / adapter op / driver op (tier-gated)
    verify   = verify(state_before, result)   # title/UIA/clipboard/DOM/screenshot-diff
    if verify.failed and retries < 2:
        re-ground: try next rung on ladder; escalate to vision if configured
    if verify.failed and retries exhausted: → re-plan with observed state (max 3 re-plans)
    capture record → episodic + (on success of full task) procedural candidate
answer = compose final answer (+ spoken TTS if voice mode)
```

Design choices & rationale:
- **Plan-then-execute:** cloud LLM produces a full multi-step plan once; steps run locally in ms. A hybrid beats pure reactive (slow) and pure planned (brittle) — recovery re-plans are bounded.
- **Verify-first philosophy:** every observable state diff (window title changes, clipboard content, file exists, DOM URL) is cheap nondeterministic ground truth — this is what makes control of arbitrary apps trustworthy.
- **Cancelation/stepping:** step events stream to UI/WS; user can interrupt or approve mid-plan (T2/T3 confirmations inline).
- **Skill capture:** successful multi-step runs save as parameterized YAML plans ("photoshop_desaturate(folder, fmt, quality)") — next time instant + policy-visible, editable by the user.

---

## 9. Milestones (each independently shippable, testable)

### M0 — Naming brain + memory session (foundation)  ~1 week
1. `model_gateway.py` (OpenRouter/DeepSeek default → other clouds → local fallbacks; tools + JSON schema); unit tests with `FakeLLM`-style mock.
2. `memory/session.py` + wire into `execute()`: entity slots, pronoun/“that X” resolution, rolling summary.
3. `memory/store.py`: activate SQLite+Chroma (imports fixed), record every execution + retrieval API.
4. Router: NEW_ACTION → `brain/agent_loop` minimal (plan → run ≤1 skill → answer).
5. server.py session endpoints + WS step events.

**Acceptance:** "open leoantony.png" → "and make it fullscreen" → "close it again" work as one conversation; "what did I open yesterday?" answers from episodic memory; any novel-but-simple command (e.g. "create a folder called invoices in downloads") completes via brain with ≤4 steps.

### M1 — Native computer control  ~1–2 weeks
1. `computer/driver.py` (SendInput/clipboard/window mgmt) + `screen.py` (mss + Windows OCR).
2. `computer/uia.py` promote + `ladder.py`; integrate as SkillAtoms.
3. Brain gains multi-step GUI actions with verify pass ("click X in current app and confirm it happened").

**Acceptance:** Notepad: open→type→save-as with dialog handling. Paint: open image→invert colors→save copy. Any window: "minimize everything except vscode". Screenshot+OCR grounding: "click Export in the dialog".

### M2 — Adapters & procedural memory  ~2 weeks
1. Adapters: vscode (CLI+palette), photoshop (COM+JSX incl. actions playback), aftereffects (aerender+JSX), office COM.
2. `memory/skills.py`: plan capture; replays parameterized skills locally; user-editable in UI.
3. Registry integration (adapter ops as fast-path tools).

**Acceptance:** "open D:\Codee in vscode and install the python extension"; "open wedding.jpg in photoshop, desaturate 50%, save as copy_desat.jpg"; "render comp 'teaser' from my project to desktop as h264" each succeed end-to-end; a second run of the same flows runs <2s via replay (no LLM).

### M3 — Installer + permission tiers  ~1 week
1. `installer.py` (winget-first) streaming progress; 2. `permissions.py` tiers + confirm UI/voice; 3. policy v2 applied to every executor.

**Acceptance:** "install 7zip without admin annoyances" → winget silent route w/ single confirm; "uninstall spotify"; attempts to silently delete System32-class paths blocked.

### M4 — Conversational hardening + voice + observability  ~1–2 weeks
1. Voice pipeline rebuild (see §12): silero-VAD gating, openWakeWord-class KWS, whisper only on real utterances, prompt priming from memory, cloud-STT option.
2. Autocorrect v3 (see §12): Levenshtein pass + gateway rewrite for low-confidence routes + learned corrections in semantic memory.
3. Voice loop through orchestrator + spoken confirmations (TTS via Windows SAPI — free, local, instant on this hardware).
4. Trace v2 (step-level JSONL + latency by stage); headless e2e scenario suite (recorded-window fixtures).

**Acceptance:** wake word → command → confirm → result fully hands-free with <3s perceived latency on 2 cores; "hey winbrow… open the file I was editing in photoshop" resolves via session memory; 25-script regression with ≥90% pass across sessions; no regression in existing tests (`test_winbrow_all.py`; updated for refactor).

### M5 — Optional accelerators (after hardware upgrade or w/ cloud vision)
Windows "observe" background service (proactive suggestions: "you opened report.pdf yesterday — open again?"), OmniParser v2 / UI-TARS pixel tier if a GPU or cloud-VLM budget exists, GPU llama.cpp for a local 7–14B brain.

---

## 10. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| RAM exhaustion (1.8 GB free) with uvicorn+Chroma+OCR | Medium | Dev caps; embeddings cache; auto-GC session summaries; Ollama/llama.cpp off unless dialog-local tasks |
| STT/timing race in GUI dialogs (save-as focus) | Medium | Adapter-level waits (`wait_for_window`), clipboard paste rules, UIA verification post-act |
| UAC blocks install from non-elevated process | Medium | Never bypass: explicit consent UI ("a UAC prompt will appear"), winget `--scope user` fallback |
| Antivirus flags SendInput automation | Medium | Sign/publish whitelist note; avoid driver-level libs; document |
| AE/ExtendScript guardian-app quirks | Medium | Prefer aerender; timeouts + progress parse; UI fallback |
| Cloud cost/outage | High on this plan | Gateway multi-provider fallback chain + tiny local model for unresolved refs/summaries; strict max-tokens budgets |
| LLM-driven actions unsafe | Medium | Tiers + policy gate on *every* step, incl. brain; blocklists persist |
| Keyword-bank removal regressions | Low | Keep golden traces: record current behavior first (`tools/trace_before_v3.jsonl`) then compare after refactor |

---

## 11. Decision log (rubric: speed · reliability · cost · coverage)

1. **Grounding ladder over pixel-first** — API/UIA gives ms-scale, token-cheap, verifiable; pixel agent only as last resort given no GPU.
2. **Cloud-first brain, local fast-path** — mandated by hardware; gateway abstracts it so a future GPU upgrade flips `priority` to local transparently.
3. **Adapters as first-class skill atoms (not prompt-only)** — reliability for the money apps (PS/AE/VSCode); the GUI loop is for everything else.
4. **Procedural memory = parameterized plans** (PS-A only) — unlocks "do that again" for GUI flows, not just shell.
5. **Unified action space through registry** — System 1 and System 2 share atoms, so speed and coverage never fork.
6. **Tiers on every executor path** — T1..T3 confirmations are visible to users, never silent.
7. **Session working memory as separate layer** — different lifecycle/access patterns (ms-latency, in-RAM) than episodic (on-disk, queryable).
8. **Delete keyword-duplicates** — three overlapping keyword banks today; one `IntentClassifier` after refactor removes the misroute class.
9. **OpenRouter + DeepSeek as default brain** — OpenAI-compatible (tools + JSON schema), ~$0.3/M input tokens, zero local load; gateway keeps multi-provider fallback so no vendor lock-in. Local Llama/Qwen downloads dropped from startup.
10. **STT stays local, LLM goes cloud** — audio must be processed in real time on-device; text can afford 1–3s round-trips. VAD/KWS wake-word replaces whisper-per-chunk (~10× CPU reduction).

---

## 12. Voice, STT & autocorrect — current state and v3 design

### 12.1 Current state (as-built)

| Piece | File | Status |
|---|---|---|
| Recording | `stt.py:130` AudioRecorder — sounddevice 16kHz mono int16, temp WAV | Works |
| STT | `stt.py:84` WhisperSTT — spawns `whisper-cli.exe` (base.en GGUF) per file | Works, one-shot only |
| Wake word | `voice.py:86` — transcribes **every 1s chunk** and substring-matches "hey winbrow" | Works, ~10× CPU waste on 2 cores |
| Streaming | `stt.py:127` | `NotImplementedError` |
| Autocorrect | `router.py:317` `autocorrect_utterance()` — token Levenshtein vs ~60-word vocab; concat-word merge; disk-exists guard so real paths are never "repaired" | Works for commands; no proper-noun memory |
| Known bug | `stt.py:49,77` use `platform.system()` without importing `platform` | Crashes when binary missing |
| Known waste | `voice.py` rebuilds agents/routers per command; calls `extract_intent()` then discards the result | To be deleted in M4 |

### 12.2 v3 voice pipeline

```
mic stream (16k mono)
  │
  ▼
[VAD gate — silero-vad ONNX, ~2MB, <10ms/100ms-frame on 2 cores]
  │ only speech frames advance
  ▼
[Wake-word KWS — openWakeWord-class ONNX (~10MB) streaming on rolling 1.5s buffer]
  │ "hey winbrow" (or push-to-talk hotkey / UI mic button — all three entry points)
  ▼
[Utterance capture — VAD end-of-speech (0.8s trailing silence) → bounded ≤30s]
  │
  ▼
[whisper.cpp — base.en (or small.en trade-up if latency allows), threads=2, beam=1
 initial_prompt primed from memory: recent file names, app names, last entities
 → proper nouns like "leoantony.png" transcribe correctly instead of autocorrected guesses]
  │
  ├─ optional cloud STT (Groq Whisper / OpenAI whisper-1 / Deepgram) if
  │  WINBROW_CLOUD_STT=1 — faster + better accents; adds network dependency
  ▼
{ text, confidence, segments } → Session Orchestrator (§2) — same path as typed input
  │
  ▼
[TTS — Windows SAPI (PowerShell System.Speech), local + instant; spoken answers,
 confirmations ("Install 7-Zip — say confirm"), and step narrations in voice mode]
```

Design notes:
- **VAD/KWS replace whisper-per-chunk** — CPU drops from near-100% standby to ~2–5%; whisper runs only for actual commands.
- **One entry point**: voice and typed input merge at `orchestrator.execute()`; voice adds `modality="voice"` so the UI can echo prompts and use spoken-length phrasing.
- **Prompt priming list** built fresh per utterance from working memory (last entities) + top episodic file names — this is the single highest-leverage fix for "it doesn't understand file names".
- Wake word model trained on "hey winbrow" via openWakeWord custom training, or ship generic KWS with config in UI. Fallback: keyboard push-to-talk (zero extra deps).

### 12.3 Autocorrect v3 — three stages

1. **Stage 1 (keep)**: existing Levenshtein pass (`router.py:317`) as the System 1 pre-filter — cheap, typo-proof for the 60-word command vocabulary.
2. **Stage 2 (new, brain)**: for commands that reach the brain with low route confidence or STT confidence < threshold, the gateway gets a cheap "normalize" call — rewrite the utterance against a live vocabulary hint block (running apps, adapter app-ids, recent entities, seed-tool names). DeepSeek-class models handle this trivially and it removes the whole "closest-word guess" class of error.
3. **Stage 3 (new, learned)**: when a rewrite X→Y leads to a successful execution, persist the pair to semantic memory; future X tokens auto-normalize in Stage 1 (no LLM call). User can review/clear learned corrections in the UI.

Failure modes covered: "open chrome" spoken as "open crome" (Stage 1), "put this in my notepad" → "notepad" intent (Stage 2), and personal proper nouns ("leoantony", "codee") that Levenshtein would destroy (prompt priming + Stage 3 memory, plus the existing disk-exists guard).
