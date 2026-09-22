# Launcher for the video downloader.
#
# To enable Instagram, uncomment one of the lines below. Instagram refuses
# anonymous requests, so a logged-in session is required. See README.md.
#
# $env:DOWNLOADER_BROWSER = "firefox"              # or chrome / edge / brave
# $env:DOWNLOADER_COOKIES = "C:\path\to\cookies.txt"

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "No virtualenv found. Creating one..." -ForegroundColor Yellow
    python -m venv .venv
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements.txt
}

Start-Process "http://127.0.0.1:5000"
& $python app.py
