"""
WinBrow Web Server
==================
FastAPI backend for the WinBrow desktop and browser control agent.
Provides REST and WebSocket endpoints for command routing, execution trace,
live context updates, and learned tool management.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

# Set Windows proactor event loop policy early for subprocess support
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from winbrow.agent import WinBrowAgent
from winbrow.brain import uses_builtin_local_server
from winbrow.windows import run_powershell
from winbrow.model_manager import get_model_path, ensure_llama_cpp, ensure_all_models
from winbrow.llm import QwenLLM
from winbrow.integrations import (
    clear_integration,
    google_oauth,
    list_integrations,
    save_integration,
    test_integration,
)


def _load_env_file() -> None:
    """Tiny .env loader (no python-dotenv dependency). Ignored if absent."""
    import logging as _logging
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and (key not in os.environ or not os.environ[key]):
                os.environ[key] = value
    except Exception as e:
        _logging.getLogger("winbrow-server").warning(f".env load failed (non-fatal): {e}")

# ---------------------------------------------------------------------------
# Setup & Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("winbrow-server")

# Load OPENROUTER_API_KEY etc. from .env (if present) before anything reads env
_load_env_file()

app = FastAPI(title="WinBrow — Hands-free Windows & Browser Control", version="3.0.0")

# Global Agent Instance
agent = WinBrowAgent()

# Global LLM Instance
llm_instance: Optional[QwenLLM] = None

# M5: proactive suggestions observer (background refresh + on-demand reads)
from winbrow.observe import Observer, Suggestion  # noqa: E402
_observer: Optional[Observer] = None
_observe_task: Optional[asyncio.Task] = None
_last_refresh_ts: float = 0.0
_OBSERVE_MIN_INTERVAL_PRI_SEC = 30.0

# M6: MCP bridge connections (background connect + status)
from winbrow.mcp import get_mcp_hub, reset_mcp_hub  # noqa: E402
_mcp_task: Optional[asyncio.Task] = None
_mcp_conns: list = []


def get_observer() -> Observer:
    global _observer
    if _observer is None:
        _observer = Observer()
    return _observer


async def _observe_loop() -> None:
    """Background suggestion refresh — cheap (SQLite), off the request path."""
    interval = float(os.environ.get("WINBROW_OBSERVE_INTERVAL_S", "180"))
    while True:
        try:
            get_observer().refresh()
        except Exception as e:
            log.warning(f"Observe refresh failed (non-fatal): {e}")
        await asyncio.sleep(max(30.0, interval))

# Static Files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# In-memory settings state
runtime_settings = {
    "provider": "auto",
    "api_key": "",
    "endpoint": "http://localhost:11434/api/generate",
    "project_id": "",
}

# Active WebSocket connections (for broadcast)
_ws_clients: list[WebSocket] = []


@app.on_event("startup")
async def _startup() -> None:
    """Download models and warm executors on startup.

    The cloud-first v3 brain needs no local LLM: llama.cpp/Qwen only starts
    when WINBROW_LOCAL_LLM=1 (offline mode).
    """
    global llm_instance

    log.info("Starting WinBrow server...")

    # 1. Ensure STT models are downloaded (whisper). Skip LLM models in cloud mode.
    try:
        log.info("Checking/downloading required models...")
        await asyncio.get_event_loop().run_in_executor(None, ensure_all_models)
        log.info("Models ready")
    except Exception as e:
        log.warning(f"Model download failed (will retry on first use): {e}")

    # 2. Local Qwen brain (WINBROW_BRAIN_PROVIDER=local|qwen, or WINBROW_LOCAL_LLM=1).
    # Uses the built-in llama.cpp server unless WINBROW_LOCAL_LLM_ENDPOINT points
    # at an external OpenAI-compatible server (Ollama / LM Studio).
    if uses_builtin_local_server():
        try:
            log.info("Starting local Qwen brain (llama.cpp)...")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: ensure_llama_cpp(download=True))
            llm_instance = QwenLLM()
            await llm_instance.start()
            log.info(f"Local Qwen brain ready at {llm_instance.base_url}")
        except Exception as e:
            log.warning(f"Local LLM server failed to start ({e}); "
                        "brain will use the cloud fallback provider.")
            llm_instance = None

    if agent.gateway.available:
        prov = agent.gateway.providers[0]
        fallbacks = ", ".join(p.name for p in agent.gateway.providers[1:]) or "none"
        log.info(f"Brain online: {prov.name} / {prov.model} (fallbacks: {fallbacks})")
    else:
        log.warning("Brain offline: set OPENROUTER_API_KEY or WINBROW_BRAIN_PROVIDER=local "
                    "(generator tier + heuristics still work).")

    # 3. Warm up PowerShell
    try:
        await run_powershell("Write-Output warm", timeout=60)
    except Exception as e:
        log.warning(f"PowerShell warm-up failed (non-fatal): {e}")

    # 4. M5: observe service — proactive suggestions on a background loop
    global _observe_task
    try:
        get_observer().refresh()
        _observe_task = asyncio.create_task(_observe_loop())
        log.info("Observe service running "
                 f"({len(get_observer().current)} suggestion(s) seeded)")
    except Exception as e:
        log.warning(f"Observe service failed to start (non-fatal): {e}")

    # 5. M6: MCP bridge — best-effort background connect for all enabled
    # servers; tools register into the shared agent registry (fresh
    # catalogs served from cache if a server is offline).
    global _mcp_task, _mcp_conns
    if not get_mcp_hub().disabled:
        _mcp_task = asyncio.create_task(_mcp_connect_loop())


async def _mcp_connect_loop() -> None:
    """Connect ONLY preload servers at boot; the rest spawn lazily on use."""
    global _mcp_conns
    try:
        hub = get_mcp_hub()
        preload = [s for s in hub.specs() if s.preload and s.enabled]
        _mcp_conns = await hub.connect_with_registry(
            agent.registry, specs=preload)
        ok = sum(1 for c in _mcp_conns if not c.error)
        log.info(f"MCP bridge: {ok}/{len(_mcp_conns)} preload server(s) connected "
                 f"({sum(len(c.tools) for c in _mcp_conns if not c.error)} tool(s))")
    except Exception as e:
        log.warning(f"MCP bridge connect failed (non-fatal): {e}")


# ids of preload servers given a second connect attempt by /api/mcp; kept on
# failure so a genuinely dead server is not re-spawned on every poll
_mcp_kick: set = set()


async def _mcp_kick_connect(server_id: str) -> None:
    """One extra background connect for a preload server + tool re-register."""
    ok = False
    try:
        from winbrow.mcp import get_mcp_hub, _register_hub_tools
        hub = get_mcp_hub()
        spec = next((s for s in hub.specs() if s.id == server_id), None)
        if spec is not None:
            conn = await hub.connect(spec)
            _register_hub_tools(hub, agent.registry)
            ok = not conn.error
            log.info(f"MCP kick: '{server_id}' "
                     f"{'connected' if ok else 'failed: ' + conn.error}")
    except Exception as e:
        log.debug(f"MCP kick connect failed for '{server_id}': {e}")
    finally:
        if ok:
            _mcp_kick.discard(server_id)


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Terminate MCP children, close sockets, bound every stop to seconds."""
    global llm_instance, _observe_task, _mcp_task, _mcp_conns
    log.info("Shutting down...")
    if _observe_task:
        _observe_task.cancel()
        _observe_task = None
    if _mcp_task:
        _mcp_task.cancel()
        try:
            await _mcp_task
        except (asyncio.CancelledError, Exception):
            pass
        _mcp_task = None
    _mcp_conns = []
    try:
        reset_mcp_hub()          # terminates all MCP subprocesses
    except Exception:
        pass
    for ws in list(_ws_clients):   # unblock the graceful window immediately
        try:
            await ws.close()
        except Exception:
            pass
    _ws_clients.clear()
    if llm_instance:
        log.info("Stopping LLM server...")
        try:
            await asyncio.wait_for(llm_instance.stop(), timeout=3.0)
        except Exception:
            pass
        llm_instance = None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ExecuteRequest(BaseModel):
    command: str = ""
    provider: Optional[str] = None
    api_key: Optional[str] = None
    endpoint: Optional[str] = None
    project_id: Optional[str] = None
    session_id: Optional[str] = None


class SettingsRequest(BaseModel):
    provider: str = "auto"
    api_key: str = ""
    endpoint: str = ""
    project_id: str = ""


class IntegrationSaveRequest(BaseModel):
    values: dict[str, str] = {}


class BrowserRequest(BaseModel):
    action: str
    url: Optional[str] = None
    query: Optional[str] = None
    engine: Optional[str] = "google"
    selector: Optional[str] = None
    text: Optional[str] = None
    direction: Optional[str] = "next"
    num: Optional[int] = 1
    new_tab: bool = True


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "templates" / "index.html"
    # no-store: the UI ships updates with every server restart; browsers must
    # never keep serving an older mid-edit page from cache
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.post("/api/execute")
async def execute_command(req: ExecuteRequest):
    """Execute a desktop or browser task through the routing engine + brain."""
    cmd = req.command.strip()
    if not cmd:
        return JSONResponse({"error": "Empty command"}, status_code=400)

    provider = req.provider or runtime_settings["provider"]
    api_key = req.api_key or runtime_settings["api_key"]
    endpoint = req.endpoint or runtime_settings["endpoint"]
    project_id = req.project_id or runtime_settings["project_id"]

    async def _emit(event: dict) -> None:
        """Stream brain plan/step events to WebSocket clients."""
        await _broadcast(event)

    result = await agent.execute(
        cmd,
        api_key=api_key if api_key else None,
        provider=provider,
        custom_endpoint=endpoint if endpoint else None,
        project_id=project_id if project_id else None,
        emit=_emit,
        session_id=req.session_id or "default",
    )

    # NOTE: We intentionally do NOT broadcast the result over WebSocket here.
    # The HTTP response already returns the result to the caller (the UI's
    # run() function), and the UI's ws.onmessage handler also calls
    # renderCard() for "result" messages — causing every task to appear
    # twice in the trace.  WebSocket-initiated executions (action=execute)
    # send their own result at line ~778, so those are covered.

    return JSONResponse(result)


@app.get("/api/context")
async def get_context():
    """Get live Windows context (active window, running apps, system state)."""
    loop = asyncio.get_event_loop()
    ctx = await loop.run_in_executor(None, agent.get_context)
    return JSONResponse(ctx.to_dict())


@app.get("/api/system")
async def system_info():
    """System info: context + server version + Laya router + brain + MCP status."""
    loop = asyncio.get_event_loop()
    ctx = await loop.run_in_executor(None, agent.get_context)
    try:
        mcp = {"servers": get_mcp_hub().status(),
               "enabled": not get_mcp_hub().disabled}
    except Exception:
        mcp = {"servers": [], "enabled": False}
    return JSONResponse({
        "context": ctx.to_dict(),
        "version": "3.0.0",
        "laya_loaded": agent.router._loaded,
        "tool_count": len(agent.registry.all_tools()),
        "history_count": len(agent.history),
        "brain": agent.gateway.status(),
        "session_summary": agent.session.summary(),
        "mcp": mcp,
    })


@app.get("/api/tools")
async def get_tools():
    """Get all registered tools including dynamically learned ones."""
    from winbrow.permissions import tool_tier
    tools_list = []
    for t in agent.registry.all_tools():
        tools_list.append({
            "name": t.name,
            "description": t.description,
            "is_learned": t.is_learned,
            "impl": getattr(t, "impl", None),
            "scope": t.scope,
            "examples": t.examples,
            "tier": tool_tier(t.name),
            "args": [
                {
                    "name": a.name,
                    "kind": a.kind,
                    "instructions": a.instructions,
                    "default": a.default,
                    "criteria": a.criteria,
                }
                for a in t.args
            ],
            "script": t.script.strip(),
        })
    return JSONResponse({"tools": tools_list, "count": len(tools_list)})


@app.get("/api/mcp")
async def mcp_detail():
    """MCP bridge detail for the Tools page: runtime status + full tool
    catalog per server (live connection preferred, catalog cache fallback)."""
    from winbrow.mcp import get_mcp_hub, _load_cache
    from winbrow.permissions import tool_tier
    try:
        hub = get_mcp_hub()
        # a boot-time preload connect can miss its window (cold npx/python
        # imports); nudge it once in the background so "live" converges
        # instead of showing 0/N forever
        for spec in hub.specs():
            if (spec.preload and spec.enabled
                    and spec.id not in hub.connections
                    and spec.id not in _mcp_kick):
                _mcp_kick.add(spec.id)
                asyncio.create_task(_mcp_kick_connect(spec.id))
        st = {s["id"]: s for s in hub.status()}
        cache = _load_cache(hub.cache_path)
        servers = []
        for spec in hub.specs():
            meta = st.get(spec.id) or {}
            conn = hub.connections.get(spec.id)
            catalog = (conn.tools if conn and conn.tools else []) \
                      or (cache.get(spec.id) or [])
            tools = [{
                "name": t.get("name", ""),
                "full_name": f"mcp_{spec.id}_{t.get('name', '')}",
                "description": t.get("description", ""),
                "tier": tool_tier(f"mcp_{spec.id}_{t.get('name', '')}"),
            } for t in catalog]
            servers.append({
                "id": spec.id,
                "transport": meta.get("transport", "http" if spec.url else "stdio"),
                "enabled": spec.enabled,
                "preload": spec.preload,
                "tier": spec.tier or "",
                "available": meta.get("available", False),
                "error": meta.get("error", ""),
                "source": " ".join(spec.command) if spec.command else (spec.url or ""),
                "description": spec.description,
                "tools": tools,
                "cached": bool(spec.id in cache) and not catalog,
            })
        return JSONResponse({"enabled": not hub.disabled, "servers": servers})
    except Exception as e:
        return JSONResponse({"enabled": False, "servers": [],
                             "error": str(e)}, status_code=200)


@app.get("/api/history")
async def get_history():
    """Get recent command execution history."""
    return JSONResponse(agent.history[-50:])


# ---------------------------------------------------------------------------
# Session & memory endpoints (v3)
# ---------------------------------------------------------------------------

@app.get("/api/session")
async def get_session_state():
    """Current conversation: turns + resolved entities."""
    s = agent.session
    return JSONResponse({
        "session_id": s.session_id,
        "summary": s.summary(),
        "turns": [t.to_dict() for t in s.turns],
        "entities": {k: [e.to_dict() for e in v] for k, v in s.entities.items()},
    })


class SessionRequest(BaseModel):
    session_id: Optional[str] = None


@app.post("/api/session/reset")
async def reset_session(req: SessionRequest):
    """Clear working memory (start a fresh conversation)."""
    if req.session_id and req.session_id != "default":
        from winbrow.memory import get_session
        get_session(req.session_id).reset()
    else:
        agent.session.reset()
    return JSONResponse({"status": "reset"})


class SkillsRequest(BaseModel):
    skill_id: Optional[str] = None


@app.get("/api/skills")
async def list_skills():
    """Procedural memory: captured multi-step skills (replayable)."""
    return JSONResponse({"skills": agent.skills.list()})


@app.post("/api/skills/delete")
async def delete_skill(req: SkillsRequest):
    """Remove a captured skill (user-editable procedural memory)."""
    if not req.skill_id:
        return JSONResponse({"error": "skill_id required"}, status_code=400)
    ok = agent.skills.delete(req.skill_id)
    return JSONResponse({"deleted": ok, "remaining": len(agent.skills)})


@app.post("/api/skills/approve")
async def approve_skill(req: SkillsRequest):
    """Enable a reviewed skill proposal for replay."""
    if not req.skill_id:
        return JSONResponse({"error": "skill_id required"}, status_code=400)
    ok = agent.skills.approve(req.skill_id)
    return JSONResponse({"approved": ok, "remaining": len(agent.skills)})


# ---------------------------------------------------------------------------
# Permissions & installer endpoints (v3/M3)
# ---------------------------------------------------------------------------

class ConfirmRequest(BaseModel):
    confirmation_id: str
    approve: bool = False
    remember: bool = False   # force-remember T2 choice (T3 always re-asks)


@app.get("/api/permissions")
async def get_permissions_state():
    """Pending confirmations + remembered T2 allowlist."""
    from winbrow.permissions import get_permissions
    pm = get_permissions()
    return JSONResponse({
        "pending": pm.pending_list(),
        "allowed": pm.list_allowed(),
    })


@app.post("/api/confirm")
async def confirm_pending(req: ConfirmRequest):
    """Approve/deny a pending T2/T3 action; approval re-runs the cached utterance."""
    from winbrow.permissions import get_permissions
    pm = get_permissions()
    p = pm.get_pending(req.confirmation_id)
    if not p:
        return JSONResponse({"error": "Unknown confirmation_id"}, status_code=404)
    # T2 choices are remembered by default (plan §6); T3 never.
    remember = req.remember or (p.tier == "t2")
    pm.resolve_pending(p.id, approve=req.approve, remember=remember)
    if not req.approve:
        pm.consume(p.tool, p.args)
        return JSONResponse({
            "status": "denied", "confirmation_id": p.id,
            "tool": p.tool, "result": {
                "success": False, "type": "denied",
                "output": "Action denied by user.",
            },
        })
    # Re-run the cached utterance in the same session (gate sees 'approved').
    result = await agent.execute(p.utterance, session_id=p.session_id or "default")
    pm.consume(p.tool, p.args)
    return JSONResponse({
        "status": "approved", "confirmation_id": p.id,
        "tool": p.tool, "result": result,
    })


@app.get("/api/install/status")
async def install_status(job_id: Optional[str] = None):
    """Streaming install job progress (winget output tail)."""
    from winbrow.installer import installer_status
    return JSONResponse(installer_status(job_id))


@app.get("/api/memories")
async def get_memories(q: Optional[str] = None, limit: int = 20):
    """Search episodic memory ('what did I do yesterday?') or list recent."""
    if q:
        hits = agent.store.search_episodic(q, limit=min(limit, 50))
    else:
        hits = agent.store.get_recent(limit=min(limit, 50))
    return JSONResponse({
        "query": q,
        "results": [
            {
                "kind": h.kind,
                "label": h.label,
                "path": h.path,
                "app": h.app,
                "ts": h.timestamp,
                "metadata": h.metadata,
            }
            for h in hits
        ],
    })


class MemoryRequest(BaseModel):
    fact: str


@app.post("/api/memories/fact")
async def add_fact(req: MemoryRequest):
    """Manually teach the agent a fact ('remember my exports folder is X')."""
    fact = req.fact.strip()
    if not fact:
        return JSONResponse({"error": "Empty fact"}, status_code=400)
    agent.store.add_fact(fact)
    return JSONResponse({"status": "saved", "fact": fact})


# ---------------------------------------------------------------------------
# M5: observe service — proactive suggestions
# ---------------------------------------------------------------------------

class DismissRequest(BaseModel):
    id: str


@app.get("/api/suggestions")
async def get_suggestions(refresh: Optional[str] = None):
    """Current proactive suggestions. Refreshed in background every ~3min;
    ?refresh=1 forces a rebuild (rate-limited to 1 / 30s on 2 cores)."""
    obs = get_observer()
    global _last_refresh_ts
    if refresh and (time.time() - _last_refresh_ts) >= _OBSERVE_MIN_INTERVAL_PRI_SEC:
        try:
            await asyncio.get_event_loop().run_in_executor(None, obs.refresh)
            _last_refresh_ts = time.time()
        except Exception as e:
            log.warning(f"Suggestion refresh failed: {e}")
    return JSONResponse({
        "suggestions": [s.to_dict() for s in obs.current],
    })


@app.post("/api/suggestions/dismiss")
async def dismiss_suggestion(req: DismissRequest):
    """Swat away a suggestion — it stays dismissed for 12h (persisted)."""
    obs = get_observer()
    before = [s for s in obs.current if s.id == req.id]
    obs.dismissals.dismiss(req.id)
    if before:
        obs.current = [s for s in obs.current if s.id != req.id]
    return JSONResponse({"status": "dismissed", "id": req.id})


@app.post("/api/settings")
async def save_settings(req: SettingsRequest):
    """Update runtime LLM settings. provider='openrouter' + api_key wires the brain."""
    runtime_settings["provider"] = req.provider
    runtime_settings["api_key"] = req.api_key
    runtime_settings["endpoint"] = req.endpoint
    runtime_settings["project_id"] = req.project_id
    if req.provider == "openrouter" and req.api_key:
        os.environ["OPENROUTER_API_KEY"] = req.api_key
        from winbrow.brain import reset_gateway
        reset_gateway()
        agent.rebuild_gateway()
    return JSONResponse({"status": "saved", "settings": {
        "provider": req.provider,
        "has_api_key": bool(req.api_key),
        "endpoint": req.endpoint,
        "project_id": req.project_id,
    }})


@app.get("/api/settings")
async def get_settings():
    return JSONResponse({
        "provider": runtime_settings["provider"],
        "has_api_key": bool(runtime_settings["api_key"]),
        "endpoint": runtime_settings["endpoint"],
        "project_id": runtime_settings["project_id"],
    })


# ---------------------------------------------------------------------------
# Integrations (optional skills: Gemini, Google, Postgres, Outline, n8n, ...)
# ---------------------------------------------------------------------------

@app.get("/api/integrations")
async def get_integrations():
    """Return masked provider status plus optional Python dependency status."""
    return JSONResponse(list_integrations())


@app.post("/api/integrations/{provider}")
async def update_integration(provider: str, req: IntegrationSaveRequest):
    try:
        status = save_integration(provider, req.values)
    except KeyError:
        return JSONResponse({"error": f"Unknown integration '{provider}'"}, status_code=404)
    return JSONResponse({"status": "saved", "provider": status})


@app.post("/api/integrations/{provider}/test")
async def test_integration_endpoint(provider: str):
    try:
        result = await asyncio.to_thread(test_integration, provider)
    except KeyError:
        return JSONResponse({"error": f"Unknown integration '{provider}'"}, status_code=404)
    return JSONResponse(result)


@app.delete("/api/integrations/{provider}")
async def delete_integration(provider: str):
    try:
        removed = clear_integration(provider)
    except KeyError:
        return JSONResponse({"error": f"Unknown integration '{provider}'"}, status_code=404)
    return JSONResponse({"deleted": removed})


@app.post("/api/integrations/google/oauth")
async def google_oauth_endpoint():
    """Run the installed-app OAuth flow. Local desktop only; blocks until done."""
    result = await asyncio.to_thread(google_oauth)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


# ---------------------------------------------------------------------------
# Browser-specific REST endpoints
# ---------------------------------------------------------------------------

@app.post("/api/browser")
async def browser_action(req: BrowserRequest):
    """Direct browser control endpoint."""
    b = agent.browser
    action = req.action.lower()

    if action == "tabs":
        result = await b.list_tabs()
    elif action == "navigate":
        result = await b.navigate_to(req.url or "", new_tab=req.new_tab)
    elif action == "new_tab":
        result = await b.new_tab()
    elif action == "close_tab":
        result = await b.close_current_tab()
    elif action == "search":
        result = await b.search_web(req.query or "", engine=req.engine or "google")
    elif action == "reload":
        result = await b.reload_page()
    elif action == "back":
        result = await b.go_back()
    elif action == "forward":
        result = await b.go_forward()
    elif action == "screenshot":
        result = await b.screenshot()
    elif action == "url":
        result = await b.get_current_url()
    elif action == "scroll":
        result = await b.scroll_page(req.direction or "down")
    elif action == "execute_js":
        result = await b.execute_js(req.text or "document.title")
    elif action == "read_page":
        result = await b.read_page_text()
    elif action == "launch_cdp":
        result = await b.launch_with_cdp(req.url or "")
    elif action == "switch_tab":
        result = await b.switch_tab(req.direction or "next")
    elif action == "incognito":
        result = await b.open_incognito(req.url or "")
    elif action == "find":
        result = await b.find_on_page(req.query or "")
    elif action == "click":
        result = await b.click_element(req.selector or "body")
    elif action == "type":
        result = await b.type_in_element(req.selector or "input", req.text or "")
    elif action == "bookmark":
        result = await b.bookmark_page()
    elif action == "zoom_in":
        result = await b.zoom("in")
    elif action == "zoom_out":
        result = await b.zoom("out")
    elif action == "zoom_reset":
        result = await b.zoom("reset")
    elif action in ("ultrafast", "laya_ultrafast"):
        goal = req.query or req.text or "search page"
        result = await b.run_ultrafast_task(goal)
    else:
        return JSONResponse({"error": f"Unknown browser action: {action}"}, status_code=400)

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# WebSocket for Live Context & Feedback
# ---------------------------------------------------------------------------

async def _broadcast(msg: dict) -> None:
    """Broadcast a message to all connected WebSocket clients."""
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    log.info(f"WebSocket client connected (total: {len(_ws_clients)})")
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            action = msg.get("action")

            if action == "context":
                loop = asyncio.get_event_loop()
                ctx = await loop.run_in_executor(None, agent.get_context)
                await ws.send_json({"type": "context", "data": ctx.to_dict()})

            elif action == "execute":
                cmd = msg.get("command", "").strip()
                if cmd:
                    await ws.send_json({"type": "routing", "command": cmd})
                    res = await agent.execute(
                        cmd,
                        api_key=runtime_settings.get("api_key") or None,
                        provider=runtime_settings.get("provider", "auto"),
                        custom_endpoint=runtime_settings.get("endpoint") or None,
                        project_id=runtime_settings.get("project_id") or None,
                        session_id=str(msg.get("session_id") or "default"),
                        emit=lambda ev: ws.send_json(ev),
                    )
                    await ws.send_json({"type": "result", "data": res})

            elif action == "system":
                loop = asyncio.get_event_loop()
                ctx = await loop.run_in_executor(None, agent.get_context)
                await ws.send_json({
                    "type": "system",
                    "data": {
                        "context": ctx.to_dict(),
                        "tool_count": len(agent.registry.all_tools()),
                        "laya_loaded": agent.router._loaded,
                    }
                })

    except WebSocketDisconnect:
        log.info("WebSocket disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}")
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    # Ctrl+C must always terminate even with WebSocket clients open: bound
    # the graceful window (voice/live-context sockets otherwise keep the
    # shutdown waiting on reconnecting browser tabs), then hard-exit for any
    # stray non-daemon threads (MCP subprocess watchdogs, model workers).
    uvicorn.run("server:app", host="127.0.0.1", port=port, reload=False,
                timeout_graceful_shutdown=3)
    os._exit(0)
