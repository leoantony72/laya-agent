"""
Browser Task Automation for WinBrow
====================================
Windows-native browser automation layer.
Drives Chrome/Edge via:
  1. Chrome DevTools Protocol (CDP) over WebSocket for deep tab control
  2. PowerShell + WScript.Shell for keyboard shortcut injection
  3. URL navigation via Start-Process
  4. Win32 window handle manipulation via ctypes

Supports:
  - Tab management (open, close, switch, list)
  - URL navigation and search
  - JavaScript injection and DOM reading
  - Screenshot via CDP Page.captureScreenshot
  - Multi-step browser flows (scroll, click, type, wait)
  - Address bar focus & URL reading
  - Bookmark, history, downloads
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

from .windows import run_powershell

log = logging.getLogger("winbrow.browser")

# ────────────────────────────────────────────────────────────────────────────
# Chrome DevTools Protocol helpers
# ────────────────────────────────────────────────────────────────────────────

_CDP_PORT = 9222  # launch Chrome/Edge with --remote-debugging-port=9222


def _cdp_request(path: str, data: Optional[dict] = None) -> Any:
    """Synchronous HTTP request to the local CDP endpoint."""
    url = f"http://localhost:{_CDP_PORT}{path}"
    try:
        if data is not None:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode(),
                headers={"Content-Type": "application/json"},
            )
        else:
            req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _cdp_available() -> bool:
    return _cdp_request("/json/version") is not None


async def _cdp_send(session_id: str, method: str, params: dict = None) -> dict:
    """Send a CDP command via the /json/protocol HTTP interface (no WS needed)."""
    # We use the simpler HTTP-based activate/navigate API where possible
    pass


# ────────────────────────────────────────────────────────────────────────────
# Browser Controller
# ────────────────────────────────────────────────────────────────────────────

class BrowserController:
    """
    Controls Chrome / Edge on Windows.
    Falls back gracefully when CDP is not available.
    """

    def __init__(self, preferred_browser: str = "auto"):
        # "auto" picks whichever is running; can override to "chrome" or "edge"
        self.preferred_browser = preferred_browser

    # ── Detection ──────────────────────────────────────────────────────────

    async def get_running_browser(self) -> str:
        """Return name of whichever browser is currently running."""
        script = """
        $browsers = @('chrome', 'msedge', 'firefox')
        foreach ($b in $browsers) {
            if (Get-Process -Name $b -ErrorAction SilentlyContinue) {
                Write-Output $b; break
            }
        }
        """
        out = await run_powershell(script)
        name = (out.get("stdout") or "").strip().lower()
        return name or "chrome"

    async def ensure_browser_open(self) -> dict[str, Any]:
        """Make sure a browser window is visible and focused (single call)."""
        script = """
        $browser = $null
        foreach ($b in @('chrome', 'msedge', 'firefox')) {
            if (Get-Process -Name $b -ErrorAction SilentlyContinue) { $browser = $b; break }
        }
        if (-not $browser) { $browser = "chrome" }
        $proc = Get-Process -Name $browser -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($proc -and $proc.MainWindowHandle -ne 0) {
            $ws = New-Object -ComObject WScript.Shell
            $ws.AppActivate($proc.Id) | Out-Null
            return "Focused $browser"
        } else {
            Start-Process $browser
            return "Launched $browser"
        }
        """
        return await run_powershell(script)

    # ── CDP Tab Operations ─────────────────────────────────────────────────

    async def list_tabs(self) -> dict[str, Any]:
        """List all open browser tabs via CDP."""
        loop = asyncio.get_event_loop()
        tabs = await loop.run_in_executor(None, lambda: _cdp_request("/json/list"))
        if tabs is None:
            # Fallback: use PowerShell to query window titles
            script = """
            $procs = Get-Process | Where-Object { $_.ProcessName -in @('chrome','msedge','firefox') -and $_.MainWindowTitle -ne '' }
            $procs | ForEach-Object { Write-Output "[$($_.ProcessName)] $($_.MainWindowTitle)" }
            """
            out = await run_powershell(script)
            return {
                "success": True,
                "cdp_available": False,
                "tabs": (out.get("stdout") or "No browser windows found"),
            }
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        return {
            "success": True,
            "cdp_available": True,
            "tab_count": len(page_tabs),
            "tabs": [{"id": t["id"], "title": t.get("title", ""), "url": t.get("url", "")} for t in page_tabs],
        }

    async def navigate_to(self, url: str, new_tab: bool = False) -> dict[str, Any]:
        """Navigate the active tab to a URL, or open a new tab first."""
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url

        # Try CDP first (fastest, no focus switch needed)
        loop = asyncio.get_event_loop()
        tabs = await loop.run_in_executor(None, lambda: _cdp_request("/json/list"))
        if tabs:
            page_tabs = [t for t in tabs if t.get("type") == "page"]
            if page_tabs:
                target_id = page_tabs[0]["id"]
                if new_tab:
                    new_tab_resp = await loop.run_in_executor(
                        None, lambda: _cdp_request("/json/new", {"url": url})
                    )
                    if new_tab_resp:
                        return {"success": True, "method": "cdp_new_tab", "url": url}
                else:
                    activate = await loop.run_in_executor(
                        None, lambda: _cdp_request(f"/json/activate/{target_id}")
                    )
                    # Navigate via address bar shortcut
                    script = f"""
                    $ws = New-Object -ComObject WScript.Shell
                    $ws.SendKeys("^l")
                    Start-Sleep -Milliseconds 200
                    $ws.SendKeys("{url}")
                    $ws.SendKeys("{{ENTER}}")
                    return "Navigated to {url}"
                    """
                    return await run_powershell(script)

        # Pure PowerShell fallback
        safe_url = url.replace('"', '%22')
        if new_tab:
            script = f"""
            $ws = New-Object -ComObject WScript.Shell
            $ws.SendKeys("^t")
            Start-Sleep -Milliseconds 400
            $ws.SendKeys("^l")
            Start-Sleep -Milliseconds 200
            Add-Type -AssemblyName System.Windows.Forms
            [System.Windows.Forms.Clipboard]::SetText("{safe_url}")
            $ws.SendKeys("^v")
            $ws.SendKeys("{{ENTER}}")
            return "Opened new tab and navigated to {url}"
            """
        else:
            script = f'Start-Process "{safe_url}"; return "Opened {url}"'
        return await run_powershell(script)

    async def open_url(self, url: str, new_window: bool = False) -> dict[str, Any]:
        """Open a URL (alias for navigate_to)."""
        return await self.navigate_to(url, new_tab=new_window)

    async def search_web(self, query: str, engine: str = "google") -> dict[str, Any]:
        """Search the web using a given query and search engine."""
        enc = urllib.parse.quote_plus(query)
        ENGINES = {
            "google": f"https://www.google.com/search?q={enc}",
            "bing": f"https://www.bing.com/search?q={enc}",
            "youtube": f"https://www.youtube.com/results?search_query={enc}",
            "github": f"https://github.com/search?q={enc}&type=repositories",
            "stackoverflow": f"https://stackoverflow.com/search?q={enc}",
            "reddit": f"https://www.reddit.com/search/?q={enc}",
            "wikipedia": f"https://en.wikipedia.org/w/index.php?search={enc}",
            "maps": f"https://maps.google.com/maps?q={enc}",
            "amazon": f"https://www.amazon.com/s?k={enc}",
        }
        url = ENGINES.get(engine.lower(), ENGINES["google"])
        result = await self.navigate_to(url)
        result["query"] = query
        result["engine"] = engine
        return result

    async def close_current_tab(self) -> dict[str, Any]:
        """Close the active browser tab."""
        script = """
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("^w")
        return "Closed active tab"
        """
        return await run_powershell(script)

    async def new_tab(self) -> dict[str, Any]:
        """Open a blank new tab."""
        script = """
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("^t")
        return "Opened new tab"
        """
        return await run_powershell(script)

    async def switch_tab(self, direction: str = "next") -> dict[str, Any]:
        """Switch between browser tabs."""
        key = "^{TAB}" if direction == "next" else "^+{TAB}"
        script = f"""
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("{key}")
        return "Switched to {direction} tab"
        """
        return await run_powershell(script)

    async def switch_tab_by_number(self, num: int) -> dict[str, Any]:
        """Switch to tab by number (1-8)."""
        n = max(1, min(8, num))
        script = f"""
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("^{n}")
        return "Switched to tab {n}"
        """
        return await run_powershell(script)

    async def reload_page(self, hard: bool = False) -> dict[str, Any]:
        """Reload the current page (hard reload clears cache)."""
        key = "^{F5}" if hard else "{F5}"
        script = f"""
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("{key}")
        return "{'Hard reloaded' if hard else 'Reloaded'} page"
        """
        return await run_powershell(script)

    async def go_back(self) -> dict[str, Any]:
        """Navigate browser history backward."""
        script = """
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("%{LEFT}")
        return "Navigated back"
        """
        return await run_powershell(script)

    async def go_forward(self) -> dict[str, Any]:
        """Navigate browser history forward."""
        script = """
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("%{RIGHT}")
        return "Navigated forward"
        """
        return await run_powershell(script)

    async def get_current_url(self) -> dict[str, Any]:
        """Get the URL of the active browser tab."""
        # Try CDP first
        loop = asyncio.get_event_loop()
        tabs = await loop.run_in_executor(None, lambda: _cdp_request("/json/list"))
        if tabs:
            page_tabs = [t for t in tabs if t.get("type") == "page"]
            if page_tabs:
                return {
                    "success": True,
                    "url": page_tabs[0].get("url", ""),
                    "title": page_tabs[0].get("title", ""),
                    "method": "cdp",
                }
        # Fallback: focus address bar and read via clipboard
        script = """
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("^l")
        Start-Sleep -Milliseconds 300
        $ws.SendKeys("^c")
        Start-Sleep -Milliseconds 200
        $ws.SendKeys("{ESCAPE}")
        $url = Get-Clipboard
        return "Current URL: $url"
        """
        out = await run_powershell(script)
        url_match = re.search(r"https?://[^\s]+", out.get("stdout", ""))
        return {
            "success": True,
            "url": url_match.group(0) if url_match else "Unknown",
            "method": "clipboard",
        }

    async def screenshot(self, save_path: Optional[str] = None) -> dict[str, Any]:
        """Capture screenshot of the active browser tab via CDP."""
        loop = asyncio.get_event_loop()
        tabs = await loop.run_in_executor(None, lambda: _cdp_request("/json/list"))
        if not tabs:
            return {"success": False, "error": "CDP not available. Launch browser with --remote-debugging-port=9222"}

        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if not page_tabs:
            return {"success": False, "error": "No page tabs found"}

        # Use the /json/protocol approach - capture via DevTools
        ws_url = page_tabs[0].get("webSocketDebuggerUrl", "")
        if not ws_url:
            return {"success": False, "error": "No WebSocket debugger URL"}

        try:
            import websockets
            async with websockets.connect(ws_url, ping_interval=None) as ws:
                cmd = {"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png", "quality": 85}}
                await ws.send(json.dumps(cmd))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                img_data = resp.get("result", {}).get("data", "")
                if img_data:
                    if save_path:
                        import os
                        os.makedirs(os.path.dirname(save_path), exist_ok=True)
                        with open(save_path, "wb") as f:
                            f.write(base64.b64decode(img_data))
                        return {"success": True, "path": save_path, "method": "cdp_ws"}
                    return {"success": True, "data_len": len(img_data), "method": "cdp_ws"}
        except ImportError:
            pass  # websockets not installed, fall back
        except Exception as e:
            log.warning(f"CDP screenshot failed: {e}")

        # Fallback: PowerShell PrintScreen
        save_path = save_path or r"C:\Users\Public\winbrow_screenshot.png"
        script = f"""
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
        $bmp = New-Object System.Drawing.Bitmap($screen.Width, $screen.Height)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)
        $bmp.Save("{save_path}")
        $g.Dispose(); $bmp.Dispose()
        return "Screenshot saved to {save_path}"
        """
        result = await run_powershell(script)
        result["path"] = save_path
        return result

    async def execute_js(self, code: str) -> dict[str, Any]:
        """Execute JavaScript in the active tab via CDP."""
        loop = asyncio.get_event_loop()
        tabs = await loop.run_in_executor(None, lambda: _cdp_request("/json/list"))
        if not tabs:
            return {"success": False, "error": "CDP not available"}
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if not page_tabs:
            return {"success": False, "error": "No page tabs found"}
        ws_url = page_tabs[0].get("webSocketDebuggerUrl", "")
        try:
            import websockets
            async with websockets.connect(ws_url, ping_interval=None) as ws:
                cmd = {"id": 1, "method": "Runtime.evaluate", "params": {"expression": code, "returnByValue": True}}
                await ws.send(json.dumps(cmd))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                result = resp.get("result", {}).get("result", {})
                return {"success": True, "value": result.get("value"), "type": result.get("type")}
        except ImportError:
            return {"success": False, "error": "websockets package not installed. Run: pip install websockets"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def scroll_page(self, direction: str = "down", amount: int = 3) -> dict[str, Any]:
        """Scroll the page up or down."""
        key = "{PGDN}" if direction == "down" else "{PGUP}"
        script_parts = [
            """$ws = New-Object -ComObject WScript.Shell"""
        ]
        for _ in range(amount):
            script_parts.append(f'$ws.SendKeys("{key}")')
            script_parts.append("Start-Sleep -Milliseconds 100")
        script_parts.append(f'return "Scrolled {direction} {amount} times"')
        return await run_powershell("\n".join(script_parts))

    async def find_on_page(self, text: str) -> dict[str, Any]:
        """Open browser's find-in-page with search text."""
        script = f"""
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("^f")
        Start-Sleep -Milliseconds 400
        $ws.SendKeys("{text}")
        return "Searching page for: {text}"
        """
        return await run_powershell(script)

    async def zoom(self, action: str = "reset") -> dict[str, Any]:
        """Zoom browser in, out, or reset."""
        KEYS = {"in": "^{+}", "out": "^{-}", "reset": "^0"}
        key = KEYS.get(action, "^0")
        script = f"""
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("{key}")
        return "Zoom {action}"
        """
        return await run_powershell(script)

    async def open_devtools(self) -> dict[str, Any]:
        """Open browser DevTools."""
        script = """
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("{F12}")
        return "Opened DevTools"
        """
        return await run_powershell(script)

    async def bookmark_page(self) -> dict[str, Any]:
        """Bookmark the current page."""
        script = """
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("^d")
        Start-Sleep -Milliseconds 400
        $ws.SendKeys("{ENTER}")
        return "Page bookmarked"
        """
        return await run_powershell(script)

    async def open_incognito(self, url: str = "") -> dict[str, Any]:
        """Open a URL in an incognito/private window."""
        browser = await self.get_running_browser()
        if browser == "msedge":
            flag = "--inprivate"
        else:
            flag = "--incognito"
        target = f'"{url}"' if url else ""
        script = f"""
        $exePath = (Get-Process -Name "{browser}" -ErrorAction SilentlyContinue | Select-Object -First 1).Path
        if ($exePath) {{
            Start-Process $exePath -ArgumentList "{flag} {target}"
            return "Opened incognito window"
        }} else {{
            Start-Process "{browser}" -ArgumentList "{flag} {target}"
            return "Opened incognito window"
        }}
        """
        return await run_powershell(script)

    async def browser_shortcut(self, action: str) -> dict[str, Any]:
        """Send browser keyboard shortcut."""
        SHORTCUTS = {
            "new_tab": "^t",
            "close_tab": "^w",
            "reopen_tab": "^+t",
            "next_tab": "^{TAB}",
            "prev_tab": "^+{TAB}",
            "reload": "{F5}",
            "hard_reload": "^{F5}",
            "bookmark": "^d",
            "history": "^h",
            "downloads": "^j",
            "zoom_in": "^{+}",
            "zoom_out": "^{-}",
            "zoom_reset": "^0",
            "address_bar": "^l",
            "settings": "chrome://settings/",
            "extensions": "chrome://extensions/",
            "incognito": "^+n",
            "fullscreen": "{F11}",
            "developer_tools": "{F12}",
            "source": "^u",
            "print": "^p",
            "save": "^s",
            "find": "^f",
            "select_all": "^a",
        }
        key = SHORTCUTS.get(action)
        if not key:
            return {"success": False, "error": f"Unknown browser shortcut: {action}"}
        if key.startswith("chrome://") or key.startswith("edge://"):
            return await self.navigate_to(key)
        script = f"""
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("{key}")
        return "Executed browser shortcut: {action}"
        """
        return await run_powershell(script)

    async def launch_with_cdp(self, url: str = "") -> dict[str, Any]:
        """
        Launch Chrome/Edge with CDP remote debugging enabled.
        This enables advanced automation (JS execution, screenshots, tab control).
        """
        target_url = f'"{url}"' if url else '""'
        script = f"""
        $chromePaths = @(
            "$env:ProgramFiles\\Google\\Chrome\\Application\\chrome.exe",
            "$env:ProgramFiles(x86)\\Google\\Chrome\\Application\\chrome.exe",
            "$env:LOCALAPPDATA\\Google\\Chrome\\Application\\chrome.exe"
        )
        $edgePaths = @(
            "$env:ProgramFiles\\Microsoft\\Edge\\Application\\msedge.exe",
            "$env:ProgramFiles(x86)\\Microsoft\\Edge\\Application\\msedge.exe"
        )
        $found = $null
        foreach ($p in $chromePaths) {{ if (Test-Path $p) {{ $found = $p; break }} }}
        if (-not $found) {{
            foreach ($p in $edgePaths) {{ if (Test-Path $p) {{ $found = $p; break }} }}
        }}
        if ($found) {{
            Start-Process $found -ArgumentList "--remote-debugging-port=9222 {target_url}"
            Start-Sleep -Milliseconds 1500
            return "Launched browser with CDP on port 9222. Path: $found"
        }} else {{
            return "ERROR: Could not find Chrome or Edge executable"
        }}
        """
        return await run_powershell(script)

    async def read_page_text(self) -> dict[str, Any]:
        """Read the visible text content of the current page via CDP."""
        code = """
        Array.from(document.querySelectorAll('h1,h2,h3,p,li,td,th,span,a'))
          .map(e => e.innerText.trim())
          .filter(t => t.length > 10)
          .slice(0, 50)
          .join('\\n')
        """
        result = await self.execute_js(code)
        if result.get("success"):
            return {"success": True, "text": result.get("value", ""), "method": "cdp"}
        return {"success": False, "error": result.get("error", "CDP unavailable")}

    async def click_element(self, selector: str) -> dict[str, Any]:
        """Click a DOM element by CSS selector via CDP."""
        code = f"""
        const el = document.querySelector('{selector}');
        if (el) {{ el.click(); 'Clicked: {selector}' }} else {{ 'Element not found: {selector}' }}
        """
        return await self.execute_js(code)

    async def type_in_element(self, selector: str, text: str) -> dict[str, Any]:
        """Type text into a DOM input element via CDP."""
        safe_text = text.replace("'", "\\'").replace('"', '\\"')
        code = f"""
        const el = document.querySelector('{selector}');
        if (el) {{
            el.focus();
            el.value = '{safe_text}';
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            '{safe_text}'
        }} else {{ 'Element not found' }}
        """
        return await self.execute_js(code)

    async def fill_and_submit_form(self, fields: dict[str, str], submit_selector: str = "") -> dict[str, Any]:
        """Fill multiple form fields and optionally submit."""
        results = []
        for selector, value in fields.items():
            r = await self.type_in_element(selector, value)
            results.append({"selector": selector, "result": r})
        if submit_selector:
            submit_r = await self.click_element(submit_selector)
            results.append({"selector": submit_selector, "result": submit_r})
        return {"success": True, "steps": results}

    async def wait_for_load(self, timeout_ms: int = 3000) -> dict[str, Any]:
        """Wait for the page to finish loading."""
        await asyncio.sleep(timeout_ms / 1000)
        code = "document.readyState"
        result = await self.execute_js(code)
        return {"success": True, "ready_state": result.get("value", "unknown")}
