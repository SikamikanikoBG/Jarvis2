"""What an earlier turn looks like to the model - and that its answer survives its own bulk.

The AI Masterclass chat, 2026-09-11: turn one found the mail after thirty searches and a
41k-character folder dump; turn two asked about "the event" and the model had never heard of
it, because the trim counted that dump at full size, threw the whole turn away, and the
compaction summary covered only the question. Three turns, the mail hunted for three times.
"""

from __future__ import annotations

from jarvis_core.engine.context import OLD_TOOL_RESULT_HEAD, earlier_turn_view, tool_result_head
from jarvis_core.features.compaction import _cut_index
from jarvis_proto import Message, Role, Run, RunKind, ToolCall
from tests.conftest import Harness


def test_the_head_is_idempotent_and_leaves_short_results_alone():
    short = Message.tool("c1", "x.search", "small result", run_id="run_a")
    assert tool_result_head(short) is short
    long = Message.tool("c2", "x.folders", "F" * 5_000, run_id="run_a")
    head = tool_result_head(long)
    assert head.content.startswith("F" * OLD_TOOL_RESULT_HEAD)
    assert "[truncated to save context: 5,000 chars in full." in head.content
    assert 'jarvis.result_read(ref="c2")' in head.content
    assert tool_result_head(head) is head  # already a head: untouched
    assert long.content == "F" * 5_000  # a copy was made; the original (and the DB) keep the text


def test_earlier_turns_ride_as_heads_and_stubs_and_the_current_run_is_whole():
    ctx_a = "[Context for the request above]\n\n## Skill: email_triage\n# Skill: Email Triage\n" + "x" * 8_000
    ctx_b = "[Context for the request above]\n\n## Skill: browser_page_navigation\n" + "y" * 9_000
    history = [
        Message.user("find the mail", run_id="run_a"),
        Message.user(ctx_a, name="context", run_id="run_a"),
        Message.assistant(tool_calls=[ToolCall(id="c1", name="outlook.folders")], run_id="run_a"),
        Message.tool("c1", "outlook.folders", "F" * 41_000, run_id="run_a"),
        Message.assistant("Found it: AI Masterclass, 6 October.", run_id="run_a"),
        Message.user("propose a card", run_id="run_b"),
        Message.user(ctx_b, name="context", run_id="run_b"),
        Message.tool("c9", "browser.read", "B" * 5_000, run_id="run_b"),
    ]
    view = earlier_turn_view(history, "run_b")
    assert [m.role for m in view] == [m.role for m in history]
    assert view[0].content == "find the mail"
    assert view[1].content.startswith("[Context for the request above was injected for an earlier turn")
    assert "email_triage" in view[1].content and len(view[1].content) < 200
    assert len(view[3].content) < 1_000 and 'ref="c1"' in view[3].content
    assert view[4].content == "Found it: AI Masterclass, 6 October."  # the answer, whole
    # The current run: its context and its results are what the model is working from.
    assert view[6].content == ctx_b and view[7].content == "B" * 5_000
    # Fork copies carry no run id; they are earlier turns too.
    orphan = Message.tool("c3", "x", "Z" * 3_000)
    assert len(earlier_turn_view([orphan], "run_b")[0].content) < 1_000


async def test_the_previous_turns_answer_survives_its_own_bulk(harness: Harness):
    core = harness.core
    core.apply_settings(core.settings.model_copy(update={"history_token_budget": 6_000}))  # ~19k chars
    conv = await core.store.create_conversation()
    a = "run_a"
    for m in (
        Message.user("виж мейла от Ваня", run_id=a),
        Message.user(
            "[Context for the request above]\n\n## Skill: email_triage\n" + "x" * 6_000, name="context", run_id=a
        ),
        Message.assistant(tool_calls=[ToolCall(id="c1", name="workocholic.outlook_folders")], run_id=a),
        Message.tool("c1", "workocholic.outlook_folders", "F" * 41_000, run_id=a),
        Message.assistant("Намерих го: AI Masterclass на Капитал, 6 октомври в Милениум.", run_id=a),
        Message.user("предложи кратка визитка за събитието", run_id="run_b"),
    ):
        m.conversation_id = conv.id
        await core.store.add_message(m)
    run = Run(id="run_b", conversation_id=conv.id, kind=RunKind.CHAT, input_text="предложи кратка визитка за събитието")
    await core.store.create_run(run)

    messages = await core.context.assemble(run)
    texts = [m.content for m in messages[1:]]  # after the system message
    assert any("AI Masterclass" in t for t in texts), "the previous turn's answer is exactly what this question needs"
    folders = next(m for m in messages if m.role is Role.TOOL)
    assert len(folders.content) < 1_000 and 'ref="c1"' in folders.content
    assert not any("x" * 1_000 in t for t in texts), "an earlier turn's skill text does not ride along"
    assert texts[-1] == "предложи кратка визитка за събитието"
    # Under the budget with room to spare: the trim had nothing to throw away.
    assert sum(len(t) + 8 for t in texts) < core.context.budget_chars
    # And the full folder dump is still in the DB for jarvis.result_read.
    whole = await core.store.tool_result("c1")
    assert whole is not None and len(whole[1]) == 41_000


def test_compaction_cuts_at_a_message_arsen_wrote_not_at_the_injected_context():
    def u(text: str, name: str | None = None) -> Message:
        return Message.user(text, name=name)

    t = Message.tool("c", "t", "r")
    history = [
        u("q1"), u("ctx1", name="context"), Message.assistant("a1"), t, t, Message.assistant("a1 final"),
        u("q2"), u("ctx2", name="context"), Message.assistant("a2"), t, t, Message.assistant("a2 final"),
        u("q3"), u("ctx3", name="context"),
    ]  # fmt: skip
    # 14 messages; the middle (7) is ctx2, which must not be the cut: q2 at 6 is.
    assert _cut_index(history) == 6
    # One long turn: nothing older to fold. Heads and the trim deal with it, not a summary of
    # the question alone.
    one = [
        u("q1"),
        u("ctx1", name="context"),
        Message.assistant("a"),
        t,
        Message.assistant("final"),
        u("q2"),
        u("ctx2", name="context"),
    ]
    assert _cut_index(one) == 0
    # A supervisor note is not a boundary either.
    assert (
        _cut_index(
            [u("q1"), Message.assistant("a"), u("q2"), u("n", name="supervisor"), Message.assistant("b"), u("q3")]
        )
        == 2
    )
