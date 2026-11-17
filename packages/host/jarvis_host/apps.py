"""``app_start`` / ``app_windows`` - start a program that must keep running, and see its windows.

2026-11-17, jarvisvm: an FX task had to drive MetaTrader 5 from Python. Every terminal it started
through ``shell_run`` either died with the command (the timeout kills the whole tree) or sat behind
a modal "Login" / "Open an Account" dialog nobody could see - the session was disconnected and the
screen tools could not run. The Python package answered "IPC timeout" for forty minutes, and the
model guessed at sessions, integrity levels and Wine. Two plain facts would have ended it: the
program is still running, and this is the dialog in front of it.

``app_start`` launches detached (it outlives the call) and reports, after a short settle, whether
it is still running and which windows it shows. ``app_windows`` lists a running program's windows.
Both read windows with EnumWindows, which works in a disconnected session too - no screen needed.
A window of class ``#32770`` is a dialog; a dialog over a program usually means it waits for input.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DIALOG_CLASS = "#32770"
MAX_WAIT_S = 60.0

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class AppError(RuntimeError):
    pass


Window = dict[str, Any]


def _enum_windows() -> list[Window]:
    """Every top-level window: pid, title, class, visible. (Windows only.)"""
    import ctypes
    from ctypes import wintypes

    user32: Any = ctypes.windll.user32  # pyright: ignore[reportAttributeAccessIssue]
    out: list[Window] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)  # pyright: ignore[reportAttributeAccessIssue, reportUntypedFunctionDecorator]
    def on_window(hwnd: Any, _lparam: Any) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, title, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        out.append(
            {
                "handle": int(hwnd or 0),
                "pid": int(pid.value),
                "title": title.value,
                "class": cls.value,
                "visible": bool(user32.IsWindowVisible(hwnd)),
            }
        )
        return True

    user32.EnumWindows(on_window, 0)
    return out


def _pids_named(name: str) -> list[int]:
    exe = name if name.lower().endswith(".exe") else name + ".exe"
    done = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    pids = []
    for line in done.stdout.splitlines():
        parts = line.strip().strip('"').split('","')
        if len(parts) > 1 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def describe_windows(windows: list[Window], pids: set[int]) -> dict[str, Any]:
    """The visible, titled windows of these processes, dialogs called out."""
    shown = [
        {"handle": w["handle"], "pid": w["pid"], "title": w["title"], "dialog": w["class"] == DIALOG_CLASS}
        for w in windows
        if w["pid"] in pids and w["visible"] and w["title"]
    ]
    out: dict[str, Any] = {"windows": shown}
    dialogs = [w["title"] for w in shown if w["dialog"]]
    if dialogs:
        out["note"] = (
            "a dialog is open over this program (" + ", ".join(repr(d) for d in dialogs) + "); a program waiting on "
            "a dialog usually answers nothing else - its API, IPC or automation time out - until the dialog is "
            "dealt with (screen_controls / screen_click on it, or start the program so it does not ask: a config "
            "file, a command-line switch, saved credentials)"
        )
    return out


class Apps:
    def __init__(
        self,
        *,
        allowed: bool,
        enum_windows: Callable[[], list[Window]] | None = None,
        pids_named: Callable[[str], list[int]] | None = None,
        popen: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.allowed = allowed
        self._enum = enum_windows or _enum_windows
        self._pids_named = pids_named or _pids_named
        self._popen = popen or subprocess.Popen
        self._sleep = sleep or time.sleep

    def start(
        self, path: str, args: list[str] | None = None, cwd: str | None = None, wait_s: float = 8.0
    ) -> dict[str, Any]:
        if not self.allowed:
            raise AppError("starting programs is disabled on this host (shell.allow = false in host.toml)")
        if sys.platform != "win32" and self._popen is subprocess.Popen:
            raise AppError("app_start is for a Windows host; on Linux use shell_run with nohup/setsid")
        exe = Path(path)
        if self._popen is subprocess.Popen and not exe.is_file():
            raise AppError(f"no program at {path!r}")
        argv = [str(exe), *(args or [])]
        base = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        common: dict[str, Any] = {
            "cwd": cwd or str(exe.parent),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        try:
            proc = self._popen(argv, creationflags=base | _CREATE_BREAKAWAY_FROM_JOB, **common)
        except OSError:
            # The host's job does not allow breakaway: still detached, just inside the job.
            proc = self._popen(argv, creationflags=base, **common)
        self._sleep(max(0.0, min(float(wait_s), MAX_WAIT_S)))
        running = proc.poll() is None
        out: dict[str, Any] = {"pid": proc.pid, "running": running}
        if not running:
            out["exit_code"] = proc.returncode
            out["note"] = "it exited already - a second instance handing over to one that runs, or a failed start"
            return out
        seen = describe_windows(self._enum(), {proc.pid})
        out.update(seen)
        if not seen["windows"]:
            out.setdefault("note", "running, with no visible window yet (still starting, or a tray/background program)")
        return out

    def windows(self, process: str) -> dict[str, Any]:
        """Windows of a running program, by pid or by name (e.g. 'terminal64')."""
        text = str(process).strip()
        pids = {int(text)} if text.isdigit() else set(self._pids_named(text))
        if not pids:
            raise AppError(f"no running process {text!r}")
        out = describe_windows(self._enum(), pids)
        out["pids"] = sorted(pids)
        return out
