"""``shell_run`` — a command in PowerShell 7 (falling back to Windows PowerShell) with a deadline."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

MAX_OUTPUT_CHARS = 20_000
MAX_TIMEOUT_S = 600.0


class ShellError(RuntimeError):
    pass


class ShellDisabled(ShellError):
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


def cap(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return text[:head] + f"\n… [{len(text) - limit} chars omitted] …\n" + text[-tail:]


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
        timeout_s = max(1.0, min(float(timeout_s or 60.0), MAX_TIMEOUT_S))
        workdir = Path(cwd).expanduser() if cwd else self.default_cwd
        if not workdir.is_dir():
            raise ShellError(f"cwd {workdir} is not a directory")
        argv = [*self.launcher, command]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=str(workdir), capture_output=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired as exc:
            out = cap(_decode(exc.stdout if isinstance(exc.stdout, bytes) else None))
            err = cap(_decode(exc.stderr if isinstance(exc.stderr, bytes) else None))
            raise ShellTimeout(
                f"command did not finish within {timeout_s:g}s and was killed"
                + (f"; partial stdout:\n{out}" if out else "")
                + (f"; partial stderr:\n{err}" if err else ""),
                out,
                err,
            ) from None
        except FileNotFoundError as exc:
            raise ShellError(f"interpreter not found: {argv[0]} ({exc})") from exc
        return {
            "exit_code": proc.returncode,
            "stdout": cap(_decode(proc.stdout)),
            "stderr": cap(_decode(proc.stderr)),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "cwd": str(workdir),
            "interpreter": self.interpreter,
        }
