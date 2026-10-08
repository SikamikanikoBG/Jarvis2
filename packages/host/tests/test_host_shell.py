"""shell_run — captured output, a real deadline, and an off switch."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from jarvis_host.shell import Shell, ShellDisabled, ShellError, ShellRefused, ShellTimeout, cap, refusal

PY = [sys.executable, "-c"]


def test_runs_and_captures_output(tmp_path: Path):
    shell = Shell(launcher=PY, default_cwd=tmp_path)
    res = shell.run(
        "import os, sys; print('out', os.getcwd() == sys.argv[0] or True); print('err', file=sys.stderr); sys.exit(3)"
    )
    assert res["exit_code"] == 3 and res["stdout"].startswith("out ") and res["stderr"] == "err\n"
    assert res["cwd"] == str(tmp_path) and res["duration_ms"] >= 0


def test_timeout_kills_and_raises_with_partial_output(tmp_path: Path):
    shell = Shell(launcher=PY, default_cwd=tmp_path)
    t0 = time.monotonic()
    with pytest.raises(ShellTimeout, match="did not finish within 1s") as info:
        shell.run("import sys, time; print('partial'); sys.stdout.flush(); time.sleep(10)", timeout_s=1)
    assert time.monotonic() - t0 < 5
    assert info.value.stdout.strip() == "partial"


def test_timeout_kills_grandchildren_that_hold_the_pipes(tmp_path: Path):
    # 2026-09-27: an `ssh` started by the command waited at a password prompt with the pipes inherited;
    # subprocess.run's timeout path then read those pipes forever and the host's worker thread was lost.
    shell = Shell(launcher=PY, default_cwd=tmp_path)
    code = (
        "import subprocess, sys, time;"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "print(c.pid, flush=True); time.sleep(60)"
    )
    t0 = time.monotonic()
    with pytest.raises(ShellTimeout, match="with every process it started") as info:
        shell.run(code, timeout_s=1)
    assert time.monotonic() - t0 < 12
    grandchild = int(info.value.stdout.split()[0])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(grandchild):
        time.sleep(0.1)
    # Seen alive ~1 in 10 full-suite runs on ardi, never alone (0/40 under load): say what it was.
    assert not _alive(grandchild), _describe_proc(grandchild)


def test_a_prompt_gets_eof_instead_of_waiting(tmp_path: Path):
    shell = Shell(launcher=PY, default_cwd=tmp_path)
    t0 = time.monotonic()
    res = shell.run("import sys; line = sys.stdin.readline(); print(repr(line))", timeout_s=10)
    assert res["stdout"].strip() == "''" and time.monotonic() - t0 < 5


def _describe_proc(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            fields = fh.read().split(")")[-1].split()
        with open(f"/proc/{pid}/cmdline") as fh:
            cmd = fh.read().replace("\0", " ")[:120]
    except OSError as exc:
        return f"pid {pid}: {exc}"
    return f"pid {pid} still alive: state={fields[0]} ppid={fields[1]} pgid={fields[2]} cmd={cmd!r}"


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import subprocess

        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
        ).stdout
        return str(pid) in out
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill(0): it is dead for our purposes
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        # kill(0) said the process existed a moment ago, but its /proc entry is already
        # gone: init reaped the (zombie) process in the gap between the two calls. That is
        # dead, not alive - the previous `return True` here was a race that flaked ~1 in 10
        # full-suite runs (the process was killed, the probe just lost the reap race).
        return False


def test_disabled_and_bad_input(tmp_path: Path):
    with pytest.raises(ShellDisabled):
        Shell(allowed=False, launcher=PY, default_cwd=tmp_path).run("print(1)")
    shell = Shell(launcher=PY, default_cwd=tmp_path)
    with pytest.raises(ShellError, match="command is empty"):
        shell.run("   ")
    with pytest.raises(ShellError, match="not a directory"):
        shell.run("print(1)", cwd=str(tmp_path / "missing"))


def test_cap_keeps_head_and_tail():
    text = "a" * 100 + "b" * 100
    capped = cap(text, limit=50)
    assert capped.startswith("a" * 25) and capped.endswith("b" * 25) and "150 chars omitted" in capped
    assert cap("short", limit=50) == "short"


@pytest.mark.parametrize(
    "command",
    [
        # what an agent actually ran on JarvisVM (2026-09-27), killing the host daemon with its http.servers
        "Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep -Milliseconds 300",
        "Get-Process -Name python,pythonw | Where-Object { $_.Id -ne 4 } | Stop-Process",
        "gps python | % { $_.Kill() }",
        "(Get-Process python).Kill()",
        "Stop-Process -Name python -Force",
        "spps -n node, python",
        "taskkill /F /IM python.exe",
        'taskkill /im "uv.exe" /t /f',
        "wmic process where name='python.exe' delete",
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Invoke-CimMethod -MethodName Terminate",
        # the same on the Linux workspace
        "pkill python3",
        "pkill -9 -f python",
        "killall python3.12",
        "kill $(pgrep python)",
        "pgrep -f python | xargs -r kill -9",
    ],
)
def test_refuses_killing_python_by_name(command: str):
    why = refusal(command)
    assert why and "by PID" in why and "Stop-Process -Id" in why


@pytest.mark.parametrize(
    "command",
    [
        "Stop-Process -Id 10716 -Force",
        "$p = Start-Process python -ArgumentList '-m','http.server','8899' -PassThru; Stop-Process -Id $p.Id",
        "Get-Process python | Select-Object Id, StartTime",
        "python -m http.server 8899",
        "Get-Process node | Stop-Process",  # not the daemon's name: allowed
        "taskkill /PID 10716 /F",
        "uv pip install python-pptx",
        "pkill -f 'http.server 8899'",
        "kill 10716",
        "server_pid=$!; kill $server_pid",
    ],
)
def test_allows_everything_else(command: str):
    assert refusal(command) is None


def test_refuses_the_daemons_own_pid(tmp_path: Path):
    assert refusal("Stop-Process -Id 4242 -Force", own_pids=(4242,)) == (
        "refused: PID 4242 is the host daemon (or its launcher); stopping it would cut you off from this machine."
    )
    assert refusal("Stop-Process -Id 42420 -Force", own_pids=(4242,)) is None
    assert refusal("Get-Process -Id 4242", own_pids=(4242,)) is None  # looking is fine
    shell = Shell(launcher=PY, default_cwd=tmp_path)
    with pytest.raises(ShellRefused, match="is the host daemon"):
        shell.run(f"import os; os.kill({__import__('os').getpid()}, 9)  # taskkill")


@pytest.mark.parametrize(
    "command",
    [
        "Start-Process OUTLOOK.EXE -ArgumentList '/profile','Default'; Start-Sleep -Seconds 12",
        "Start-Process 'C:\\Program Files\\Microsoft Office\\root\\Office16\\OUTLOOK.EXE'",
        '& "C:\\Program Files\\Microsoft Office\\root\\Office16\\OUTLOOK.EXE" /profile Outlook',
        "cmd /c start outlook",
    ],
)
def test_refuses_starting_outlook_by_hand(command: str):
    why = refusal(command)
    assert why and "outlook_restart" in why


def test_looking_at_outlook_is_fine():
    assert refusal("Get-Process OUTLOOK -ErrorAction SilentlyContinue | Select-Object Id,Responding") is None
