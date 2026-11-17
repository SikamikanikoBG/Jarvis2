"""app_start / app_windows: a program that outlives the call, and the dialog in front of it."""

from __future__ import annotations

import pytest

from jarvis_host.apps import AppError, Apps

TERMINAL = 4242


class Proc:
    def __init__(self, pid: int = TERMINAL, code: int | None = None) -> None:
        self.pid, self.returncode = pid, code

    def poll(self) -> int | None:
        return self.returncode


def windows_with_login() -> list[dict]:
    return [
        {
            "handle": 1,
            "pid": TERMINAL,
            "title": "Pepperstone MetaTrader 5 - Netting",
            "class": "MetaQuotes",
            "visible": True,
        },
        {"handle": 2, "pid": TERMINAL, "title": "Login", "class": "#32770", "visible": True},
        {"handle": 3, "pid": TERMINAL, "title": "GDI+ Window", "class": "GDI+", "visible": False},
        {"handle": 4, "pid": 7, "title": "Taskbar", "class": "Shell_TrayWnd", "visible": True},
    ]


def test_a_started_program_reports_it_runs_and_the_dialog_that_blocks_it():
    """2026-11-17: MT5 sat behind a Login dialog nobody could see; its Python API timed out for 40 min."""
    launched: list[dict] = []

    def popen(argv, **kw):  # type: ignore[no-untyped-def]
        launched.append({"argv": argv, **kw})
        return Proc()

    apps = Apps(allowed=True, enum_windows=windows_with_login, popen=popen, sleep=lambda s: None)
    out = apps.start(r"C:\T\terminal64.exe", ["/portable"], wait_s=5)
    assert out["running"] is True and out["pid"] == TERMINAL
    assert [w["title"] for w in out["windows"]] == ["Pepperstone MetaTrader 5 - Netting", "Login"]
    assert out["windows"][1]["dialog"] is True
    assert "'Login'" in out["note"] and "time out" in out["note"]
    flags = launched[0]["creationflags"]
    assert flags & 0x00000008 and flags & 0x01000000, "detached and out of the host's job: it outlives the call"


def test_a_job_that_refuses_breakaway_still_starts_detached():
    calls: list[int] = []

    def popen(argv, **kw):  # type: ignore[no-untyped-def]
        calls.append(kw["creationflags"])
        if kw["creationflags"] & 0x01000000:
            raise OSError("access denied")
        return Proc()

    apps = Apps(allowed=True, enum_windows=lambda: [], popen=popen, sleep=lambda s: None)
    out = apps.start("x.exe")
    assert out["running"] and len(calls) == 2 and not calls[1] & 0x01000000
    assert "no visible window" in out["note"]


def test_a_program_that_exits_at_once_says_so():
    apps = Apps(allowed=True, enum_windows=lambda: [], popen=lambda a, **k: Proc(code=1), sleep=lambda s: None)
    out = apps.start("x.exe")
    assert out["running"] is False and out["exit_code"] == 1 and "exited" in out["note"]


def test_windows_by_name_and_by_pid_and_refusals():
    apps = Apps(
        allowed=False, enum_windows=windows_with_login, pids_named=lambda n: [TERMINAL] if "terminal" in n else []
    )
    by_name = apps.windows("terminal64")
    assert by_name["pids"] == [TERMINAL] and any(w["dialog"] for w in by_name["windows"])
    assert apps.windows(str(TERMINAL))["windows"][0]["title"].startswith("Pepperstone")
    with pytest.raises(AppError, match="no running process"):
        apps.windows("nothing")
    with pytest.raises(AppError, match="disabled"):
        apps.start("x.exe")
