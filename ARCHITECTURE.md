# WinBrow - Voice-First Windows Desktop Agent

## Architecture Overview

WinBrow is a voice-first Windows desktop control agent that implements a **System 1 / System 2** architecture inspired by human cognition:

- **System 1 (Fast)**: Laya decision router + deterministic tools → executes in ~200ms
- **System 2 (Slow)**: Conversational LLM (Qwen3.5 0.8B) → intent parsing, reference resolution
- **Fallback Vision**: Qwen2.5-VL for when accessibility/browser automation fails

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        VOICE INPUT (Wake Word)                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STT LAYER (Whisper.cpp)                                                    │
│  • Audio recording → Whisper.cpp (base.en model)                           │
│  • Streaming transcription support                                          │
│  • Output: text + confidence + segments                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  CONVERSATIONAL LLM (System 2) - Qwen3.5 0.8B                              │
│  • Intent parsing: utterance → structured JSON intent                      │
│  • Reference resolution: "that file" → concrete path                       │
│  • Runs via llama.cpp server (Qwen3.5 0.8B 4-bit GGUF)                    │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  ROUTING LAYER (System 1)                                                   │
│  1. Laya Choice Router (primary)                                           │
│     - Tool choice over available registry tools                            │
│     - Enum args via Laya choice questions                                  │
│     - Text args extracted from utterance                                   │
│  2. Exact-Intent Fallback (when Laya times out/unavailable)               │
│     - Volume, mute, lock screen, open_folder (validated), close_folder    │
│  3. Small LLM Tier (opt-in, Ollama)                                       │
│     - Paraphrase/typos handling                                            │
│  4. Generator Tier (last resort)                                          │
│     - LLM writes PowerShell tool → policy check → execute → persist       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  EXECUTION LAYER                                                            │
│  • 41 built-in tools (PowerShell templates)                               │
│  • Browser control via CDP (laya-ultrafast)                               │
│  • PowerShell persistent host (sub-second execution)                       │
│  • Native WASAPI volume control                                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  CONTEXT & MEMORY                                                           │
│  • Windows Context: active app, running apps, volume, battery, dark mode  │
│  • Semantic Memory: SQLite + ChromaDB (files, folders, apps, searches)    │
│  • Context Memory: last opened file/folder/app for "that file" references │
│  • Vision Fallback: Qwen2.5-VL screenshot analysis                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Module Reference

### Core Modules (`winbrow/`)

| Module | Purpose | Key Classes/Functions |
|--------|---------|----------------------|
| `agent.py` | Main orchestrator | `WinBrowAgent.execute()`, `_dispatch_browser()` |
| `router.py` | Laya + fallback routing | `WinBrowRouter.route()`, `_fallback_route()` |
| `registry.py` | Tool registry (41 tools) | `ToolRegistry`, `_load_seed_tools()` |
| `windows.py` | Windows OS primitives | `get_current_windows_context()`, `run_powershell()` |
| `stt.py` | Whisper.cpp STT | `WhisperSTT.transcribe_file()`, `AudioRecorder` |
| `llm.py` | Qwen3.5 LLM | `QwenLLM.extract_intent()`, `resolve_reference()` |
| `vision.py` | Qwen2.5-VL fallback | `VisionFallback.analyze_screenshot()` |
| `stt.py` | Speech-to-text | `WhisperSTT`, `AudioRecorder` |
| `accessibility.py` | UIAutomation tree | `AccessibilityTree.get_foreground_tree()` |
| `memory.py` | Semantic memory | `SemanticMemory`, `ContextMemory` |
| `voice.py` | Voice pipeline | `VoiceAgent`, `VoiceCommandHandler` |
| `model_manager.py` | Auto-download models | `get_model_path()`, `ensure_llama_cpp()` |
| `vision.py` | Vision fallback | `VisionFallback`, `ScreenshotCapture` |
| `accessibility.py` | UIAutomation | `AccessibilityTree` |
| `small_llm.py` | Opt-in Ollama tier | `route_with_llm()`, `validate_choice()` |
| `voice.py` | Voice pipeline | `VoiceAgent`, `VoiceCommandHandler` |

### Infrastructure

| File | Purpose |
|------|---------|
| `server.py` | FastAPI server (REST + WebSocket) |
| `server.py:_warmup()` | Pre-warms PowerShell host |
| `cli.py` | Developer CLI (`python -m winbrow observe`) |
| `generator.py` | Dynamic tool generation |
| `policy.py` | Safety validation |
| `browser_task.py` | CDP browser automation |
| `pshost.py` | Persistent PowerShell host |
| `resolvers.py` | Argument resolution & validation |
| `files.py` | File/folder/app enumeration |
| `last_opened.py` | Context memory |
| `trace.py` | JSONL tracing |

---

## Data Flow: Voice Command → Execution

```
User: "open leoantony.png file"
         │
         ▼
    [STT] Whisper.cpp → "open leoantony.png file"
         │
         ▼
    [LLM] Qwen3.5 extracts intent:
    {
      "intent": "tool",
      "tool": "open_file",
      "arguments": {"target": "leoantony.png", "app": ""},
      "confidence": 0.95
    }
         │
         ▼
    [ROUTER] WinBrowRouter.route()
         │
         ├── Laya Choice (primary) → tool: open_file, args: {target: "leoantony.png"}
         │   └─ If timeout/error → Fallback
         │
         ▼
    [FALLBACK] _resolve_open_file()
         │
         ├── Extract filename: "leoantony.png"
         ├── Search Downloads first → found: C:\Users\...\Downloads\leoantony.png
         └── Route: open_file {target: "Leoantony.pdf", app: ""}
         │
         ▼
    [EXECUTE] open_file PowerShell template
         │
         ├── Find file (Downloads → Desktop → Documents → Pictures)
         ├── Open in default app (or Chrome/Edge if specified)
         └── Returns: "Opened C:\Users\...\Leoantony.pdf"
         │
         ▼
    [MEMORY] Record successful open
         │
         ├── ContextMemory.record_open("file", path, label)
         └── SemanticMemory.record_file_open()
         │
         ▼
    [RESPONSE] Returns structured trace:
    {
      "route": {"kind": "tool", "tool_name": "open_file", "tier": "heuristic"},
      "execution": {"success": true, "output": "Opened ..."},
      "total_elapsed_ms": 2275
    }
```

---

## Tool Registry (41 Built-in Tools)

| Category | Tools |
|----------|-------|
| **System** | `system_volume`, `volume_set_level`, `toggle_dark_mode`, `lock_screen`, `system_uptime`, `system_spec_info`, `system_performance`, `current_time` |
| **Apps** | `app_focus_or_launch`, `open_common_app` (15 apps), `open_file`, `open_folder`, `close_folder_window` |
| **Window** | `window_action` (min/max/snap/close), `always_on_top` |
| **Media** | `media_playback`, `screenshot_clipboard`, `take_screenshot` |
| **Input** | `type_text` |
| **Settings** | `windows_settings`, `open_settings_page`, `bluetooth_settings`, `night_light` |
| **Files** | `find_files`, `find_large_files`, `organize_desktop`, `clean_temp_files`, `clean_downloads` |
| **Network** | `network_status`, `get_wifi_password`, `public_ip`, `flush_dns` |
| **Power** | `shutdown_timer`, `countdown_timer`, `lock_screen` |
| **Clipboard** | `clipboard_inspect` |
| **Battery** | `battery_health` |
| **Browser** | `web_search`, `browser_task` (CDP) |
| **Generated** | Dynamic tools persisted to `tools/learned.json` |

---

## Key Features Implemented

### 1. Auto-Download Models
```python
# On first run, automatically downloads:
# - Whisper base.en (~140MB)
# - Qwen3.5-0.8B 4-bit GGUF (~500MB)  
# - Qwen2.5-VL 3B 4-bit GGUF (~2GB)
# - llama.cpp server (built from source)
# - whisper.cpp (built from source)
```

### 2. Persistent PowerShell Host
```python
# winbrow/pshost.py - Persistent PowerShell host
# Avoids 1-3s cold spawn per command
# Warm at startup, survives script errors
```

### 2. Laya-First Routing with Fallback
```python
# router.py - Laya-first, heuristic fallback
async def route(self, utterance, ctx, memory=None):
    # 1. Try Laya (with 5s timeout)
    # 2. Exact-intent fallback (volume, mute, lock, open_folder, close_folder, open_file)
    # 3. Opt-in small LLM (Ollama) for paraphrases
    # 4. NEW_ACTION → generator tier
```

### 3. Context Memory for Follow-ups
```python
# "open that file" → resolves to last opened file
# memory.record_open("file", path, label)
# memory.resolve_reference("that file") → returns last file path
```

### 4. Vision Fallback
```python
# When accessibility/browser fails:
vision = VisionFallback()
await vision.start()
result = await vision.analyze_screenshot(screenshot_path, prompt)
# Returns structured UI analysis, clickable elements, OCR text
```

### 5. Accessibility Tree (UIAutomation)
```python
tree = await AccessibilityTree(max_nodes=200).get_foreground_tree()
# Returns structured UI tree with elements, bounding boxes, control types
```

---

## Configuration (Environment Variables)

| Variable | Default | Description |
|--------|---------|-------------|
| `LAYA_TIMEOUT_S` | `5.0` | Laya prediction timeout |
| `WINBROW_ROUTER_LLM` | `""` | Enable small LLM tier (set to `1`) |
| `WINBROW_ROUTER_MODEL` | `qwen2.5:0.5b` | Ollama model for small LLM tier |
| `WINBROW_ROUTER_ENDPOINT` | `http://localhost:11434` | Ollama endpoint |
| `WINBROW_ROUTER_TIMEOUT` | `25.0` | LLM timeout seconds |
| `WINBROW_DISABLED_TOOLS` | `""` | Comma-separated disabled tools |
| `WINBROW_ENABLED_TOOLS` | `""` | Override risky default-off tools |
| `WINBROW_ALLOW_SHUTDOWN` | `""` | Set to `1` to enable shutdown_timer |
| `OPENAI_API_KEY` | `""` | OpenAI key for generator tier |
| `LAYA_TIMEOUT_S` | `5.0` | Laya prediction timeout |

---

## Running the System

### Start Server (Web UI + API)
```bash
# Install dependencies
pip install -r requirements.txt
pip install chromadb comtypes sounddevice mss uiautomation

# Start server (auto-downloads models on first run)
python -m uvicorn server:app --host 127.0.0.1 --port 8765

# Or use start.bat on Windows
start.bat
```

### Voice Agent
```bash
# Listen for wake word "hey winbrow"
python -m winbrow.voice --listen

# Transcribe audio file
python -m winbrow.voice --file recording.wav

# Self-test
python -m winbrow.voice --test
```

### CLI Utilities
```bash
# Observe foreground window accessibility tree
python -m winbrow observe --max-nodes 50 --depth 4

# Test API
python test_agent.py
```

### Run Tests
```bash
# All tests (47 tests, ~5-8s)
python -m pytest test_winbrow_all.py -q

# With live Ollama (requires Ollama running)
WINBROW_ROUTER_LLM=1 python -m pytest test_winbrow_all.py -q
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET /` | Web UI |
| `POST /api/execute` | Execute command |
| `GET /api/context` | Live Windows context |
| `GET /api/system` | System info + tool count |
| `GET /api/tools` | All registered tools |
| `GET /api/history` | Last 50 executions |
| `POST /api/settings` | Update LLM settings |
| `WS /ws` | WebSocket (live context + execution) |
| `POST /api/browser` | Direct browser control |

---

## Safety & Guardrails

| Layer | Protection |
|-------|------------|
| **Policy** | `policy.py` - Blocks destructive scripts (format, delete system, bcdedit) |
| **Confirmation** | Destructive tools require explicit confirmation |
| **Disabled Tools** | `WINBROW_DISABLED_TOOLS` env var |
| **Risky Defaults** | `shutdown_timer` disabled by default |
| **Validation** | `resolvers.py` validates option IDs, clamps volumes, exact extension match |
| **Generator Safety** | Scripts validated by `policy.py` before execution |
| **No Silent Defaults** | Unparseable volumes return error, not silent 50% |

---

## Testing

```bash
# All tests (47 tests)
python -m pytest test_winbrow_all.py -q

# Key test categories:
# - Router: tier selection, fallback behavior, typo handling
# - Volume: max=100, min=0, digits, mute/unmute
# - Folders: real dir resolution, "open downloads folder please"
# - Files: "open images.jpg" → open_file, not open_folder
# - Close folder: "close the downloads foler" (typo-proof)
# - Memory: record/retrieve last file/folder
# - LLM tier: validation, unknown tool rejection, bad enum coercion
```

---

## Project Structure

```
desktop_agent/
├── server.py                 # FastAPI server
├── start.bat                 # Windows launcher
├── requirements.txt          # Python dependencies
├── tools/
│   ├── seed.json            # 41 built-in tool definitions
│   ├── learned.json         # Dynamically generated tools
│   └── context_memory.json  # Persistent context memory
├── winbrow/
│   ├── __init__.py
│   ├── agent.py             # Main orchestrator
│   ├── router.py            # Laya + fallback + LLM tier
│   ├── registry.py          # Tool registry (loads seed.json)
│   ├── windows.py           # Windows OS primitives
│   ├── stt.py               # Whisper.cpp STT
│   ├── llm.py               # Qwen3.5 LLM (llama.cpp)
│   ├── vision.py            # Qwen2.5-VL fallback
│   ├── stt.py               # Whisper.cpp STT
│   ├── accessibility.py     # UIAutomation tree
│   ├── memory.py            # Semantic + Context memory
│   ├── voice.py             # Voice pipeline
│   ├── model_manager.py     # Auto-download models
│   ├── vision.py            # Qwen2.5-VL vision
│   ├── accessibility.py     # UIAutomation
│   ├── small_llm.py         # Opt-in Ollama tier
│   ├── router.py            # Laya + fallback + LLM
│   ├── registry.py          # Tool registry
│   ├── windows.py           # Windows OS layer
│   ├── generator.py         # Dynamic tool generation
│   ├── policy.py            # Safety validation
│   ├── browser_task.py      # CDP browser control
│   ├── pshost.py            # Persistent PowerShell
│   ├── policy.py            # Safety validation
│   ├── cli.py               # Developer CLI
│   ├── trace.py             # JSONL tracing
│   ├── model_manager.py     # Model downloader
│   ├── files.py             # File/folder/app enumeration
│   ├── last_opened.py       # Context memory
│   ├── resolvers.py         # Argument resolution
│   ├── policy.py            # Safety validation
│   ├── browser_task.py      # CDP browser automation
│   ├── pshost.py            # Persistent PowerShell
│   ├── policy.py            # Safety validation
│   ├── cli.py               # CLI
│   ├── trace.py             # JSONL tracing
│   ├── model_manager.py     # Model downloader
│   ├── files.py             # File/app enumeration
│   ├── last_opened.py       # Context memory
│   ├── resolvers.py         # Argument resolution
│   ├── policy.py            # Safety
│   ├── browser_task.py      # Browser automation
│   ├── pshost.py            # PowerShell host
│   ├── generator.py         # Dynamic tools
│   ├── router.py            # Routing
│   ├── agent.py             # Orchestrator
│   ├── windows.py           # Windows APIs
│   └── __init__.py
├── templates/index.html      # Web UI
├── static/                   # Static assets
├── logs/trace.jsonl         # Execution traces
├── test_winbrow_all.py      # Test suite
├── test_agent.py            # API smoke test
├── requirements.txt
└── WORKING.md               # This file
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Laya timeout (5s) | Normal on CPU-only; fallback handles it. Reduce with `LAYA_TIMEOUT_S`. |
| Model download fails | Check internet, HuggingFace accessible. Delete partial file in `models/`. |
| Ollama not found | Install Ollama, `ollama pull qwen2.5:0.5b`, set `WINBROW_ROUTER_LLM=1`. |
| `comtypes` import error | `pip install comtypes` (Windows only). |
| `sounddevice` error | `pip install sounddevice` or use WASAPI fallback. |
| PowerShell execution policy | `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` |
| UIAutomation missing | `pip install comtypes` (Windows 10/11 built-in). |

---

## Future Enhancements (Roadmap)

- [ ] **System 2 Planner**: Multi-step task decomposition
- [ ] **Proactive Suggestions**: "You opened report.pdf yesterday, open it again?"
- [ ] **Cross-app Workflows**: "Copy text from Chrome → paste into Word"
- [ ] **GPU Acceleration**: Metal/CUDA for llama.cpp
- [ ] **Wake Word Model**: Custom Porcupine/Picovoice integration
- [ ] **Plugin System**: User-defined tools via Python/JSON
- [ ] **Telemetry Dashboard**: Local metrics + latency heatmap
- [ ] **Cross-platform**: macOS/Linux support (UIAutomation → AT-SPI/AppKit)

---

*Last updated: 2024 - WinBrow v2.0.0*