$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

function Get-DotEnvValue([string]$Name) {
    if (-not (Test-Path .env)) { return $null }
    $line = Get-Content .env | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -Last 1
    if (-not $line) { return $null }
    return ($line -replace "^\s*$Name\s*=\s*", "").Trim().Trim('"').Trim("'")
}

$Email = $env:PHARMAKON_DEMO_EMAIL
if (-not $Email) { $Email = Get-DotEnvValue "PHARMAKON_DEMO_EMAIL" }
$Password = $env:PHARMAKON_DEMO_PASSWORD
if (-not $Password) { $Password = Get-DotEnvValue "PHARMAKON_DEMO_PASSWORD" }
if (-not $Email -or -not $Password) {
    throw "PHARMAKON_DEMO_EMAIL and PHARMAKON_DEMO_PASSWORD are not set. Add both to .env (see .env.example) or set them in the environment, then run this again."
}

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    throw "cloudflared not found. Install it with: winget install Cloudflare.cloudflared, then open a new PowerShell window."
}

Write-Host "Stopping old servers and tunnels..." -ForegroundColor Cyan
Get-NetTCPConnection -LocalPort 5173, 5174, 8000 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

$log = Join-Path $env:TEMP "pharmakon-tunnel.log"
Remove-Item $log -ErrorAction SilentlyContinue

Write-Host "Starting the tunnel..." -ForegroundColor Cyan
$cloudflared = (Get-Command cloudflared).Source
$tunnel = Start-Process -FilePath $cloudflared `
    -ArgumentList "tunnel", "--url", "http://localhost:5173" `
    -RedirectStandardError $log -WindowStyle Hidden -PassThru

$url = $null
for ($i = 0; $i -lt 60 -and -not $url; $i++) {
    Start-Sleep -Seconds 1
    try {
        $match = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction Stop |
            Select-Object -First 1
        if ($match) { $url = $match.Matches[0].Value }
    } catch { }
}

if (-not $url) {
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
    throw "The tunnel did not start. Check your internet connection. Log: $log"
}

$env:PHARMAKON_COOKIE_SECURE = "1"
$env:VITE_API_BASE = "/api"
$env:PHARMAKON_ALLOWED_HOSTS = ".trycloudflare.com"
$env:PHARMAKON_CORS_ORIGINS = $url
$env:PHARMAKON_DEMO_EMAIL = $Email
$env:PHARMAKON_DEMO_PASSWORD = $Password

$py = "python"
if (Test-Path ".\.venv\Scripts\python.exe") { $py = ".\.venv\Scripts\python.exe" }

Write-Host "Seeding the demo user..." -ForegroundColor Cyan
& $py scripts/seed_demo_user.py --food-demo

try { Set-Clipboard -Value $url } catch { }

Write-Host ""
Write-Host "=======================================================" -ForegroundColor Green
Write-Host " Open this in Safari on your iPhone:" -ForegroundColor Green
Write-Host "   $url" -ForegroundColor Yellow
Write-Host " Log in with: $Email  (the demo password from .env)" -ForegroundColor Green
Write-Host " (The address is also copied to your clipboard.)" -ForegroundColor Green
Write-Host " Press Ctrl+C in this window to stop everything." -ForegroundColor Green
Write-Host "=======================================================" -ForegroundColor Green
Write-Host ""

try {
    & .\run.ps1
}
finally {
    Write-Host "Stopping the tunnel..." -ForegroundColor Cyan
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
    Get-NetTCPConnection -LocalPort 5173, 5174, 8000 -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
}
