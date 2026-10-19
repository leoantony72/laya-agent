"""Ctrl+C shutdown repro: open a WS, send CTRL_C_EVENT to the process group,
assert the server process exits within the graceful bound (was: never)."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request as ur
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PORT = 8765
try:
    ur.urlopen(f"http://127.0.0.1:{PORT}/api/system", timeout=3)
    print("STALE server already running — aborting")
    sys.exit(2)
except Exception:
    pass

proc = subprocess.Popen(
    [sys.executable, "server.py"],
    cwd=str(Path(__file__).resolve().parent),
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    encoding="utf-8", errors="replace",
)
print("server pid:", proc.pid)

live = False
for _ in range(90):
    time.sleep(1)
    try:
        ur.urlopen(f"http://127.0.0.1:{PORT}/api/system", timeout=2)
        live = True
        break
    except Exception:
        pass
if not live:
    print("server never became live")
    proc.kill()
    sys.exit(1)
print("server live")


async def _ws():
    import websockets
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws") as ws:
        try:
            await ws.send(json.dumps({"type": "ping"}))
            return "opened"
        except Exception as e:
            return f"open-failed {e.__class__.__name__}"


loop = asyncio.new_event_loop()
try:
    asyncio.set_event_loop(loop)
    print("ws:", loop.run_until_complete(_ws()))
except Exception as e:
    print(f"ws open: {e.__class__.__name__}: {e}")
finally:
    loop.close()

print("Ctrl+C now (process-group event)...")
t0 = time.perf_counter()
os.kill(proc.pid, signal.CTRL_C_EVENT)
try:
    code = proc.wait(timeout=30)
    dt = time.perf_counter() - t0
    print(f"PROCESS EXITED code={code} in {dt:.1f}s "
          f"({'FIXED' if dt <= 15 else 'STILL SLOW'})")
except subprocess.TimeoutExpired:
    print("PROCESS STILL ALIVE AFTER 30s — NOT FIXED")
    proc.kill()
    sys.exit(1)
