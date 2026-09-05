# Restart jarvis-host after a code change.
# start-host.ps1 detaches the daemon with Start-Process, so Stop-ScheduledTask does NOT end it:
# the task run ends, the old python keeps the port, and the next start exits with "already
# listening" - the new code never runs (2026-09-06: two "restarts" changed nothing). This script
# finds the ONE process on the port, proves it is jarvis-host by its command line, stops that PID
# only (never a name-based kill), then starts the scheduled task and waits for the port.
$ErrorActionPreference = 'Stop'
$port = 9030

$conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    $procId = $conn.OwningProcess
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId").CommandLine
    if ($cmd -notmatch 'jarvis-host') {
        Write-Error "pid $procId owns port $port but is not jarvis-host ($cmd); refusing to stop it"
        exit 2
    }
    Write-Host "stopping jarvis-host pid $procId"
    Stop-Process -Id $procId -Force
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 300
    }
} else {
    Write-Host "nothing listening on $port"
}

Start-ScheduledTask -TaskName JarvisHost
$deadline = (Get-Date).AddSeconds(30)
while (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
}
$conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $conn) {
    Write-Error "jarvis-host did not come up on $port within 30 s (see data\host.log.err)"
    exit 1
}
$p = Get-Process -Id $conn.OwningProcess
Write-Host "jarvis-host up: pid $($p.Id) started $($p.StartTime)"
