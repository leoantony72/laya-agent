# WinBrow — How the App Works (Complete Technical Reference)

> Windows-native desktop & browser control agent. You type (or speak) a
> command; the app routes it to the right Windows action in milliseconds and
> shows what happened. This document describes the **current** implementation
> in high detail: every layer, every decision, every file.

## Table of Contents

1. What the App Is
2. Running It
3. Big-Picture Architecture
4. End-to-End Request Lifecycle
5. The Windows Layer (`winbrow/windows.py`)
6. The Router (`winbrow/router.py`)
7. The Tool Registry - All 41 Built-in Tools
8. Deep Dive: Volume Control Pipeline
9. Deep Dive: Open-Folder Resolution
10. Deep Dive: Close Flows (Window vs App)
11. The Generator Tier (`winbrow/generator.py`)
12. The Safety Policy (`winbrow/policy.py`)
13. Browser Control (`winbrow/browser_task.py`)
14. The Web Server (`server.py`)
15. The Frontend (`templates/index.html`)
16. History and Learned Tools
17. Error-Handling Philosophy
18. Performance Characteristics (Measured)
19. Tests
20. File Map
21. Worked Examples (Step-by-Step Traces)
22. Known Limitations

---

## 1. What the App Is

WinBrow is a **command router + executor** for Windows 10/11, exposed through
a small web UI. There is no autonomous click-loop, no screenshot reasoning,
and no persistent background agent. Each user command is an independent
request/response cycle:

```
command text → classify intent → render a PowerShell script (or call a
native API) → execute it → return human-readable output
```

Key design principle: **choose, don't generate**. A decision model (Laya) or a
deterministic heuristic picks one of 41 pre-written tools with arguments.
Only when nothing matches does an LLM/synthesizer write a new PowerShell tool
on the fly — and that tool is then saved for instant reuse.

There are two codebases in the repo. The live one is **`winbrow/` +
`server.py`**. The older prototype (`agent.py`, `run.py`, `test_agent.py`)
uses screenshot/pyautogui loops and is **not** served or imported by the
server.

---

## 2. Running It

`start.bat` runs `py -3 -m uvicorn server:app --host 127.0.0.1 --port 8765`,
then open `http://127.0.0.1:8765`. Dependencies are in `requirements.txt`
(`laya`, `fastapi[standard]`, `uvicorn[standard]`, `pyautogui`, `mss`,
`psutil`, `Pillow`, `jinja2`, `python-multipart`).

On server startup (`_warmup` in `server.py`), one throwaway PowerShell command
runs once. Reason: the first `powershell.exe` spawn in a Windows session costs
5–25 seconds (cold .NET JIT + Defender scan); warming pays that cost at boot
instead of on the user's first command.

---

## 3. Big-Picture Architecture

```
Browser UI (templates/index.html)
  │  REST (/api/execute) or WebSocket (/ws)
  ▼
FastAPI server (server.py)
  │  WinBrowAgent.execute(utterance)          [winbrow/agent.py]
  │    1. get_context() → WindowsContext      [winbrow/windows.py]
  │    2. router.route(utterance, ctx) → Route [winbrow/router.py]
  │         ├─ heuristic pass 1 (raw text, ~1 ms)
  │         ├─ heuristic pass 2 (typo-corrected text)
  │         └─ Laya Choice model (5 s cap) → fallback heuristics
  │    3. dispatch by Route.kind:
  │         ├─ "tool"    → render PowerShell template → run_powershell()
  │         │             (volume_set_level bypasses PowerShell → native WASAPI)
  │         ├─ "browser" → BrowserController (CDP or keystroke fallback)
  │         ├─ NEW_ACTION→ browser dispatch? else ScriptGenerator tier
  │         ├─ CHAT      → fixed helpful reply
  │         └─ STOP      → "standing by" reply
  ▼
Structured JSON trace { utterance, route, context, execution, total_elapsed_ms }
```

Module responsibilities:

| Module | Role |
|---|---|
| `winbrow/agent.py` | Orchestrator. Owns registry, router, generator, browser. Entry: `execute()`. |
| `winbrow/router.py` | Intent → `(tool, args)`. Heuristics first, Laya second. |
| `winbrow/registry.py` | 41 `Tool` definitions (PowerShell templates + arg specs) + learned-tool persistence. |
| `winbrow/windows.py` | Live OS context, native volume (WASAPI/COM), `run_powershell()`. |
| `winbrow/generator.py` | Writes new PowerShell tools via LLM or offline synthesizer. |
| `winbrow/policy.py` | Blocks destructive scripts. |
| `winbrow/browser_task.py` | Chrome/Edge control via CDP + keystroke fallback. |
| `server.py` | FastAPI app: pages, REST, WebSocket, settings, warm-up. |
| `templates/index.html` | The entire UI (inline CSS + JS, no build step). |
| `tools/learned.json` | Persisted generated tools (currently `[]` — §11 explains why). |

---

## 4. End-to-End Request Lifecycle

### 4.1 REST path

`POST /api/execute {"command": "...", "provider"?, "api_key"?, "endpoint"?}`:

1. Empty command → HTTP 400.
2. Provider/key/endpoint resolve from the request or the in-memory
   `runtime_settings` (set via `POST /api/settings`; defaults: provider
   `auto`, endpoint `http://localhost:11434/api/generate`).
3. `await agent.execute(cmd, ...)` runs the full pipeline (§4.3).
4. The result is broadcast to all WebSocket clients and returned as JSON.

### 4.2 WebSocket path

`WS /ws` accepts JSON frames:

- `{"action": "context"}` → replies `{"type": "context", "data": {...}}`.
- `{"action": "execute", "command": "..."}` → first sends
  `{"type": "routing", "command": ...}` (UI shows a spinner), then runs the
  agent with server-side runtime settings and replies
  `{"type": "result", "data": {...}}`.
- `{"action": "system"}` → context + tool count + Laya status.

Dead sockets are pruned on broadcast failure.

### 4.3 `WinBrowAgent.execute()` internals

1. `ctx = await run_in_executor(get_context)` — process enumeration + COM
   reads stay off the event loop.
2. `browser_fast = _is_browser_intent(utterance)` — single-word keywords use
   word-boundary regex (plural allowed), so `tab` matches `tabs` but never
   `tables`; multi-word phrases use substring match.
3. `is_search` stays on the tool path for `search …`/`google …` (unless the
   phrase is about tabs/scroll/screenshots/incognito).
4. `route = await router.route(utterance, ctx)` (§6).
5. Dispatch, in order:
   - **`tool`**: `volume_set_level` calls `set_windows_volume(level)`
     directly in Python (no PowerShell spawn); `success` is true when the
     message contains `"set to"`. Everything else renders its PowerShell
     template and runs it; `success` mirrors the exit code; `output` is
     stdout, else stderr, else `"Done"`.
   - **Browser fast-path** (`browser_fast and not is_search and route.kind
     != "tool"`): `_dispatch_browser()` (§13); the route kind is rewritten to
     `"browser"` for display.
   - **`NEW_ACTION`**: browser dispatch first if `browser_fast`, else the
     generator tier (§11). The record carries the tool, `is_learned: True`,
     the rendered script, and output (stdout → stderr → honest-failure
     `reason`).
   - **`CHAT`** → capability summary. **`STOP`** → "Standing by."
6. `total_elapsed_ms` is stamped; the result is appended to the 100-entry
   ring-buffer `agent.history` (served by `GET /api/history`).

---

## 5. The Windows Layer (`winbrow/windows.py`)

### 5.1 `WindowsContext`

```python
active_app: str = "Desktop"   # pretty name, e.g. "Google Chrome"
active_title: str = ""        # foreground window title
running_apps: list[str]       # user-facing processes, sorted
volume_level: int = 50        # 0–100 (50 doubles as "could not read")
is_muted: bool = False
dark_mode: bool = True        # from HKCU…\Personalize\AppsUseLightTheme
battery_percent / battery_plugged
```

Captured by `_capture_windows_context()`:

- **Foreground window**: `GetForegroundWindow` + `GetWindowTextW` +
  `GetWindowThreadProcessId` via ctypes, PID → process name via psutil,
  prettified through `COMMON_APP_MAP` (`chrome` → `Google Chrome`, …).
- **Running apps**: one `psutil.process_iter(["name"])` pass, skipping
  non-`.exe` names and `SYSTEM_PROCESS_PREFIXES` (`svchost`, `dwm`,
  `conhost`, …).
- **Battery**: `psutil.sensors_battery()`. **Dark mode**: registry
  `AppsUseLightTheme == 0`. **Volume**: `get_windows_volume()`.

### 5.2 Context cache

`get_current_windows_context(use_cache=True)` memoizes for **2 s**
(`_CTX_CACHE` + `_CTX_TTL_S`): cold captures cost up to ~600 ms, warm reads
~2–15 ms. `invalidate_context_cache()` runs after every successful volume
set so the next read is fresh.

### 5.3 Native volume (WASAPI, zero dependencies)

`set_windows_volume()` / `get_windows_volume()` drive the Windows Core Audio
API over COM with hand-rolled ctypes vtables (`MMDeviceEnumerator →
IMMDevice → IAudioEndpointVolume`, `SetMasterVolumeLevelScalar`). Level
parsing is centralized in `_parse_volume_level()` (§8). Reads return `50` on
any COM failure (headless/RDP) — treated as "unknown", never a real level.

### 5.4 `run_powershell(script, timeout=30)`

Async PowerShell execution used by every tool except volume:

- Prepends UTF-8 output-encoding boilerplate.
- Spawns `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass
  -Command <script>` with piped stdout/stderr.
- Returns `{success, returncode, stdout, stderr, elapsed_ms}`; timeouts and
  spawn failures become structured error dicts, never exceptions.
- Cost: ~1–3 s warm per spawn, up to ~25 s stone cold (see §2 warm-up).

## 6. The Router (`winbrow/router.py`)

`WinBrowRouter.route(utterance, ctx)` returns a `Route(kind, tool, args,
confidence, probabilities, latency_ms, reason)` with `kind` in `{"tool",
"chat", "new_action", "stop", "uncertain"}`. Routing is a three-stage
pipeline, cheapest first.

### Stage 0 — Heuristic pass 1 (raw text, ~1 ms)

`_heuristic_route()` lowercases the text (after conversational-prefix
stripping) and runs ~25 ordered intent blocks: volume → media → dark mode →
settings panes → time → file search → file-open → folder → wallpaper →
brightness → type → bluetooth → uptime → running apps → screenshot → night
light → large files → notes → lock → IP → shutdown → common apps → desktop
organize → generic open/launch → web search → window management →
close-folder → close-app → performance → system info → wifi → clipboard →
network → temp/download cleanup → battery → timer. Any block may return
`Route(kind="tool", confidence >= 0.8)`, which short-circuits everything,
including Laya.

### Stage 0b — Typo-corrected second pass

Runs **only if pass 1 missed**:

1. Extra vocabulary is built from `ctx.running_apps` + `ctx.active_app`.
2. `corrected = autocorrect_utterance(text, extra)`.
3. Heuristics re-run on the corrected text; on a confident hit the router
   logs `'raw' -> 'corrected' -> tool` and returns it.
4. Otherwise the **corrected** text is handed to Laya (cleaner model input).

Because correction only runs after a miss, it can only *add* routes — a
confident raw match (e.g. the real `codee` folder) is never "corrected" away.

### Stage 1 — Laya Choice model (capped)

On a full miss, a `questions` dict is built: one `choice` question over the
first 61 tools (each described with two examples), plus `chat` /
`new_action` / `stop` options, plus one `choice` question per enum tool-arg.
`Router.predict(text, questions)` runs on a **daemon thread** with a hard
**5-second** cap (`LAYA_PREDICT_TIMEOUT_S`, `_predict_with_timeout`):

- `chat` / `stop` → returned directly.
- `new_action` or confidence < 0.35 → `NEW_ACTION` ("no tool matches").
- Otherwise enum args come from Laya's per-arg choice; text args come from
  `_extract_text_arg()`.

A daemon thread (not a pool worker) is deliberate: after a timeout the
abandoned inference must not linger inside an executor the event loop joins
at shutdown — otherwise every timed-out command stalls process exit by
minutes (measured improvement: 251 s → 5.2 s wall time on the same test).
Any Laya exception or timeout logs a warning and falls through to a final
heuristic call (which yields `NEW_ACTION` when nothing matches).

### 6.1 Conversational-prefix stripping

`_strip_conversational_prefix()` loops off leading fluff — `can/could/will/
would you`, `please`, `hey/ok/hi/hello`, `i want/need/'d like you to`, `do me
a favor` — so *"can you open the desktop_agent folder"* routes as *"open the
desktop_agent folder"*. Applied inside `_heuristic_route()` and
`_extract_text_arg()`; Laya always sees the full original sentence.

### 6.2 The autocorrect engine

- `_levenshtein(a, b)`: classic DP edit distance (words are short).
- `_closest_word(word, vocab)`: budget 1 edit for words of 4 or fewer chars,
  2 edits otherwise, with a length pre-filter.
- Vocabulary: ~60 command verbs, app names, folder/system words, plus live
  running-app names. Stopwords and short tokens are skipped; tokens with
  digits/underscores (paths, filenames) are never touched.
- Merged-word repair: tokens of 8+ chars with no close match are tried at
  every split (`openchrome` → `open` + `chrome`) when both halves resolve.
- Repeat-guard: a word that merely extends a vocab word with a repeated
  trailing character (`codee` vs `code`) is left alone.

Verified corrections: `openc hrome` → `open chrome`, `reblox` → `roblox`,
`qbitorrent` → `qbittorrent`.

### 6.3 Key intent blocks (selection)

- **Volume** (§8): max-words/100% → `"100"`; min-words → `"0"`; else digit
  regex clamped 0–100. Mute words fall through to `system_volume`.
  Settings requests are excluded first. Tolerates STT typos.
- **Settings panes** (before app/folder/open handling): sound/audio →
  `windows_settings(sound)`; wifi/network → `(network)`; bluetooth; display;
  else `open_settings_page` for privacy/storage/accounts/…, falling through
  to generic Settings-app launch when no page matches.
- **File-open**: `open <image|photo|pdf|file|document|video|song|audio> [of]
  <name>` → `find_files(name)`; explicitly skips `explorer`.
- **Folder** (§9): evidence-ordered (system name → real dir → exact app →
  real dir → fuzzy app → honest attempt) with word-boundary app exclusions.
- **Wallpaper** (tolerant to `wallaper`/`wall paper`): → `set_wallpaper`;
  vague follow-ups (`use any image`) route with an empty arg so the tool
  answers with guidance instead of a fake success.
- **Screenshot**: clipboard/copy/paste → `screenshot_clipboard`; otherwise
  `take_screenshot` with destination sniffed from the sentence.
- **Close-folder**: `close … folder … window` → `close_folder_window`.
- **Close-app**: `close/kill/quit/exit/terminate <name>` (no window/folder/
  tab words) with the name matching a known or running app →
  `kill_unresponsive_process`.
- **Performance**: cpu/memory/ram-usage and `top processes` phrases →
  `system_performance` (placed before generic system info).
- **Type**: leading `type/write/enter text` → `type_text` (pastes via
  clipboard for Unicode).

### 6.4 `_extract_text_arg()`

Per-tool argument pullers used by both heuristic and Laya paths: prefix
stripping for search/app/folder (plus trailing-`folder`, leading-`my`/`the`
cleanup for folders), keyword-first level parsing for volume, digit clamping,
and raw-text fallback. Always runs on prefix-stripped text.

## 7. The Tool Registry — All 41 Built-in Tools

Each `Tool` has a `name`, one-line `description` (also fed to Laya as the
choice text), a PowerShell `script` template with `{{arg}}` placeholders, an
optional `scope` (app-specific tools only surface when that app is focused),
typed `args` (`enum` with choice criteria, or free `text`), a `speak` mode,
`examples`, and an `is_learned` flag. `render_script()` substitutes
`{{arg}}` with quote-escaped values (`"` → backtick-quote, `'` → doubled).

| # | Name | What it does | Args |
|---|---|---|---|
| 1 | `system_volume` | Volume up/down/mute/unmute via media keys | action enum |
| 2 | `media_playback` | Play/pause, next, previous track | operation enum |
| 3 | `toggle_dark_mode` | Dark/light theme via `HKCU:...\Personalize` | mode enum |
| 4 | `app_focus_or_launch` | Focus a running app or `Start-Process` it | app_name text |
| 5 | `window_action` | Minimize/maximize/desktop/close active window | action enum |
| 6 | `organize_desktop` | Move loose Desktop files into a dated archive | — |
| 7 | `web_search` | Google search or open a URL directly | query text |
| 8 | `windows_settings` | Open sound/network/bluetooth/display/battery/apps panes | pane enum |
| 9 | `clipboard_inspect` | Show or clear the clipboard | action enum |
| 10 | `get_wifi_password` | `netsh` key extraction for current/saved networks | profile text |
| 11 | `battery_health` | Charge status or full `powercfg` report | action enum |
| 12 | `system_spec_info` | OS/CPU/RAM/disk/uptime summary | — |
| 13 | `clean_temp_files` | Delete Windows Temp contents | — |
| 14 | `kill_unresponsive_process` | Kill hung or named processes | target text |
| 15 | `network_status` | Local IP + internet ping check | — |
| 16 | `current_time` | Time, date, timezone, weekday | — |
| 17 | `find_files` | Name search across Desktop/Documents/Downloads/Pictures | search_term text |
| 18 | `open_folder` | System folders **and** arbitrary named folders/paths | folder text |
| 19 | `volume_set_level` | Exact 0–100 volume (native API, no PowerShell) | level text |
| 20 | `screen_brightness` | WMI brightness up/down/max/min/half | action enum |
| 21 | `screenshot_clipboard` | Full-screen capture to clipboard | — |
| 22 | `type_text` | Type into the focused window via clipboard paste | text text |
| 23 | `open_common_app` | Calculator/Paint/Terminal/Notepad/TaskMgr/… (15 apps) | app enum |
| 24 | `system_uptime` | Time since last boot + boot timestamp | — |
| 25 | `find_large_files` | Top-15 space hogs in user folders | — |
| 26 | `night_light` | Open Night Light settings with on/off intent | state enum |
| 27 | `show_running_apps` | Visible windows with titles + memory | — |
| 28 | `quick_note` | Timestamped Desktop note opened in Notepad | content text |
| 29 | `bluetooth_settings` | Open Bluetooth settings | — |
| 30 | `shutdown_timer` | Shutdown/restart timers + cancel | action enum |
| 31 | `open_settings_page` | 19 Settings pages (privacy/storage/about/…) | page enum |
| 32 | `countdown_timer` | Open the Clock app (timer/alarm/stopwatch) | — |
| 33 | `lock_screen` | `LockWorkStation` immediately | — |
| 34 | `public_ip` | Public IP via api.ipify.org, copied to clipboard | — |
| 35 | `always_on_top` | PowerToys Win+Ctrl+T pin toggle | — |
| 36 | `clean_downloads` | Archive 30+ day old Downloads | — |
| 37 | `set_wallpaper` | Set wallpaper from a path or searched picture | image text |
| 38 | `take_screenshot` | Save full-screen PNG to Desktop/Downloads/Documents/Pictures | location text |
| 39 | `close_folder_window` | Close Explorer windows showing a folder | folder text |
| 40 | `system_performance` | CPU/RAM + top-5 processes | — |
| 41 | `open_file` | Find a file and open it (default app or Chrome/Edge) | target/app text |

`available(ctx)` filters by `scope`; `all_tools()` feeds counts and the
Tools tab. Learned tools load from `tools/learned.json` after built-ins, with
two guards: entries colliding with a built-in name, or containing the legacy
no-op placeholder marker, are skipped with a warning. `save_learned()`
renames colliding generated tools to `<name>_custom` instead of overwriting
built-ins.

## 8. Deep Dive: Volume Control Pipeline

1. Router: settings-page exclusion → `max/maximum/full/all the way up/full
   blast/100%` returns `level "100"` (conf 0.98); `min/minimum/zero` returns
   `"0"`; else a digit regex on volume/sound/audio phrasing, clamped 0–100
   (conf 0.95); else mute-aware `system_volume` (conf 0.9).
2. `_extract_text_arg()` repeats the same keyword-first logic with
   word-boundary regexes (a bare `0` can never match inside `20`).
3. `agent.execute()` calls `set_windows_volume(level)` **in-process** —
   no PowerShell spawn, ~2–15 ms.
4. `_parse_volume_level()` re-parses defensively (keywords → digits →
   clamp); unparseable input returns `None`, producing an explicit
   *"could not determine a level"* message — never a silent default.
5. WASAPI COM sets the scalar; the context cache is invalidated; the reply is
   `"Master volume set to N%"`.

## 9. Deep Dive: Open-Folder Resolution

Router evidence order for `open …`: (1) system-folder nickname →
`open_folder`; (2) exact installed/running app match → `app_focus_or_launch`
(so `open qbittorrent` launches even though a `qBittorrent` data folder
exists on disk); (3) real directory via `_folder_exists_on_disk()` (pure
Python `os.scandir`: user folders one level, fixed-drive roots two levels,
`C:`–`F:`, milliseconds) → `open_folder`; (4) fuzzy app match (typo-fixed)
→ app launch; (5) `open_folder` anyway — its script gives an honest error
listing everywhere it looked.

The PowerShell script mirrors this: system-folder map → literal/`~` path →
fixed-local-drive roots (two levels; **fixed drives only**, because
network/disconnected drives stall enumeration for seconds) + user folders →
`ERROR: Could not find…`. It never opens a wrong folder as a fallback.

## 10. Deep Dive: Close Flows (Window vs App)

- `close window / this window / active window / current window` →
  `window_action(close)` (Alt+F4 to the foreground window).
- `close … folder … window` → `close_folder_window(folder)`: resolves the
  folder, enumerates `Shell.Application` windows, URL-decodes `LocationURL`,
  and `.Quit()`s exact matches; reports how many closed or that none matched.
- `close/kill/quit/exit/terminate <name>` (without window/folder/tab words)
  where the name matches a known or running app (typo-tolerant:
  `reblox` → `roblox`) → `kill_unresponsive_process(target)`, which kills
  hung processes or name-matched ones and reports counts (or "not running").

## 11. The Generator Tier (`winbrow/generator.py`)

Reached only on `NEW_ACTION` (genuinely novel tasks). `generate_and_execute()`:

1. `_call_llm()`: OpenAI `gpt-4o-mini` if a key exists → local endpoint
   (10 s socket timeout, 12 s overall) → **offline `_synthesize_tool()`**.
2. The synthesizer pattern-matches 25+ task families (wifi, ping, battery,
   disk, temp cleanup, kill, CPU/RAM, screenshot **with destination
   sniffing**, lock, sleep, restart/shutdown, startup programs, services,
   network info, clipboard, focus assist, task manager, event viewer, error
   logs, installed software, updates, firewall, resolution, explorer paths,
   notepad, calculator, copy-to-clipboard, create/rename folders, recycle
   bin, brightness, system info, run-as-admin, DNS flush, control panel).
3. Anything else yields a **generic placeholder** (`# Generated automation
   for: …` + `Executed custom task:` echo).
4. `validate_script_safety()` (§12) runs before execution.
5. Execution via `run_powershell(..., timeout=30)`.
6. Persistence: on success the tool is saved to `tools/learned.json` —
   **except** placeholders, which return `success: False` with an honest
   *"I couldn't figure out how to automate that yet"* message instead of a
   fake success. This is why `learned.json` is intentionally `[]`: past
   placeholder pollution was purged, and the load/save shadowing guards (§7)
   keep it clean going forward.

## 12. The Safety Policy (`winbrow/policy.py`)

`validate_script_safety()` raises `PolicyError` on blocked patterns:
volume formatting / `C:\Windows|System32` deletion, forced shutdown/reboot,
disk init/clear cmdlets, `bcdedit`, `reg delete HKLM`, drive-root `rmdir`.
`is_destructive_action()` separately flags delete/kill/format-style intents.
Every built-in registers through the validator, and every generated tool is
checked before execution.

## 13. Browser Control (`winbrow/browser_task.py`)

`BrowserController` drives Chrome/Edge in two layers:

- **CDP (fast path)**: HTTP to `localhost:9222` (`/json/list`, `/json/new`,
  `/json/activate`) for tab listing, navigation, new tabs; raw WebSocket
  (`Page.captureScreenshot`, `Runtime.evaluate`) for screenshots, JS
  execution, page reading, clicking/typing via CSS selectors (3–8 s
  timeouts; missing `websockets` package degrades gracefully).
- **PowerShell + `WScript.Shell` keystrokes** (universal fallback): new/close/
  switch/reload/back/forward tabs, find-in-page, zoom, bookmarks, incognito
  flags, address-bar navigation with clipboard-pasted URLs, clipboard URL
  reading, `launch_with_cdp()` (adds `--remote-debugging-port=9222`).

`_dispatch_browser()` maps phrases to ~20 operations (new/close/list/switch
tabs, back/forward, reload, scroll with repeat counts, find-on-page, read
page, current URL, screenshots, CDP launch, URL navigation, bookmark/history/
downloads/fullscreen/devtools/print shortcuts, incognito). The pure-REST
`/api/browser` endpoint exposes the same operations directly
(`tabs/navigate/new_tab/close_tab/search/reload/back/forward/screenshot/url/
scroll/execute_js/read_page/launch_cdp/switch_tab/incognito/find/click/type/
bookmark/zoom_in/zoom_out/zoom_reset`).

## 14. The Web Server (`server.py`)

- `GET /` → the UI. Static dir mounted at `/static`.
- `POST /api/execute` → §4.1. `GET /api/context` (executor-offloaded),
  `/api/system` (context + version + Laya flag + tool/history counts),
  `/api/tools` (full registry incl. arg schemas + scripts),
  `/api/history` (last 50), `GET/POST /api/settings` (provider/key/endpoint;
  key reads are masked as `has_api_key`), `POST /api/browser`.
- `WS /ws` → §4.2 with reconnect-tolerant broadcast.
- Startup `_warmup()` absorbs the cold PowerShell spawn (§2).
- Single global `WinBrowAgent`; `PORT` env override (default 8765).

## 15. The Frontend (`templates/index.html`)

One dependency-free file (no build, no CDN — works offline):

- **Layout**: full-viewport chat column (max 680 px): slim header (logo,
  `Ready/Working/Listening` status with pulsing dot, Settings) → one-line
  live context (`App · N apps · battery`, polled every 10 s + after each run)
  → scrolling activity feed (flex-fills, internal scroll, snaps to newest) →
  bottom-docked command card (input + Mic + Run, 6 suggestion chips,
  saffron–cream–green tricolor strip).
- **Empty state**: centered hero (`What should I run?` + 2×2 clickable
  example tasks); replaced by the first result card.
- **Result cards**: command, type tag (Tool/Learned/Browser/Chat/Error),
  output block (red for errors), collapsible PowerShell source, footer with
  tool name, confidence, and total ms. Max 30 cards.
- **Settings modal**: provider/API key/endpoint → `/api/settings`.
- **Voice**: Web Speech API fills the input and submits; the Mic button hides
  where unsupported.
- **Theme/motion**: warm light tricolor tokens (saffron primary, India
  green, Ashoka navy, maroon), dot-grid + tinted washes, staggered
  rise-in entrances, hover lifts, spinner, `prefers-reduced-motion` support.
  Zero emojis.

## 16. History and Learned Tools

- `agent.history`: in-memory 100-entry ring buffer of full result traces;
  last 50 served by `/api/history`. No disk persistence (restarts clear it).
- `tools/learned.json`: persisted generated tools, loaded after built-ins
  with anti-shadow/anti-placeholder guards; writes rename on builtin-name
  collision. Placeholders are never persisted (§11).

## 17. Error-Handling Philosophy

No silent wrong actions, ever: unknown folders name every searched location;
unparseable volumes explain valid phrasing; missing wallpaper images ask
which image; un-automatable tasks say so instead of fake `Task completed`
(the old placeholder lie was removed from both the return path and the
persist path); PowerShell timeouts/failures surface stderr. `success: False`
renders red in the UI; every tool record keeps its script for inspection.

## 18. Performance Characteristics (Measured)

| Path | Latency |
|---|---|
| Heuristic route (all 33 covered intents) | ~1 ms |
| Full local tool command (warm) | 0.6–5 s (dominated by `powershell.exe` spawn) |
| Cold first-ever PowerShell spawn | 5–25 s (paid once via startup warm-up) |
| Unknown command (Laya cap + generator + PS) | ~8–13 s |
| Laya timeout wall time (daemon thread) | 5.2 s (was 251 s with pool workers) |
| Context capture | ~600 ms cold, ~2–15 ms warm/cached |

## 19. Tests

`test_winbrow_all.py` — 34 tests: the 33 heuristic/router tests run in under
a second; the Laya fallback test takes ~5 s by design (the timeout). Covered:
tool count, volume max/digits/min/typo/`_parse_volume_level` unit cases,
custom folders (`codee`, `desktop_agent`), `open code` still launching the
app, sound-settings routing, file intents, Explorer launch, wallpaper +
typo + image extraction, screenshot destinations, typo corrections
(`openc hrome`, `qbittorrent`), no-overcorrection (`codee`), close flows,
performance builtin, vague-follow-up guidance, browser-intent `tables`
guard, plus all pre-existing intent tests. `test_agent.py` is a manual
REST smoke script (needs a live server).

## 20. File Map

```
desktop_agent/
├── server.py              # FastAPI app (REST + WS + warm-up)
├── templates/index.html   # Entire UI (inline CSS/JS)
├── static/                # Mounted static dir (empty)
├── tools/learned.json     # Persisted generated tools ([])
├── test_winbrow_all.py    # 34-test suite (the real tests)
├── test_agent.py          # Manual REST smoke script (needs live server)
├── requirements.txt / start.bat / run.py
├── agent.py               # Legacy prototype (NOT served; pyautogui-based)
├── clean_encoding.py
└── winbrow/
    ├── agent.py           # Orchestrator: execute() + browser dispatch
    ├── router.py          # Heuristics → autocorrect → Laya → fallback
    ├── registry.py        # 40 tools + learned persistence + guards
    ├── windows.py         # Context, WASAPI volume, run_powershell()
    ├── generator.py       # LLM + offline synthesizer + no-op guard
    ├── policy.py          # Destructive-script blocking
    └── browser_task.py    # CDP + keystroke browser control
```

## 21. Worked Examples (Step-by-Step Traces)

**"set the volume to max"** → prefix strip (no-op) → volume block matches
`max` → `Route(volume_set_level, {level: "100"}, 0.98, ~1 ms)` → agent calls
`set_windows_volume("100")` in-process → `_parse_volume_level` → 100.0 →
WASAPI scalar → cache invalidated → `"Master volume set to 100%"`.

**"openc hrome"** → pass 1 misses (no known words) → second pass corrects to
`open chrome` → exclusion skips folder block → generic open →
`app_focus_or_launch(chrome)` → PowerShell focuses or `Start-Process`es it.

**"open qbittorrent"** → folder block: not a system name; exact app match
wins over the real `D:\qbittorent\qBittorrent` data folder →
`app_focus_or_launch(qbittorrent)`.

**"open codee folder"** → `folder_arg = "codee"`; autocorrect never runs
(first pass hits 0.92); disk scan finds `D:\Codee` → Explorer opens it.

**"take a screenshot and save it in downloads folder"** → screenshot block,
`download` sniffed → `take_screenshot({location: downloads})` →
`C:\Users\USER\Downloads\screenshot_<timestamp>.png`.

**"show system performance top processes"** → `system_performance` builtin
(CPU/RAM/top-5) in ~3 s — previously a 20 s generator round-trip.

**"blorple the zzz"** → heuristics miss → corrected text still misses → Laya
(≤5 s) → `NEW_ACTION` → synthesizer placeholder → policy pass → echo runs →
success reported `False` with guidance; nothing persisted.

## 22. Known Limitations

- PowerShell spawn cost (~1–3 s) applies per tool execution; trivial tools
  could move to native Python calls (only volume has so far).
- Laya on CPU-only machines always exceeds the 5 s cap, so it effectively
  never contributes there; heuristics + synthesizer carry the load.
- No multi-turn dialogue state: follow-ups ("use any image") re-prompt
  rather than filling slots from history.
- `get_windows_volume()` returns 50 when COM audio is unreachable; the UI
  then shows 50 as "unknown".
- History and runtime settings are in-memory only; restarts clear them.
- `agent.py`/`run.py` legacy prototype is dead code but still in the repo.


