@echo off
title Laya Desktop Control Agent
set PYTHONUNBUFFERED=1
echo ========================================================
echo   Starting Laya Desktop Control Agent & Web UI (http://127.0.0.1:8765)
echo ========================================================
echo.

rem A previous run can leave a detached server on port 8765: it has no
rem console (so its logs go nowhere) and it blocks this start. Free the port.
for /f "delims=" %%p in ('powershell -NoProfile -Command "(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue).OwningProcess"') do (
    echo   Stopping previous server on port 8765 ^PID %%p^...
    taskkill /F /T /PID %%p >nul 2>&1
)
echo.

where python >nul 2>nul
if %errorlevel%==0 (
    python -m uvicorn server:app --host 127.0.0.1 --port 8765 --reload --reload-dir winbrow --reload-dir . --loop asyncio
) else (
    py -3 -m uvicorn server:app --host 127.0.0.1 --port 8765 --reload --reload-dir winbrow --reload-dir . --loop asyncio
)
pause
