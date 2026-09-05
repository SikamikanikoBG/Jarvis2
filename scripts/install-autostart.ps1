# Register (or remove) a per-user logon task that starts jarvis-host after a restart.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Remove
#
# Per-user only: no admin rights, no service, nothing system-wide. Remove it with -Remove or
# in Task Scheduler under the name below.
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$taskName = 'JarvisHost'
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'start-host.ps1'

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
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Start 40s after logon: Outlook and Tailscale want to be up first.
$trigger.Delay = 'PT40S'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description 'Starts jarvis-host (Jarvis V2 Outlook/files/shell/screen MCP server) at logon.' | Out-Null

Write-Host "registered '$taskName' - jarvis-host starts 40s after each logon"
Write-Host "  run now:  Start-ScheduledTask -TaskName $taskName"
Write-Host "  remove:   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Remove"
