# Dev startup: API on :8000 (background), Vite client on :5173 (foreground).
# Ctrl-C stops both.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv\Scripts\Activate.ps1)) {
    Write-Error "No .venv found. See docs/SETUP.md."
}
. .\.venv\Scripts\Activate.ps1

if (-not (Test-Path client\node_modules)) {
    Push-Location client
    npm ci
    Pop-Location
}

if (-not $env:SHOW_UNVERIFIED_RULES) { $env:SHOW_UNVERIFIED_RULES = "1" }

$uvicorn = Start-Process -FilePath python `
    -ArgumentList "-m", "uvicorn", "app.api.main:app", "--port", "8000", "--reload", "--log-level", "warning" `
    -NoNewWindow -PassThru

try {
    Push-Location client
    npm run dev
}
finally {
    Pop-Location
    # /T kills the whole tree: --reload runs the server in a child process.
    taskkill /PID $uvicorn.Id /T /F | Out-Null
}
