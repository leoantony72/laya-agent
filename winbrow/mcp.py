"""MCP capability bridge ("Capability Multiplexer")
====================================================
Connects WinBrow to open Model-Context-Protocol servers and turns every
MCP tool into a first-class registry tool (routing / permission gate /
brain planning / skill replay all treat it like a native tool).

Transports (stdlib only — no MCP SDK dependency):
  • stdio            — line-delimited JSON-RPC 2.0 over a spawned
                       subprocess, started lazily at first use.
  • streamable HTTP  — POST initialize/tools-list/tools-call per the
                       2025-03-26+ spec (SSE responses parsed too).

Servers are configured by the user in tools/mcp_servers.json:
    {"id": "wikipedia",
     "command": ["wikipedia-mcp", "--transport", "stdio"],
     "tier": "t1"}
    {"id": "example-remote", "url": "https://host/mcp", "enabled": false}

Safety model:
  • registry names are namespaced mcp_{server}_{tool} and store the
    mcp://server/tool routing in the script field (impl-executed, so the
    field never becomes a PowerShell template);
  • tiers go through the normal permission classifier (write/system verbs
    → T2/T3 — a downgrade is impossible through MCP names) plus an
    explicit read-only "t1" override per server in config;
  • every call passes the single permission gate in agent.run_skill;
  • output clamp (32 KB), per-call deadline, args size cap, and env
    passthrough restricted to keys explicitly listed in the config.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("winbrow.mcp")

MCP_IMPL = "mcp"

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
CONFIG_PATH = TOOLS_DIR / "mcp_servers.json"
CACHE_PATH = TOOLS_DIR / "mcp_cache.json"

PROTOCOL_VERSION = "2025-06-18"          # announced; the server's own choice wins
DEFAULT_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 25.0
MAX_OUTPUT_CHARS = 32_000
MAX_ARGS_JSON = 200_000                  # serialized args cap
MAX_ARG_CHARS = 100_000                  # single arg cap (file contents)
MAX_TOOLS_PER_SERVER = 64                # catalog cap

# Cold-start flake healers: freshly-spawned stdio servers often fail their
# first network calls (slow/empty) until the process has warmed its sockets.
# After connect, fire invisible background probes for these servers so the
# first *user* call already hits a warm server.
_WARM_SERVERS = {
    "wikipedia": ("search_wikipedia", {"query": "wikipedia"}, 3),
}
_warm_lock = threading.Lock()
_warmed_ids: set = set()


def _warm_server(hub: "McpHub", spec: "McpServerSpec") -> None:
    cfg = _WARM_SERVERS.get(spec.id)
    if not cfg:
        return
    with _warm_lock:
        if spec.id in _warmed_ids:
            return
        _warmed_ids.add(spec.id)
    tool, args, tries = cfg
    for _ in range(int(tries)):
        try:
            res = asyncio.run(hub.call(spec.id, tool, dict(args)))
            if '"status": "success"' in str(res.get("output", "")).replace(
                    '"status":"success"', '"status": "success"'):
                return          # warm — stop probing
        except Exception as e:
            log.debug(f"mcp warmup {spec.id}: {e}")
        time.sleep(0.5)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (text or "").lower()).strip("_")


def _load_config(path: Path) -> List[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [s for s in (data.get("servers") or [])
                if isinstance(s, dict) and s.get("id")]
    except Exception as e:
        log.warning(f"mcp config unreadable: {e}")
        return []


def _load_cache(path: Optional[Path] = None) -> dict:
    p = Path(path or CACHE_PATH)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.debug(f"mcp cache unreadable: {e}")
    return {}


def _save_cache(cache: dict, path: Optional[Path] = None) -> None:
    p = Path(path or CACHE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- raw RPC helpers
class JsonRpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class _StdioTransport:
    """Line-delimited JSON-RPC 2.0 over a spawned server subprocess.

    On Windows every child is bound to a kill-on-close Job Object: if the
    parent dies (crash, Ctrl+C, rough kill) the OS reaps the MCP children
    too — no orphans, no inherited listening-socket ghosts."""
    _JOB_KILL_ON_CLOSE = 0x00002000      # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    def __init__(self, server_id: str, command: List[str], env: Optional[dict] = None):
        self.server_id = server_id
        self.command = command
        self.env = env or {}
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._job = None                 # Windows job handle (kill-on-close)

    def _env(self) -> dict:
        full = os.environ.copy()
        for k, v in self.env.items():        # explicit-only passthrough
            full[k] = str(v)
        return full

    def _bind_job(self) -> None:
        """Assign the freshly spawned child to a kill-on-close job."""
        if os.name != "nt" or not self.proc:
            return
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create = k32.CreateJobObjectW
            create.restype = ctypes.c_void_p
            create.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            job = create(None, None)
            if not job:
                return
            limit = k32.SetInformationJobObject
            class _Blimp(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]
            info = _Blimp()
            info.LimitFlags = self._JOB_KILL_ON_CLOSE
            limit.restype = ctypes.c_int
            limit.argtypes = [ctypes.c_void_p, ctypes.c_int,
                              ctypes.c_void_p, ctypes.c_uint32]
            if not limit(job, 1, ctypes.byref(info), ctypes.sizeof(info)):
                return
            assign = k32.AssignProcessToJobObject
            assign.restype = ctypes.c_int
            assign.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            if assign(job, int(self.proc._handle)):
                self._job = job
        except Exception as e:
            log.debug(f"job-object bind failed '{self.server_id}': {e}")

    def start(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            cmd = list(self.command)
            if cmd and cmd[0] in ("python", "python3", "py"):
                import sys
                cmd[0] = sys.executable
            root_dir = TOOLS_DIR.parent
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                bufsize=1, env=self._env(), cwd=str(root_dir), creationflags=flags,
            )
            self._bind_job()

    def _send(self, method: str, params: Optional[dict], notify: bool = False) -> Any:
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError(f"MCP server '{self.server_id}' is not running")
        if notify:
            msg = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            except Exception:
                pass
            return None
        self._next_id += 1
        rid = self._next_id
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": rid}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        # read until OUR id arrives; skip notifications like logging/progress
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"MCP server '{self.server_id}' closed the stream")
            line = line.strip()
            if not line:
                continue
            try:
                out = json.loads(line)
            except json.JSONDecodeError:
                continue
            if out.get("id") == rid:
                if "error" in out:
                    err = out["error"]
                    raise JsonRpcError(int(err.get("code", -1)),
                                       str(err.get("message", "MCP error")))
                return out.get("result", {})

    def request(self, method: str, params: Optional[dict], timeout: float) -> Any:
        """Blocking call executed on a helper thread (watchdog-bounded)."""
        holder: Dict[str, Any] = {}
        started = threading.Event()

        def _run():
            started.set()
            try:
                holder["result"] = self._send(method, params)
            except Exception as e:
                holder["error"] = e

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        if not started.wait(2.0):
            raise RuntimeError(f"MCP worker thread failed to start '{self.server_id}'")
        th.join(max(0.5, timeout))
        if th.is_alive():
            self.stop()      # dead server must not wedge future calls
            raise TimeoutError(f"MCP call '{method}' timed out after {timeout}s")
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")

    def stop(self) -> None:
        with self._lock:
            if self.proc is not None:
                try:
                    if self.proc.stdin:
                        self.proc.stdin.close()
                except Exception:
                    pass
                try:
                    self.proc.terminate()
                except Exception:
                    pass
                self.proc = None
            if self._job is not None:      # close ends the job → children die
                try:
                    import ctypes
                    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                        ctypes.c_void_p(self._job))
                except Exception:
                    pass
                self._job = None


class _HttpTransport:
    """Streamable-HTTP MCP transport (JSON or SSE responses)."""

    def __init__(self, server_id: str, url: str, headers: Optional[dict] = None):
        self.server_id = server_id
        self.url = url
        self.headers = {"Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"}
        if os.environ.get("WINBROW_HTTP_UA"):
            self.headers["User-Agent"] = os.environ["WINBROW_HTTP_UA"]
        self.headers.update(headers or {})
        self.session: Optional[str] = None
        self._next_id = 0


def _http_request(transport: _HttpTransport, method: str, params: Optional[dict],
                  timeout: float) -> Any:
    """Blocking JSON-RPC POST with Mcp-Session-Id bookkeeping."""
    import urllib.request
    transport._next_id += 1
    headers = dict(transport.headers)
    if transport.session:
        headers["Mcp-Session-Id"] = transport.session
    payload = {"jsonrpc": "2.0", "method": method, "id": transport._next_id}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        transport.url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        sid = resp.headers.get("Mcp-Session-Id")
        body = resp.read().decode("utf-8", "replace")
    if sid:
        transport.session = sid
    out = (json.loads(body) if body.lstrip().startswith("{")
           else _parse_sse(body, want_id=transport._next_id))
    if not isinstance(out, dict):
        raise RuntimeError(f"MCP HTTP server '{transport.server_id}' returned no JSON")
    if "error" in out:
        err = out["error"]
        raise JsonRpcError(int(err.get("code", -1)), str(err.get("message", "MCP error")))
    return out.get("result", {})


def _parse_sse(body: str, want_id: Optional[int] = None) -> Any:
    """Stream → the id-bearing JSON-RPC response (notifications skipped).

    Real servers interleave progress/log notifications BEFORE the answer;
    those carry no `id` and must not shadow the result we asked for."""
    prefer: Any = None
    fallback: Any = None
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk.startswith("{"):
            continue
        try:
            msg = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if "id" not in msg:
            continue                      # notification/log line → skip
        if want_id is not None and msg.get("id") == want_id:
            return msg
        if fallback is None:
            fallback = msg
    return fallback if fallback is not None else prefer


def _rpc(transport: Any, method: str, params: Optional[dict], timeout: float) -> Any:
    """Uniform JSON-RPC request across transports."""
    if isinstance(transport, _HttpTransport):
        return _http_request(transport, method, params, timeout)
    return transport.request(method, params, timeout)


# --------------------------------------------------------------------------- hub
@dataclass
class McpServerSpec:
    id: str
    command: Optional[List[str]] = None
    url: Optional[str] = None
    env: dict = field(default_factory=dict)
    tier: Optional[str] = None          # explicit verified read-only "t1" override
    enabled: bool = True
    timeout_s: float = DEFAULT_TIMEOUT_S
    description: str = ""
    preload: bool = False               # connect at server startup (RAM discipline)


@dataclass
class McpConnection:
    spec: McpServerSpec
    transport: Any                       # _StdioTransport | _HttpTransport
    tools: List[dict] = field(default_factory=list)
    connected_at: float = 0.0
    error: str = ""


class McpHub:
    """Owns all MCP server connections; registers tools into a ToolRegistry."""

    def __init__(self, config_path: Path = CONFIG_PATH,
                 cache_path: Path = CACHE_PATH):
        self.config_path = Path(config_path)
        self.cache_path = Path(cache_path)
        self.connections: Dict[str, McpConnection] = {}
        self.failures: Dict[str, str] = {}           # id → last error

    @property
    def disabled(self) -> bool:
        return os.environ.get("WINBROW_MCP_DISABLE", "") not in ("", "0")

    # ---------------------------------------------------------------- config
    def specs(self, config_path: Optional[Path] = None) -> List[McpServerSpec]:
        out: List[McpServerSpec] = []
        for raw in _load_config(config_path or self.config_path):
            tier = raw.get("tier")
            if tier not in ("t0", "t1"):
                tier = None        # config may only *lower* to t0/t1, never t2+
            out.append(McpServerSpec(
                id=_slug(str(raw["id"])),
                command=[str(c) for c in raw["command"]] if raw.get("command") else None,
                url=str(raw["url"]) if raw.get("url") else None,
                env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
                tier=tier, enabled=bool(raw.get("enabled", True)),
                timeout_s=float(raw.get("timeout_s", DEFAULT_TIMEOUT_S)),
                description=str(raw.get("description", "")),
                preload=bool(raw.get("preload", False)),
            ))
        return out

    def status(self) -> List[dict]:
        cache = _load_cache(self.cache_path)
        out = []
        for spec in self.specs():
            conn = self.connections.get(spec.id)
            err = conn.error if conn else self.failures.get(spec.id, "")
            out.append({
                "id": spec.id,
                "transport": "http" if spec.url else "stdio",
                "available": bool(conn and not conn.error),
                "tools": len(conn.tools) if conn else len(cache.get(spec.id, [])),
                "enabled": spec.enabled,
                "error": err,
            })
        return out

    # ---------------------------------------------------------------- lifecycle
    def _make_transport(self, spec: McpServerSpec) -> Any:
        if spec.url:
            return _HttpTransport(spec.id, spec.url)
        if not spec.command:
            raise RuntimeError(f"MCP server '{spec.id}' has neither command nor url")
        return _StdioTransport(spec.id, spec.command, spec.env)

    def _connect_sync(self, spec: McpServerSpec,
                      capture_transport: Optional[Any] = None) -> McpConnection:
        """Full handshake + tools/list on a helper thread."""
        tx = capture_transport or self._make_transport(spec)
        conn = McpConnection(spec=spec, transport=tx)
        # the connect budget honours per-server timeout_s: npx cold starts
        # (package resolve/download) routinely exceed the 25s floor
        budget = max(CONNECT_TIMEOUT_S, spec.timeout_s)
        try:
            if isinstance(tx, _HttpTransport):
                res = _http_request(tx, "initialize", {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "winbrow", "version": "6.0"},
                }, budget)
            else:
                tx.start()
                res = tx.request("initialize", {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "winbrow", "version": "6.0"},
                }, budget)
                tx._send("notifications/initialized", None, notify=True)
            _ = res.get("serverInfo", {})  # handshake actually completed
            tools_res = _rpc(tx, "tools/list", {}, budget)
            conn.tools = self._catalog(tools_res)
            conn.connected_at = time.time()
            conn.error = ""
        except Exception as e:
            conn.error = f"{type(e).__name__}: {e}"
            conn.tools = []
            try:
                tx.stop()
            except Exception:
                pass
        return conn

    def _catalog(self, tools_res: Any) -> List[dict]:
        tools = (tools_res or {}).get("tools") or []
        out = []
        for t in tools[:MAX_TOOLS_PER_SERVER]:
            name = _slug(str(t.get("name", "")))
            if not name:
                continue
            out.append({
                "name": name,
                "description": str(t.get("description", ""))[:400],
                "inputSchema": t.get("inputSchema") or {},
            })
        return out

    def _maybe_warmup(self, spec: McpServerSpec, conn: Optional[McpConnection]) -> None:
        if (conn is not None and not conn.error and not self.disabled
                and spec.id in _WARM_SERVERS):
            threading.Thread(target=_warm_server, args=(self, spec),
                             daemon=True, name=f"mcp-warm-{spec.id}").start()

    async def connect(self, spec: McpServerSpec) -> McpConnection:
        loop = asyncio.get_event_loop()
        conn = await loop.run_in_executor(None, lambda: self._connect_sync(spec))
        self._record(conn)
        self._maybe_warmup(spec, conn)
        return conn

    def _record(self, conn: McpConnection) -> None:
        self.connections[conn.spec.id] = conn
        if conn.error:
            self.failures[conn.spec.id] = conn.error
        elif conn.tools:
            try:
                cache = _load_cache(self.cache_path)
                cache[conn.spec.id] = conn.tools
                _save_cache(cache, self.cache_path)
            except Exception as e:
                log.debug(f"mcp cache save failed: {e}")

    async def connect_with_registry(
            self, registry,
            specs: Optional[List[McpServerSpec]] = None) -> List[McpConnection]:
        """Connect every enabled server (or the given subset) and register tools."""
        if self.disabled:
            return []
        conns: List[McpConnection] = []
        for spec in (specs if specs is not None else self.specs()):
            if not spec.enabled:
                continue
            conn = await self.connect(spec)
            conns.append(conn)
        _register_hub_tools(self, registry)
        return conns

    # ---------------------------------------------------------------- calls
    async def call(self, server_id: str, tool: str, args: dict,
                   deadline_s: Optional[float] = None) -> dict:
        """tools/call with lazy (re)connect. Never raises."""
        if self.disabled:
            return {"success": False, "output": "MCP bridge is disabled"}
        spec = next((s for s in self.specs() if s.id == _slug(server_id)), None)
        if spec is None or not spec.enabled:
            return {"success": False,
                    "output": f"MCP server '{server_id}' is not configured"}
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._call_sync(spec, tool, args, deadline_s))

    def _call_sync(self, spec: McpServerSpec, tool: str, args: dict,
                   deadline_s: Optional[float]) -> dict:
        if spec.id not in self.connections:
            # tiny cache refresh pre-touch: the first ghost call also refreshes
            conn = self._connect_sync(spec)
            self._record(conn)
            self._maybe_warmup(spec, conn)
        conn = self.connections.get(spec.id)
        if conn is None or conn.error:
            return {"success": False,
                    "output": (f"MCP server '{spec.id}' unavailable: "
                               f"{conn.error if conn else 'not connected'}")}
        # schema-aware coercion AFTER lazy connect (planner may send "3" for int)
        schema: Any = None
        for t in conn.tools or []:
            if t.get("name") == _slug(tool):
                schema = t.get("inputSchema")
                break
        try:
            payload = _clamp_args(_coerce_schema_args(args, schema))
        except Exception as e:
            return {"success": False, "output": f"Invalid MCP arguments: {e}"}
        deadline = deadline_s or spec.timeout_s
        res: Any = None
        last_err: Optional[str] = None
        for attempt in range(2):        # flaky server: one re-arm+retry
            try:
                res = _rpc(conn.transport, "tools/call",
                           {"name": tool, "arguments": payload}, deadline)
                last_err = None
                break
            except JsonRpcError as e:
                if e.code == -32601:    # unknown tool → refresh catalog once
                    conn = self._connect_sync(spec)
                    self._record(conn)
                    if conn.error:
                        return {"success": False,
                                "output": f"MCP server '{spec.id}': {conn.error}"}
                    try:
                        res = _rpc(conn.transport, "tools/call",
                                   {"name": tool, "arguments": payload}, deadline)
                        last_err = None
                        break
                    except Exception as e2:
                        last_err = f"MCP call failed: {e2}"
                else:
                    last_err = f"MCP call failed: {e.message}"
                break
            except TimeoutError as e:
                return {"success": False, "output": str(e)}
            except Exception as e:
                last_err = f"MCP call failed: {e}"
                conn = self._connect_sync(spec)     # dead stream → re-arm, retry once
                self._record(conn)
                if conn.error:
                    break
        if last_err is not None:
            return {"success": False, "output": last_err}
        if isinstance(res, dict) and res.get("isError"):
            output = _extract_text(res) or "MCP tool reported an error"
            if tool == "edit_file" and "exact match" in output.lower():
                output += (
                    "\nHINT: oldText must match the file byte-for-byte. "
                    "For a whole-file rewrite, call mcp_filesystem_write_file "
                    "once with the complete content instead."
                )
            return {"success": False, "output": output}
        return {"success": True,
                "output": _extract_text(res) or "(empty result)"}

    def stop_all(self) -> None:
        for conn in list(self.connections.values()):
            try:
                conn.transport.stop()
            except Exception:
                pass
        self.connections.clear()


_hubs: Dict[str, McpHub] = {}


def get_mcp_hub(config_path: Optional[Path] = None) -> McpHub:
    key = str(config_path or CONFIG_PATH)
    if key not in _hubs:
        _hubs[key] = McpHub(config_path=Path(config_path or CONFIG_PATH))
    return _hubs[key]


def reset_mcp_hub() -> None:
    for hub in list(_hubs.values()):
        try:
            hub.stop_all()
        except Exception:
            pass
    _hubs.clear()


# --------------------------------------------------------------------------- registry bridge
def _schema_args(schema: dict) -> List[Tuple[str, dict]]:
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    out = []
    for name, meta in list(props.items())[:12]:
        if not isinstance(meta, dict):
            meta = {}
        out.append((str(name), {"meta": meta, "required": str(name) in required}))
    return out


def _schema_argspec(name: str, meta: dict) -> "ArgSpec":  # noqa: F821
    from .registry import ArgSpec
    instr = str((meta or {}).get("description", ""))[:160] or f"mcp arg {name}"
    default = (meta or {}).get("default")
    return ArgSpec(name=name, kind="text", instructions=instr,
                   default=str(default) if default is not None else None,
                   criteria={})


def _examples_for(spec: "McpServerSpec", tool: dict) -> List[str]:  # noqa: F821
    name = tool.get("name", "")
    desc = (tool.get("description") or "").strip()
    first_sentence = desc.split(".")[0].strip()[:80]
    return list(filter(None, [
        f"{name.replace('_', ' ')} ({spec.id})",
        first_sentence,
    ]))[:3]


def _register_hub_tools(hub: McpHub, registry) -> int:
    """Register every cached+connected catalog tool as impl=MCP tools.

    Also honors the per-server "tier": "t1" override for verified
    read-only catalogs — but never for names the default classifier
    already considers T2/T3 (that downgrade is structurally impossible).
    """
    from .registry import Tool
    from .permissions import TOOL_TIERS, _default_tier
    count = 0
    cache = _load_cache(hub.cache_path)
    for spec in hub.specs():
        if not spec.enabled:
            continue
        # prefer the LIVE connected catalog, fall back to cache
        conn = hub.connections.get(spec.id)
        catalog = (conn.tools if conn and conn.tools else cache.get(spec.id) or [])
        for t in catalog[:MAX_TOOLS_PER_SERVER]:
            full = f"mcp_{spec.id}_{t['name']}"
            script = f"mcp://{spec.id}/{t['name']}"
            if registry.get(full) is not None:
                continue
            args = []
            try:
                for arg_name, info in _schema_args(t.get("inputSchema", {})):
                    args.append(_schema_argspec(arg_name, info.get("meta", {})))
            except Exception as e:
                log.debug(f"MCP arg mapping failed {full}: {e}")
            try:
                registry.register(Tool(
                    name=full,
                    description=(
                        (t.get("description") or "").strip()[:280]
                        or f"{spec.id} MCP tool {t['name']}"),
                    script=script,          # impl-executed; never a PS template
                    args=args,
                    speak="result",
                    examples=_examples_for(spec, t),
                    is_learned=True,
                    impl=MCP_IMPL,
                ))
                count += 1
            except Exception as e:
                log.debug(f"MCP tool registration failed {full}: {e}")
            if spec.tier == "t1":
                low = full.lower()
                if low not in TOOL_TIERS and _default_tier(low) == "t1":
                    TOOL_TIERS[low] = "t1"
    return count


def get_mcp_tools(registry, hub: Optional["McpHub"] = None) -> List["Tool"]:  # noqa: F821
    """Offline-registered MCP tools from the catalog cache (agent bootstrap)."""
    hub = hub or get_mcp_hub()
    if hub.disabled:
        return []
    from .registry import Tool
    from .permissions import TOOL_TIERS, _default_tier
    registered: List[Tool] = []
    try:
        cache = _load_cache(hub.cache_path)
    except Exception:
        cache = {}
    for spec in hub.specs():
        if not spec.enabled:
            continue
        for t in (cache.get(spec.id) or [])[:MAX_TOOLS_PER_SERVER]:
            full = f"mcp_{spec.id}_{t['name']}"
            if registry.get(full) is not None:
                continue
            args = []
            try:
                for arg_name, info in _schema_args(t.get("inputSchema", {})):
                    args.append(_schema_argspec(arg_name, info.get("meta", {})))
            except Exception as e:
                log.debug(f"MCP arg mapping failed {full}: {e}")
            registered.append(Tool(
                name=full,
                description=(t.get("description") or "").strip()[:280]
                            or f"{spec.id} MCP tool {t['name']}",
                script=f"mcp://{spec.id}/{t['name']}",
                args=args, speak="result", examples=_examples_for(spec, t),
                is_learned=True, impl=MCP_IMPL,
            ))
            if spec.tier == "t1":
                low = full.lower()
                if low not in TOOL_TIERS and _default_tier(low) == "t1":
                    TOOL_TIERS[low] = "t1"
    for t in registered:
        try:
            registry.register(t)
        except Exception as e:
            log.debug(f"MCP cache registration failed {t.name}: {e}")
    return registered


def _mcp_parse(tool) -> Tuple[str, str]:
    """Tool → (server_id, remote_tool_name) from the mcp:// script carrier."""
    s = (getattr(tool, "script", "") or "").strip()
    if s.startswith("mcp://"):
        raw = s[len("mcp://"):]
        server, _, tool_name = raw.partition("/")
        return server, tool_name
    return "", ""


async def execute_mcp_tool(tool_name: str, tool, args: dict) -> dict:
    """Called from agent.run_skill for impl=="mcp" tools. Never raises."""
    hub = get_mcp_hub()
    server, remote_tool = _mcp_parse(tool)
    t0 = time.perf_counter()
    if not server or not remote_tool:
        return {"type": "mcp", "tool": tool_name, "success": False,
                "output": "Malformed MCP tool registration"}
    out = await hub.call(server, remote_tool, args)
    out.setdefault("type", "mcp")
    out.setdefault("tool", tool_name)
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


# --------------------------------------------------------------------------- payload helpers
def _clamp_args(args: dict) -> dict:
    """Bound MCP args without corrupting file contents or structured values.

    The old implementation truncated every serialized value at 4,000 chars and
    then ``json.loads`` the result. That silently cut write_file content to
    4,000 chars and made edit_file's ``edits`` array invalid JSON. Reject
    oversized values with a clear error instead.
    """
    out: Dict[str, Any] = {}
    for k, v in (args or {}).items():
        key = str(k)
        if isinstance(v, str):
            if len(v) > MAX_ARG_CHARS:
                raise ValueError(
                    f"argument '{key}' is too large ({len(v)} chars; "
                    f"max {MAX_ARG_CHARS}). Split the file or write a smaller chunk."
                )
            out[key] = v
            continue
        try:
            blob = json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"argument '{key}' is not JSON-serializable: {exc}") from exc
        if len(blob) > MAX_ARG_CHARS:
            raise ValueError(
                f"argument '{key}' is too large ({len(blob)} chars; "
                f"max {MAX_ARG_CHARS}). Split the file or write a smaller chunk."
            )
        out[key] = v
    if len(json.dumps(out, ensure_ascii=False)) > MAX_ARGS_JSON:
        raise ValueError(
            f"MCP arguments too large (max {MAX_ARGS_JSON} chars). "
            "Write a smaller chunk."
        )
    return out


def _coerce_schema_args(args: dict, input_schema: Any) -> dict:
    """Fix planner/type mix-ups against the cached inputSchema.

    LLM planners emit numbers as strings ("3", "1.5") and booleans as
    "true"/"false"; fastmcp servers then reject the call with pure JSON-Schema
    validation errors. Coerce against the declared property types; drop args
    that cannot be converted instead of failing the whole call.
    """
    if not isinstance(args, dict) or not isinstance(input_schema, dict):
        return args
    props = input_schema.get("properties") or {}
    if not isinstance(props, dict):
        return args
    out: Dict[str, Any] = {}
    for k, v in args.items():
        spec = props.get(k) if isinstance(props.get(k), dict) else None
        t = str((spec or {}).get("type") or "")
        try:
            if t == "integer":
                if isinstance(v, bool):
                    out[k] = int(v)
                elif isinstance(v, int):
                    out[k] = v
                elif isinstance(v, float):
                    out[k] = int(v)
                elif isinstance(v, str) and v.strip():
                    out[k] = int(float(v.strip()))
                else:
                    continue
            elif t == "number":
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    out[k] = v
                elif isinstance(v, str) and v.strip():
                    out[k] = float(v.strip())
                else:
                    continue
            elif t == "boolean":
                if isinstance(v, bool):
                    out[k] = v
                elif isinstance(v, str):
                    lv = v.strip().lower()
                    if lv in ("true", "yes", "1", "on"):
                        out[k] = True
                    elif lv in ("false", "no", "0", "off"):
                        out[k] = False
                    else:
                        continue
                else:
                    out[k] = bool(v)
            else:
                out[k] = v
        except (ValueError, TypeError):
            continue    # unfixable: drop the arg rather than fail the call
    return out


def _extract_text(res: Any) -> str:
    """tools/call result → joined content[].text (resource blobs JSON'd).
    Servers that return only structuredContent (no text content) fall back
    to a JSON dump of that structure."""
    if not isinstance(res, dict):
        return str(res)[:MAX_OUTPUT_CHARS]
    content = res.get("content") or []
    texts: List[str] = []
    total = 0
    for c in content:
        if not isinstance(c, dict):
            continue
        if "text" in c:
            s = str(c.get("text", ""))
        elif "data" in c:
            s = json.dumps(c.get("data", c), ensure_ascii=False)
        else:
            s = json.dumps(c, ensure_ascii=False)
        if total + len(s) > MAX_OUTPUT_CHARS:
            s = s[: max(0, MAX_OUTPUT_CHARS - total)]
        texts.append(s)
        total += len(s)
        if total >= MAX_OUTPUT_CHARS:
            break
    if not texts and res.get("structuredContent") is not None:
        texts.append(json.dumps(res["structuredContent"],
                                ensure_ascii=False)[:MAX_OUTPUT_CHARS])
    return "\n".join(texts)[:MAX_OUTPUT_CHARS]
