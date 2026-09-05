"""shell_run — captured output, a real deadline, and an off switch."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from jarvis_host.shell import Shell, ShellDisabled, ShellError, ShellTimeout, cap

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
