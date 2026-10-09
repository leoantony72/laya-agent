@echo off
title Laya Desktop Control Agent
echo ========================================================
echo   Starting Laya Desktop Control Agent & Web UI (http://127.0.0.1:8765)
echo ========================================================
echo.
py -3 -m uvicorn server:app --host 127.0.0.1 --port 8765 --reload --loop asyncio:ProactorEventLoop
pause
