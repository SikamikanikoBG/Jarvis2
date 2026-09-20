' Launches start-host.ps1 without any console window at all (wscript runs without a window;
' the child powershell is created hidden). Used by the JarvisHost scheduled task so the
' 5-minute watchdog never flashes a terminal on screen.
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""R:\Projects\Jarvis2\scripts\start-host.ps1"""
sh.Run cmd, 0, False
