' Launches start-host.ps1 without any console window at all (wscript runs without a window;
' the child powershell is created hidden). Used by the JarvisHost scheduled task so the
' 5-minute watchdog never flashes a terminal on screen.
' The ps1 is found next to this file, so the checkout can live anywhere.
Set sh = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & here & "\start-host.ps1"""
sh.Run cmd, 0, False
