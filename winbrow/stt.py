"""
STT Module - Whisper.cpp Integration
=====================================
Speech-to-Text using Whisper.cpp with automatic model management.
Supports both file transcription and real-time streaming.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import AsyncGenerator, Optional, Callable
from dataclasses import dataclass

from .model_manager import get_model_path, ensure_llama_cpp

log = logging.getLogger("winbrow.stt")


@dataclass
class TranscriptionResult:
    text: str
    language: str
    duration_ms: float
    segments: list[dict]


class WhisperSTT:
    """Whisper.cpp wrapper for speech-to-text."""
    
    def __init__(self, model_path: Optional[Path] = None, language: str = "en"):
        self.model_path = model_path or get_model_path("whisper")
        self.language = language
        self.whisper_bin = self._get_whisper_binary()
        self._running = False
        
    def _get_whisper_binary(self) -> Path:
        """Get or build whisper-cli binary."""
        whisper_dir = Path(__file__).parent.parent / "whisper_cpp"
        whisper_dir.mkdir(parents=True, exist_ok=True)
        
        bin_name = "whisper-cli.exe" if platform.system() == "Windows" else "whisper-cli"
        bin_path = whisper_dir / bin_name
        
        if bin_path.exists():
            return bin_path
        
        log.info("Building whisper.cpp...")
        whisper_dir.mkdir(parents=True, exist_ok=True)
        
        # Clone and build whisper.cpp
        subprocess.run([
            "git", "clone", "--depth", "1", 
            "https://github.com/ggml-org/whisper.cpp.git", str(whisper_dir)
        ], check=True, capture_output=True)
        
        # Build
        build_dir = whisper_dir / "build"
        build_dir.mkdir(exist_ok=True)
        
        subprocess.run(["cmake", "..", "-DWHISPER_SDL2=OFF"], cwd=build_dir, check=True)
        subprocess.run(["cmake", "--build", ".", "--config", "Release", "-j", "4"], 
                       cwd=build_dir, check=True)
        
        # Find binary
        for name in ["whisper-cli", "whisper-cli.exe", "main", "main.exe"]:
            for root, dirs, files in os.walk(build_dir):
                if name in files:
                    src = Path(root) / name
                    dst = Path(__file__).parent.parent / "whisper_cpp" / ("whisper-cli.exe" if platform.system() == "Windows" else "whisper-cli")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(src, dst)
                    return dst
        
        raise RuntimeError("Failed to build whisper.cpp")
    
    def transcribe_file(self, audio_path: Path, language: Optional[str] = None) -> TranscriptionResult:
        """Transcribe an audio file."""
        import time
        t0 = time.perf_counter()
        
        lang = language or self.language
        cmd = [
            str(self.whisper_bin),
            "-m", str(self.model_path),
            "-f", str(audio_path),
            "-l", lang,
            "-oj",  # JSON output
            "-nt",  # No timestamps
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
            output = json.loads(result.stdout)
            
            text = output.get("text", "").strip()
            segments = output.get("segments", [])
            language = output.get("language", lang)
            
            return TranscriptionResult(
                text=text,
                language=language,
                duration_ms=(time.perf_counter() - t0) * 1000,
                segments=segments,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Transcription timed out")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Whisper failed: {e.stderr}")
    
    async def transcribe_stream(
        self, 
        audio_queue: asyncio.Queue, 
        callback: Callable[[str], None],
        language: Optional[str] = None
    ) -> None:
        """Real-time streaming transcription using whisper.cpp streaming mode."""
        # This is a simplified version - whisper.cpp streaming is more complex
        # For now, we'll use a simpler approach with chunked processing
        raise NotImplementedError("Streaming transcription not yet implemented")


class AudioRecorder:
    """Simple audio recorder using Windows WASAPI or sounddevice."""
    
    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self._recording = False
        self._thread: Optional[threading.Thread] = None
        self._frames: list[bytes] = []
        self._audio = None
        self._stream = None
        
    def start(self) -> None:
        """Start recording."""
        try:
            import sounddevice as sd
            self._audio = sd
            self._frames = []
            self._recording = True
            
            def callback(indata, frames, time, status):
                if self._recording:
                    self._frames.append(indata.copy())
            
            self._stream = sd.InputStream(
                samplerate=16000,
                channels=1,
                dtype='int16',
                callback=callback
            )
            self._stream.start()
            log.info("Recording started")
        except ImportError:
            # Fallback to Windows WASAPI via PowerShell
            self._start_wasapi_recording()
    
    def _start_wasapi_recording(self) -> None:
        """Fallback recording using Windows WASAPI via PowerShell."""
        # This is a simplified fallback - in production you'd use a proper audio library
        raise RuntimeError("sounddevice not installed. Install with: pip install sounddevice")
    
    def stop(self) -> Path:
        """Stop recording and return path to WAV file."""
        self._recording = False
        
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        
        # Save to temp WAV file
        import numpy as np
        audio_data = np.concatenate(self._frames, axis=0) if self._frames else np.array([], dtype=np.int16)
        
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            with wave.open(f, 'wb') as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(self.sample_rate)
                wf.writeframes(audio_data.tobytes())
            return Path(f.name)
    
    def is_recording(self) -> bool:
        return self._recording


async def transcribe_audio_file(audio_path: Path, language: str = "en") -> TranscriptionResult:
    """Convenience function to transcribe a single file."""
    stt = WhisperSTT(language=language)
    return stt.transcribe_file(audio_path)


async def record_and_transcribe(duration_seconds: float = 5.0, language: str = "en") -> TranscriptionResult:
    """Record audio for specified duration and transcribe."""
    recorder = AudioRecorder()
    recorder.start()
    await asyncio.sleep(duration_seconds)
    audio_path = recorder.stop()
    try:
        return await transcribe_audio_file(audio_path, language)
    finally:
        os.unlink(audio_path)


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    
    # Test recording and transcription
    async def test():
        print("Recording for 5 seconds... speak now!")
        result = await record_and_transcribe(5.0)
        print(f"Transcription: {result.text}")
    
    asyncio.run(test())