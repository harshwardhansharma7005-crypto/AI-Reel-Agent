# Generate synthetic sample media and render ONE sample Reel (never publishes).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
& .\.venv\Scripts\python.exe -m reel_agent check
& .\.venv\Scripts\python.exe -m reel_agent demo
Write-Host "Open the newest folder under media_demo\output\ to watch reel.mp4" -ForegroundColor Green
