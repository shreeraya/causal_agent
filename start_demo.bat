@echo off
setlocal
cd /d "%~dp0"
title start-causal-agent-demo

rem ---- detect what is already running ----
set APP=0
set TUN=0
netstat -ano | findstr "LISTENING" | findstr /C:":8000 " >nul 2>&1 && set APP=1
tasklist /FI "IMAGENAME eq cloudflared.exe" 2>nul | find /I "cloudflared.exe" >nul && set TUN=1

if "%APP%"=="1" if "%TUN%"=="1" (
    echo Demo is already running.
    if exist demo_url.txt (
        echo.
        echo   Public URL:
        type demo_url.txt
    ) else (
        echo   URL not captured yet - check the "causal-agent-demo" window.
    )
    echo.
    echo Access code: see DEMO_PASSCODE in .env
    pause
    exit /b 0
)

rem ---- partial or dead state: clean up stale tunnel, then start fresh ----
echo Cleaning up stale processes...
taskkill /IM cloudflared.exe /F >nul 2>&1
if exist demo_url.txt del demo_url.txt

echo Starting demo (app + tunnel) in its own window...
start "causal-agent-demo" powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0start_demo.ps1"

echo Waiting for the public URL (up to ~60s)...
set /a TRIES=0
:wait
ping -n 4 127.0.0.1 >nul
if exist demo_url.txt goto show
set /a TRIES+=1
if %TRIES% lss 20 goto wait
echo Timed out - check the "causal-agent-demo" window for errors.
pause
exit /b 1

:show
echo.
echo   Public URL:
type demo_url.txt
echo.
echo Share it with the access code (DEMO_PASSCODE in .env).
echo Leave the "causal-agent-demo" window open. Run stop_demo.bat to shut down.
pause
exit /b 0
