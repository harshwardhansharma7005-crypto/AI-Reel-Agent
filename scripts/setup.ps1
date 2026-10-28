# One-time setup on Windows. Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "== AI Reel Agent setup ==" -ForegroundColor Cyan

# 1. Python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "Python not found. Install Python 3.10+ from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then rerun." -ForegroundColor Red
    exit 1
}
python --version

# 2. Virtual environment
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment (.venv)..."
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. FFmpeg
$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ff) {
    Write-Host "FFmpeg not found. Install it with:  winget install Gyan.FFmpeg   (then CLOSE and reopen the terminal/VS Code)" -ForegroundColor Yellow
} else {
    Write-Host "FFmpeg found: $($ff.Source)" -ForegroundColor Green
}

# 4. .env
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example (add your keys there later)." -ForegroundColor Green
}

Write-Host ""
Write-Host "Next:  scripts\demo.ps1   (makes a sample Reel)   |   scripts\run_tests.ps1" -ForegroundColor Cyan
