# Register (or remove) a per-user task that keeps jarvis-host running: at logon, and every few
# minutes after that.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Remove
#
# Per-user only: no admin rights, no service, nothing system-wide. Remove it with -Remove or
# in Task Scheduler under the name below.
#
# The repeat is not belt-and-braces. 2026-09-07: the daemon's log stops mid-request at 03:28 and
# the process was gone; -RestartCount does not help, because start-host.ps1 detaches the daemon
# and exits 0, so the task always "succeeded". Nothing noticed for four and a half hours, and
# the 07:20 news digest could not send: no Outlook, no files, no shell, on this machine.
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$taskName = 'JarvisHost'
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'start-host.ps1'
$everyMinutes = 5

if ($Remove) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "removed the '$taskName' logon task"
    } else {
        Write-Host "no '$taskName' task registered"
    }
    exit 0
}

if (-not (Test-Path $script)) { throw "missing $script" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory $repo
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Start 40s after logon: Outlook and Tailscale want to be up first.
$atLogon.Delay = 'PT40S'

# The watchdog: fire every $everyMinutes, forever. start-host.ps1 is idempotent - if the port is
# already listening it prints and exits - so a firing that finds a healthy daemon costs nothing.
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes $everyMinutes)
# An empty duration means "indefinitely". Setting it through the object avoids the
# -RepetitionDuration parameter, which rejects TimeSpan::MaxValue on some builds.
$watchdog.Repetition.Duration = ''
$watchdog.Repetition.StopAtDurationEnd = $false

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($atLogon, $watchdog) `
    -Settings $settings -Principal $principal -Force `
    -Description "Keeps jarvis-host (Jarvis V2 Outlook/files/shell/screen MCP server) running: at logon and every $everyMinutes minutes." | Out-Null

Write-Host "registered '$taskName' - jarvis-host starts 40s after each logon, and is restarted within $everyMinutes min if it dies"
Write-Host "  run now:  Start-ScheduledTask -TaskName $taskName"
Write-Host "  remove:   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Remove"
