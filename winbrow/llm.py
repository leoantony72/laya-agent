"""
Conversational LLM - Qwen3.5 0.8B via llama.cpp
================================================
Fast, local conversational LLM for intent parsing and semantic understanding.
Uses llama.cpp server for fast inference with Qwen3.5 0.8B 4-bit GGUF.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from .model_manager import get_model_path, ensure_llama_cpp

log = logging.getLogger("winbrow.llm")


class QwenLLM:
    """Qwen3.5 0.8B LLM via llama.cpp server."""
    
    def __init__(
        self, 
        model_path: Optional[Path] = None,
        host: str = "127.0.0.1",
        port: int = 8080,
        n_ctx: int = 4096,
        n_threads: int = 4,
    ):
        self.model_path = model_path or get_model_path("qwen")
        self.host = host
        self.port = port
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.base_url = f"http://{host}:{port}"
        self.server_process: Optional[subprocess.Popen] = None
        self._server_ready = False
        
    async def start(self) -> None:
        """Start the llama.cpp server."""
        if self._server_ready:
            return
        
        llama_server = ensure_llama_cpp()
        self.model_path = self.model_path or get_model_path("qwen")
        
        cmd = [
            str(self.llama_server),
            "-m", str(self.model_path),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "-c", "4096",
            "-t", str(self.n_threads),
            "--mlock",
            "--no-mmap",
        ]
        
        log.info(f"Starting llama.cpp server on {self.base_url}...")
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
                with urllib.request.urlopen(f"{self.base_url}/health", timeout=2) as resp:
                    if resp.status == 200:
                        self._server_ready = True
                        log.info(f"llama.cpp server ready on {self.base_url}")
                        return
            except Exception:
                pass
        
        raise RuntimeError("llama.cpp server failed to start")
    
    async def stop(self) -> None:
        """Stop the llama.cpp server."""
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
    
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"
    
    async def _request(self, endpoint: str, payload: dict) -> dict:
        """Make HTTP request to llama.cpp server."""
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
                loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=30)),
                timeout=35,
            )
            return json.loads(response.read().decode())
        except asyncio.TimeoutError:
            raise RuntimeError("LLM request timed out")
        except Exception as e:
            raise RuntimeError(f"LLM request failed: {e}")
    
    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 512,
        stop: Optional[List[str]] = None,
    ) -> str:
        """Chat completion endpoint."""
        payload = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        
        result = await self._request("/v1/chat/completions", payload)
        return result["choices"][0]["message"]["content"]
    
    async def completion(
        self,
        prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 256,
        stop: Optional[List[str]] = None,
    ) -> str:
        """Text completion endpoint."""
        payload = {
            "prompt": prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        
        result = await self._request("/v1/completions", payload)
        return result["choices"][0]["text"]
    
    async def extract_intent(
        self, 
        utterance: str, 
        context: str = "",
        available_tools: List[str] = None
    ) -> Dict[str, Any]:
        """Extract structured intent from user utterance."""
        
        tools_list = "\n".join(f"- {t}" for t in (available_tools or []))
        
        system_prompt = f"""You are an intent parser for a Windows desktop agent. 
Parse the user's utterance into a structured intent.

Available tools: {tools_list if available_tools else "See context"}

Current context: {context}

Output ONLY valid JSON with this structure:
{{
  "intent": "tool_name_or_chat",
  "confidence": 0.0-1.0,
  "tool": "tool_name_if_applicable",
  "arguments": {{"arg_name": "value"}},
  "requires_clarification": false,
  "clarification_question": "",
  "reasoning": "brief explanation"
}}

Rules:
- If the utterance maps to a known tool, set intent="tool" and provide the tool name
- If it's general chat/greeting, set intent="chat"
- If ambiguous or missing required args, set requires_clarification=true
- Only use tools from the available list
- Be concise"""

        user_prompt = f"User said: \"{utterance}\""
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        
        response = await self.chat_completion(
            messages=messages,
            temperature=0.1,
            max_tokens=512,
            stop=["}\n", "```"],
        )
        
        # Parse JSON from response
        try:
            # Extract JSON from response
            content = response.strip()
            # Find JSON object
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                json_str = content[start:end]
                return json.loads(json_str)
        except Exception as e:
            log.warning(f"Failed to parse LLM intent: {e}")
        
        # Fallback
        return {
            "intent": "new_action",
            "confidence": 0.3,
            "tool": None,
            "arguments": {},
            "requires_clarification": True,
            "clarification_question": "Could you clarify what you'd like me to do?",
            "reasoning": "Failed to parse intent from LLM response"
        }
    
    async def resolve_reference(
        self,
        reference: str,
        context: str,
        recent_items: List[Dict] = None
    ) -> Optional[str]:
        """Resolve a reference like 'that file', 'it', 'the document' to a concrete path."""
        
        system_prompt = """You are a reference resolver for a desktop agent.
Given a reference like 'that file', 'it', 'the document', and recent context,
resolve it to the most likely file/path.

Recent context:
{context}

Recent items:
{recent_items}

Reference to resolve: {reference}

Output JSON:
{{
  "resolved_path": "absolute/path/or/empty",
  "confidence": 0.0-1.0,
  "reasoning": "explanation"
}}"""

        recent_str = json.dumps(recent_items or [], indent=2)
        
        user_prompt = f"Context: {context}\n\nReference: {reference}"
        
        messages = [
            {"role": "system", "content": system_prompt.format(
                context=context,
                recent_items=recent_str,
                reference=reference
            )},
            {"role": "user", "content": user_prompt},
        ]
        
        response = await self.chat_completion(
            messages=messages,
            temperature=0.1,
            max_tokens=256,
        )
        
        try:
            content = response.strip()
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(content[start:end])
                return result.get("resolved_path") if result.get("confidence", 0) > 0.5 else None
        except Exception:
            pass
        return None
    
    async def health_check(self) -> bool:
        """Check if the LLM server is healthy."""
        try:
            import urllib.request
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False


class FakeLLM:
    """Fake LLM for testing without a real model."""
    
    def __init__(self):
        self.responses = []
    
    def add_response(self, response: str):
        self.responses.append(response)
    
    async def extract_intent(self, utterance: str, context: str = "", available_tools: List[str] = None) -> Dict[str, Any]:
        if self.responses:
            return json.loads(self.responses.pop(0))
        # Default fallback
        return {
            "intent": "new_action",
            "confidence": 0.5,
            "tool": None,
            "arguments": {},
            "requires_clarification": True,
            "clarification_question": "Could you clarify?",
            "reasoning": "Test mode"
        }
    
    async def resolve_reference(self, reference: str, context: str, recent_items: List[Dict] = None) -> Optional[str]:
        return None
    
    async def health_check(self) -> bool:
        return True


async def get_llm(use_fake: bool = False) -> "QwenLLM | FakeLLM":
    """Get LLM instance (real or fake for testing)."""
    if use_fake:
        return FakeLLM()
    
    llm = QwenLLM()
    await llm.start()
    return llm


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    
    async def test():
        # Test with fake LLM
        llm = FakeLLM()
        llm.add_response(json.dumps({
            "intent": "tool",
            "confidence": 0.95,
            "tool": "open_file",
            "arguments": {"target": "report.pdf", "app": "chrome"},
            "requires_clarification": False,
            "clarification_question": "",
            "reasoning": "User wants to open a PDF file"
        }))
        
        result = await llm.extract_intent("open report.pdf in chrome")
        print(f"Intent: {result}")
        
        # Test reference resolution
        ref = await llm.resolve_reference(
            "open that file",
            "Just opened report.pdf",
            [{"path": "/home/user/report.pdf", "label": "report.pdf"}]
        )
        print(f"Reference resolved to: {ref}")
    
    asyncio.run(test())