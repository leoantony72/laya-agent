import urllib.request
import json

def test_api(cmd):
    data = json.dumps({'command': cmd}).encode('utf-8')
    req = urllib.request.Request('http://127.0.0.1:8765/api/execute', data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req) as resp:
            res = json.loads(resp.read().decode())
            print(f"COMMAND: {cmd}")
            route = res.get("route", {})
            print(f"ROUTE KIND: {route.get('kind')} | TOOL: {route.get('tool_name')} | CONF: {route.get('confidence')}")
            exec_info = res.get("execution", {})
            print(f"EXEC TYPE: {exec_info.get('type')}")
            print(f"SUCCESS: {exec_info.get('success')}")
            print(f"OUTPUT:\n{str(exec_info.get('output', ''))[:300]}")
            print(f"TOTAL LATENCY: {res.get('total_elapsed_ms')}ms")
            print("=" * 60)
            return res
    except Exception as e:
        print(f"Error testing {cmd}: {e}")

if __name__ == "__main__":
    test_api("turn on dark mode")
    test_api("get system uptime")
    test_api("show battery health")
    test_api("show my wifi password")
