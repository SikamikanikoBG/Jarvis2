# Start jarvis-host (this machine's Outlook / files / shell / screen, as an MCP server).
# Idempotent: if something is already listening on the port, it exits instead of starting a
# second daemon. Registered to run at logon by scripts/install-autostart.ps1.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$port = 9030

$listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "jarvis-host is already listening on $port (pid $($listening[0].OwningProcess))"
    exit 0
}

$logDir = Join-Path $repo 'data'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'host.log'

Set-Location $repo
Write-Host "starting jarvis-host from $repo (log: $log)"
# uv resolves the workspace venv itself; no activation needed.
Start-Process -FilePath 'uv' -ArgumentList 'run', 'jarvis-host' `
    -WorkingDirectory $repo -WindowStyle Hidden `
    -RedirectStandardOutput $log -RedirectStandardError "$log.err"
