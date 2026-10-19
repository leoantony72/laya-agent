import asyncio
import time

from winbrow.mcp import McpHub


async def main():
    hub = McpHub()
    spec = next(s for s in hub.specs() if s.id == "wikipedia")
    conn = await hub.connect(spec)
    print("connected:", bool(conn and not conn.error), "| err:", conn.error)
    names = [t["name"] for t in conn.tools]
    print("tools:", names)
    tool = next(n for n in names if "search" in n)

    queries = ["alan turing", "alan turing", "python programming language",
               "alan turing", "tokyo", "alan turing"]
    for q in queries:
        t0 = time.perf_counter()
        res = await hub.call("wikipedia", tool, {"query": q})
        dt = (time.perf_counter() - t0) * 1000
        out = res.get("output", "")
        n, status = -1, "?"
        try:
            import json
            data = json.loads(out)
            n = len(data.get("results", []))
            status = data.get("status", "?")
        except Exception:
            status = out[:60]
        print(f"{dt:7.0f} ms  {status!s:12} results={n:2}  {q!r}")


asyncio.run(main())
