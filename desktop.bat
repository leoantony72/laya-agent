@echo off
setlocal
title WinBrow Desktop
cd /d "%~dp0frontend"

if not exist "node_modules\electron\dist\electron.exe" (
  echo Installing desktop dependencies...
  call npm.cmd install --no-audit --no-fund
  if errorlevel 1 goto :fail
)

if not exist "dist\index.html" (
  echo Building the interface...
  call npm.cmd run build
  if errorlevel 1 goto :fail
)

echo Starting WinBrow...
start "" "node_modules\electron\dist\electron.exe" .
exit /b 0

:fail
echo.
echo WinBrow could not start. Run the commands above manually to see the error.
pause
exit /b 1
