@echo off
setlocal
cd /d "%~dp0"
title stop-causal-agent-demo
echo Stopping the demo...

rem 1. the launcher window first - otherwise it restarts the tunnel in 10s
taskkill /FI "WINDOWTITLE eq causal-agent-demo*" /T /F >nul 2>&1

rem 2. the tunnel
taskkill /IM cloudflared.exe /F >nul 2>&1

rem 3. the app: whatever is listening on port 8000
for /f "tokens=5" %%p in ('netstat -ano ^| findstr "LISTENING" ^| findstr /C:":8000 "') do taskkill /PID %%p /F >nul 2>&1

if exist demo_url.txt del demo_url.txt

echo Done. The public URL is now dead (a NEW url is generated on next start).
pause
exit /b 0
