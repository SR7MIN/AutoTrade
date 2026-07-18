$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Missing .venv. Follow README setup first."
}

& ".venv\Scripts\python.exe" -m autotrade daemon --symbols BTCUSDT --interval 1m
