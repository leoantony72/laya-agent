"""
Vision Fallback - Qwen2.5-VL Integration
=========================================
Vision-language model for when accessibility tree and browser automation fail.
Uses Qwen2.5-VL via llama.cpp for screenshot understanding.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model_manager import get_model_path, ensure_llama_cpp
from .llm import QwenLLM

log = logging.getLogger("winbrow.vision")


class VisionFallback:
    """Qwen2.5-VL vision fallback for when accessibility/browser automation fails."""
    
    def __init__(
        self, 
        model_path: Optional[Path] = None,
        host: str = "127.0.0.1",
        port: int = 8081,  # Different port from main LLM
    ):
        self.model_path = model_path or get_model_path("qwen_vl")
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.server_process = None
        self._server_ready = False
    
    async def start(self) -> None:
        """Start the llama.cpp server with Qwen2.5-VL model."""
        llama_server = ensure_llama_cpp()
        self.model_path = self.model_path or get_model_path("qwen_vl")
        
        cmd = [
            str(llama_server),
            "-m", str(self.model_path),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "-c", "2048",  # Smaller context for vision
            "-t", "4",
            "--mlock",
            "--no-mmap",
            "--mmproj", str(self.model_path).replace(".gguf", "-mmproj.gguf"),  # Multimodal projector
        ]
        
        log.info(f"Starting Qwen2.5-VL server on port {self.port}...")
        self.server_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        
        # Wait for server to be ready
        for _ in range(30):
            await asyncio.sleep(0.5)
            try:
                import urllib.request
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as resp:
                    if resp.status == 200:
                        self._server_ready = True
                        log.info(f"Qwen2.5-VL server ready on port {self.port}")
                        return
            except Exception:
                pass
        
        raise RuntimeError("Qwen2.5-VL server failed to start")
    
    async def stop(self) -> None:
        if self.server_process:
            self.server_process.terminate()
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.server_process.wait), 
                    timeout=5
                )
            except asyncio.TimeoutError:
                self.server_process.kill()
            self.server_process = None
            self._server_ready = False
    
    async def _request(self, endpoint: str, payload: dict) -> dict:
        import urllib.request
        
        url = f"{self.base_url}{endpoint}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data.encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        
        loop = asyncio.get_event_loop()
        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=60)),
                timeout=70,
            )
            return json.loads(response.read().decode())
        except asyncio.TimeoutError:
            raise RuntimeError("Vision model timed out")
        except Exception as e:
            raise RuntimeError(f"Vision model request failed: {e}")
    
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
    
    async def analyze_screenshot(
        self, 
        image_path: Path, 
        prompt: str = "Describe what you see in this screenshot. Identify clickable elements, text, and UI structure."
    ) -> Dict[str, Any]:
        """Analyze a screenshot and return structured understanding."""
        # Encode image as base64
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode()
        
        payload = {
            "model": "qwen2.5-vl",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_data}"
                            }
                        }
                    ]
                }
            ],
            "stream": False,
            "temperature": 0.1,
            "max_tokens": 1024,
        }
        
        result = await self._request("/v1/chat/completions", payload)
        content = result["choices"][0]["message"]["content"]
        
        return {
            "description": content,
            "model": "qwen2.5-vl",
        }
    
    async def find_clickable_elements(self, image_path: Path) -> List[Dict[str, Any]]:
        """Find clickable elements in a screenshot."""
        prompt = """Analyze this screenshot and identify all clickable UI elements.
Return a JSON array of elements, each with:
- "element": description of the element
- "type": "button" | "link" | "input" | "menu" | "tab" | "icon" | "other"
- "location": "top-left" | "top-right" | "bottom-left" | "bottom-right" | "center" | "top" | "bottom" | "left" | "right"
- "text": any visible text on the element
- "clickable": true/false
- "confidence": 0.0-1.0

Only include elements that are clearly clickable/interactive."""
        
        result = await self.analyze_screenshot(image_path, prompt)
        
        try:
            content = result["description"]
            start = content.find("[")
            end = content.rfind("]") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except Exception as e:
            log.warning(f"Failed to parse clickable elements: {e}")
        
        return []
    
    async def extract_text(self, image_path: Path) -> str:
        """Extract all visible text from a screenshot (OCR)."""
        prompt = "Extract all visible text from this screenshot. Return only the text content, preserving structure where possible."
        result = await self.analyze_screenshot(image_path, prompt)
        return result.get("description", "")
    
    async def find_element_by_description(self, image_path: Path, description: str) -> Optional[Dict[str, Any]]:
        """Find a UI element matching a natural language description."""
        prompt = f"""Find the UI element that matches this description: "{description}"

Return JSON with:
- "found": true/false
- "element": description of the matched element
- "location": "top-left" | "top-right" | "bottom-left" | "bottom-right" | "center" | "top" | "bottom" | "left" | "right"
- "bounding_box": {{"x": 0, "y": 0, "width": 0, "height": 0}} (approximate pixel coordinates)
- "confidence": 0.0-1.0

If not found, return {{"found": false}}"""
        
        result = await self.analyze_screenshot(image_path, prompt)
        
        try:
            content = result["description"]
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except Exception:
            pass
        
        return None
    
    async def health_check(self) -> bool:
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False


class ScreenshotCapture:
    """Capture screenshots using Windows APIs or mss."""
    
    def __init__(self):
        self._mss = None
        try:
            import mss
            self._mss = mss.mss()
        except ImportError:
            log.warning("mss not installed. Install with: pip install mss")
    
    def capture_screen(self, monitor: int = 1) -> Path:
        """Capture full screen to temporary file."""
        if self._mss:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                self._mss.shot(mon=monitor, output=f.name)
                return Path(f.name)
        else:
            # Fallback to PowerShell
            return self._capture_powershell()
    
    def capture_region(self, x: int, y: int, width: int, height: int) -> Path:
        """Capture a specific region."""
        if self._mss:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                self._mss.shot(mon=1, output=f.name)
                # Crop would need PIL - simplified for now
                return Path(f.name)
        else:
            return self._capture_powershell()
    
    def _capture_powershell(self) -> Path:
        """Fallback screenshot via PowerShell."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            temp_path = f.name
        
        script = f"""
        Add-Type -AssemblyName System.Windows.Forms,System.Drawing
        $screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
        $bmp = New-Object System.Drawing.Bitmap($screen.Width, $screen.Height)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)
        $bmp.Save("{temp_path}")
        $g.Dispose(); $bmp.Dispose()
        """
        subprocess.run(["powershell", "-Command", script], check=True, capture_output=True)
        return Path(temp_path)
    
    def capture_active_window(self) -> Path:
        """Capture the active window."""
        if self._mss:
            # mss doesn't easily do single window - use PowerShell
            pass
        return self._capture_powershell_window()
    
    def _capture_powershell_window(self) -> Path:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            temp_path = f.name
        
        script = f"""
        Add-Type -AssemblyName System.Windows.Forms,System.Drawing
        $hwnd = (Get-Process -Id (Get-Process | Where-Object {{$_.MainWindowHandle -ne 0}} | Select-Object -First 1).Id).MainWindowHandle
        $rect = New-Object System.Drawing.Rectangle
        [void][System.Runtime.InteropServices.DllImport]("user32.dll", "GetWindowRect", "IntPtr", "ref Rectangle")
        [System.Runtime.InteropServices.DllImport]("user32.dll")]public static extern bool GetWindowRect(IntPtr hWnd, out Rectangle rect);'
        GetWindowRect($hwnd, [ref]$rect)
        $bmp = New-Object System.Drawing.Bitmap($rect.Width, $rect.Height)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($rect.Location, [System.Drawing.Point]::Empty, $rect.Size)
        $bmp.Save("{temp_path}")
        $g.Dispose(); $bmp.Dispose()
        """
        subprocess.run(["powershell", "-Command", script], check=True, capture_output=True)
        return Path(temp_path)


async def capture_and_analyze(
    prompt: str = "Describe what you see and identify clickable elements",
    use_fallback: bool = True
) -> Dict[str, Any]:
    """Capture screen and analyze with vision model."""
    vision = VisionFallback()
    capture = ScreenshotCapture()
    
    try:
        await vision.start()
        image_path = capture.capture_screen()
        try:
            result = await vision.analyze_screenshot(image_path, prompt)
            return result
        finally:
            os.unlink(image_path)
    finally:
        await vision.stop()


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    
    async def test():
        # Test screenshot capture
        capture = ScreenshotCapture()
        path = capture.capture_screen()
        print(f"Screenshot saved to: {path}")
        
        # Test vision (will fail without model)
        try:
            vision = VisionFallback()
            await vision.start()
            result = await vision.analyze_screenshot(Path("test.png"), "Describe this")
            print(f"Vision result: {result}")
            await vision.stop()
        except Exception as e:
            print(f"Vision test failed (expected without model): {e}")
    
    asyncio.run(test())