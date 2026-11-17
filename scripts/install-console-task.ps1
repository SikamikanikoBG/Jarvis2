# Install (or remove) the one SYSTEM task the host needs to give itself a screen again.
#
#   Run ONCE, in an elevated PowerShell (Run as administrator), on the Windows host:
#   powershell -ExecutionPolicy Bypass -File scripts\install-console-task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-console-task.ps1 -Remove
#
# Why: the screen tools (screen_grab/windows/controls/click/type) need the host's session on the
# console. After an RDP disconnect it is not, and only SYSTEM may `tscon` a disconnected session
# back. The host runs without admin rights, so it cannot create a SYSTEM task itself
# (schtasks /Create ... /RU SYSTEM -> "Access is denied"; jarvisvm, 2026-11-17: a whole FX task
# ran blind because of it). This installs that task once, with permission for the host's own user
# to RUN it (not to change it). The task does one thing: put that user's session on the console.
param([switch]$Remove, [string]$User = $env:USERNAME)

$ErrorActionPreference = 'Stop'
$taskName = 'Jarvis2 host console'
$dir = Join-Path $env:ProgramData 'Jarvis2'
$cmd = Join-Path $dir 'console.cmd'

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'run this from an elevated PowerShell (Run as administrator)'
}

$svc = New-Object -ComObject Schedule.Service
$svc.Connect()
$root = $svc.GetFolder('\')

if ($Remove) {
    try { $root.DeleteTask($taskName, 0); Write-Host "removed '$taskName'" } catch { Write-Host "no '$taskName' task" }
    Remove-Item $cmd -ErrorAction SilentlyContinue
    exit 0
}

New-Item -ItemType Directory -Force $dir | Out-Null
# The session id changes with every logon, so the task looks it up each time it runs.
Set-Content -Encoding ascii $cmd @"
@echo off
for /f "tokens=2,3" %%a in ('query session $User ^| findstr /i "$User"') do (
  if /i "%%a"=="$User" (tscon %%b /dest:console) else (tscon %%a /dest:console)
)
"@

$def = $svc.NewTask(0)
$def.RegistrationInfo.Description = "Jarvis2: put $User's session on the console so jarvis-host has a screen."
$def.Principal.UserId = 'SYSTEM'
$def.Principal.LogonType = 5      # service account
$def.Principal.RunLevel = 1       # highest
$def.Settings.ExecutionTimeLimit = 'PT1M'
$def.Settings.AllowDemandStart = $true
$def.Settings.DisallowStartIfOnBatteries = $false
$action = $def.Actions.Create(0)
$action.Path = $cmd

# Administrators and SYSTEM: full control. The host's user: read + run, nothing else.
$sid = (New-Object Security.Principal.NTAccount($User)).Translate([Security.Principal.SecurityIdentifier]).Value
$sddl = "D:(A;;FA;;;BA)(A;;FA;;;SY)(A;;GRGX;;;$sid)"
$root.RegisterTaskDefinition($taskName, $def, 6, 'SYSTEM', $null, 5, $sddl) | Out-Null
Write-Host "installed '$taskName' (runs $cmd as SYSTEM; $User may run it)"
