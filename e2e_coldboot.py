import json
import subprocess
import sys
import time
import urllib.request

PORT = 8899
BASE = f"http://127.0.0.1:{PORT}"

proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
     "--port", str(PORT), "--log-level", "warning"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

up = False
for _ in range(60):
    time.sleep(0.5)
    try:
        with urllib.request.urlopen(f"{BASE}/api/session", timeout=2) as r:
            r.read()
        up = True
        break
    except Exception:
        continue
print("server up:", up)

def utt(text, timeout=90):
    body = json.dumps({"command": text, "session_id": "coldboot"}).encode()
    req = urllib.request.Request(f"{BASE}/api/execute", body,
                                 {"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read().decode("utf-8"))
    ms = (time.perf_counter() - t0) * 1000
    ex = res.get("execution", {})
    out = str(ex.get("output", ""))[:110]
    n = -2
    st = "?"
    try:
        d = json.loads(out if len(out) < 110 else str(ex.get("output")))
        n = len(d.get("results", []))
        st = d.get("status", "?")
    except Exception:
        pass
    print(f"{ms:7.0f} ms success={ex.get('success')} status={st} results={n}")
    print("   ", out)

try:
    print("— cold boot, immediate search —")
    utt("search wikipedia for alan turing")
    time.sleep(1.0)
    print("— second search (warm) —")
    utt("search wikipedia for grace hopper")
finally:
    proc.terminate()
print("done")
