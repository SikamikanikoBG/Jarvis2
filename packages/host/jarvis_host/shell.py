"""``shell_run`` — a command in PowerShell 7 (falling back to Windows PowerShell) with a deadline.

The deadline has to hold for the whole process tree, not just the interpreter. ``subprocess.run(timeout=)``
kills only the direct child and then, on Windows, reads the pipes to the end: a grandchild that inherited
them (an ``ssh`` waiting at a password prompt, 2026-09-27) keeps them open, so the read never returns and
the worker thread is lost for good. A few of those and every later call, ``Write-Output hi`` included,
timed out. So the command runs in its own process group with stdin closed (a prompt gets EOF instead of
waiting), a timeout kills the whole tree, and the leftover output is read with a deadline of its own.

The daemon is itself a python.exe (under uv.exe). Agents cleaning up their own ``python -m http.server``
ran ``Get-Process python | Stop-Process -Force`` and killed the host with it (2026-09-27), then waited for
"zombie processes" to let go of a host that was gone. Commands that stop python/uv by name, or this
daemon by its PID, are refused with a pointer to ``Stop-Process -Id``: kill what you started, by PID.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

MAX_OUTPUT_CHARS = 20_000
MAX_TIMEOUT_S = 600.0
DRAIN_TIMEOUT_S = 5.0  # after a kill: how long to wait for the pipes to close before giving up on the tail


class ShellError(RuntimeError):
    pass


class ShellDisabled(ShellError):
    pass


class ShellRefused(ShellError):
    pass


class ShellTimeout(ShellError):
    def __init__(self, message: str, stdout: str, stderr: str) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


def default_launcher() -> list[str]:
    if sys.platform == "win32":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
    return [shutil.which("bash") or "/bin/sh", "-c"]


_NAME = r"['\"]?(?:python|pythonw|py|uv)(?:\.exe)?['\"]?(?![\w.-])"  # the daemon's own process names
_KILL = r"(?:Stop-Process|spps|kill|taskkill|Terminate)"
_KILL_BY_NAME = [
    re.compile(p, re.IGNORECASE)
    for p in (
        # Get-Process python | Stop-Process,  gps python | % { $_.Kill() },  (Get-Process python).Kill()
        rf"\b(?:Get-Process|gps)\b[^;\n|]*?\s{_NAME}[^;\n]*?(?:\|[^;\n]*?\b{_KILL}\b|\)\s*\.\s*Kill\s*\()",
        # Stop-Process -Name python,  spps -n node, python
        rf"\b(?:Stop-Process|spps|kill)\b[^;\n|]*?-(?:Name|ProcessName|n)\s+(?:[\w.'\"]+\s*,\s*)*{_NAME}",
        # taskkill /F /IM python.exe
        rf"\btaskkill\b[^;\n|]*?/IM\s+{_NAME}",
        # wmic process where name='python.exe' delete,  Win32_Process -Filter "Name='python.exe'" | Invoke-CimMethod Terminate
        rf"\bname\s*=\s*{_NAME}[^;\n]*?\b(?:{_KILL}|delete)\b",
    )
]


def refusal(command: str, own_pids: Sequence[int] = ()) -> str | None:
    """Why `command` must not run, or None. It would kill this daemon: python/uv by name, or one of `own_pids`."""
    if any(p.search(command) for p in _KILL_BY_NAME):
        return (
            "refused: this stops python/uv processes by name, and the host daemon you are talking to is one of "
            "them - it would cut you off from this machine. Stop only what you started, by PID: keep the id from "
            "`$p = Start-Process ... -PassThru` (`$p.Id`) and run `Stop-Process -Id <pid> -Force`; to find a "
            "server you started, `Get-NetTCPConnection -LocalPort <port> -State Listen | Select OwningProcess`."
        )
    if re.search(rf"\b{_KILL}\b", command, re.IGNORECASE):
        for pid in own_pids:
            if re.search(rf"(?<![\w.]){pid}(?![\w.])", command):
                return f"refused: PID {pid} is the host daemon (or its launcher); stopping it would cut you off from this machine."
    return None


def cap(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return text[:head] + f"\n… [{len(text) - limit} chars omitted] …\n" + text[-tail:]


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill `proc` and everything it started. Best effort: the tree may already be (partly) gone."""
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)  # the command runs in its own session: pgid == pid
    with contextlib.suppress(OSError):
        proc.kill()


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


class Shell:
    def __init__(
        self, *, allowed: bool = True, launcher: Sequence[str] | None = None, default_cwd: Path | None = None
    ) -> None:
        self.allowed = allowed
        self.launcher = list(launcher) if launcher is not None else default_launcher()
        self.default_cwd = default_cwd or Path.home()

    @property
    def interpreter(self) -> str:
        return Path(self.launcher[0]).name if self.launcher else ""

    def run(self, command: str, cwd: str | None = None, timeout_s: float = 60.0) -> dict[str, Any]:
        if not self.allowed:
            raise ShellDisabled("shell_run is disabled on this host (shell.allow = false in host.toml)")
        if not command.strip():
            raise ShellError("command is empty")
        why = refusal(command, (os.getpid(), os.getppid()))
        if why:
            raise ShellRefused(why)
        timeout_s = max(1.0, min(float(timeout_s or 60.0), MAX_TIMEOUT_S))
        workdir = Path(cwd).expanduser() if cwd else self.default_cwd
        if not workdir.is_dir():
            raise ShellError(f"cwd {workdir} is not a directory")
        argv = [*self.launcher, command]
        t0 = time.monotonic()
        try:
            # own process group / session, so a timeout can take the whole tree down (kill_tree)
            if sys.platform == "win32":
                proc = subprocess.Popen(
                    argv,
                    cwd=str(workdir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(workdir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
        except FileNotFoundError as exc:
            raise ShellError(f"interpreter not found: {argv[0]} ({exc})") from exc
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            kill_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=DRAIN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                # something outside the tree still holds the pipes: keep what was read, never block on the rest
                stdout = exc.stdout if isinstance(exc.stdout, bytes) else None
                stderr = exc.stderr if isinstance(exc.stderr, bytes) else None
            out, err = cap(_decode(stdout)), cap(_decode(stderr))
            raise ShellTimeout(
                f"command did not finish within {timeout_s:g}s and was killed (with every process it started)"
                + (f"; partial stdout:\n{out}" if out else "")
                + (f"; partial stderr:\n{err}" if err else ""),
                out,
                err,
            ) from None
        return {
            "exit_code": proc.returncode,
            "stdout": cap(_decode(stdout)),
            "stderr": cap(_decode(stderr)),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "cwd": str(workdir),
            "interpreter": self.interpreter,
        }
