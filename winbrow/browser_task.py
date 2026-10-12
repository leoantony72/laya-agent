"""
Browser Task Automation for WinBrow
====================================
Windows-native browser automation layer.
Drives the user's default browser (Brave/Chrome/Edge, auto-detected) via:
  1. Chrome DevTools Protocol (CDP) over WebSocket for deep tab control
     - legacy discovery: HTTP /json/* (separate-profile windows)
     - consent mode: DevToolsActivePort + one persistent browser WebSocket
       (brave/chrome://inspect/#remote-debugging "Allow remote debugging"),
       so the agent controls the SAME window with the user's real profile
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
  - One-time remote-debugging opt-in guidance when CDP is not enabled yet
  - WINBROW_BROWSER_MODE=separate to opt back into a throwaway profile window
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from typing import Any, Optional

try:
    import winreg  # default-browser detection (Windows only)
except ImportError:  # pragma: no cover
    winreg = None

from .windows import run_powershell

log = logging.getLogger("winbrow.browser")

# ────────────────────────────────────────────────────────────────────────────
# Chrome DevTools Protocol helpers
# ────────────────────────────────────────────────────────────────────────────

_CDP_PORT = 9222  # launch Chrome/Edge with --remote-debugging-port=9222

# ────────────────────────────────────────────────────────────────────────────
# Browser facts (default-browser aware)
# ────────────────────────────────────────────────────────────────────────────

_PROGID_PREFIXES = {
    "brave": ("bravehtml",),
    "chrome": ("chromehtml", "chromehhtm", "chromeurl"),
    "msedge": ("msedgehtm", "edgehtm", "microsoftedge"),
}


def get_default_browser() -> str:
    """Canonical name ('brave' | 'chrome' | 'msedge') of the default HTTPS handler."""
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as key:
            progid, _ = winreg.QueryValueEx(key, "ProgId")
    except OSError:
        return ""
    p = (progid or "").lower()
    for name, prefixes in _PROGID_PREFIXES.items():
        if p.startswith(prefixes):
            return name
    return ""


def _exe_candidates(name: str) -> list[str]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    la = os.environ.get("LOCALAPPDATA", "")
    tables = {
        "brave": [
            rf"{la}\BraveSoftware\Brave-Browser\Application\brave.exe",
            rf"{pf}\BraveSoftware\Brave-Browser\Application\brave.exe",
            rf"{pf86}\BraveSoftware\Brave-Browser\Application\brave.exe",
        ],
        "chrome": [
            rf"{la}\Google\Chrome\Application\chrome.exe",
            rf"{pf}\Google\Chrome\Application\chrome.exe",
            rf"{pf86}\Google\Chrome\Application\chrome.exe",
        ],
        "msedge": [
            rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
            rf"{pf}\Microsoft\Edge\Application\msedge.exe",
        ],
    }
    return tables.get(name, [])


def _find_browser_exe(name: str) -> str:
    for path in _exe_candidates(name):
        if path and os.path.isfile(path):
            return path
    return ""


def _user_data_dir(name: str) -> str:
    la = os.environ.get("LOCALAPPDATA", "")
    return {
        "brave": rf"{la}\BraveSoftware\Brave-Browser\User Data",
        "chrome": rf"{la}\Google\Chrome\User Data",
        "msedge": rf"{la}\Microsoft\Edge\User Data",
    }.get(name, "")


def _inspect_page(name: str) -> str:
    """Page with the built-in 'Allow remote debugging' opt-in (Chromium 144+)."""
    return {
        "brave": "brave://inspect/#remote-debugging",
        "chrome": "chrome://inspect/#remote-debugging",
        "msedge": "edge://inspect/#remote-debugging",
    }.get(name, "")


def _display_name(name: str) -> str:
    return {"brave": "Brave", "chrome": "Chrome", "msedge": "Edge"}.get(name, name)


def _process_running(name: str) -> bool:
    try:
        import psutil
        wanted = {name.lower(), f"{name.lower()}.exe"}
        return any(p.name().lower() in wanted for p in psutil.process_iter(["name"]))
    except Exception:
        return False


def _browser_order() -> list[str]:
    """Default browser first, then the rest."""
    order = ["brave", "chrome", "msedge"]
    default = get_default_browser()
    if default in order:
        order = [default] + [b for b in order if b != default]
    return order


def _running_browser() -> str:
    """Canonical name of a running Chromium browser ('' if none), default preferred."""
    for name in _browser_order():
        if _process_running(name):
            return name
    return ""


def _setup_hint(name: str = "") -> str:
    name = name or get_default_browser() or _running_browser() or "your browser"
    page = _inspect_page(name)
    display = _display_name(name)
    return (
        f"One-time setup: open {page} in {display} and tick 'Allow remote debugging "
        "for this browser instance'. The setting persists across restarts and "
        "laya will then control this same window with your profile."
    )


def _patch_local_state_enable(name: str) -> bool:
    """Enable remote debugging by writing the opt-in pref into Local State.

    Chromium persists the inspect-page opt-in as
    `devtools.remote_debugging.user-enabled` in the profile root's Local State
    file. Writing it while the browser is CLOSED makes the next launch start
    the debugging server on the real profile with zero user interaction.
    """
    if _process_running(name):
        return False  # the running browser would clobber the file on exit
    state_path = os.path.join(_user_data_dir(name), "Local State")
    if not os.path.isfile(state_path):
        return False
    try:
        with open(state_path, "r", encoding="utf-8-sig") as fh:
            state = json.load(fh)
        rd = state.setdefault("devtools", {}).setdefault("remote_debugging", {})
        if rd.get("user-enabled") is True:
            return True  # already opted in
        rd["user-enabled"] = True
        tmp_path = state_path + ".winbrow.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(tmp_path, state_path)
        log.info(f"Enabled remote-debugging opt-in in {state_path}")
        return True
    except Exception as e:
        log.warning(f"Could not patch Local State for {name}: {e}")
        return False


# ────────────────────────────────────────────────────────────────────────────
# Chrome DevTools Protocol helpers
# ────────────────────────────────────────────────────────────────────────────

def _cdp_status(path: str, timeout: float = 2.0) -> Optional[int]:
    """HTTP status of a local CDP endpoint; None when nothing is listening.

    In consent mode (see below) discovery endpoints answer 404 — that still
    proves the debugging server is up, which is why status is returned rather
    than a parsed body.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{_CDP_PORT}{path}", timeout=timeout
        ) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def _cdp_request(path: str, data: Optional[dict] = None) -> Any:
    """Synchronous HTTP request to the local CDP endpoint (legacy discovery)."""
    url = f"http://127.0.0.1:{_CDP_PORT}{path}"
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


def _devtools_ws_endpoint() -> Optional[str]:
    """Browser-level ws:// endpoint from DevToolsActivePort (consent mode).

    Chromium's opt-in remote debugging (brave/chrome://inspect/#remote-debugging)
    disables the HTTP /json endpoints and writes port + ws path to this file in
    the profile root instead.
    """
    for name in _browser_order():
        port_file = os.path.join(_user_data_dir(name), "DevToolsActivePort")
        if not os.path.isfile(port_file):
            continue
        try:
            with open(port_file, "r", encoding="ascii", errors="ignore") as fh:
                lines = [ln.strip() for ln in fh.readlines()[:2]]
            if len(lines) < 2:
                continue
            port, path = int(lines[0]), lines[1]
        except (OSError, ValueError):
            continue
        try:  # the file can be stale after a browser exit
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                pass
        except OSError:
            continue
        if not path.startswith("/"):
            path = "/" + path
        return f"ws://127.0.0.1:{port}{path}"
    return None


def _cdp_backend() -> Optional[str]:
    """'http' (legacy /json discovery), 'ws' (consent mode), or None."""
    if _cdp_status("/json/version") == 200:
        return "http"
    if _devtools_ws_endpoint():
        return "ws"
    return None


def _cdp_available() -> bool:
    return _cdp_backend() is not None


class _BrowserCDP:
    """Persistent browser-level WebSocket for consent mode.

    When remote debugging is enabled through the browser's opt-in page, every
    NEW WebSocket connection triggers an 'Allow?' prompt in the browser and the
    HTTP /json endpoints answer 404. So we keep exactly one browser-level socket
    open, answer the prompt once, and multiplex page commands over it using
    Target.attachToTarget sessions.
    """

    def __init__(self) -> None:
        self._ws: Any = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._reader: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._sessions: dict[str, str] = {}

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def ensure(self, timeout: float = 30.0) -> None:
        """Connect, waiting up to `timeout` for the user to click Allow."""
        if self._ws is not None:
            return
        async with self._lock:
            if self._ws is not None:
                return
            endpoint = _devtools_ws_endpoint()
            if not endpoint:
                raise RuntimeError(
                    "Remote debugging endpoint not found (DevToolsActivePort). "
                    + _setup_hint()
                )
            import websockets
            try:
                ws = await websockets.connect(endpoint, ping_interval=None, open_timeout=timeout)
            except Exception as e:
                raise RuntimeError(
                    f"Could not open the browser debugging WebSocket ({e}). "
                    "Click 'Allow' in the browser prompt, then retry. " + _setup_hint()
                ) from e
            self._ws = ws
            self._reader = asyncio.create_task(self._reader_loop(ws))

    async def _reader_loop(self, ws: Any) -> None:
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
        except Exception:
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("Browser debugging connection closed"))
            self._pending.clear()
            self._sessions.clear()
            with contextlib.suppress(Exception):
                await ws.close()
            if self._ws is ws:
                self._ws = None

    async def call(
        self,
        method: str,
        params: Optional[dict] = None,
        session_id: Optional[str] = None,
        timeout: float = 15.0,
    ) -> dict:
        """Send a CDP command and return the full response dict."""
        await self.ensure()
        ws = self._ws
        if ws is None:
            raise RuntimeError("Not connected to the browser")
        self._next_id += 1
        mid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        msg: dict[str, Any] = {"id": mid, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        try:
            await ws.send(json.dumps(msg))
        except Exception:
            self._pending.pop(mid, None)
            raise
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise TimeoutError(f"{method} timed out after {timeout}s") from None

    async def attach(self, target_id: str) -> str:
        """Attach to a target (once) and return its sessionId."""
        sid = self._sessions.get(target_id)
        if sid:
            return sid
        resp = await self.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        sid = resp.get("result", {}).get("sessionId")
        if not sid:
            raise RuntimeError(f"attachToTarget failed: {resp.get('error')}")
        self._sessions[target_id] = sid
        return sid

    def invalidate(self, target_id: str) -> None:
        self._sessions.pop(target_id, None)


_BrowserCDP_CLIENT = _BrowserCDP()

# Cooldown so repeated calls don't spam the opt-in tab while the user decides.
_INSPECT_OPENED_AT = 0.0


class _HttpPageConn:
    """Short-lived per-page WebSocket (legacy HTTP-discovery mode)."""

    def __init__(self, ws: Any):
        self._ws = ws
        self._id = 0

    async def call(self, method: str, params: Optional[dict] = None,
                   timeout: float = 10.0) -> dict:
        self._id += 1
        mid = self._id
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method} timed out after {timeout}s")
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") == mid:
                return msg


class _WsPageConn:
    """Page commands multiplexed over the shared consent-mode browser socket."""

    def __init__(self, target_id: str):
        self._target = target_id

    async def call(self, method: str, params: Optional[dict] = None,
                   timeout: float = 10.0) -> dict:
        last: dict = {}
        for attempt in (1, 2):
            sid = await _BrowserCDP_CLIENT.attach(self._target)
            resp = await _BrowserCDP_CLIENT.call(
                method, params, session_id=sid, timeout=timeout
            )
            last = resp
            err = str(resp.get("error", ""))
            if "ession" in err and attempt == 1:  # stale session -> re-attach once
                _BrowserCDP_CLIENT.invalidate(self._target)
                continue
            return resp
        return last



# Sites addressable by bare name for plain "open <site>" goals.
_PLAIN_NAV_SITES = {
    "youtube": "www.youtube.com",
    "google": "www.google.com",
    "github": "www.github.com",
    "amazon": "www.amazon.com",
    "instagram": "www.instagram.com",
    "reddit": "www.reddit.com",
    "twitter": "www.twitter.com",
    "x": "x.com",
    "linkedin": "www.linkedin.com",
    "facebook": "www.facebook.com",
    "netflix": "www.netflix.com",
    "spotify": "open.spotify.com",
    "maps": "maps.google.com",
    "gmail": "mail.google.com",
    "drive": "drive.google.com",
    "wikipedia": "en.wikipedia.org",
    "whatsapp": "web.whatsapp.com",
}
_PLAIN_NAV_STOPWORDS = (
    "search", "find", "play", "watch", "video", "look up", "query",
    "book", "flight", "recipe", "download", "buy", "order",
)


def _plain_nav_url(goal: str) -> Optional[str]:
    """Return a URL when the goal is just 'open/go to <site>' (no interaction).

    Those simple goals open a tab in the user's OWN browser window instead of
    spawning a separate controlled window.
    """
    g = (goal or "").strip().lower()
    m = re.match(
        r"^(?:please\s+)?(?:open|go to|visit|navigate to|browse|take me to)\s+"
        r"(?:the\s+|my\s+|up\s+)?(.+?)\s*(?:please|thanks)?[.!?]?$",
        g,
    )
    if not m:
        return None
    rest = m.group(1).strip()
    if any(kw in rest for kw in _PLAIN_NAV_STOPWORDS):
        return None
    url_match = re.search(
        r"(https?://\S+|[\w\-]+\.(?:com|org|net|io|dev|ai|gov|edu|co|me|app)(?:/\S*)?)",
        rest,
    )
    if url_match:
        matched = url_match.group(1).rstrip(".,")
        return matched if matched.startswith("http") else f"https://{matched}"
    if rest in _PLAIN_NAV_SITES:
        return "https://" + _PLAIN_NAV_SITES[rest]
    return None


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
        self._current_tab_id: Optional[str] = None

    # ── Detection ──────────────────────────────────────────────────────────

    async def get_running_browser(self) -> str:
        """Return name of whichever browser is currently running."""
        script = """
        $browsers = @('brave', 'chrome', 'msedge', 'firefox')
        foreach ($b in $browsers) {
            if (Get-Process -Name $b -ErrorAction SilentlyContinue) {
                Write-Output $b; break
            }
        }
        """
        out = await run_powershell(script)
        name = (out.get("stdout") or "").strip().lower()
        return name or "chrome"

    # ── CDP backend helpers (HTTP discovery + consent-mode WebSocket) ───────

    async def _tabs(self) -> tuple[str, list[dict[str, Any]]]:
        """Return (backend, page tabs) shaped like HTTP /json/list entries.

        Any failure (endpoint gone, Allow prompt still pending) degrades to
        ("none", []) so callers fall back to their keyboard-shortcut paths.
        """
        try:
            backend = _cdp_backend()
            if backend == "http":
                loop = asyncio.get_event_loop()
                tabs = await loop.run_in_executor(None, lambda: _cdp_request("/json/list"))
                if not tabs:
                    return ("none", [])
                return ("http", [t for t in tabs if t.get("type") == "page"])
            if backend == "ws":
                resp = await _BrowserCDP_CLIENT.call("Target.getTargets")
                infos = resp.get("result", {}).get("targetInfos", [])
                return ("ws", [
                    {
                        "id": i.get("targetId"),
                        "title": i.get("title", ""),
                        "url": i.get("url", ""),
                        "type": i.get("type"),
                        "webSocketDebuggerUrl": "",
                    }
                    for i in infos
                    if i.get("type") == "page"
                ])
        except Exception as e:
            log.debug(f"CDP tab listing failed: {e}")
        return ("none", [])

    def _pick_tab(self, tabs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """Prefer the remembered tab, else the first non-internal page."""
        if getattr(self, "_current_tab_id", None):
            for t in tabs:
                if t.get("id") == self._current_tab_id:
                    return t
        non_internal = [
            t for t in tabs
            if not t.get("url", "").startswith(("chrome://", "brave://", "edge://"))
        ]
        target = non_internal[0] if non_internal else (tabs[0] if tabs else None)
        if target:
            self._current_tab_id = target.get("id")
        return target

    @staticmethod
    def _http_new_tab(path: str) -> Any:
        # Chrome >= 119 requires PUT for /json/new; older builds only accept GET.
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{_CDP_PORT}{path}", method="PUT"
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (404, 405):
                return _cdp_request(path)
            return None
        except Exception:
            return None

    async def _open_tab(self, url: str) -> Optional[dict[str, Any]]:
        """Open a new tab via CDP; returns the new tab entry (or None)."""
        try:
            backend = _cdp_backend()
            if backend == "http":
                loop = asyncio.get_event_loop()
                quoted = urllib.parse.quote(url, safe=":/?&=%")
                resp = await loop.run_in_executor(
                    None, lambda: self._http_new_tab(f"/json/new?{quoted}")
                )
                if resp and isinstance(resp, dict) and "id" in resp:
                    self._current_tab_id = resp["id"]
                    return resp
                return None
            if backend == "ws":
                resp = await _BrowserCDP_CLIENT.call("Target.createTarget", {"url": url})
                target_id = resp.get("result", {}).get("targetId")
                if not target_id:
                    log.debug(f"Target.createTarget failed: {resp.get('error')}")
                    return None
                with contextlib.suppress(Exception):
                    await _BrowserCDP_CLIENT.call(
                        "Target.activateTarget", {"targetId": target_id}
                    )
                self._current_tab_id = target_id
                return {"id": target_id, "url": url}
        except Exception as e:
            log.debug(f"CDP new tab failed: {e}")
        return None

    async def _activate_tab(self, tab_id: str) -> None:
        backend = _cdp_backend()
        if backend == "http":
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: _cdp_request(f"/json/activate/{tab_id}")
            )
        elif backend == "ws":
            with contextlib.suppress(Exception):
                await _BrowserCDP_CLIENT.call(
                    "Target.activateTarget", {"targetId": tab_id}
                )

    @asynccontextmanager
    async def _page_conn(self, tab: dict[str, Any]):
        """Connection for page-level commands in whichever backend is live."""
        backend = _cdp_backend()
        if backend == "ws" and tab.get("id"):
            yield _WsPageConn(tab["id"])
            return
        ws_url = (tab.get("webSocketDebuggerUrl") or "").replace("localhost", "127.0.0.1")
        if backend == "http" and ws_url:
            import websockets
            ws = await websockets.connect(ws_url, ping_interval=None)
            try:
                yield _HttpPageConn(ws)
            finally:
                with contextlib.suppress(Exception):
                    await ws.close()
            return
        raise RuntimeError("CDP not available")

    async def _open_inspect_page(self, browser: str, force: bool = False) -> None:
        """Open the browser's remote-debugging opt-in page in its own window."""
        global _INSPECT_OPENED_AT
        page = _inspect_page(browser)
        if not page:
            return
        if not force and time.monotonic() - _INSPECT_OPENED_AT < 120:
            return  # already offered recently; don't spam tabs
        script = f"""
        $proc = Get-Process -Name "{browser}" -ErrorAction SilentlyContinue |
                Where-Object {{ $_.MainWindowHandle -ne 0 }} | Select-Object -First 1
        if (-not $proc) {{ $proc = Get-Process -Name "{browser}" -ErrorAction SilentlyContinue | Select-Object -First 1 }}
        if (-not $proc -or -not $proc.Path) {{ return "ERROR: {browser} not running" }}
        Start-Process $proc.Path -ArgumentList '{page}'
        return "Opened {page}"
        """
        out = await run_powershell(script)
        if not out.get("success") or "ERROR" in (out.get("stdout") or ""):
            log.debug(f"Could not open {page}: {out.get('stdout') or out.get('stderr')}")
        else:
            _INSPECT_OPENED_AT = time.monotonic()

    async def _enable_remote_debugging(self, browser: str) -> dict[str, Any]:
        """Turn on the remote-debugging opt-in with no manual steps if possible.

        - Browser CLOSED: patch `devtools.remote_debugging.user-enabled` into
          Local State so the next launch starts the debug server automatically.
        - Browser RUNNING: open the opt-in page, focus it and toggle the
          checkbox with keyboard (best effort), then verify via `DevToolsActivePort`.
        """
        if not _process_running(browser):
            ok = _patch_local_state_enable(browser)
            return {"enabled": ok, "method": "local_state"}

        await self._open_inspect_page(browser, force=True)
        await asyncio.sleep(1.0)  # let the page render and take focus
        script = f"""
        $proc = Get-Process -Name "{browser}" -ErrorAction SilentlyContinue |
                Where-Object {{ $_.MainWindowHandle -ne 0 }} | Select-Object -First 1
        if (-not $proc) {{ return "ERROR: {browser} not running" }}
        $ws = New-Object -ComObject WScript.Shell
        $ws.AppActivate($proc.Id) | Out-Null
        Start-Sleep -Milliseconds 400
        $ws.SendKeys("{{TAB}}")
        Start-Sleep -Milliseconds 200
        $ws.SendKeys(" ")
        return "Sent TAB+SPACE to toggle remote-debugging opt-in"
        """
        out = await run_powershell(script)
        if not out.get("success") or "ERROR" in (out.get("stdout") or ""):
            log.debug(f"Could not toggle opt-in: {out.get('stdout') or out.get('stderr')}")
            return {"enabled": False, "method": "ui_automation", "hint": _setup_hint(browser)}
        for _ in range(16):  # up to 8s for the checkbox to take effect
            await asyncio.sleep(0.5)
            if _cdp_backend():
                log.info("Remote debugging enabled via inspect-page opt-in automation")
                return {"enabled": True, "method": "ui_automation"}
        return {"enabled": False, "method": "ui_automation", "hint": _setup_hint(browser)}

    async def ensure_browser_open(self) -> dict[str, Any]:
        """Make sure a browser window is visible and focused.

        Attaches to remote debugging when it is already on; otherwise focuses
        (or starts) the browser without forcing the one-time opt-in — the hint
        is returned in `setup_hint` instead.
        """
        if _cdp_available():
            return {"success": True, "message": "CDP running"}

        # Starts the default browser if nothing is running (no setup nagging).
        launch_res = await self.launch_with_cdp(guide=False)
        if launch_res.get("method") != "opt_in_required":
            for _ in range(6):
                await asyncio.sleep(0.4)
                if _cdp_available():
                    break
        if _cdp_available():
            return {"success": True, "message": "Browser ready with CDP (real profile)",
                    **launch_res}

        # CDP still off: focus the user's window (or start the default browser).
        name = _running_browser() or get_default_browser() or "chrome"
        exe = _find_browser_exe(name)
        script = f"""
        $proc = Get-Process -Name "{name}" -ErrorAction SilentlyContinue |
                Where-Object {{ $_.MainWindowHandle -ne 0 }} | Select-Object -First 1
        if ($proc) {{
            $ws = New-Object -ComObject WScript.Shell
            $ws.AppActivate($proc.Id) | Out-Null
            return "Focused {name} (remote debugging not enabled)"
        }} else {{
            $exe = "{exe}"
            if (-not $exe -or -not (Test-Path $exe)) {{
                return "ERROR: Could not find executable for '{name}'"
            }}
            Start-Process $exe
            return "Launched {name} (remote debugging not enabled)"
        }}
        """
        out = await run_powershell(script)
        result: dict[str, Any] = {"success": True, **out,
                                  "message": "Browser focused/started"}
        if not out.get("success") or "ERROR" in (out.get("stdout") or ""):
            result["success"] = False
        if not _cdp_available():
            result["setup_hint"] = _setup_hint(name)
        return result

    async def ensure_browser_with_cdp(self, url: str = "") -> dict[str, Any]:
        """
        Ensure the user's default-profile browser is controllable over CDP.

        Attaches when remote debugging is already on; otherwise runs the
        guided attach flow (which may require the one-time browser opt-in).
        """
        backend = _cdp_backend()
        if backend:
            return {"success": True, "method": f"cdp_already_running_{backend}",
                    "cdp_backend": backend,
                    "message": "CDP already available (real profile)"}
        return await self.launch_with_cdp(url)

    # ── CDP Tab Operations ─────────────────────────────────────────────────

    async def list_tabs(self) -> dict[str, Any]:
        """List all open browser tabs via CDP."""
        backend, page_tabs = await self._tabs()
        if backend == "none":
            # Fallback: use PowerShell to query window titles
            script = """
            $procs = Get-Process | Where-Object { $_.ProcessName -in @('brave','chrome','msedge','firefox') -and $_.MainWindowTitle -ne '' }
            $procs | ForEach-Object { Write-Output "[$($_.ProcessName)] $($_.MainWindowTitle)" }
            """
            out = await run_powershell(script)
            return {
                "success": True,
                "cdp_available": False,
                "tabs": (out.get("stdout") or "No browser windows found"),
            }
        return {
            "success": True,
            "cdp_available": True,
            "cdp_backend": backend,
            "tab_count": len(page_tabs),
            "tabs": [{"id": t["id"], "title": t.get("title", ""), "url": t.get("url", "")} for t in page_tabs],
        }

    async def navigate_to(self, url: str, new_tab: bool = False) -> dict[str, Any]:
        """Navigate the active tab to a URL, or open a new tab first."""
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url

        # Ensure browser with CDP is running
        if not _cdp_available():
            await self.ensure_browser_open()

        # Try CDP first (fastest, most reliable, no extra windows)
        backend, page_tabs = await self._tabs()
        if backend != "none":
            if new_tab:
                # Open a new tab cleanly via CDP
                new_tab_resp = await self._open_tab(url)
                if new_tab_resp:
                    await asyncio.sleep(0.5)
                    return {"success": True, "method": "cdp_new_tab", "url": url}
            else:
                target = self._pick_tab(page_tabs)
                if target:
                    self._current_tab_id = target["id"]
                    await self._activate_tab(target["id"])
                    try:
                        async with self._page_conn(target) as conn:
                            await conn.call("Page.navigate", {"url": url}, timeout=5)
                        await asyncio.sleep(0.5)
                        return {"success": True, "method": "cdp_navigate", "url": url}
                    except Exception as e:
                        log.debug(f"CDP Page.navigate failed: {e}")
                else:
                    # No page tab existed; open one cleanly via CDP
                    new_tab_resp = await self._open_tab(url)
                    if new_tab_resp:
                        await asyncio.sleep(0.5)
                        return {"success": True, "method": "cdp_new_tab", "url": url}

        # Fallback only when CDP completely unavailable
        safe_url = url.replace('"', '%22').replace('{', '{{').replace('}', '}}')
        if new_tab:
            focus_keys = '$ws.SendKeys("^t")\n            Start-Sleep -Milliseconds 300'
        else:
            focus_keys = '$ws.SendKeys("^l")\n            Start-Sleep -Milliseconds 200'
        script = f"""
        $proc = Get-Process | Where-Object {{ $_.ProcessName -in @('brave','chrome','msedge','firefox') -and $_.MainWindowHandle -ne 0 }} | Select-Object -First 1
        if ($proc) {{
            $ws = New-Object -ComObject WScript.Shell
            $ws.AppActivate($proc.Id) | Out-Null
            Start-Sleep -Milliseconds 200
            {focus_keys}
            Add-Type -AssemblyName System.Windows.Forms
            [System.Windows.Forms.Clipboard]::SetText("{safe_url}")
            $ws.SendKeys("^v")
            Start-Sleep -Milliseconds 50
            $ws.SendKeys("{{ENTER}}")
            return "Navigated to {url}"
        }} else {{
            $exe = $null
            foreach ($p in @(
                "$env:ProgramFiles\\Google\\Chrome\\Application\\chrome.exe",
                "${{env:ProgramFiles(x86)}}\\Google\\Chrome\\Application\\chrome.exe",
                "$env:LOCALAPPDATA\\Google\\Chrome\\Application\\chrome.exe",
                "${{env:ProgramFiles(x86)}}\\Microsoft\\Edge\\Application\\msedge.exe",
                "$env:ProgramFiles\\Microsoft\\Edge\\Application\\msedge.exe"
            )) {{ if (Test-Path $p) {{ $exe = $p; break }} }}
            if (-not $exe) {{
                return "ERROR: No browser window available and no Chrome/Edge executable found"
            }}
            Start-Process $exe -ArgumentList "{safe_url}"
            return "Opened {url}"
        }}
        """
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
        result = await self.navigate_to(url, new_tab=True)
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
        backend, tabs = await self._tabs()
        if tabs:
            target = self._pick_tab(tabs)
            if target:
                return {
                    "success": True,
                    "url": target.get("url", ""),
                    "title": target.get("title", ""),
                    "method": f"cdp_{backend}",
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
        backend, tabs = await self._tabs()
        if not tabs:
            return {"success": False, "error": "CDP not available. Enable remote debugging (see setup hint)"}

        target = self._pick_tab(tabs)
        if not target:
            return {"success": False, "error": "No page tabs found"}

        try:
            async with self._page_conn(target) as conn:
                resp = await conn.call(
                    "Page.captureScreenshot", {"format": "png", "quality": 85}, timeout=8
                )
            img_data = resp.get("result", {}).get("data", "")
            if img_data:
                if save_path:
                    import os
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    with open(save_path, "wb") as f:
                        f.write(base64.b64decode(img_data))
                    return {"success": True, "path": save_path, "method": f"cdp_{backend}"}
                return {"success": True, "data_len": len(img_data), "method": f"cdp_{backend}"}
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
        backend, tabs = await self._tabs()
        if not tabs:
            return {"success": False, "error": "CDP not available"}
        target = self._pick_tab(tabs)
        if not target:
            return {"success": False, "error": "No page tabs found"}
        try:
            async with self._page_conn(target) as conn:
                resp = await conn.call(
                    "Runtime.evaluate",
                    {"expression": code, "returnByValue": True},
                    timeout=8,
                )
            if "error" in resp:
                return {"success": False, "error": str(resp["error"])}
            result = resp.get("result", {}).get("result", {})
            return {"success": True, "value": result.get("value"), "type": result.get("type")}
        except ImportError:
            return {"success": False, "error": "websockets package not installed. Run: pip install websockets"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def click_at(self, x: int, y: int) -> dict[str, Any]:
        """Trusted mouse click at viewport coordinates via the CDP Input domain."""
        backend, tabs = await self._tabs()
        if not tabs:
            return {"success": False, "error": "CDP not available"}
        target = self._pick_tab(tabs)
        if not target:
            return {"success": False, "error": "No page tab found"}
        events = [
            {"type": "mouseMoved", "x": int(x), "y": int(y), "button": "none", "buttons": 0},
            {"type": "mousePressed", "x": int(x), "y": int(y), "button": "left", "buttons": 1, "clickCount": 1},
            {"type": "mouseReleased", "x": int(x), "y": int(y), "button": "left", "buttons": 0, "clickCount": 1},
        ]
        try:
            async with self._page_conn(target) as conn:
                # The tab must be active or Chromium may swallow the input.
                with contextlib.suppress(Exception):
                    await conn.call("Page.bringToFront", timeout=3)
                for ev in events:
                    # Brave occasionally omits a reply for mouseMoved.
                    with contextlib.suppress(Exception):
                        await conn.call("Input.dispatchMouseEvent", ev, timeout=3)
            return {"success": True, "x": int(x), "y": int(y), "method": f"cdp_{backend}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def send_browser_key(self, key: str) -> dict[str, Any]:
        """Send a trusted key event to the current tab via the CDP Input domain.

        Unlike JS dispatchEvent, CDP input counts as a real user gesture, so it
        can start media playback that autoplay policy would otherwise block.
        """
        if not key:
            return {"success": False, "error": "No key given"}
        backend, tabs = await self._tabs()
        if not tabs:
            return {"success": False, "error": "CDP not available"}
        target = self._pick_tab(tabs)
        if not target:
            return {"success": False, "error": "No page tab found"}
        vk = ord(key[0].upper())
        events = [
            {"type": "rawKeyDown", "windowsVirtualKeyCode": vk, "key": key, "code": f"Key{key[0].upper()}"},
            {"type": "char", "text": key},
            {"type": "keyUp", "windowsVirtualKeyCode": vk, "key": key, "code": f"Key{key[0].upper()}"},
        ]
        try:
            async with self._page_conn(target) as conn:
                for ev in events:
                    await conn.call("Input.dispatchKeyEvent", ev, timeout=5)
            return {"success": True, "key": key, "method": f"cdp_{backend}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def bring_to_front(self) -> dict[str, Any]:
        """Focus the current tab's window via CDP (used before sending real keys)."""
        backend, tabs = await self._tabs()
        if not tabs:
            return {"success": False, "error": "CDP not available"}
        target = self._pick_tab(tabs)
        if not target:
            return {"success": False, "error": "No page tab found"}
        try:
            async with self._page_conn(target) as conn:
                await conn.call("Page.bringToFront", timeout=5)
            return {"success": True, "method": f"cdp_{backend}"}
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
        if key.startswith("chrome://") or key.startswith("edge://") or key.startswith("brave://"):
            return await self.navigate_to(key, new_tab=True)
        script = f"""
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("{key}")
        return "Executed browser shortcut: {action}"
        """
        return await run_powershell(script)

    async def launch_with_cdp(self, url: str = "", guide: bool = True) -> dict[str, Any]:
        """
        Make the user's own default-profile browser controllable over CDP.

        Preference order:
          1. CDP already up (HTTP discovery or consent-mode WebSocket) -> attach.
          2. WINBROW_BROWSER_MODE=separate -> legacy behavior: a throwaway
             profile window without the user's logins/cookies.
          3. Browser already running -> enable the remote-debugging opt-in
             automatically (inspect page + checkbox toggle, best effort) and
             fall back to a one-time hint if that fails.
          4. Nothing running -> patch the opt-in pref into Local State and start
             the default browser with its normal profile; the debug server then
             comes up by itself.
        """
        backend = _cdp_backend()
        if backend:
            return {"success": True, "method": f"attach_{backend}", "cdp_backend": backend,
                    "stdout": "Attached to the running browser over CDP (real profile)",
                    "message": "Attached to the running browser over CDP (real profile)"}

        if os.environ.get("WINBROW_BROWSER_MODE", "").lower() == "separate":
            return await self._launch_separate_profile(url)

        running = _running_browser()
        browser = running or get_default_browser() or "brave"
        if running:
            # Already open without CDP -> turn the one-time opt-in on for the
            # user automatically when guided; manual hint as fallback.
            hint = _setup_hint(running)
            if guide:
                auto = await self._enable_remote_debugging(running)
                if auto.get("enabled"):
                    msg = (f"Enabled remote debugging on {_display_name(running)}; "
                           "attached to this window with your real profile")
                    return {"success": True, "method": "enable_opt_in",
                            "cdp_backend": _cdp_backend(), "stdout": msg, "message": msg}
                hint = auto.get("hint") or hint
            msg = (f"{_display_name(running)} is running without remote debugging. "
                   f"{hint}")
            log.info(f"Remote debugging not enabled on running {running}: {hint}")
            return {"success": True, "method": "opt_in_required",
                    "setup": "enable_remote_debugging", "cdp_backend": None,
                    "setup_hint": hint, "stdout": msg, "message": msg}

        exe = _find_browser_exe(browser)
        if not exe:
            for name in _browser_order():
                exe = _find_browser_exe(name)
                if exe:
                    browser = name
                    break
        if not exe:
            return {"success": False, "method": "error", "cdp_backend": None,
                    "stdout": "ERROR: Could not find Brave, Chrome, or Edge executable",
                    "message": "ERROR: Could not find Brave, Chrome, or Edge executable"}

        # Silent auto-enable: pre-write the opt-in pref so this launch starts
        # the debug server on the real profile with no manual step.
        _patch_local_state_enable(browser)

        # Default profile (no --user-data-dir): keeps the user's real logins.
        arg_list = "--remote-debugging-port=9222 --no-first-run --no-default-browser-check"
        if url:
            arg_list += f" \"{url}\""
        ps_args = arg_list.replace('"', '`"')  # escape quotes for the PS string
        script = (
            f'Start-Process "{exe}" -ArgumentList "{ps_args}"\n'
            f'return "Launched {browser} (default profile) with --remote-debugging-port=9222"'
        )
        out = await run_powershell(script)
        for _ in range(10):
            if _cdp_backend():
                break
            await asyncio.sleep(0.5)
        if _cdp_backend():
            msg = out.get("stdout") or f"Launched {browser} with CDP (real profile)"
            return {"success": True, "method": "launch_default_profile",
                    "cdp_backend": _cdp_backend(), "stdout": msg, "message": msg}
        if guide:
            await asyncio.sleep(1.0)
            await self._open_inspect_page(browser)
        msg = (f"{_display_name(browser)} started, but remote debugging is not active yet. "
               f"{_setup_hint(browser)}")
        return {"success": True, "method": "opt_in_required",
                "setup": "enable_remote_debugging", "cdp_backend": None,
                "stdout": msg, "message": msg}

    async def _launch_separate_profile(self, url: str = "") -> dict[str, Any]:
        """Legacy behavior: controllable window on a throwaway profile.

        Kept as an explicit opt-in (WINBROW_BROWSER_MODE=separate) for machines
        where the browser's remote-debugging opt-in is not wanted.
        """
        target = f'"{url}"' if url else ''
        script = f"""
        $paths = @{{
            'brave' = @(
                "$env:ProgramFiles\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
                "${{env:ProgramFiles(x86)}}\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
                "$env:LOCALAPPDATA\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"
            )
            'chrome' = @(
                "$env:ProgramFiles\\Google\\Chrome\\Application\\chrome.exe",
                "${{env:ProgramFiles(x86)}}\\Google\\Chrome\\Application\\chrome.exe",
                "$env:LOCALAPPDATA\\Google\\Chrome\\Application\\chrome.exe"
            )
            'msedge' = @(
                "$env:ProgramFiles\\Microsoft\\Edge\\Application\\msedge.exe",
                "${{env:ProgramFiles(x86)}}\\Microsoft\\Edge\\Application\\msedge.exe"
            )
        }}
        $order = @('brave', 'chrome', 'msedge')
        # Prefer whichever browser the user already has open.
        foreach ($b in $order) {{
            if (Get-Process -Name $b -ErrorAction SilentlyContinue) {{ $order = @($b) + @($order | Where-Object {{ $_ -ne $b }}); break }}
        }}
        $found = $null
        $foundName = $null
        foreach ($b in $order) {{
            foreach ($p in $paths[$b]) {{ if (Test-Path $p) {{ $found = $p; $foundName = $b; break }} }}
            if ($found) {{ break }}
        }}
        if ($found) {{
            $profileDir = Join-Path $env:LOCALAPPDATA ("WinBrow\\cdp_profile_" + $foundName)
            if (-not (Test-Path $profileDir)) {{ New-Item -ItemType Directory -Path $profileDir -Force | Out-Null }}
            $argList = "--remote-debugging-port=9222 --user-data-dir=`"$profileDir`" --no-first-run --no-default-browser-check"
            $dest = "{target}"
            if ($dest) {{ $argList += " $dest" }}
            Start-Process $found -ArgumentList $argList
            Start-Sleep -Milliseconds 1500
            return "Launched $foundName with CDP on port 9222 (separate controlled window). Path: $found"
        }} else {{
            return "ERROR: Could not find Brave, Chrome, or Edge executable"
        }}
        """
        out = await run_powershell(script)
        for _ in range(8):
            if _cdp_backend():
                break
            await asyncio.sleep(0.5)
        out["method"] = "separate_profile"
        out["cdp_backend"] = _cdp_backend()
        return out

    async def open_in_user_browser(self, url: str) -> dict[str, Any]:
        """Open a URL as a new tab in the user's OWN browser window (no CDP)."""
        safe = url.replace('"', "")
        order = _browser_order()
        order_ps = ", ".join(f"'{b}'" for b in order)
        exe_ps = ",\n                ".join(
            f'"{p}"' for b in order for p in _exe_candidates(b) if p
        )
        script = f"""
        $exe = $null
        foreach ($b in @({order_ps})) {{
            $p = Get-Process -Name $b -ErrorAction SilentlyContinue |
                 Where-Object {{ $_.MainWindowHandle -ne 0 }} | Select-Object -First 1
            if ($p) {{ $exe = $p.Path; break }}
        }}
        if (-not $exe) {{
            foreach ($p in @(
                {exe_ps}
            )) {{ if (Test-Path $p) {{ $exe = $p; break }} }}
        }}
        if (-not $exe) {{ return "ERROR: No browser found" }}
        Start-Process $exe -ArgumentList "{safe}"
        return "Opened {safe} in your browser"
        """
        return await run_powershell(script)

    async def read_page_text(self) -> dict[str, Any]:
        """Read the visible text content of the current page via CDP, extracting search cards if on Google."""
        code = """
        (() => {
          const isGoogle = window.location.hostname.includes('google');
          if (isGoogle) {
            const cards = [];
            const resultBlocks = document.querySelectorAll('#rso .g, #rso div[data-hveid]');
            for (const b of resultBlocks) {
              const h3 = b.querySelector('h3');
              if (h3) {
                const title = (h3.innerText || '').trim();
                const snippetEl = b.querySelector('div[style*="-webkit-line-clamp"], .VwiC3b, span[style*="line-height"]');
                const snippet = snippetEl ? (snippetEl.innerText || '').trim() : '';
                if (title) {
                  cards.push(snippet ? `${title}: ${snippet}` : title);
                }
              }
              if (cards.length >= 6) break;
            }
            if (cards.length > 0) return cards.join('\\n\\n');
          }
          return Array.from(document.querySelectorAll('h1,h2,h3,p,li,td,th,span,a'))
            .map(e => (e.innerText || '').trim())
            .filter(t => t.length > 15)
            .slice(0, 40)
            .join('\\n');
        })()
        """
        result = await self.execute_js(code)
        if result.get("success"):
            return {"success": True, "text": result.get("value", "") or "", "method": "cdp"}
        return {"success": False, "error": result.get("error", "CDP unavailable")}

    async def click_element(self, selector: str) -> dict[str, Any]:
        """Click a DOM element by CSS selector via CDP."""
        code = f"""
        const el = document.querySelector('{selector}');
        if (el) {{ el.click(); 'Clicked: {selector}' }} else {{ 'Element not found: {selector}' }}
        """
        return await self.execute_js(code)

    async def type_in_element(self, selector: str, text: str) -> dict[str, Any]:
        """
        Type text into a DOM input element.

        Strategy:
          1. CDP WebSocket: focus element then set value + dispatch input/change events.
             Also dispatches keydown/keypress/keyup for each character so frameworks
             (React, Vue, Angular) that rely on keyboard events pick up the change.
          2. PowerShell clipboard-paste fallback when CDP is unavailable.
        """
        # -- CDP path ----------------------------------------------------------
        backend, tabs = await self._tabs()
        target = self._pick_tab(tabs) if tabs else None
        if target:
            try:
                # Step 1: focus + set value via Runtime.evaluate
                set_val_js = f"""
                (function() {{
                    const sel = {json.dumps(selector)};
                    const el = document.querySelector(sel)
                             || document.querySelector('[data-wb-idx="' + sel.replace(/[^\\d]/g,'') + '"]');
                    if (!el) return 'not_found';
                    el.focus();
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value') ||
                                                   Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');
                    if (nativeInputValueSetter && nativeInputValueSetter.set) {{
                        nativeInputValueSetter.set.call(el, {json.dumps(text)});
                    }} else {{
                        el.value = {json.dumps(text)};
                    }}
                    el.dispatchEvent(new Event('input',  {{bubbles:true}}));
                    el.dispatchEvent(new Event('change', {{bubbles:true}}));
                    return 'ok';
                }})()
                """
                async with self._page_conn(target) as conn:
                    resp = await conn.call(
                        "Runtime.evaluate",
                        {"expression": set_val_js, "returnByValue": True},
                        timeout=8,
                    )
                val = resp.get("result", {}).get("result", {}).get("value", "")
                if val == "ok":
                    return {"success": True, "output": f"Typed '{text}' via CDP",
                            "method": f"cdp_{backend}"}
                if val == "not_found":
                    log.warning(f"type_in_element: element not found for selector '{selector}'")
                    # Fall through to PowerShell fallback
            except Exception as e:
                log.debug(f"type_in_element CDP failed: {e}")

        # -- PowerShell clipboard-paste fallback -------------------------------
        # Focus browser, click on the element via Tab-key walking is unreliable;
        # instead we rely on the caller having clicked/focused the element first.
        safe_ps = text.replace('"', '`"').replace("'", "'")
        script = f"""
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.Clipboard]::SetText("{safe_ps}")
        $proc = Get-Process | Where-Object {{ $_.ProcessName -in @('brave','chrome','msedge','firefox') -and $_.MainWindowHandle -ne 0 }} | Select-Object -First 1
        if ($proc) {{ $ws2 = New-Object -ComObject WScript.Shell; $ws2.AppActivate($proc.Id) | Out-Null; Start-Sleep -Milliseconds 300 }}
        $ws = New-Object -ComObject WScript.Shell
        $ws.SendKeys("^v")
        Start-Sleep -Milliseconds 200
        $ws.SendKeys("{{ENTER}}")
        return "Typed via clipboard paste"
        """
        result = await run_powershell(script)
        result["method"] = "powershell_clipboard"
        return result

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

    async def run_ultrafast_task(self, goal: str, max_steps: int = 20) -> dict[str, Any]:
        """
        Execute an Ultrafast multi-step browser task using Laya typed decisions.

        Simple "open/go to <site>" goals are handed to the user's own browser
        window (new tab, no CDP). Interactive tasks attach to the user's
        default-profile browser over CDP (same window, same logins) — with a
        one-time remote-debugging opt-in when it has not been enabled yet.
        """
        # Plain navigation: open a tab in the user's own browser window.
        nav_url = _plain_nav_url(goal)
        if nav_url:
            out = await self.open_in_user_browser(nav_url)
            msg = (out.get("stdout") or out.get("stderr") or "").strip()
            ok = out.get("success", False) and not msg.startswith("ERROR")
            log.info(f"Plain navigation goal -> user's browser: {nav_url} ({msg})")
            return {
                "success": ok,
                "goal": goal,
                "final_answer": msg or f"Opened {nav_url} in your browser",
                "steps": [{"step": 1, "action": "open_in_user_browser", "reason": msg, "url": nav_url}],
                "total_elapsed_ms": out.get("elapsed_ms", 0.0),
                "method": "user_browser",
            }

        # Ensure CDP is available — all interactive actions depend on it.
        # Attaches to the user's own browser/profile when remote debugging is
        # on; otherwise runs the guided one-time setup flow.
        setup_hint = None
        if not _cdp_available():
            log.info("CDP not available — attaching to the default browser (real profile) …")
            launch_res = await self.ensure_browser_with_cdp()
            log.debug(f"CDP attach result: {launch_res}")
            if launch_res.get("setup") == "enable_remote_debugging":
                setup_hint = launch_res.get("stdout")
            # Wait up to 4 s for the debugging endpoint to come up
            for _ in range(8):
                await asyncio.sleep(0.5)
                if _cdp_available():
                    break
            if not _cdp_available():
                setup_hint = setup_hint or _setup_hint()
                log.warning(
                    "Remote debugging not enabled yet. "
                    "Interactive actions (type/click) will use PowerShell fallbacks. "
                    f"{setup_hint}"
                )

        from .ultrafast import LayaUltrafastEngine
        engine = LayaUltrafastEngine(self)
        res = await engine.run_task(goal=goal, max_steps=max_steps)
        result = {
            "success": res.success,
            "goal": res.goal,
            "final_answer": res.final_answer,
            "steps": res.steps,
            "total_elapsed_ms": res.total_elapsed_ms,
            "method": res.method,
        }
        if setup_hint and not _cdp_available():
            result["setup_hint"] = setup_hint
        return result

