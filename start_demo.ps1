# Launches the demo (FastAPI app + Cloudflare quick tunnel) resilient to crashes.
# Run in its own PowerShell window and leave it open:   .\start_demo.ps1
# The public URL is printed here and saved to demo_url.txt (new URL on every
# tunnel restart - trycloudflare URLs are ephemeral by design).
#
# Note: the tunnel cannot survive laptop sleep. For demos, set your power plan
# to not sleep on AC power, or ask for the named-tunnel setup (stable URL).

$Host.UI.RawUI.WindowTitle = "causal-agent-demo"   # stop_demo.bat finds this window by title
$root = $PSScriptRoot
$urlFile = Join-Path $root "demo_url.txt"
$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
if (-not (Test-Path $cloudflared)) { $cloudflared = "cloudflared" }  # fall back to PATH

# 1. app: start uvicorn if port 8000 is not already listening
if (-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) {
    Write-Host "starting app on port 8000..."
    Start-Process python -ArgumentList "-m", "uvicorn", "app:app", "--port", "8000" `
        -WorkingDirectory $root -WindowStyle Minimized
    Start-Sleep -Seconds 4
} else {
    Write-Host "app already running on port 8000"
}

# 2. tunnel: run forever, restart on exit, surface the URL each time
while ($true) {
    Write-Host "`nstarting cloudflare tunnel..." -ForegroundColor Cyan
    & $cloudflared tunnel --url http://localhost:8000 2>&1 | ForEach-Object {
        $line = "$_"
        if ($line -match "https://[a-z0-9-]+\.trycloudflare\.com") {
            $url = $Matches[0]
            Set-Content -Path $urlFile -Value $url
            Write-Host "`n  PUBLIC URL: $url`n" -ForegroundColor Green
        }
        if ($line -match "ERR|error") { Write-Host $line -ForegroundColor DarkYellow }
    }
    Write-Host "tunnel exited - restarting in 10s (URL will change)..." -ForegroundColor Yellow
    Start-Sleep -Seconds 10
}
