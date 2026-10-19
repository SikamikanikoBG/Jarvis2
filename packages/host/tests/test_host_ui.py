"""The screen tools against a fake desktop: finding windows and controls, clicking, typing, and
putting the session back on the console first (2026-10-19: a hidden Microsoft sign-in window in an
RDP-disconnected session stopped work mail for two days; there was no screen to read or click)."""

from __future__ import annotations

from typing import Any

import pytest

from jarvis_host.ui import Console, Ui, UiError, _literal


class Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class Info:
    def __init__(self, control_type: str = "Button", automation_id: str = "", class_name: str = "", pid: int = 1):
        self.control_type, self.automation_id, self.class_name, self.process_id = (
            control_type,
            automation_id,
            class_name,
            pid,
        )


class El:
    def __init__(
        self, name: str, control_type: str = "Button", *, invokable: bool = True, kids: list[El] | None = None
    ):
        self.name = name
        self.element_info = Info(control_type)
        self.invokable = invokable
        self.kids = kids or []
        self.handle = abs(hash(name)) % 100000
        self.events: list[str] = []

    def window_text(self) -> str:
        return self.name

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def rectangle(self) -> Rect:
        return Rect(10, 20, 110, 50)

    def descendants(self) -> list[El]:
        out: list[El] = []
        for k in self.kids:
            out += [k, *k.descendants()]
        return out

    def invoke(self) -> None:
        if not self.invokable:
            raise RuntimeError("no InvokePattern")
        self.events.append("invoke")

    def click_input(self) -> None:
        self.events.append("click_input")

    def set_focus(self) -> None:
        self.events.append("focus")


class Desk:
    def __init__(self, *wins: El) -> None:
        self.wins = list(wins)

    def windows(self) -> list[El]:
        return self.wins


class Still:
    """A console the session is already on: nothing to do."""

    def ensure(self) -> str | None:
        return None


def ui_with(*wins: El) -> tuple[Ui, list[Any]]:
    ui = Ui(console=Still())  # type: ignore[arg-type]
    desk = Desk(*wins)
    ui.desktop = lambda: desk
    seen: list[Any] = []
    ui.mouse_click = lambda x, y: seen.append(("click", x, y))
    ui.send_keys = lambda k: seen.append(("keys", k))
    return ui, seen


def sign_in() -> El:
    return El(
        "Windows Security",
        "Window",
        kids=[
            El("Microsoft", "Text"),
            El("Send another request to my Microsoft Authenticator app", "Hyperlink"),
            El("Continue", "Button", invokable=False),
            El("Password", "Edit"),
        ],
    )


def test_windows_and_their_controls_are_listed_by_name():
    ui, _ = ui_with(El("Inbox - Outlook", "Window"), sign_in(), El("", "Pane"))
    titles = [w["title"] for w in ui.windows()["windows"]]
    assert titles == ["Inbox - Outlook", "Windows Security"]  # untitled panes are not windows to work
    listed = ui.controls("windows security")
    assert listed["window"] == "Windows Security" and listed["complete"] is True
    assert ("Continue", "Button") in [(c["name"], c["type"]) for c in listed["controls"]]
    assert listed["controls"][0]["rect"] == [10, 20, 110, 50]


def test_a_named_control_is_invoked_or_clicked_when_it_cannot_be_invoked():
    win = sign_in()
    ui, _ = ui_with(win)
    link = ui.click(window="Security", name="send another request")
    assert link["done"] == "invoked" and link["control"].startswith("Send another request")
    button = ui.click(window="Windows Security", name="Continue", control_type="button")
    assert button["done"] == "clicked"
    assert win.descendants()[2].events == ["click_input"]


def test_coordinates_click_the_screen_and_a_missing_control_says_what_to_do():
    ui, seen = ui_with(sign_in())
    assert ui.click(x=456, y=432) == {"done": "clicked", "at": [456, 432]}
    assert seen == [("click", 456, 432)]
    with pytest.raises(UiError, match="screen_controls"):
        ui.click(window="Windows Security", name="Approve")
    with pytest.raises(UiError, match="no visible window"):
        ui.controls("Notepad")
    with pytest.raises(UiError, match="give the window"):
        ui.click(name="Continue")


def test_text_is_typed_as_written_and_keys_are_pressed():
    win = sign_in()
    ui, seen = ui_with(win)
    out = ui.type(text="p@ss (1+1)", keys="{ENTER}", window="Windows Security", name="Password")
    assert seen == [("keys", "p@ss {(}1{+}1{)}"), ("keys", "{ENTER}")]
    assert out["into"] == "Password in Windows Security"
    assert _literal("~%^") == "{~}{%}{^}"


def test_the_tools_are_refused_when_the_screen_is_switched_off():
    ui, _ = ui_with(sign_in())
    ui.enabled = False
    with pytest.raises(UiError, match="disabled"):
        ui.windows()


def test_a_session_off_the_console_is_put_back_on_it_as_system():
    state = {"console": 1}
    ran: list[list[str]] = []

    def run(argv: list[str]) -> str:
        ran.append(argv)
        if argv[:2] == ["schtasks", "/Run"]:
            state["console"] = 2
        return ""

    console = Console(own_session=lambda: 2, console_session=lambda: state["console"], run=run)
    assert console.ensure() == "session 2 was not on the console; reattached it"
    create = ran[0]
    assert create[:2] == ["schtasks", "/Create"] and "tscon 2 /dest:console" in create and "SYSTEM" in create
    assert [a[1] for a in ran] == ["/Create", "/Run", "/Delete"]  # the one-shot task is removed
    # Already there: nothing runs.
    ran.clear()
    assert console.ensure() is None and ran == []


def test_a_reattach_that_does_not_take_is_an_error_and_still_cleans_up(monkeypatch: pytest.MonkeyPatch):
    import jarvis_host.ui as ui_mod

    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(ui_mod.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(ui_mod.time, "sleep", lambda s: None)
    ran: list[str] = []
    console = Console(own_session=lambda: 2, console_session=lambda: 1, run=lambda a: ran.append(a[1]) or "")
    with pytest.raises(UiError, match="could not be put on the console"):
        console.ensure()
    assert ran[-1] == "/Delete"
