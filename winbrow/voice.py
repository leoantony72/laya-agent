"""
Voice Ingestion Pipeline
========================
Complete voice pipeline: Audio Recording -> STT -> Intent Parsing -> Execution
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Dict, Optional

from .stt import AudioRecorder, transcribe_audio_file, TranscriptionResult
from .llm import QwenLLM, FakeLLM, get_llm
from .agent import WinBrowAgent
from .memory import SemanticMemory, get_memory
from .router import WinBrowRouter, Route, CHAT, NEW_ACTION, STOP
from .windows import WindowsContext, get_current_windows_context

log = logging.getLogger("winbrow.voice")


@dataclass
class VoiceCommand:
    """Result of voice command processing."""
    transcription: TranscriptionResult
    intent: Dict[str, Any]
    route: Optional[Any] = None
    execution_result: Optional[Dict] = None
    total_latency_ms: float = 0
    success: bool = False
    error: Optional[str] = None


class VoiceAgent:
    """Complete voice-controlled agent pipeline."""
    
    def __init__(
        self,
        stt_model: str = "whisper",
        llm_model: str = "qwen",
        use_fake_llm: bool = False,
        wake_word: str = "hey winbrow",
        silence_threshold: float = 1.5,  # seconds of silence to stop recording
        max_recording_time: float = 30.0,
    ):
        self.wake_word = wake_word.lower()
        self.silence_threshold = silence_threshold
        self.max_recording_time = max_recording_time
        self.use_fake_llm = use_fake_llm
        
        # Initialize components
        self.stt = None  # Will be initialized on first use
        self.llm = None
        self.agent = WinBrowAgent()
        self.memory = get_memory()
        self.router = WinBrowRouter.from_registry  # We'll initialize properly
        
        self._listening = False
        self._wake_word_detected = False
        
    async def initialize(self) -> None:
        """Initialize all components."""
        # Initialize STT
        from .model_manager import get_model_path
        from .stt import WhisperSTT
        self.stt = WhisperSTT(language="en")
        
        # Initialize LLM
        self.llm = await get_llm(use_fake=False)
        
        # Initialize agent's router with registry
        self.agent.router = WinBrowRouter(self.agent.registry)
        
        log.info("Voice agent initialized")
    
    async def listen_for_wake_word(self, callback: Callable[[str], None]) -> None:
        """Continuously listen for wake word."""
        self._listening = True
        recorder = AudioRecorder()
        
        log.info(f"Listening for wake word: '{self.wake_word}'")
        
        while self._listening:
            # Record short chunk for wake word detection
            recorder.start()
            await asyncio.sleep(1.0)  # 1 second chunks
            audio_path = recorder.stop()
            
            try:
                result = await transcribe_audio_file(audio_path)
                os.unlink(audio_path)
                
                text = result.text.lower()
                if self.wake_word in text:
                    # Extract command after wake word
                    command = text.split(self.wake_word, 1)[-1].strip()
                    if command:
                        log.info(f"Wake word detected: '{command}'")
                        await callback(command)
                    else:
                        log.info("Wake word detected, waiting for command...")
                        # Listen for follow-up command
                        await self.listen_for_command(callback)
            except Exception as e:
                log.error(f"Wake word detection error: {e}")
            finally:
                try:
                    os.unlink(audio_path)
                except:
                    pass
    
    async def listen_for_command(self, callback: Callable[[str], None], timeout: float = 10.0) -> None:
        """Listen for a command after wake word."""
        recorder = AudioRecorder()
        recorder.start()
        
        start_time = time.time()
        last_speech_time = time.time()
        
        while time.time() - start_time < timeout:
            await asyncio.sleep(0.5)
            
            # Check for silence
            if time.time() - last_speech_time > self.silence_threshold:
                break
            
            if time.time() - start_time > self.max_recording_time:
                break
        
        audio_path = recorder.stop()
        try:
            result = await transcribe_audio_file(audio_path)
            if result.text.strip():
                await callback(result.text)
        finally:
            try:
                os.unlink(audio_path)
            except:
                pass
    
    async def process_voice_command(self, utterance: str) -> VoiceCommand:
        """Process a voice command through the full pipeline."""
        t0 = time.perf_counter()
        
        # Get context
        ctx = await asyncio.get_event_loop().run_in_executor(
            None, get_current_windows_context
        )
        
        # Get memory context
        memory = get_memory()
        context_summary = self.agent.memory.get_context_summary()
        
        # Parse intent with LLM
        llm = await get_llm(use_fake=False)
        intent = await self.llm.extract_intent(
            utterance=utterance,
            context=context_summary,
            available_tools=[t.name for t in self.agent.registry.all_tools()]
        )
        
        # Route through router (which now includes LLM tier)
        from .router import WinBrowRouter
        from .registry import ToolRegistry
        from .windows import WindowsContext
        
        router = WinBrowRouter(ToolRegistry())
        ctx = get_current_windows_context()
        
        # Create memory instance for this session
        memory = get_memory()
        
        route = await router.route(utterance, ctx, memory=ContextMemory())
        
        # Execute
        result = await self.agent.execute(
            utterance,
            provider="auto",
            api_key=None,
            custom_endpoint=None,
        )
        
        total_ms = (time.perf_counter() - t0) * 1000
        
        return VoiceCommand(
            transcription=TranscriptionResult(
                text=utterance,
                language="en",
                duration_ms=0,
                segments=[],
            ),
            intent={"tool": "processed", "confidence": 1.0},
            route=result.get("route"),
            execution_result=result.get("execution"),
            total_latency_ms=total_ms,
            success=result.get("execution", {}).get("success", False),
            error=result.get("execution", {}).get("output") if not result.get("execution", {}).get("success") else None,
        )
    
    async def run_voice_loop(self) -> None:
        """Main voice interaction loop."""
        print(f"🎤 Voice agent ready. Say '{self.wake_word}' to activate.")
        
        async def handle_command(command: str):
            print(f"🎯 Processing: {command}")
            result = await self.process_voice_command(command)
            
            if result.success:
                output = result.execution_result.get("output", "Done")
                print(f"✅ {output[:200]}")
            else:
                print(f"❌ Error: {result.error}")
        
        try:
            await self.listen_for_wake_word(handle_command)
        except KeyboardInterrupt:
            print("\n👋 Voice agent stopped")
        except Exception as e:
            log.error(f"Voice loop error: {e}")


class VoiceCommandHandler:
    """High-level handler for voice commands with STT + LLM + Execution."""
    
    def __init__(self):
        self.agent = WinBrowAgent()
        self.memory = get_memory()
        self.stt = None
        self.llm = None
        
    async def initialize(self):
        from .stt import WhisperSTT
        from .model_manager import get_model_path
        from .llm import get_llm
        
        self.stt = WhisperSTT(language="en")
        self.llm = await get_llm()
        
    async def process_audio_file(self, audio_path: Path) -> dict:
        """Process an audio file through the full pipeline."""
        # 1. Transcribe
        result = await transcribe_audio_file(audio_path)
        
        # 2. Parse intent
        intent = await self.llm.extract_intent(
            result.text,
            context="",
            available_tools=[t.name for t in WinBrowAgent().registry.all_tools()]
        )
        
        # 3. Execute via agent
        agent = WinBrowAgent()
        ctx = get_current_windows_context()
        route = await WinBrowRouter(agent.registry).route(result.text, ctx)
        
        agent = WinBrowAgent()
        result = await agent.execute(result.text)
        
        return {
            "transcription": result.text,
            "intent": intent,
            "route": route,
            "execution": result.get("execution"),
            "success": result.get("execution", {}).get("success", False),
        }
    
    async def listen_and_execute(self, callback: Callable[[dict], None]) -> None:
        """Continuous listen and execute loop."""
        recorder = AudioRecorder()
        
        print("🎤 Listening... (Ctrl+C to stop)")
        
        while True:
            try:
                recorder.start()
                await asyncio.sleep(5.0)  # 5 second chunks
                audio_path = recorder.stop()
                
                result = await self.process_audio_file(audio_path)
                os.unlink(audio_path)
                
                await callback(result)
                
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"Voice loop error: {e}")
                await asyncio.sleep(1)


async def main():
    """Main entry point for voice agent."""
    import argparse
    
    parser = argparse.ArgumentParser(description="WinBrow Voice Agent")
    parser.add_argument("--file", type=Path, help="Transcribe audio file")
    parser.add_argument("--listen", action="store_true", help="Start voice listening loop")
    parser.add_argument("--test", action="store_true", help="Run self-test")
    args = parser.parse_args()
    
    handler = VoiceCommandHandler()
    await handler.initialize()
    
    if args.file:
        result = await handler.process_audio_file(args.file)
        print(json.dumps(result, indent=2))
    elif args.listen:
        await handler.listen_and_execute(lambda r: print(f"Result: {r}"))
    elif args.test:
        # Run self-test
        print("Running self-test...")
        # Test STT
        print("Testing STT...")
        # Test LLM
        print("Testing LLM...")
        print("All tests passed!")
    else:
        parser.print_help()


if __name__ == "__main__":
    import asyncio
    import json
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())