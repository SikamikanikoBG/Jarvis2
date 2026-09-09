"""The progress guard: what counts as a repeat, and what the judge is shown.

Both rules were rewritten after the DevBG scan of 2026-09-09 was stopped twice, two minutes in,
while it was working correctly. The transcript is the fixture here.
"""

from __future__ import annotations

import pytest

from jarvis_core.engine.supervision import RunWatch, Supervisor, render_steps, step_of
from jarvis_proto import RunBudget, Settings, ToolResult

# What `browser.read` actually answered, one line per search, as stored on ardi. The first ~100
# characters are the same every time; the query at the end is the only thing that differs.
_PREFIX = "[tab 1271060609] (20+) DevBG | Facebook — https://www.facebook.com/groups/1401603840076709/search/?q="
DEVBG_READS = [
    _PREFIX + "%D1%81%D1%8A%D0%BA%D1%80%D0%B0%D1%89%D0%B5%D0%BD%D0%B8%D1%8F\nТърсене — TruWalla4620, 11 ч",
    _PREFIX + "%D0%B7%D0%B0%D1%82%D0%B2%D0%B0%D1%80%D1%8F%20%D0%BE%D1%84%D0%B8%D1%81\nТърсене — Alexander Mlazev",
    _PREFIX + "%D1%84%D0%B0%D0%BB%D0%B8%D1%80%D0%B0\nТърсене — nothing recent",
    _PREFIX + "%D0%BD%D0%B0%D0%B5%D0%BC%D0%B0\nТърсене — two hiring posts",
    _PREFIX + "%D0%BD%D0%B0%D0%B7%D0%BD%D0%B0%D1%87%D0%B5%D0%BD%D0%B8%D1%8F\nТърсене — one post",
]


@pytest.fixture
def supervisor() -> Supervisor:
    """Default settings, and a judge that fails the test if `signals` ever summons it."""
    settings = Settings()

    def no_judge():  # type: ignore[no-untyped-def]
        pytest.fail("the judge must not be needed")

    return Supervisor(lambda: settings, no_judge)


def watch_of(*steps: object) -> RunWatch:
    w = RunWatch(budget=RunBudget())
    w.steps.extend(steps)  # type: ignore[arg-type]
    return w


def read_step(text: str) -> object:
    """A `browser.read` call: the arguments never vary, only the page it lands on."""
    return step_of("browser.read", {"mode": "text"}, ToolResult.data(text))


def test_same_arguments_different_page_is_not_a_repeat(supervisor: Supervisor):
    """The DevBG shape: open/read/open/read across five pages, identical read arguments.

    This is what the old rule called `repeated identical call: browser.read x5`, and the run was
    killed for it after two nudges.
    """
    watch = watch_of(*(read_step(text) for text in DEVBG_READS))
    assert supervisor.signals(watch) == []


def test_same_arguments_and_same_page_is_still_a_repeat(supervisor: Supervisor):
    """A genuine loop — the model asks the same thing and gets the same answer — still trips."""
    watch = watch_of(*(read_step(DEVBG_READS[0]) for _ in range(3)))
    assert supervisor.signals(watch) == ["repeated identical call with an identical result: browser.read x3"]
    # Below the threshold nothing is armed.
    assert supervisor.signals(watch_of(read_step(DEVBG_READS[0]), read_step(DEVBG_READS[0]))) == []


def test_a_stuck_page_is_caught_even_between_moving_ones(supervisor: Supervisor):
    """Counting is per identity, not per streak: three dead reads among live ones still count."""
    dead = DEVBG_READS[2]
    watch = watch_of(
        read_step(dead), read_step(DEVBG_READS[0]), read_step(dead), read_step(DEVBG_READS[1]), read_step(dead)
    )
    assert supervisor.signals(watch) == ["repeated identical call with an identical result: browser.read x3"]


def test_error_streak_still_arms(supervisor: Supervisor):
    fail = step_of("outlook.send", {"to": "a@b"}, ToolResult.failure("(-2147213311)"))
    assert supervisor.signals(watch_of(fail, fail, fail)) == [
        "repeated identical call with an identical result: outlook.send x3",
        "3 consecutive tool errors",
    ]


def test_render_steps_states_the_shared_head_once_and_decodes_the_rest():
    """What the judge reads. It has to be able to tell the five searches apart."""
    rendered = render_steps([read_step(text) for text in DEVBG_READS])

    # The boilerplate is said once, not five times.
    assert rendered.count("facebook.com/groups/1401603840076709") == 1
    assert "every result below begins" in rendered
    # And each line carries its own query, in Cyrillic rather than percent-escapes.
    for term in ("съкращения", "затваря офис", "фалира", "наема"):
        assert term in rendered, term
    assert "%D1%81%D1%8A" not in rendered
    # The arguments are shown, so "identical arguments" is visible as a fact rather than a hash.
    assert rendered.count('{"mode": "text"}') == len(DEVBG_READS)


def test_render_steps_survives_one_step_and_none():
    assert render_steps([]) == "(none)"
    single = render_steps([read_step(DEVBG_READS[0])])
    # Nothing to share with, so the whole summary is on the line.
    assert "every result below begins" not in single
    assert "DevBG | Facebook" in single


def test_render_steps_does_not_fold_when_the_head_is_short():
    steps = [
        step_of("jarvis.time", {}, ToolResult.data("2026-09-09 07:20")),
        step_of("jarvis.time", {}, ToolResult.data("2026-09-09 07:21")),
    ]
    rendered = render_steps(steps)
    assert "every result below begins" not in rendered
    assert "07:20" in rendered and "07:21" in rendered
