"""A long run that outgrows its context keeps its ask and an index of the steps that fell off.

2026-11-17, the FX/MT5 task: 127 steps at ~75k tokens each. Its oldest steps were trimmed away
silently - with them everything up to the next user message - and it re-read the same regions of
execute.py some fifteen times ("reading the last regions I must reuse").
"""

from __future__ import annotations

from jarvis_core.engine.context import STEPS_INDEX_CHARS, ContextAssembler, steps_index
from jarvis_proto import Message, Role, ToolCall

RUN = "run_fx"


def a_long_run(steps: int, size: int = 4000) -> list[Message]:
    history = [
        Message.user("an older turn", run_id="run_old", conversation_id="c"),
        Message.assistant("its answer", run_id="run_old", conversation_id="c"),
        Message.user("# Task: FX via Python\n> drive MT5 from Python", run_id=RUN, conversation_id="c"),
    ]
    for i in range(steps):
        call = ToolCall(
            id=f"c{i}",
            name="workspace.shell_run",
            arguments={"command": f"sed -n '{i * 100},{i * 100 + 99}p' execute.py"},
        )
        history.append(Message.assistant("", tool_calls=[call], run_id=RUN, conversation_id="c"))
        history.append(
            Message.tool(
                f"c{i}",
                "workspace.shell_run",
                f"[ref c{i}] def region_{i}():\n" + "x" * size,
                run_id=RUN,
                conversation_id="c",
            )
        )
    return history


def builder() -> ContextAssembler:
    b = ContextAssembler.__new__(ContextAssembler)
    return b


def test_a_cut_inside_the_run_keeps_the_ask_and_lists_what_fell_off():
    history = a_long_run(30)
    out = builder().trim(history, budget_chars=60_000, run_id=RUN)
    assert out[0].content.startswith("# Task: FX via Python"), "the ask stays"
    index = out[1]
    assert index.role is Role.USER and index.name == "steps_index"
    assert "You DID these" in index.content and "@c0 workspace.shell_run" in index.content
    assert "def region_0():" in index.content and "jarvis.result_read" in index.content
    rest = out[2:]
    assert rest and rest[0].role is Role.ASSISTANT, "the cut lands on a call, never on an orphaned result"
    assert rest[-1].content.startswith("[ref c29]"), "the newest steps are whole"
    listed = {line.split()[1] for line in index.content.splitlines() if line.startswith("- @")}
    shown = {m.tool_call_id for m in rest if m.role is Role.TOOL}
    assert listed and not (listed & {f"@{s}" for s in shown}), "a step is either above or in the index"
    assert sum(len(m.content) for m in out) <= 60_000 + STEPS_INDEX_CHARS


def test_without_a_cut_inside_the_run_nothing_changes():
    history = a_long_run(3, size=100)
    out = builder().trim(history, budget_chars=60_000, run_id=RUN)
    assert out == history


def test_the_index_keeps_the_newest_when_it_is_too_long():
    dropped = a_long_run(400, size=10)[3:]
    text = steps_index(dropped) or ""
    assert len(text) <= STEPS_INDEX_CHARS + 60
    assert "earlier steps not listed" in text and "@c399" in text and "@c0 " not in text
