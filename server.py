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
from winbrow.windows import run_powershell
from winbrow.model_manager import get_model_path, ensure_llama_cpp, ensure_all_models
from winbrow.llm import QwenLLM

# ---------------------------------------------------------------------------
# Setup & Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("winbrow-server")

app = FastAPI(title="WinBrow — Hands-free Windows & Browser Control", version="2.0.0")

# Global Agent Instance
agent = WinBrowAgent()

# Global LLM Instance
llm_instance: Optional[QwenLLM] = None

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
    """Download models and start LLM server on startup."""
    global llm_instance
    
    log.info("Starting WinBrow server...")
    
    # 1. Ensure models are downloaded
    try:
        log.info("Checking/downloading required models...")
        await asyncio.get_event_loop().run_in_executor(None, ensure_all_models)
        log.info("Models ready")
    except Exception as e:
        log.warning(f"Model download failed (will retry on first use): {e}")
    
    # 2. Start llama.cpp server with Qwen model
    try:
        log.info("Starting local LLM server...")
        llm_instance = QwenLLM()
        await llm_instance.start()
        log.info(f"LLM server ready at {llm_instance.base_url}")
    except Exception as e:
        log.warning(f"LLM server failed to start (will retry on first use): {e}")
        llm_instance = None
    
    # 3. Warm up PowerShell
    try:
        await run_powershell("Write-Output warm", timeout=60)
    except Exception as e:
        log.warning(f"PowerShell warm-up failed (non-fatal): {e}")


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Stop LLM server on shutdown."""
    global llm_instance
    if llm_instance:
        log.info("Stopping LLM server...")
        await llm_instance.stop()
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


class SettingsRequest(BaseModel):
    provider: str = "auto"
    api_key: str = ""
    endpoint: str = ""
    project_id: str = ""


class BrowserRequest(BaseModel):
    action: str
    url: Optional[str] = None
    query: Optional[str] = None
    engine: Optional[str] = "google"
    selector: Optional[str] = None
    text: Optional[str] = None
    direction: Optional[str] = "next"
    num: Optional[int] = 1


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.post("/api/execute")
async def execute_command(req: ExecuteRequest):
    """Execute a desktop or browser task through the Laya routing engine."""
    cmd = req.command.strip()
    if not cmd:
        return JSONResponse({"error": "Empty command"}, status_code=400)

    provider = req.provider or runtime_settings["provider"]
    api_key = req.api_key or runtime_settings["api_key"]
    endpoint = req.endpoint or runtime_settings["endpoint"]
    project_id = req.project_id or runtime_settings["project_id"]

    result = await agent.execute(
        cmd,
        api_key=api_key if api_key else None,
        provider=provider,
        custom_endpoint=endpoint if endpoint else None,
        project_id=project_id if project_id else None,
    )

    # Broadcast to all WebSocket clients
    await _broadcast({"type": "execution", "data": result})

    return JSONResponse(result)


@app.get("/api/context")
async def get_context():
    """Get live Windows context (active window, running apps, system state)."""
    loop = asyncio.get_event_loop()
    ctx = await loop.run_in_executor(None, agent.get_context)
    return JSONResponse(ctx.to_dict())


@app.get("/api/system")
async def system_info():
    """System info: context + server version + Laya router status."""
    loop = asyncio.get_event_loop()
    ctx = await loop.run_in_executor(None, agent.get_context)
    return JSONResponse({
        "context": ctx.to_dict(),
        "version": "2.0.0",
        "laya_loaded": agent.router._loaded,
        "tool_count": len(agent.registry.all_tools()),
        "history_count": len(agent.history),
    })


@app.get("/api/tools")
async def get_tools():
    """Get all registered tools including dynamically learned ones."""
    tools_list = []
    for t in agent.registry.all_tools():
        tools_list.append({
            "name": t.name,
            "description": t.description,
            "is_learned": t.is_learned,
            "scope": t.scope,
            "examples": t.examples,
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


@app.get("/api/history")
async def get_history():
    """Get recent command execution history."""
    return JSONResponse(agent.history[-50:])


@app.post("/api/settings")
async def save_settings(req: SettingsRequest):
    """Update runtime LLM settings."""
    runtime_settings["provider"] = req.provider
    runtime_settings["api_key"] = req.api_key
    runtime_settings["endpoint"] = req.endpoint
    runtime_settings["project_id"] = req.project_id
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
        result = await b.navigate_to(req.url or "", new_tab=False)
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
    uvicorn.run("server:app", host="127.0.0.1", port=port, reload=False)
