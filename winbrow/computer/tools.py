"""
Computer Skill Atoms — Brain-Callable Native Actions
=====================================================
Python-backed tools the planner can emit as steps (impl="computer").

Atoms stay single-purpose; multi-step flows (open → type → save dialog)
are composed by the brain's agent loop. Every atom returns
{"success": bool, "output": str, ...extras}.

Registration: get_computer_tools() returns Tool records with
impl="computer" and empty script; args carry JSON-arg names.
Dispatch:     execute_computer_tool(name, args, ctx).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("winbrow.computer.tools")

COMPUTER_IMPL = "computer"

# Lazily-created singletons
_driver = None
_screen = None
_uia = None
_ladder = None


def _get_driver():
    global _driver
    if _driver is None:
        from .driver import ComputerDriver
        _driver = ComputerDriver()
    return _driver


def _get_screen():
    global _screen
    if _screen is None:
        from .screen import Screen
        _screen = Screen()
    return _screen


def _get_ladder():
    global _ladder
    if _ladder is None:
        from .ladder import GroundingLadder
        _ladder = GroundingLadder(driver=_get_driver(), uia=_uia, screen=_get_screen())
    return _ladder


def foreground_hwnd() -> int:
    import ctypes
    return ctypes.windll.user32.GetForegroundWindow()


# ---------------------------------------------------------------------------
# Atom implementations
# ---------------------------------------------------------------------------

def _focus_window(args: dict) -> dict:
    """snapshot args: {title (substring/regex, optional)}"""
    driver = _get_driver()
    title = str(args.get("title", "")).strip()
    if not title:
        fg = foreground_hwnd()
        for wi in driver.list_windows():
            if wi.hwnd == fg:
                return {"success": True, "output": f"Foreground: {wi.process_name}: {wi.title}",
                        "window": wi.to_dict()}
        return {"success": False, "output": "No foreground window"}
    matches = driver.find_windows(title_re=title)
    if not matches:
        return {"success": False, "output": f"No window matching '{title}'"}
    matches.sort(key=lambda w: (not w.is_minimized, -(w.rect[2] - w.rect[0])))
    res = driver.focus_window(matches[0].hwnd)
    res["window"] = matches[0].to_dict()
    time.sleep(0.2)  # let app settle (dialogs, animation)
    return res


def _computer_click(args: dict) -> dict:
    """args: {x, y, double?, button?}"""
    driver = _get_driver()
    x, y = int(args.get("x", -1)), int(args.get("y", -1))
    if x < 0 or y < 0:
        return {"success": False, "output": "Missing x/y coordinates"}
    return driver.click(x=x, y=y, button=str(args.get("button", "left")),
                        double=bool(args.get("double", False)))


def _computer_click_text(args: dict) -> dict:
    """args: {text, hwnd?} — ground a target on the current window and click."""
    driver = _get_driver()
    text = str(args.get("text", "")).strip()
    if not text:
        return {"success": False, "output": "Missing text to click"}
    hwnd = args.get("hwnd") or foreground_hwnd()
    hit = _get_ladder().resolve_target(text, hwnd=int(hwnd))
    data = hit.to_dict()
    if not hit.ok:
        return {"success": False, "output": f"Could not ground '{text}': {hit.reason}",
                "grounding": data}
    r = driver.click(x=hit.x, y=hit.y)
    r["grounding"] = data
    if not r.get("success"):
        where = f"{hit.source} hit '{hit.matched}' at ({hit.x},{hit.y})"
        r["output"] = f"{r.get('output', 'click failed')} [{where}]"
    return r


def _computer_type(args: dict) -> dict:
    """args: {text} — types into the FOCUSED control (caller focuses first)."""
    text = str(args.get("text", ""))
    return _get_driver().type_text(text, via_clipboard=True)


def _computer_key(args: dict) -> dict:
    """args: {combo} e.g. 'ctrl+s', 'alt+f4' (key/keys/hotkey accepted too)."""
    combo = str(
        args.get("combo") or args.get("key") or args.get("keys")
        or args.get("hotkey") or ""
    ).strip()
    if not combo:
        return {
            "success": False,
            "output": "Missing key combo — pass combo='ctrl+f' (argument name: combo)",
        }
    try:
        return _get_driver().press_key(combo)
    except ValueError as e:
        return {"success": False, "output": f"Bad combo: {e}"}


def _computer_scroll(args: dict) -> dict:
    amount = int(args.get("amount", 3) or 3)
    direction = str(args.get("direction", "down"))
    return _get_driver().scroll(amount=amount, direction=direction)


def _computer_paste(args: dict) -> dict:
    text = args.get("text")
    return _get_driver().paste(str(text) if text is not None else None)


def _minimize_all_except(args: dict) -> dict:
    app = str(args.get("app", "")).strip()
    if not app:
        return {"success": False, "output": "Missing 'app' to keep in front"}
    return _get_driver().minimize_all_except(app)


def _wait_for_window(args: dict) -> dict:
    title = str(args.get("title", "")).strip()
    timeout = float(args.get("timeout", 15) or 15)
    if not title:
        return {"success": False, "output": "Missing title"}
    res = _get_driver().wait_for_window(title, timeout=timeout)
    if res.get("success"):
        hwnd = res["window"]["hwnd"]
        time.sleep(0.3)
        _get_driver().focus_window(hwnd)
    return res


_BROWSER_PROCESSES = {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
    "opera.exe", "vivaldi.exe",
}
_BROWSER_TITLE_HINTS = (
    "google chrome", "microsoft edge", "mozilla firefox",
    "brave", "opera", "vivaldi",
)

# Windows.Media.Ocr (WinRT) via PowerShell. The AsTask helper is required in
# PowerShell 5.1 — WinRT async operations do not expose GetAwaiter() there.
# Paths are substituted as single-quoted PowerShell literals; results are
# written to a JSON file so console encoding cannot corrupt non-ASCII text.
_WIN_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ImagePath = '__IMAGE_PATH__'
$OutPath = '__OUT_PATH__'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
        [Windows.Globalization.Language]::new('en-US'))
}
if (-not $engine) {
    @{ available = $false; reason = 'no OCR language pack'; text = '' } |
        ConvertTo-Json -Compress | Set-Content -Path $OutPath -Encoding UTF8
    exit 0
}

$bitmap = $null
$stream = $null
try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) `
        ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) `
        ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) `
        ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) `
        ([Windows.Graphics.Imaging.SoftwareBitmap])
} finally { if ($stream) { $stream.Dispose() } }

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$lines = @()
foreach ($line in $result.Lines) {
    $lines += @{
        text = $line.Text
        rect = @([int]$line.BoundingRect.X, [int]$line.BoundingRect.Y,
                 [int]$line.BoundingRect.Width, [int]$line.BoundingRect.Height)
    }
}
@{
    available = $true
    text      = $result.Text
    width     = [int]$bitmap.PixelWidth
    height    = [int]$bitmap.PixelHeight
    lines     = $lines
} | ConvertTo-Json -Depth 4 -Compress | Set-Content -Path $OutPath -Encoding UTF8
"""


def _ps_quote(value: str) -> str:
    """Escape a value as a PowerShell single-quoted literal."""
    return str(value).replace("'", "''")


def _window_bbox(hwnd: int) -> Optional[tuple]:
    """(left, top, right, bottom) for a window, or None if unavailable."""
    try:
        for win in _get_driver().list_windows():
            if win.hwnd == hwnd and not win.is_minimized:
                l, t, r, b = win.rect
                if r > l and b > t:
                    return (l, t, r, b)
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            if rect.right > rect.left and rect.bottom > rect.top:
                return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    return None


def _active_window_bbox() -> Optional[tuple]:
    hwnd = foreground_hwnd()
    return _window_bbox(hwnd) if hwnd else None


def _browser_bbox() -> Optional[tuple]:
    """Largest visible browser window, falling back to the focused window."""
    try:
        windows = [
            w for w in _get_driver().list_windows()
            if not w.is_minimized
            and w.rect[2] > w.rect[0] and w.rect[3] > w.rect[1]
        ]
    except Exception:
        return _active_window_bbox()
    candidates = [
        w for w in windows
        if (w.process_name or "").lower() in _BROWSER_PROCESSES
        or any(h in (w.title or "").lower() for h in _BROWSER_TITLE_HINTS)
    ]
    if not candidates:
        return _active_window_bbox()
    win = max(
        candidates,
        key=lambda w: (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1]),
    )
    return win.rect


def _capture_screenshot_sync(region: str) -> Optional[str]:
    """Capture the screen/window to a temp PNG. Returns the path or None."""
    import os
    import tempfile

    region = str(region or "full").strip().lower()
    if region not in ("full", "active_window", "browser"):
        region = "full"

    fd, screenshot_path = tempfile.mkstemp(prefix="winbrow_ocr_", suffix=".png")
    os.close(fd)

    try:
        if region == "full":
            try:
                import mss
                with mss.mss() as sct:
                    sct.shot(output=screenshot_path)
                return screenshot_path
            except Exception as e:
                log.debug(f"mss capture failed ({e}); falling back to PIL")

            from PIL import ImageGrab
            ImageGrab.grab().save(screenshot_path)
            return screenshot_path

        bbox = _browser_bbox() if region == "browser" else _active_window_bbox()
        if bbox is None:
            raise RuntimeError("no capturable window found")

        from PIL import ImageGrab
        try:
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
        except TypeError:
            img = ImageGrab.grab(bbox=bbox)
        img.save(screenshot_path)
        return screenshot_path
    except Exception as e:
        log.debug(f"Screenshot capture failed for region '{region}': {e}")
        try:
            os.unlink(screenshot_path)
        except OSError:
            pass
        return None


def _windows_ocr_text(image_path: str) -> str:
    """Run Windows.Media.Ocr in a PowerShell child process; return its text."""
    import base64
    import json
    import os
    import subprocess
    import tempfile

    fd, out_json = tempfile.mkstemp(prefix="winbrow_ocr_", suffix=".json")
    os.close(fd)
    try:
        script = (
            _WIN_OCR_SCRIPT
            .replace("__IMAGE_PATH__", _ps_quote(image_path))
            .replace("__OUT_PATH__", _ps_quote(out_json))
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        proc = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
            ],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        raw = ""
        try:
            with open(out_json, "r", encoding="utf-8-sig", errors="replace") as f:
                raw = f.read().strip()
        except OSError:
            pass
        if not raw:
            detail = (
                (proc.stderr or b"").decode("utf-8", errors="replace")
                or (proc.stdout or b"").decode("utf-8", errors="replace")
            ).strip()
            raise RuntimeError(
                f"Windows OCR returned no output: {detail[:200] or 'unknown error'}"
            )

        data = json.loads(raw)
        if not data.get("available"):
            raise RuntimeError(data.get("reason") or "OCR unavailable")
        return str(data.get("text") or "").strip()
    finally:
        try:
            os.unlink(out_json)
        except OSError:
            pass


def _ocr_output(text: str) -> str:
    """Visible step output: the count plus the extracted text for the planner."""
    from ..utils.clip import clip_middle
    return (
        f"OCR extracted {len(text)} characters:\n"
        + clip_middle(text, 12_000, label="screen_read_text")
    )


def _ocr_sync(region: str) -> dict:
    """Synchronous OCR implementation. Runs in a thread executor."""
    import os
    import shutil

    region = str(region or "full").strip().lower()
    if region not in ("full", "active_window", "browser"):
        region = "full"

    screenshot_path = _capture_screenshot_sync(region)
    if not screenshot_path:
        return {"success": False, "output": "Screenshot capture failed",
                "region": region}

    try:
        try:
            import pytesseract
            from PIL import Image

            tesseract = shutil.which("tesseract")
            if not tesseract:
                for candidate in (
                    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                ):
                    if os.path.isfile(candidate):
                        tesseract = candidate
                        break
            if tesseract:
                pytesseract.pytesseract.tesseract_cmd = tesseract

            with Image.open(screenshot_path) as img:
                text = pytesseract.image_to_string(img).strip()
            return {
                "success": True,
                "text": text,
                "output": _ocr_output(text),
                "method": "pytesseract",
                "region": region,
            }
        except ImportError:
            pass
        except Exception as e:
            log.debug(f"pytesseract OCR failed ({e}); trying Windows OCR")

        try:
            text = _windows_ocr_text(screenshot_path)
            if text:
                return {
                    "success": True,
                    "text": text,
                    "output": _ocr_output(text),
                    "method": "windows_ocr",
                    "region": region,
                }
            return {
                "success": False,
                "output": (
                    "Windows OCR returned no text. The screen may be blank "
                    "or the content unreadable."
                ),
                "region": region,
            }
        except Exception as e:
            return {
                "success": False,
                "output": f"All OCR methods failed: {str(e)}",
                "region": region,
            }
    finally:
        try:
            os.unlink(screenshot_path)
        except OSError:
            pass


async def screen_read_text(region: str = "full") -> dict:
    """Capture the screen or a window and extract text via OCR.

    region: "full" | "active_window" | "browser"
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _ocr_sync, region)
    except Exception as e:
        return {"success": False, "output": f"OCR failed: {str(e)}"}


def _screen_read_text(args: dict) -> dict:
    """Sync dispatcher entry for screen_read_text (runs off the event loop)."""
    return _ocr_sync(str(args.get("region") or "full"))


def _file_dialog_save(args: dict) -> dict:
    """args: {filename?} — drive the focused Save/Save-As dialog: set name, Enter."""
    driver = _get_driver()
    filename = str(args.get("filename", "")).strip()
    driver._guard("save dialog")
    try:
        if filename:
            driver.press_key("ctrl+a")   # select dialog name field text (explorer dialogs)
            time.sleep(0.25)
            driver.type_text(filename, via_clipboard=True)
            time.sleep(0.4)
        driver.press_key("enter")
        time.sleep(0.4)
        # overwrite prompt ("Confirm Save As") when the target already exists
        if driver.wait_for_window("^Confirm Save As$", timeout=0.8).get("success"):
            driver.press_key("y")
            time.sleep(0.4)
        return {"success": True,
                "output": f"Save dialog handled: '{filename}', confirmed with Enter"}
    except Exception as e:
        return {"success": False, "output": f"Save dialog failed: {e}"}


# Actions exposed to the dispatcher below
_ACTIONS: dict[str, Callable[[dict], dict]] = {
    "focus_window": _focus_window,
    "computer_click": _computer_click,
    "computer_click_text": _computer_click_text,
    "computer_type": _computer_type,
    "computer_key": _computer_key,
    "computer_scroll": _computer_scroll,
    "computer_paste": _computer_paste,
    "minimize_all_except": _minimize_all_except,
    "wait_for_window": _wait_for_window,
    "screen_read_text": _screen_read_text,
    "file_dialog_save": _file_dialog_save,
}


def execute_computer_tool(name: str, args: dict) -> dict:
    """Dispatch a computer atom by tool name. Never raises."""
    args = args or {}
    action = _ACTIONS.get(name)
    if action is None:
        return {"success": False, "output": f"Unknown computer tool '{name}'"}
    try:
        return action(args)
    except Exception as e:
        log.warning(f"computer atom '{name}' failed: {e}")
        return {"success": False, "output": f"{name} failed: {e}"}


# ---------------------------------------------------------------------------
# Registry registration (empty script; rendered_script propagation guarded)
# ---------------------------------------------------------------------------

def get_computer_tools():
    """Tool list registered into the ToolRegistry at init."""
    from ..registry import ArgSpec, Tool

    return [
        Tool(
            name="computer_click_text",
            description=(
                "Click a button/menu/text item by description in the focused "
                "app window. Uses UIA, then OCR word-level grounding, and "
                "reports the matched source and coordinates."
            ),
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="text", kind="text", instructions="Text on the control to click, e.g. 'Save As'")],
            examples=["click save", "click the export button", "press play"],
        ),
        Tool(
            name="computer_click",
            description="Click the mouse at screen coordinates in the focused app (for ungrounded pixels).",
            script="",
            impl=COMPUTER_IMPL,
            args=[
                ArgSpec(name="x", kind="text", instructions="screen x coordinate"),
                ArgSpec(name="y", kind="text", instructions="screen y coordinate"),
            ],
            examples=["click there", "click at 900 450"],
        ),
        Tool(
            name="computer_type",
            description="Type text into the currently focused app/control (clipboard+paste).",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="text", kind="text", instructions="Text to type")],
            examples=["type hello into the box", "type the filename"],
        ),
        Tool(
            name="computer_key",
            description=(
                "Press a keyboard shortcut in the focused app, e.g. ctrl+s, "
                "alt+f4, ctrl+f. Use the 'combo' argument."
            ),
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="combo", kind="text",
                          instructions="Key combo like ctrl+shift+s (argument name: combo)")],
            examples=["save with ctrl s", "press escape", "open find with ctrl f"],
        ),
        Tool(
            name="computer_scroll",
            description="Scroll the view in the focused app.",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="direction", kind="enum",
                          instructions="Scroll direction", criteria={"down": "down", "up": "up"},
                          default="down")],
            examples=["scroll down"],
        ),
        Tool(
            name="focus_window",
            description="Bring an app window to the front by title.",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="title", kind="text", instructions="Window title text to find")],
            examples=["focus notepad", "bring paint to the front"],
        ),
        Tool(
            name="wait_for_window",
            description="Wait until an app window with a title exists, then focus it.",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="title", kind="text", instructions="Title text to wait for"),
                  ArgSpec(name="timeout", kind="text", instructions="Seconds to wait", default="10")],
            examples=["wait until notepad opens"],
        ),
        Tool(
            name="minimize_all_except",
            description="Minimize every window except the named app's windows.",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="app", kind="text", instructions="Process or window title to keep, e.g. Code.exe")],
            examples=["minimize everything except vscode", "clean the desktop except chrome"],
        ),
        Tool(
            name="screen_read_text",
            description=(
                "Capture the screen or a window and extract text via OCR. Use only "
                "for native desktop apps with no DOM/API; prefer browser_read_page "
                "for browser content."
            ),
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(
                name="region", kind="enum",
                instructions="What to capture for OCR",
                criteria={
                    "full": "entire screen",
                    "active_window": "currently focused window",
                    "browser": "browser window specifically",
                },
                default="full",
            )],
            examples=[
                "what does the dialog say", "read the popup",
                "read the active window", "read the screen",
            ],
        ),
        Tool(
            name="file_dialog_save",
            description="Complete the focused Save/Save-As dialog with a filename and confirm.",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="filename", kind="text", instructions="File name incl. extension")],
            examples=["save the file as sketch.png", "save it"],
        ),
    ]
