"""``host_status`` — what this host is, how long it has been up, and whether Outlook answers.

Two things learned the hard way on 2026-09-05, when OUTLOOK.EXE sat frozen (``Responding=False``)
for the whole afternoon: a status call must never queue behind a COM call that is already stuck
(it would just time out too), and "connected: false" is not a diagnosis — the difference between
"Outlook is not running" and "Outlook is running but hung" is what the operator needs.
"""

from __future__ import annotations

import platform
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from jarvis_host import __version__
from jarvis_host.com import ComTimeout, ComWorker
from jarvis_host.config import HostConfig
from jarvis_host.outlook import OutlookError, OutlookService


def outlook_process_state() -> dict[str, Any] | None:
    """Is OUTLOOK.EXE running, and does its window still answer messages? (Windows only.)"""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32: Any = ctypes.windll.user32  # pyright: ignore[reportAttributeAccessIssue]
    kernel32: Any = ctypes.windll.kernel32  # pyright: ignore[reportAttributeAccessIssue]
    process_query_limited_information = 0x1000
    found: dict[int, dict[str, Any]] = {}

    def image_name(pid: int) -> str:
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            kernel32.CloseHandle(handle)

    seen_pids: dict[int, bool] = {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)  # pyright: ignore[reportAttributeAccessIssue]
    def on_window(hwnd: Any, _lparam: Any) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in seen_pids:
            seen_pids[pid.value] = image_name(pid.value).lower().endswith("\\outlook.exe")
        if not seen_pids[pid.value]:
            return True
        entry = found.setdefault(pid.value, {"pid": pid.value, "hung": False, "window": ""})
        if user32.IsHungAppWindow(hwnd):
            entry["hung"] = True
        if not entry["window"]:
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                entry["window"] = title.value
        return True

    try:
        user32.EnumWindows(on_window, 0)
    except Exception as exc:  # pragma: no cover — only on an exotic desktop session
        return {"running": None, "error": str(exc)}
    procs = list(found.values())
    return {"running": bool(procs), "hung": any(p["hung"] for p in procs), "processes": procs}


class Status:
    def __init__(
        self,
        config: HostConfig,
        worker: ComWorker,
        outlook: OutlookService | None,
        process_state: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.config = config
        self.worker = worker
        self.outlook = outlook
        self.process_state = process_state or outlook_process_state
        self.started = time.monotonic()
        self.started_at = datetime.now(UTC)
        self.last_ok: dict[str, Any] | None = None
        self.last_ok_at: datetime | None = None

    async def _outlook(self, busy: bool) -> dict[str, Any]:
        if self.outlook is None:
            return {"connected": False, "error": "Outlook is only available on Windows"}
        # Cheap facts first (a window-message probe, no COM): they decide whether a ping is even sane.
        process = self.process_state()
        hung = bool(process and process.get("running") and process.get("hung"))
        result: dict[str, Any]
        if busy:
            # Never queue a ping behind a call that is already stuck; it would only time out too.
            com = self.worker.status()
            result = {
                "connected": None,
                "error": f"COM worker busy for {com.busy_for_s:.0f}s on {com.current}; ping skipped",
            }
        elif hung:
            result = {"connected": None, "error": "ping skipped: OUTLOOK.EXE is not responding"}
        else:
            try:
                result = await self.outlook.call("ping")
                self.last_ok, self.last_ok_at = result, datetime.now(UTC)
            except (ComTimeout, OutlookError) as exc:
                result = {"connected": False, "error": str(exc)}
        if self.last_ok_at is not None and not result.get("connected"):
            result["last_ok_at"] = self.last_ok_at.isoformat(timespec="seconds")
            result["last_ok_accounts"] = (self.last_ok or {}).get("accounts")
        if process is not None:
            result["process"] = process
            if hung:
                result["diagnosis"] = (
                    "OUTLOOK.EXE is running but not responding (hung); COM calls will block until it recovers or is restarted"
                )
            elif process.get("running") is False:
                result["diagnosis"] = "OUTLOOK.EXE is not running"
        return result

    async def snapshot(self) -> dict[str, Any]:
        # The busy check must be read BEFORE the ping (it decides whether to ping at all), but the
        # numbers we report must be read AFTER it — otherwise the snapshot describes the world as
        # it was before its own probe and always says completed=0.
        outlook = await self._outlook(self.worker.status().busy)
        com = self.worker.status()
        return {
            "name": self.config.name,
            "version": __version__,
            "platform": f"{platform.system()} {platform.release()}",
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "uptime_s": round(time.monotonic() - self.started, 1),
            "com": {
                "alive": com.alive,
                "busy": com.busy,
                "busy_for_s": com.busy_for_s,
                "current": com.current,
                "pending": com.pending,
                "abandoned": com.abandoned,
                "completed": com.completed,
            },
            "outlook": outlook,
            "outlook_allow_list": list(self.config.outlook_accounts),
            "fs_roots": [str(r) for r in self.config.fs_roots],
            "shell_allowed": self.config.shell_allow,
            "screen_enabled": self.config.screen_enabled,
        }
