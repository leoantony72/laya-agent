"""
Persistent PowerShell Host (M0 performance work)
================================================
A single long-lived `powershell.exe` process fed over stdin, replacing the
per-command process spawn (1–3 s warm, up to ~25 s cold) for tool execution.

Protocol per call: the script is sent verbatim, followed by sentinel lines
carrying a per-call UUID plus the `$?` success flag. stdout and stderr are
read concurrently until their sentinels. The same result dict shape as the
one-shot runner is returned, so callers are unaffected.

Self-healing: any timeout, broken pipe, or unexpected output kills the
process; the next call transparently restarts it. An `asyncio.Lock`
serializes commands. Never raises — all failures become error dicts.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import sys
import time
import uuid
from typing import Any, Optional

# Ensure Windows proactor event loop for subprocess support
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

log = logging.getLogger("winbrow.pshost")

_STARTUP_TIMEOUT = 30.0


class PersistentPS:
    """One reusable PowerShell process. Create via module singleton."""

    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pid: Optional[int] = None
        atexit.register(self._kill_at_exit)

    def _kill_at_exit(self) -> None:
        try:
            if self._pid:
                # On Windows, use taskkill instead of os.kill with SIGTERM
                import subprocess
                subprocess.run(["taskkill", "/F", "/PID", str(self._pid)], 
                             capture_output=True, timeout=5)
        except Exception:
            pass

    async def _ensure_started(self) -> bool:
        if self._proc is not None and self._proc.returncode is None:
            return True
        try:
            # Check if current loop supports subprocesses
            loop = asyncio.get_event_loop()
            if not (hasattr(loop, 'create_subprocess_exec') or hasattr(loop, 'subprocess_exec')):
                log.debug("Event loop doesn't support subprocesses, skipping persistent host")
                return False
            
            self._proc = await asyncio.create_subprocess_exec(
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-Command", "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
            )
            self._pid = self._proc.pid
            assert self._proc.stdin is not None
            self._proc.stdin.write(
                b"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
            )
            await asyncio.wait_for(self._proc.stdin.drain(), timeout=_STARTUP_TIMEOUT)
            
            # Quick health check - send a simple command
            test_marker = "__WB_TEST__"
            self._proc.stdin.write(f'Write-Output "{test_marker}"\n'.encode("utf-8"))
            await asyncio.wait_for(self._proc.stdin.drain(), timeout=5)
            
            # Read the test output
            raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=5)
            if not raw:
                raise RuntimeError("No response from PowerShell health check")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line != test_marker:
                raise RuntimeError(f"Unexpected health check response: {line}")
            
            log.info(f"Persistent PowerShell host started (PID: {self._pid})")
            return True
        except Exception as e:
            import traceback
            log.warning(f"Persistent PowerShell failed to start: {type(e).__name__}: {e}")
            log.debug(f"Traceback: {traceback.format_exc()}")
            await self._teardown()
            return False

    async def _teardown(self) -> None:
        proc, self._proc = self._proc, None
        self._pid = None
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass

    async def _read_until(
        self,
        stream: asyncio.StreamReader,
        marker: str,
        timeout: float,
    ) -> tuple[list[str], Any]:
        """Read lines until exactly `marker`. Returns (lines, tail).

        `tail` is the marker line (which may carry a `:value` suffix) or
        `False` on EOF/timeout.
        """
        lines: list[str] = []
        try:
            while True:
                raw = await asyncio.wait_for(stream.readline(), timeout=timeout)
                if not raw:
                    return lines, False
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == marker or line.startswith(marker + ":"):
                    return lines, line
                lines.append(line)
        except asyncio.TimeoutError:
            return lines, False

    async def run(self, script: str, timeout: int = 30) -> dict[str, Any]:
        t0 = time.perf_counter()
        # Transports/locks are bound to the creating event loop. Test runners
        # and one-shot scripts use a fresh loop per asyncio.run(); detect the
        # switch and drop loop-bound state synchronously before touching it.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        
        # Check if current loop supports subprocesses
        if loop is not None and not (hasattr(loop, 'create_subprocess_exec') or hasattr(loop, 'subprocess_exec')):
            log.debug("Current event loop doesn't support subprocesses, using one-shot fallback")
            loop = None
        
        if loop is not None and self._loop is not None and self._loop is not loop:
            try:
                if self._proc is not None:
                    self._proc.kill()
            except Exception:
                pass
            self._proc = None
            self._pid = None
            self._lock = asyncio.Lock()
        if loop is not None:
            self._loop = loop
        
        # If no suitable loop, return unavailable so caller falls back to one-shot
        if loop is None:
            return {
                "success": False,
                "returncode": -1,
                "stdout": "",
                "stderr": "Persistent PowerShell unavailable (event loop doesn't support subprocesses)",
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        
        out_task = None
        err_task = None
        async with self._lock:
            if not await self._ensure_started():
                return {
                    "success": False,
                    "returncode": -1,
                    "stdout": "",
                    "stderr": "Persistent PowerShell unavailable",
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                }
            assert self._proc is not None
            assert self._proc.stdin is not None
            assert self._proc.stdout is not None
            assert self._proc.stderr is not None

            uid = uuid.uuid4().hex
            out_marker = f"__WB_OUT_{uid}__"
            err_marker = f"__WB_ERR_{uid}__"
            payload = (
                script
                + f"\n$__wb_ok = $?\n"
                + f'Write-Output "{out_marker}:$__wb_ok"\n'
                + f'[Console]::Error.WriteLine("{err_marker}")\n'
            )
            try:
                self._proc.stdin.write(payload.encode("utf-8"))
                await asyncio.wait_for(self._proc.stdin.drain(), timeout=timeout)

                out_task = asyncio.ensure_future(
                    self._read_until(self._proc.stdout, out_marker, timeout)
                )
                err_task = asyncio.ensure_future(
                    self._read_until(self._proc.stderr, err_marker, timeout)
                )
                (out_lines, out_tail), (err_lines, err_found) = await asyncio.wait_for(
                    asyncio.gather(out_task, err_task), timeout=timeout + 5
                )
                if out_tail is False or not err_found:
                    raise TimeoutError("sentinel not received")

                success = str(out_tail).split(":", 1)[-1].strip().lower() == "true"
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
                return {
                    "success": success,
                    "returncode": 0 if success else 1,
                    "stdout": "\n".join(out_lines).strip(),
                    "stderr": "\n".join(err_lines).strip(),
                    "elapsed_ms": elapsed_ms,
                }
            except (asyncio.TimeoutError, TimeoutError) as e:
                log.warning(f"Persistent PowerShell call timed out; restarting ({e})")
                await self._teardown()
                return {
                    "success": False,
                    "returncode": -1,
                    "stdout": "",
                    "stderr": f"PowerShell command timed out after {timeout} seconds",
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                }
            except Exception as e:
                log.warning(f"Persistent PowerShell call failed; restarting ({e})")
                for task in (out_task, err_task):
                    try:
                        if task is not None and not task.done():
                            task.cancel()
                    except Exception:
                        pass
                await self._teardown()
                return {
                    "success": False,
                    "returncode": -1,
                    "stdout": "",
                    "stderr": str(e),
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                }

    async def close(self) -> None:
        async with self._lock:
            await self._teardown()


_host: Optional[PersistentPS] = None


def get_host() -> PersistentPS:
    global _host
    if _host is None:
        _host = PersistentPS()
    return _host


async def run_persistent(script: str, timeout: int = 30) -> dict[str, Any]:
    """Run a script on the shared persistent host (never raises)."""
    try:
        return await get_host().run(script, timeout)
    except Exception as e:
        return {
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"Persistent host error: {e}",
            "elapsed_ms": 0.0,
        }
