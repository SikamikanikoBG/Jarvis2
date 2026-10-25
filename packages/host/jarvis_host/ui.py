"""Windows' own UI, worked the way a person works it: the windows, one window's controls, a
click, keys. ``screen_windows`` / ``screen_controls`` / ``screen_click`` / ``screen_type``.

Built on pywinauto over UI Automation (backend "uia"), the same tree screen readers use, so a
button is found by its name rather than by a pixel. Some windows draw their content themselves
and show UI Automation nothing (the Microsoft sign-in dialog's web page); for those, a click
goes to screen coordinates read off ``screen_grab``.

The desktop must be there to be worked. On 2026-10-19 a hidden Microsoft sign-in window had
stopped work mail for two days: the host's session was RDP-disconnected, so there was no screen
to read (BitBlt failed) and nothing to click. :class:`Console` puts the session back on the
console (``tscon``, which only SYSTEM may run for a disconnected session) before any of these
tools, and before ``screen_grab``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any


class UiError(RuntimeError):
    pass


#: The most controls one ``screen_controls`` call walks; a browser window has thousands.
MAX_CONTROLS = 300
#: And the longest it walks: a full walk of a busy desktop took over 90 s on jarvisvm.
WALK_BUDGET_S = 20.0

_TASK = "Jarvis2 host console"
#: The window class Windows puts over a locked session (seen on jarvisvm 2026-10-19).
_LOCK_CLASS = "LockScreenBackstopFrame"
LOCKED = (
    "the console is locked: a click or keys would go to the lock screen and screen_grab shows black. "
    "It needs a sign-in at the machine; say so rather than retrying"
)


class Console:
    """Keeps the host's own session on the console, so there is a screen to read and click.

    The probes are injectable so the decision can be tested off Windows.
    """

    def __init__(
        self,
        *,
        own_session: Callable[[], int] | None = None,
        console_session: Callable[[], int] | None = None,
        run: Callable[[list[str]], str] | None = None,
    ) -> None:
        self._own = own_session or _own_session
        self._console = console_session or _console_session
        self._run = run or _run

    def ensure(self) -> str | None:
        """Attach this session to the console when it is not; what was done, or None."""
        own = self._own()
        if self._console() == own:
            return None
        # tscon of a disconnected session needs SYSTEM; a one-shot task is the way to be SYSTEM.
        self._run(
            [
                "schtasks", "/Create", "/TN", _TASK, "/TR", f"tscon {own} /dest:console",
                "/SC", "ONCE", "/ST", "00:00", "/RU", "SYSTEM", "/RL", "HIGHEST", "/F",
            ]
        )  # fmt: skip
        try:
            self._run(["schtasks", "/Run", "/TN", _TASK])
            deadline = time.monotonic() + 10
            while self._console() != own and time.monotonic() < deadline:
                time.sleep(0.25)
        finally:
            with contextlib.suppress(Exception):
                self._run(["schtasks", "/Delete", "/TN", _TASK, "/F"])
        if self._console() != own:
            raise UiError(f"session {own} could not be put on the console (tscon as SYSTEM did not take)")
        return f"session {own} was not on the console; reattached it"


class Ui:
    def __init__(self, *, enabled: bool = True, console: Console | None = None) -> None:
        self.enabled = enabled
        self.console = console or Console()
        #: pywinauto's Desktop(backend="uia"); replaced in tests.
        self.desktop: Callable[[], Any] = _desktop
        self.mouse_click: Callable[[int, int], None] = _mouse_click
        self.send_keys: Callable[[str], None] = _send_keys
        # One thread with its own COM apartment for all UI work: UI Automation is COM, and the
        # Outlook worker's apartment must not wait behind a slow walk of a browser window.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-ui", initializer=_com_init)

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, lambda: fn(*args))

    # --- tools ---------------------------------------------------------------------------

    def windows(self) -> dict[str, Any]:
        note = self._ready()
        rows = []
        for w in self.desktop().windows():
            title = _safe(w.window_text)
            if not title or not _safe(w.is_visible, False):
                continue
            rows.append(
                {
                    "handle": int(w.handle),
                    "title": title,
                    "class": _safe(lambda w=w: w.element_info.class_name),
                    "pid": _safe(lambda w=w: w.element_info.process_id),
                    "rect": _rect(w),
                }
            )
        if self._locked():
            note = LOCKED
        return _with_note({"windows": rows}, note)

    def controls(self, window: str, max_controls: int = 150) -> dict[str, Any]:
        note = self._ready()
        win = self._window(window)
        limit = max(1, min(max_controls, MAX_CONTROLS))
        rows: list[dict[str, Any]] = []
        deadline = time.monotonic() + WALK_BUDGET_S
        complete = True
        for el in win.descendants():
            if len(rows) >= limit or time.monotonic() > deadline:
                complete = False
                break
            name = _safe(el.window_text)
            auto_id = _safe(lambda el=el: el.element_info.automation_id)
            if not name and not auto_id:
                continue
            rows.append(
                {
                    "name": name[:200],
                    "type": _safe(lambda el=el: el.element_info.control_type),
                    "automation_id": auto_id,
                    "enabled": _safe(el.is_enabled, True),
                    "rect": _rect(el),
                }
            )
        out: dict[str, Any] = {"window": _safe(win.window_text), "controls": rows, "complete": complete}
        if not complete:
            out["hint"] = "listing cut short; a control you need may be further down - or click by coordinates"
        return _with_note(out, note)

    def click(
        self,
        window: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        x: int | None = None,
        y: int | None = None,
    ) -> dict[str, Any]:
        note = self._ready(acting=True)
        if name:
            if not window:
                raise UiError("give the window the control is in (a title or a handle from screen_windows)")
            win = self._window(window)
            el = _find(win, name, control_type)
            with contextlib.suppress(Exception):
                win.set_focus()
            how = "invoked"
            try:
                el.invoke()
            except Exception:
                el.click_input()
                how = "clicked"
            return _with_note({"done": how, "control": _safe(el.window_text), "window": _safe(win.window_text)}, note)
        if x is None or y is None:
            raise UiError("give a control name (with its window) or screen coordinates x and y")
        if window:
            with contextlib.suppress(Exception):
                self._window(window).set_focus()
        self.mouse_click(int(x), int(y))
        return _with_note({"done": "clicked", "at": [int(x), int(y)]}, note)

    def type(
        self, text: str = "", keys: str = "", window: str | None = None, name: str | None = None
    ) -> dict[str, Any]:
        note = self._ready(acting=True)
        if not text and not keys:
            raise UiError("give text to type and/or keys to press")
        target = "the focused control"
        if window:
            win = self._window(window)
            with contextlib.suppress(Exception):
                win.set_focus()
            target = _safe(win.window_text)
            if name:
                el = _find(win, name, None)
                el.set_focus()
                target = f"{_safe(el.window_text)} in {target}"
        if text:
            self.send_keys(_literal(text))
        if keys:
            self.send_keys(keys)
        return _with_note({"typed": len(text), "keys": keys, "into": target}, note)

    # --- helpers -------------------------------------------------------------------------

    def _ready(self, *, acting: bool = False) -> str | None:
        if not self.enabled:
            raise UiError("the screen tools are disabled on this host (screen.enabled = false in host.toml)")
        note = self.console.ensure()
        if acting and self._locked():
            raise UiError(LOCKED)
        return note

    def _locked(self) -> bool:
        return any(_safe(lambda w=w: w.element_info.class_name) == _LOCK_CLASS for w in self.desktop().windows())

    def _window(self, window: str) -> Any:
        desktop = self.desktop()
        wanted = window.strip()
        if wanted.isdigit():
            for w in desktop.windows():
                if int(w.handle) == int(wanted):
                    return w
            raise UiError(f"no window with handle {wanted}; see screen_windows")
        titled = [w for w in desktop.windows() if _safe(w.window_text) and _safe(w.is_visible, False)]
        matchers: tuple[Callable[[str], bool], ...] = (
            lambda t: t == wanted.lower(),
            lambda t: t.startswith(wanted.lower()),
            lambda t: wanted.lower() in t,
        )
        for match in matchers:
            hits = [w for w in titled if match(_safe(w.window_text).lower())]
            if hits:
                return hits[0]
        raise UiError(
            f"no visible window titled like {window!r}; open: " + ", ".join(_safe(w.window_text) for w in titled[:25])
        )


def _find(win: Any, name: str, control_type: str | None) -> Any:
    """The control called ``name`` (exact, then contained, ignoring case), of a type if given."""
    wanted = name.strip().lower()
    deadline = time.monotonic() + WALK_BUDGET_S
    exact, partial = None, None
    for el in win.descendants():
        if time.monotonic() > deadline:
            break
        if control_type and _safe(lambda el=el: el.element_info.control_type).lower() != control_type.lower():
            continue
        text = _safe(el.window_text).lower()
        if text == wanted:
            exact = el
            break
        if partial is None and wanted in text:
            partial = el
    found = exact or partial
    if found is None:
        raise UiError(
            f"no control named like {name!r} in {_safe(win.window_text)!r}; list them with screen_controls, "
            "or click by coordinates from screen_grab when the window shows UI Automation nothing"
        )
    return found


def _safe(fn: Callable[[], Any], default: Any = "") -> Any:
    """One UI Automation property; a control that vanished mid-walk must not end the walk."""
    try:
        value = fn()
    except Exception:
        return default
    return default if value is None else value


def _rect(el: Any) -> list[int] | None:
    try:
        r = el.rectangle()
        return [int(r.left), int(r.top), int(r.right), int(r.bottom)]
    except Exception:
        return None


def _with_note(out: dict[str, Any], note: str | None) -> dict[str, Any]:
    if note:
        out["session"] = note
    return out


def _literal(text: str) -> str:
    """Text for pywinauto's send_keys typed as it is: its control characters escaped."""
    return "".join(f"{{{c}}}" if c in "+^%~(){}[]" else c for c in text)


# --- Windows ---------------------------------------------------------------------------------


def _com_init() -> None:
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            importlib.import_module("pythoncom").CoInitialize()


def _pywinauto() -> Any:
    if sys.platform != "win32":
        raise UiError("the screen tools need a Windows desktop")
    try:
        return importlib.import_module("pywinauto")
    except ImportError as exc:
        raise UiError("the `pywinauto` package is not installed on this host") from exc


def _desktop() -> Any:
    return _pywinauto().Desktop(backend="uia")


def _mouse_click(x: int, y: int) -> None:
    _pywinauto()
    importlib.import_module("pywinauto.mouse").click(button="left", coords=(x, y))


def _send_keys(keys: str) -> None:
    _pywinauto()
    importlib.import_module("pywinauto.keyboard").send_keys(keys, with_spaces=True, with_newlines=True, pause=0.02)


def _own_session() -> int:
    import ctypes
    import os

    sid = ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):  # type: ignore[attr-defined]
        raise UiError("could not read this process's session id")
    return int(sid.value)


def _console_session() -> int:
    import ctypes

    return int(ctypes.windll.kernel32.WTSGetActiveConsoleSessionId())  # type: ignore[attr-defined]


def _run(argv: list[str]) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    if done.returncode != 0:
        raise UiError(f"{argv[0]} {argv[1]} failed: {(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout
