"""
Screen — Capture, Verify, Read Text
====================================
Screen substrate for grounding:

  • capture      — mss screenshot of a window/region/full screen → PNG (+ numpy)
  • compare      — PIL ImageChops diff; cheap "did anything change" verification
  • ocr          — Windows.Media.Ocr (WinRT) inside a PowerShell child process.
                   Works offline on Win10/11, no pip dependency, returns text +
                   per-line bounding boxes in SCREEN coordinates (so clicks match).

The OCR script is dependency-free; if the local language pack is missing the
result reports available=False and callers degrade gracefully.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("winbrow.computer.screen")

try:
    import mss
    MSS_AVAILABLE = True
except ImportError:
    MSS_AVAILABLE = False

try:
    from PIL import Image, ImageChops
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


@dataclass
class ScreenView:
    """A captured rectangle of the screen."""
    image_path: str
    rect: Tuple[int, int, int, int]         # left, top, width, height
    width: int = 0
    height: int = 0
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "image_path": self.image_path,
            "rect": list(self.rect),
            "width": self.width,
            "height": self.height,
        }


@dataclass
class OCRLine:
    text: str
    rect: Tuple[int, int, int, int]   # screen coords: left, top, w, h
    words: List[dict] = field(default_factory=list)  # [{text, rect}] for precise clicks

    def to_dict(self) -> dict:
        return {"text": self.text, "rect": list(self.rect), "words": self.words}


# ---------------------------------------------------------------------------
# Windows OCR via WinRT-in-PowerShell (no pip dependency)
# ---------------------------------------------------------------------------

_PS_OCR_SCRIPT = r"""
param([string]$ImagePath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
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
    # fall back to explicit English
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
        [Windows.Globalization.Language]::new('en-US'))
}
if (-not $engine) {
    @{ available = $false; reason = 'no OCR language pack' } |
        ConvertTo-Json -Compress | Set-Content -Path $OutPath -Encoding UTF8
    exit 0
}

$bitmap = $null
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
    $words = @()
    $left = [int]::MaxValue
    $top = [int]::MaxValue
    $right = [int]::MinValue
    $bottom = [int]::MinValue
    foreach ($word in $line.Words) {
        $wr = $word.BoundingRect
        $wx = [int]$wr.X
        $wy = [int]$wr.Y
        $ww = [int]$wr.Width
        $wh = [int]$wr.Height
        if ($wx -lt $left) { $left = $wx }
        if ($wy -lt $top) { $top = $wy }
        if (($wx + $ww) -gt $right) { $right = $wx + $ww }
        if (($wy + $wh) -gt $bottom) { $bottom = $wy + $wh }
        $words += [ordered]@{
            text = [string]$word.Text
            x = $wx
            y = $wy
            w = $ww
            h = $wh
        }
    }
    if ($words.Count -eq 0) {
        $left = 0; $top = 0; $right = 0; $bottom = 0
    }
    $lines += [ordered]@{
        text  = [string]$line.Text
        x     = $left
        y     = $top
        w     = [Math]::Max(0, $right - $left)
        h     = [Math]::Max(0, $bottom - $top)
        words = $words
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


def _rect4(raw: Any, ox: int = 0, oy: int = 0) -> Tuple[int, int, int, int]:
    """Normalize an OCR rect from dict, list, or 'x y w h' string."""
    if isinstance(raw, dict):
        values = [
            raw.get("x", raw.get("left", 0)),
            raw.get("y", raw.get("top", 0)),
            raw.get("w", raw.get("width", 0)),
            raw.get("h", raw.get("height", 0)),
        ]
    else:
        if isinstance(raw, str):
            parts = raw.replace(",", " ").split()
        elif isinstance(raw, (list, tuple)):
            parts = list(raw)
        else:
            parts = []
        values = []
        for part in parts[:4]:
            try:
                values.append(int(float(part)))
            except (TypeError, ValueError):
                values.append(0)
        values += [0] * (4 - len(values))
    x, y, w, h = values
    return int(x) + ox, int(y) + oy, int(w), int(h)


class Screen:
    """Capture + OCR + diff verification of the physical screen."""

    def __init__(self, driver: Optional[Any] = None):
        self.tmp_dir = Path(tempfile.gettempdir()) / "winbrow_screen"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._shot_id = 0
        self._ocr_available: Optional[bool] = None
        self.driver = driver

    # ------------------------------------------------------------------ capture
    def capture(self, rect: Optional[Tuple[int, int, int, int]] = None) -> ScreenView:
        """Full-screen capture, or region (left, top, width, height)."""
        if not MSS_AVAILABLE:
            raise RuntimeError("mss is required for screen capture (pip install mss)")
        self._shot_id += 1
        out = self.tmp_dir / f"shot_{int(time.time() * 1000)}_{self._shot_id}.png"
        with mss.mss() as sct:
            if rect:
                left, top, width, height = rect
                region = {"left": int(left), "top": int(top), "width": int(width), "height": int(height)}
            else:
                region = sct.monitors[1]  # primary monitor
            shot = sct.grab(region)
            mss.tools.to_png(shot.rgb, shot.size, output=str(out))
        return ScreenView(
            image_path=str(out), rect=(region["left"], region["top"],
                                       region["width"], region["height"]),
            width=region["width"], height=region["height"], ts=time.time(),
        )

    def capture_window(self, window: Any) -> ScreenView:
        """Capture a WindowInfo's rect (its full frame incl. decorations)."""
        l, t, r, b = window.rect
        return self.capture((max(l, 0), max(t, 0), max(r - l, 1), max(b - t, 1)))

    # ------------------------------------------------------------------ compare
    def compare_images(self, path_a: str, path_b: str, threshold: float = 0.02) -> dict:
        """Did these two captures meaningfully change? Cheap content-diff check."""
        if not PIL_AVAILABLE:
            return {"changed": True, "reason": "PIL unavailable"}
        try:
            a = Image.open(path_a).convert("L").resize((256, 144))
            b = Image.open(path_b).convert("L").resize((256, 144))
            diff = ImageChops.difference(a, b)
            hist = diff.histogram()
            total = sum(hist)
            changed_px = sum(hist[16:])  # pixels differing by >6%
            frac = changed_px / max(total, 1)
            return {"changed": frac > threshold, "diff_fraction": round(frac, 4),
                    "threshold": threshold}
        except Exception as e:
            return {"changed": True, "reason": f"compare failed: {e}"}

    # ------------------------------------------------------------------ OCR
    def ocr(self, rect: Optional[Tuple[int, int, int, int]] = None) -> dict:
        """OCR the screen region. Returns {available, text, lines:[{text,rect}]}.

        Line rects are image coordinates OFFSETEBy the capture rect so they are
        directly usable for clicking."""
        from ..windows import run_powershell_sync
        view = self.capture(rect)
        out_json = self.tmp_dir / f"ocr_{self._shot_id}.json"
        script = _PS_OCR_SCRIPT.replace(
            "param([string]$ImagePath, [string]$OutPath)",
            f"param([string]$ImagePath = '{view.image_path}', [string]$OutPath = '{out_json}')",
        )
        if script == _PS_OCR_SCRIPT:
            return {"available": False, "text": "", "lines": [],
                    "error": "OCR script template mismatch"}
        try:
            res = run_powershell_sync(script, timeout=60)
        except Exception as e:
            return {"available": False, "text": "", "lines": [], "error": str(e)}
        raw = out_json.read_text(encoding="utf-8", errors="replace") if out_json.exists() else ""
        raw = raw.strip().lstrip("\ufeff")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {"available": False, "text": "", "lines": [],
                    "error": f"OCR script failed: {str(res.get('stderr', ''))[:200]}"}
        if not data.get("available"):
            self._ocr_available = False
            return {"available": False, "text": "", "lines": [],
                    "reason": data.get("reason", "OCR unavailable")}
        self._ocr_available = True
        ox, oy = view.rect[0], view.rect[1]
        lines = []
        raw_lines = data.get("lines") or []
        if isinstance(raw_lines, dict):
            raw_lines = [raw_lines]
        for ln in raw_lines:
            if not isinstance(ln, dict):
                continue
            raw_words = ln.get("words") or []
            if isinstance(raw_words, dict):
                raw_words = [raw_words]
            words = []
            for word in raw_words:
                if not isinstance(word, dict):
                    continue
                words.append({
                    "text": str(word.get("text", "")),
                    "rect": _rect4(word, ox, oy),
                })
            lines.append(OCRLine(
                text=str(ln.get("text", "")),
                rect=_rect4(ln.get("rect", ln), ox, oy),
                words=words,
            ).to_dict())
        return {
            "available": True,
            "text": data.get("text", ""),
            "lines": lines,
            "image_path": str(view.image_path),
        }

    @property
    def ocr_available(self) -> Optional[bool]:
        return self._ocr_available


def compare_images(path_a: str, path_b: str, threshold: float = 0.02) -> dict:
    """Module-level convenience (used by tests without a Screen instance)."""
    return Screen().compare_images(path_a, path_b, threshold)
