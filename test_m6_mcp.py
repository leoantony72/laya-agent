"""M6 — MCP capability bridge
=============================
Fake stdio JSON-RPC servers + transport unit tests + registry bridge +
permission-gate tiering + dispatch through run_skill + router shortcuts.
NO real network andNO pip installs at unit level unless the packages are
already present (live tests skip otherwise).
"""
import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ["WINBROW_TTS"] = "0"
os.environ["WINBROW_NORMALIZE"] = "0"

from winbrow.mcp import (McpHub, McpServerSpec, McpConnection, MCP_IMPL,
                         MAX_ARG_CHARS, MAX_ARGS_JSON, MAX_OUTPUT_CHARS,
                         _StdioTransport, _parse_sse, _clamp_args,
                         _coerce_schema_args,
                         _extract_text, _slug, _mcp_parse, execute_mcp_tool,
                         get_mcp_tools, reset_mcp_hub)          # noqa: E402
from winbrow.registry import ArgSpec, ToolRegistry, Tool                # noqa: E402
from winbrow.permissions import (reset_permissions, tool_tier,       # noqa: E402
                                 TOOL_TIERS)

# pristine tier table captured at module import (before any test mutates it)
_IMPORT_TIERS = dict(TOOL_TIERS)

FAKE_SERVER = r'''
import json, sys
tools = [
    {"name": "search_wikipedia",
     "description": "Search Wikipedia for article titles.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "limit": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "get_summary",
     "description": "Summary of one Wikipedia article.",
     "inputSchema": {"type": "object",
                     "properties": {"title": {"type": "string"}},
                     "required": ["title"]}},
]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    method, rid = req.get("method", ""), req.get("id")

    def reply(result, rid=rid):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                     "result": result}) + "\n")
        sys.stdout.flush()

    if method == "initialize":
        reply({"protocolVersion": "2025-06-18",
               "capabilities": {"tools": {}},
               "serverInfo": {"name": "fake-wiki", "version": "0.1"}})
        # a notification BETWEEN requests must never desync the client
        sys.stdout.write(json.dumps({"jsonrpc": "2.0",
                                     "method": "notifications/progress",
                                     "params": {}}) + "\n")
        sys.stdout.flush()
    elif method == "tools/list":
        reply({"tools": tools})
    elif method == "tools/call":
        params = req.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "search_wikipedia":
            n = int(args.get("limit", 2))
            rows = [f"{args.get('query', '')}_hit_{i}" for i in range(n)]
            reply({"content": [{"type": "text", "text": "\n".join(rows)}]})
        elif name == "get_summary":
            reply({"content": [{"type": "text",
                                "text": f"Summary of {args.get('title')}."}]})
        elif name == "boom":
            reply({"isError": True,
                   "content": [{"type": "text", "text": "exploded"}]})
'''


def run(coro):
    try:
        loop = asyncio.get_event_loop()
    except (RuntimeError, DeprecationWarning):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class TestStdioTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake_path = Path(__file__).parent / "_fake_mcp_server.py"
        cls.fake_path.write_text(FAKE_SERVER, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        if cls.fake_path.exists():
            for _ in range(10):
                try:
                    cls.fake_path.unlink()
                    break
                except PermissionError:
                    time.sleep(0.2)

    def test_handshake_list_and_call(self):
        tx = _StdioTransport("fake", [sys.executable, str(self.fake_path)])
        try:
            tx.start()
            res = tx.request("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"}}, 15.0)
            self.assertEqual(res["serverInfo"]["name"], "fake-wiki")
            tx._send("notifications/initialized", None, notify=True)
            tools = tx.request("tools/list", {}, 15.0)["tools"]
            self.assertEqual(len(tools), 2)
            self.assertEqual(tools[0]["name"], "search_wikipedia")
            call = tx.request("tools/call", {
                "name": "search_wikipedia",
                "arguments": {"query": "curry", "limit": 3}}, 15.0)
            text = "".join(c["text"] for c in call["content"])
            self.assertIn("curry_hit_2", text)
            call2 = tx.request("tools/call", {
                "name": "get_summary", "arguments": {"title": "Sun"}}, 15.0)
            self.assertIn("Summary of Sun.", call2["content"][0]["text"])
        finally:
            tx.stop()

    def test_request_to_dead_server_raises(self):
        tx = _StdioTransport("dead", [sys.executable, "-c", "pass"])
        with self.assertRaises(RuntimeError):
            tx.request("tools/list", {}, 5.0)
        tx.stop()


class TestJsonHelpers(unittest.TestCase):
    def test_parse_sse(self):
        body = 'event: message\\n' + \
               'data: {"jsonrpc":"2.0","id":1,"result":{"x":1}}\\n\\n'
        body = body.replace("\\n", "\n")
        self.assertEqual(_parse_sse(body)["result"]["x"], 1)

    def test_parse_sse_skips_notifications(self):
        # open-meteo-style real stream: progress notifications BEFORE result
        body = ("\n".join([
            "event: message",
            "data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\","
            "\"params\":{\"level\":\"info\"}}",
            "",
            "event: message",
            "data: {\"jsonrpc\":\"2.0\",\"id\":7,\"result\":{\"ok\":2}}",
            "",
            "event: message",
            "data: {\"jsonrpc\":\"2.0\",\"id\":9,\"result\":{\"other\":1}}",
        ]) + "\n")
        self.assertEqual(_parse_sse(body, want_id=7)["result"]["ok"], 2)
        # without want_id: first id-bearing message still wins
        self.assertEqual(_parse_sse(body).get("id"), 7)

    def test_clamp_args(self):
        self.assertEqual(_clamp_args({"query": "hi"}), {"query": "hi"})
        # File content must not be silently truncated: write_file/edit_file
        # depend on the exact text the planner sent.
        out = _clamp_args({"blob": "x" * 9000})
        self.assertEqual(len(out["blob"]), 9000)
        edits = {"edits": [{"oldText": "a" * 5000, "newText": "b" * 5000}]}
        self.assertEqual(_clamp_args(edits), edits)
        # Oversized values are rejected clearly instead of becoming invalid JSON.
        with self.assertRaises(ValueError):
            _clamp_args({"blob": "x" * (MAX_ARG_CHARS + 1)})
        with self.assertRaises(ValueError):
            _clamp_args({f"a{i}": "y" * 70_000 for i in range(4)})
        self.assertGreater(MAX_ARGS_JSON, 70_000)

    def test_extract_text(self):
        res = {"content": [{"type": "text", "text": "abc def"},
                           {"type": "resource", "data": {"p": 1}}]}
        self.assertIn("abc def", _extract_text(res))
        self.assertLessEqual(len(_extract_text(res)), MAX_OUTPUT_CHARS)

    def test_extract_text_clips_head_and_reports_full_copy(self):
        from winbrow import mcp
        body = "HEAD " + ("x" * 150_000)
        out = mcp._extract_text({"content": [{"type": "text", "text": body}]})
        self.assertTrue(out.startswith("HEAD "))
        self.assertIn("truncated", out)
        self.assertLessEqual(len(out), mcp.MAX_OUTPUT_CHARS + 300)

    def test_coerce_schema_args(self):
        schema = {"type": "object", "properties": {
            "max_results": {"type": "integer"},
            "score": {"type": "number"},
            "verbose": {"type": "boolean"},
            "query": {"type": "string"},
        }}
        out = _coerce_schema_args(
            {"max_results": "3", "score": "1.5", "verbose": "true",
             "query": "attention is all you need"}, schema)
        self.assertEqual(out["max_results"], 3)
        self.assertEqual(out["score"], 1.5)
        self.assertIs(out["verbose"], True)
        self.assertEqual(out["query"], "attention is all you need")
        # unfixable values are dropped, not fatal
        out2 = _coerce_schema_args({"max_results": "three", "query": "x"}, schema)
        self.assertEqual(out2, {"query": "x"})
        # no schema → passthrough
        self.assertEqual(_coerce_schema_args({"a": "3"}, None), {"a": "3"})


# ---------------------------------------------------------------- hub with fakes
class _ScriptedHub(McpHub):
    """Replaces real transports with scripted fixtures (no subprocesses)."""

    def __init__(self, fixtures: dict, **kw):
        super().__init__(**kw)
        self.fixtures = fixtures
        self.seqs: dict = {}          # (server, tool) → call index

    def _make_transport(self, spec):
        return None

    def _connect_sync(self, spec, capture_transport=None):
        data = self.fixtures.get(spec.id, {})
        conn = McpConnection(spec=spec, transport=_DeadTransport(spec.id))
        conn.error = data.get("error", "")
        conn.tools = [] if conn.error else list(data.get("tools", []))
        conn.connected_at = time.time() if not conn.error else 0.0
        self._record(conn)
        return conn

    def _call_sync(self, spec, tool, args, deadline_s=None):
        if spec.id not in self.connections:
            self._connect_sync(spec)
            self._maybe_warmup(spec, self.connections.get(spec.id))
        conn = self.connections.get(spec.id)
        if conn is None or conn.error:
            return {"success": False,
                    "output": (f"MCP server '{spec.id}' unavailable: "
                               f"{conn.error if conn else 'not connected'}")}
        scripted = (self.fixtures.get(spec.id, {}).get("call") or {})
        resp = scripted.get(tool)
        if resp is None:
            return {"success": False, "output": f"no fixture for {tool}"}
        if isinstance(resp, dict) and "sequence" in resp:
            # each call advances; the final value repeats forever
            items = [str(x) for x in resp["sequence"]]
            key = (spec.id, tool)
            idx = self.seqs.get(key, 0)
            self.seqs[key] = idx + 1
            return {"success": True, "output": items[min(idx, len(items) - 1)]}
        return {"success": True,
                "output": resp if isinstance(resp, str) else json.dumps(resp)}


class _DeadTransport:
    def __init__(self, server_id):
        self.server_id = server_id
        self.stopped = False

    def stop(self):
        self.stopped = True


def make_hub(tmp: Path, fixtures: dict, tiers: dict = None) -> _ScriptedHub:
    cfg = tmp / "mcp_servers.json"
    servers = []
    for sid in fixtures:
        ent = {"id": sid, "command": ["fake"], "enabled": True,
               "description": "test server"}
        if tiers and sid in tiers:
            ent["tier"] = tiers[sid]
        servers.append(ent)
    cfg.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    hub = _ScriptedHub(fixtures, config_path=cfg,
                       cache_path=tmp / "mcp_cache.json")
    return hub


SCHEMA_WIKI = {
    "tools": [{
        "name": "search_wikipedia",
        "description": "Search Wikipedia for article titles.",
        "inputSchema": {"type": "object",
                        "properties": {"query": {"type": "string",
                                                 "description": "Search phrase"}}},
    }],
    "call": {"search_wikipedia": ["turing", "and the enigma"]},
}


class TestHubBridge(unittest.TestCase):
    def setUp(self):
        reset_permissions()
        self._tiers = dict(TOOL_TIERS)          # restore-test snapshot
        TOOL_TIERS.clear()
        TOOL_TIERS.update(dict(_IMPORT_TIERS))
        self.tmp = Path(os.environ.get("TEMP", ".")) / f"mcp_hub_{os.getpid()}"
        self.tmp.mkdir(exist_ok=True)
        for stale in self.tmp.glob("mcp_cache.json"):
            stale.unlink()
        # scripted hubs must not fire the real background warmup probes
        import winbrow.mcp as m
        m._warmed_ids.add("wikipedia")
    def tearDown(self):
        reset_permissions()
        TOOL_TIERS.clear()
        TOOL_TIERS.update(self._tiers)

    def test_connect_registers_bridge_tools(self):
        hub = make_hub(self.tmp, {"wikipedia": SCHEMA_WIKI}, tiers={})
        conns = run(hub.connect_with_registry(ToolRegistry()))
        self.assertEqual(len(conns), 1)
        registry = ToolRegistry()
        run(hub.connect_with_registry(registry))
        tool = registry.get("mcp_wikipedia_search_wikipedia")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.impl, MCP_IMPL)
        self.assertEqual(tool.script, "mcp://wikipedia/search_wikipedia")
        self.assertEqual([a.name for a in tool.args], ["query"])
        self.assertEqual(tool.args[0].instructions, "Search phrase")
        joined = " ".join(tool.examples).lower()
        self.assertIn("search wikipedia", joined)

    def test_cache_offline_registration(self):
        hub = make_hub(self.tmp, {"wikipedia": SCHEMA_WIKI})
        run(hub.connect_with_registry(ToolRegistry()))      # populates cache
        self.assertTrue(hub.cache_path.exists())
        reg2 = ToolRegistry()
        tools = get_mcp_tools(reg2, hub=hub)   # offline bootstrap from cache
        names = [t.name for t in tools]
        self.assertIn("mcp_wikipedia_search_wikipedia", names)
        self.assertIsNotNone(reg2.get(names[0]))

    def test_tier_seed_read_only(self):
        hub = make_hub(self.tmp, {"wikipedia": SCHEMA_WIKI}, tiers={"wikipedia": "t1"})
        reg = ToolRegistry()
        run(hub.connect_with_registry(reg))
        self.assertEqual(tool_tier("mcp_wikipedia_search_wikipedia"), "t1")

    def test_tier_never_downgrades_t2_t3(self):
        fixtures = {"evil": {"tools": [{"name": "install",
                                        "description": "installs things",
                                        "inputSchema": {}}]}}
        hub = make_hub(self.tmp, fixtures, tiers={"evil": "t1"})
        reg = ToolRegistry()
        run(hub.connect_with_registry(reg))
        # default classifier must win: install verb → T3 — the config cannot
        # downgrade system-class content through MCP names.
        self.assertEqual(tool_tier("mcp_evil_install"), "t3")

    def test_write_verb_defaults_t2(self):
        fixtures = {"notedb": {"tools": [{"name": "write_file",
                                          "description": "writes a file",
                                          "inputSchema": {}}]}}
        hub = make_hub(self.tmp, fixtures)
        reg = ToolRegistry()
        run(hub.connect_with_registry(reg))
        self.assertEqual(tool_tier("mcp_notedb_write_file"), "t2")

    def test_api_mcp_reports_live_status(self):
        """Regression: /api/mcp (mcp_detail) must map per-server status from
        hub.status() by id — not return the fallback defaults for every row
        (available always False, error always "")."""
        import server as srv
        import winbrow.mcp as m
        hub = make_hub(self.tmp, {"wikipedia": SCHEMA_WIKI})
        run(hub.connect_with_registry(ToolRegistry()))
        orig_conf, orig_hub = m.CONFIG_PATH, m.get_mcp_hub
        m.CONFIG_PATH = hub.cache_path.parent / "mcp_servers.json"
        m.get_mcp_hub = lambda: hub
        try:
            resp = run(srv.mcp_detail())
        finally:
            m.CONFIG_PATH, m.get_mcp_hub = orig_conf, orig_hub
        data = json.loads(resp.body)
        by_id = {s["id"]: s for s in data["servers"]}
        self.assertIn("wikipedia", by_id)
        row = by_id["wikipedia"]
        self.assertTrue(row["available"])
        self.assertEqual(row["transport"], "stdio")
        self.assertEqual(row["tools"][0]["name"], "search_wikipedia")
        self.assertEqual(row["error"], "")

    def test_call_and_error_content(self):
        fixtures = {"wiki2": {
            "tools": [{"name": "boom", "description": "d", "inputSchema": {}}],
            "call": {"boom": "exploded"},
        }}
        hub = make_hub(self.tmp, fixtures)
        hub.fixtures["wiki2"]["call"]["boom"] = "exploded"
        out = run(hub.call("wiki2", "boom", {}))
        self.assertTrue(out["success"])
        self.assertEqual(out["output"], "exploded")

    def test_call_to_unconfigured(self):
        hub = make_hub(self.tmp, {},
                       tiers=None)
        out = run(hub.call("nope", "x", {}))
        self.assertFalse(out["success"])
        self.assertIn("not configured", out["output"])

    def test_agent_run_skill_mcp_flow(self):
        hub = make_hub(self.tmp, {"wikipedia": SCHEMA_WIKI},
                       tiers={"wikipedia": "t1"})
        from winbrow.agent import WinBrowAgent
        import winbrow.mcp as m
        ag = WinBrowAgent()
        run(hub.connect_with_registry(ag.registry))
        name = "mcp_wikipedia_search_wikipedia"
        self.assertIsNotNone(ag.registry.get(name))
        self.assertEqual(tool_tier(name), "t1")
        # route execute_mcp_tool (global singleton) to the scripted hub
        m._hubs[str(m.CONFIG_PATH)] = hub
        try:
            out = run(ag.run_skill(name, {"query": "turing"},
                                   utterance="wiki turing", session_id="m6t"))
            self.assertTrue(out["success"])
            self.assertIn("turing", str(out["output"]))
            self.assertEqual(out.get("type"), "mcp")
        finally:
            m._hubs.pop(str(m.CONFIG_PATH), None)

    def test_hub_disable_env(self):
        hub = make_hub(self.tmp, {"wikipedia": SCHEMA_WIKI})
        os.environ["WINBROW_MCP_DISABLE"] = "1"
        try:
            conns = run(hub.connect_with_registry(ToolRegistry()))
            self.assertEqual(conns, [])
        finally:
            os.environ["WINBROW_MCP_DISABLE"] = ""


class TestRouterShortcuts(unittest.TestCase):
    def setUp(self):
        from winbrow.router import WinBrowRouter
        reset_permissions()
        TOOL_TIERS.clear()
        TOOL_TIERS.update(dict(_IMPORT_TIERS))
        self.registry = ToolRegistry()
        self.registry.register(Tool(
            name="mcp_wikipedia_search_wikipedia",
            description="Search Wikipedia.",
            script="mcp://wikipedia/search_wikipedia",
            args=[_ArgSpecArg()], speak="result",
            examples=["search wikipedia"], is_learned=True, impl=MCP_IMPL))
        self.registry.register(Tool(
            name="mcp_wikipedia_get_summary",
            description="Get one article summary.",
            script="mcp://wikipedia/get_summary",
            args=[ArgSpec(name="title", kind="text", instructions="title")],
            speak="result",
            examples=["what is the ecliptic on wikipedia"],
            is_learned=True, impl=MCP_IMPL))
        self.camel = WinBrowRouter(self.registry)  # Laya worker may be absent

    def _route_once(self, utterance):
        ctx = type("Ctx", (), {"active_app": "Brave",
                               "active_title": "x",
                               "running_apps": []})()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            r = loop.run_until_complete(self.camel.route(utterance, ctx))
        except Exception:
            r = None
        finally:
            loop.close()
        return r

    def test_wikipedia_search_vs_no_mcp_tool(self):
        # without the MCP tool registered the route must fall through,
        # not crash — unregister to simulate hub failure
        reg = self.registry
        name = "mcp_wikipedia_search_wikipedia"
        tool = reg.get(name)
        reg._tools.pop(name, None)
        try:
            r = self._route_once("search wikipedia for alan turing")
            self.assertTrue(r is None or r.kind != "tool"
                            or r.tool.name != name)
        finally:
            self.registry.register(tool)

    def test_wikipedia_summary_vs_search(self):
        r = self._route_once("what is the prime meridian on wikipedia")
        self.assertIsNotNone(r)
        self.assertEqual(r.kind, "tool")
        self.assertEqual(r.tool.name, "mcp_wikipedia_get_summary")
        self.assertEqual(r.args.get("title"), "prime meridian")
        r2 = self._route_once("search wikipedia for alan turing")
        self.assertIsNotNone(r2)
        self.assertEqual(r2.tool.name, "mcp_wikipedia_search_wikipedia")
        self.assertEqual(r2.args.get("query"), "alan turing")
        # fallback: summary tool missing → search takes over, still routed
        self.registry._tools.pop("mcp_wikipedia_get_summary", None)
        r3 = self._route_once("what is a geodesic on wikipedia")
        self.assertIsNotNone(r3)
        self.assertEqual(r3.tool.name, "mcp_wikipedia_search_wikipedia")


def _ArgSpecArg():
    from winbrow.registry import ArgSpec
    return ArgSpec(name="query", kind="text", instructions="phrase")


class TestBundledKeyless(unittest.TestCase):
    """Handshake + catalog of the bundled stdlib server (no network calls)."""
    SERVER = Path(__file__).parent / "tools" / "mcp_servers" / "winbrow_keyless.py"

    def test_handshake_and_catalog(self):
        if not self.SERVER.exists():
            self.skipTest("bundled server not present")
        tx = _StdioTransport("bundled", [sys.executable, str(self.SERVER),
                                         "--api", "dictionary"])
        try:
            tx.start()
            res = tx.request("initialize", {"protocolVersion": "2025-06-18",
                                            "capabilities": {},
                                            "clientInfo": {"name": "t",
                                                           "version": "1"}}, 20.0)
            self.assertEqual(res["serverInfo"]["name"], "winbrow-dictionary")
            tx._send("notifications/initialized", None, notify=True)
            tools = tx.request("tools/list", {}, 20.0)["tools"]
            names = {t["name"] for t in tools}
            self.assertIn("define", names)
            self.assertIn("synonyms", names)
        finally:
            tx.stop()

    def test_unknown_tool_is_error(self):
        if not self.SERVER.exists():
            self.skipTest("bundled server not present")
        tx = _StdioTransport("bundled2", [sys.executable, str(self.SERVER),
                                          "--api", "dictionary"])
        try:
            tx.start()
            tx.request("initialize", {"protocolVersion": "2025-06-18",
                                      "capabilities": {},
                                      "clientInfo": {"name": "t",
                                                     "version": "1"}}, 20.0)
            call = tx.request("tools/call", {"name": "no_such_tool",
                                             "arguments": {}}, 20.0)
            self.assertTrue(call.get("isError"))
            self.assertIn("unknown tool", call["content"][0]["text"])
        finally:
            tx.stop()


class TestDirectShortcuts(unittest.TestCase):
    """agent.execute pre-router MCP shortcuts: wiki search phrase, git verbs,
    time-in-city, local PDF paths, and drive-path file reads. These must fire
    BEFORE the browser/is_search fast-path (which previously swallowed them)."""

    def setUp(self):
        reset_permissions()
        self._tiers = dict(TOOL_TIERS)
        TOOL_TIERS.clear()
        TOOL_TIERS.update(dict(_IMPORT_TIERS))
        self.tmp = Path(os.environ.get("TEMP", ".")) / f"mcp_sc_{os.getpid()}"
        self.tmp.mkdir(exist_ok=True)
        for stale in self.tmp.glob("mcp_cache.json"):
            stale.unlink()
        import winbrow.mcp as m
        m._warmed_ids.add("wikipedia")

    def tearDown(self):
        reset_permissions()
        TOOL_TIERS.clear()
        TOOL_TIERS.update(self._tiers)

    def _agent_with(self, fixtures):
        from winbrow.agent import WinBrowAgent
        import winbrow.mcp as m
        hub = make_hub(self.tmp, fixtures)
        ag = WinBrowAgent()
        run(hub.connect_with_registry(ag.registry))
        m._hubs[str(m.CONFIG_PATH)] = hub
        self.addCleanup(lambda: m._hubs.pop(str(m.CONFIG_PATH), None))
        return ag

    def test_wikipedia_search_phrase(self):
        ag = self._agent_with({"wikipedia": SCHEMA_WIKI})
        out = run(ag.execute("search wikipedia for alan turing", session_id="m6sc"))
        self.assertEqual(out["route"]["tier"], "mcp_shortcut")
        self.assertEqual(out["route"]["tool_name"], "mcp_wikipedia_search_wikipedia")
        self.assertEqual(out["route"]["args"], {"query": "alan turing"})
        self.assertEqual(out["execution"]["type"], "mcp")
        self.assertIn("turing", str(out["execution"]["output"]).lower())

    def test_git_status_shortcut(self):
        fixtures = {"git": {
            "tools": [{"name": "git_status", "description": "d", "inputSchema": {}}],
            "call": {"git_status": "On branch main, nothing to commit"}}}
        ag = self._agent_with(fixtures)
        out = run(ag.execute("show git status", session_id="m6sc"))
        self.assertEqual(out["route"]["tier"], "mcp_shortcut")
        self.assertEqual(out["route"]["tool_name"], "mcp_git_git_status")
        self.assertIn("main", str(out["execution"]["output"]))

    def test_time_in_city_shortcut(self):
        fixtures = {"time": {
            "tools": [{"name": "get_current_time", "description": "d", "inputSchema": {}}],
            "call": {"get_current_time": "Asia/Tokyo 15:00"}}}
        ag = self._agent_with(fixtures)
        out = run(ag.execute("what time is it in tokyo", session_id="m6sc"))
        self.assertEqual(out["route"]["tool_name"], "mcp_time_get_current_time")
        self.assertEqual(out["route"]["args"].get("timezone"), "Asia/Tokyo")
        out2 = run(ag.execute("time in delhi", session_id="m6sc"))
        self.assertEqual(out2["route"]["args"].get("timezone"), "Asia/Kolkata")

    def test_pdf_and_read_file_shortcuts(self):
        fixtures = {
            "pdf": {
                "tools": [{"name": n, "description": "d", "inputSchema": {}}
                          for n in ("pdf_extract_text", "pdf_info", "pdf_search")],
                "call": {"pdf_extract_text": "hello pdf text",
                         "pdf_info": "2 pages",
                         "pdf_search": "page 2: needle"}},
            "filesystem": {
                "tools": [{"name": "read_text_file", "description": "d", "inputSchema": {}}],
                "call": {"read_text_file": "file contents here"}},
        }
        ag = self._agent_with(fixtures)
        p = r"C:\tmp\report.pdf"
        out = run(ag.execute(f"read the pdf {p}", session_id="m6sc"))
        self.assertEqual(out["route"]["tool_name"], "mcp_pdf_pdf_extract_text")
        self.assertEqual(out["route"]["args"], {"path": p})
        out2 = run(ag.execute(f"pdf info {p}", session_id="m6sc"))
        self.assertEqual(out2["route"]["tool_name"], "mcp_pdf_pdf_info")
        out3 = run(ag.execute(f"search {p} for needle", session_id="m6sc"))
        self.assertEqual(out3["route"]["tool_name"], "mcp_pdf_pdf_search")
        self.assertEqual(out3["route"]["args"].get("query"), "needle")
        out4 = run(ag.execute(r"read file C:\tmp\notes.txt", session_id="m6sc"))
        self.assertEqual(out4["route"]["tool_name"], "mcp_filesystem_read_text_file")
        self.assertIn("contents", str(out4["execution"]["output"]))

    def test_city_timezone_resolver(self):
        from winbrow.agent import _city_timezone
        self.assertEqual(_city_timezone("tokyo"), "Asia/Tokyo")
        self.assertEqual(_city_timezone("New York"), "America/New_York")
        self.assertEqual(_city_timezone("london"), "Europe/London")
        self.assertEqual(_city_timezone("atlantis"), None)

    # wikipedia search has a cold-start flake (slow/empty first calls after
    # the stdio server boots): the shortcut retries ONE time when the first
    # resultset is empty
    _WIKI_EMPTY = json.dumps({"query": "alan turing", "results": [],
                              "status": "no_results"})
    _WIKI_HIT = json.dumps({"query": "alan turing",
                            "results": [{"title": "Alan Turing",
                                         "snippet": "codebreaker"}],
                            "status": "success", "count": 1})

    def test_wikipedia_empty_search_retries_once(self):
        import winbrow.mcp as m
        fixtures = {"wikipedia": {
            "tools": SCHEMA_WIKI["tools"],
            "call": {"search_wikipedia": {
                "sequence": [self._WIKI_EMPTY, self._WIKI_HIT]}}}}
        ag = self._agent_with(fixtures)
        out = run(ag.execute("search wikipedia for alan turing", session_id="m6sc"))
        self.assertEqual(out["route"]["tier"], "mcp_shortcut")
        self.assertIn("codebreaker", str(out["execution"]["output"]))
        hub = m._hubs[str(m.CONFIG_PATH)]
        # exactly two calls: the empty one + ONE retry (never three+)
        self.assertEqual(hub.seqs[("wikipedia", "search_wikipedia")], 2)

    def test_wikipedia_retry_keeps_empty_when_retry_also_empty(self):
        fixtures = {"wikipedia": {
            "tools": SCHEMA_WIKI["tools"],
            "call": {"search_wikipedia": {"sequence": [self._WIKI_EMPTY]}}}}
        ag = self._agent_with(fixtures)
        out = run(ag.execute("search wikipedia for alan turing", session_id="m6sc"))
        self.assertEqual(out["route"]["tier"], "mcp_shortcut")
        out_text = str(out["execution"]["output"])
        self.assertIn("no_results", out_text)
        import winbrow.mcp as m
        hub = m._hubs[str(m.CONFIG_PATH)]
        self.assertEqual(hub.seqs[("wikipedia", "search_wikipedia")], 2)

    def test_wikipedia_hit_not_retried(self):
        fixtures = {"wikipedia": {
            "tools": SCHEMA_WIKI["tools"],
            "call": {"search_wikipedia": {"sequence": [self._WIKI_HIT]}}}}
        ag = self._agent_with(fixtures)
        run(ag.execute("search wikipedia for alan turing", session_id="m6sc"))
        import winbrow.mcp as m
        hub = m._hubs[str(m.CONFIG_PATH)]
        self.assertEqual(hub.seqs[("wikipedia", "search_wikipedia")], 1)


class TestWarmup(unittest.TestCase):
    """Cold-start flake healer: hidden background probes after connect."""

    def setUp(self):
        self.tmp = Path(os.environ.get("TEMP", ".")) / f"mcp_wu_{os.getpid()}"
        self.tmp.mkdir(exist_ok=True)
        for stale in self.tmp.glob("mcp_cache.json"):
            stale.unlink()

    def test_warm_server_probes_until_success(self):
        import winbrow.mcp as m
        empty = json.dumps({"results": [], "status": "no_results"})
        hit = json.dumps({"results": [{"title": "x"}], "status": "success"})
        hub = make_hub(self.tmp, {"wikipedia": {
            "tools": SCHEMA_WIKI["tools"],
            "call": {"search_wikipedia": {"sequence": [empty, empty, hit]}}}})
        spec = next(s for s in hub.specs() if s.id == "wikipedia")
        hub._connect_sync(spec)
        m._warmed_ids.discard("wikipedia")
        try:
            m._warm_server(hub, spec)          # synchronous probe loop
        finally:
            m._warmed_ids.add("wikipedia")
        self.assertEqual(hub.seqs[("wikipedia", "search_wikipedia")], 3)

    def test_warm_server_only_warms_once(self):
        import winbrow.mcp as m
        hit = json.dumps({"results": [{"title": "x"}], "status": "success"})
        hub = make_hub(self.tmp, {"wikipedia": {
            "tools": SCHEMA_WIKI["tools"],
            "call": {"search_wikipedia": {"sequence": [hit]}}}})
        spec = next(s for s in hub.specs() if s.id == "wikipedia")
        m._warmed_ids.discard("wikipedia")
        try:
            m._warm_server(hub, spec)              # first: marks + probes
            self.assertEqual(hub.seqs[("wikipedia", "search_wikipedia")], 1)
            m._warm_server(hub, spec)              # second: guard exits
        finally:
            m._warmed_ids.add("wikipedia")
        self.assertEqual(hub.seqs[("wikipedia", "search_wikipedia")], 1)

    def test_connect_spawns_warmup_thread_for_wikipedia(self):
        import unittest.mock
        import winbrow.mcp as m
        calls = []
        with unittest.mock.patch.object(m, "_warm_server",
                                        lambda hub, spec: calls.append(spec.id)):
            hub = make_hub(self.tmp, {"wikipedia": SCHEMA_WIKI,
                                      "git": {"tools": [
                                          {"name": "git_status", "description": "d",
                                           "inputSchema": {}}]}})
            for sid in ("wikipedia", "git"):
                spec = next(s for s in hub.specs() if s.id == sid)
                run(hub.connect(spec))
        for _ in range(50):
            time.sleep(0.02)
            if calls:
                break
        self.assertEqual(calls, ["wikipedia"])

    def test_warmup_thread_on_lazy_first_call(self):
        """First ghost call goes through _call_sync's lazy connect — the
        warmup must spawn there too (not only from connect())."""
        import unittest.mock
        import winbrow.mcp as m
        calls = []
        with unittest.mock.patch.object(m, "_warm_server",
                                        lambda hub, spec: calls.append(spec.id)):
            hub = make_hub(self.tmp, {"wikipedia": SCHEMA_WIKI})
            run(hub.call("wikipedia", "search_wikipedia", {"query": "x"}))
        for _ in range(50):
            time.sleep(0.02)
            if calls:
                break
        self.assertEqual(calls, ["wikipedia"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
