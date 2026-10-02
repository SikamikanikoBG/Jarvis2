"""What an earlier turn looks like to the model - and that its answer survives its own bulk.

The AI Masterclass chat, 2026-09-11: turn one found the mail after thirty searches and a
41k-character folder dump; turn two asked about "the event" and the model had never heard of
it, because the trim counted that dump at full size, threw the whole turn away, and the
compaction summary covered only the question. Three turns, the mail hunted for three times.
"""

from __future__ import annotations

from jarvis_core.engine.context import conversation_ledger, earlier_turn_view, last_turn_skills
from jarvis_core.engine.views import aged
from jarvis_core.features.compaction import _cut_index
from jarvis_proto import Message, Role, Run, RunKind, ToolCall
from tests.conftest import Harness


def test_the_view_is_idempotent_and_leaves_short_results_alone():
    short = Message.tool("c1", "x.search", "small result", run_id="run_a")
    assert aged(short) is short
    long = Message.tool("c2", "x.folders", "F" * 5_000, run_id="run_a")
    view = aged(long)
    assert view.content.startswith("@c2: 1 sections (5,000 chars)")
    assert "[@c2: 5,000 chars in 1 section; shown: outline." in view.content
    assert 'jarvis.result_read(ref="@c2.N")' in view.content
    assert len(view.content) <= 1_300
    assert aged(view) is view  # already a view that fits: untouched
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
    assert view[1].content.startswith("[The context block for the request above is not repeated here")
    assert "omitted" not in view[1].content  # "omitted" read as "lost" and the model started over
    assert "email_triage" in view[1].content and len(view[1].content) < 200
    assert len(view[3].content) < 1_300 and 'ref="@c1' in view[3].content
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
    assert len(folders.content) < 1_300 and 'ref="@c1' in folders.content
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


def _digest_turn(run_id: str = "run_a") -> list[Message]:
    """The SmartLab digest, 2026-10-02: twenty steps, a file written, Outlook down, Discord used."""
    calls = [
        ToolCall(id="h1", name="jarvisvm.host_status"),
        *(ToolCall(id=f"s{i}", name="web.search", arguments={"query": f"AI news {i}"}) for i in range(6)),
        ToolCall(
            id="w1", name="workspace.fs_write", arguments={"path": "/workspace/smartlab_ai_digest_2026-10-02.html"}
        ),
        ToolCall(
            id="d1", name="notify.discord", arguments={"files": [{"filename": "digest.html", "content": "H" * 30_000}]}
        ),
    ]
    out = [
        Message.user(
            "Curate the SmartLab AI Digest. Create DRAFT to aiprocesstransformation@postbank.bg", run_id=run_id
        ),
        Message.user(
            "[Context for the request above]\n\n## Skill: smartlab-digest-runbook\n" + "r" * 5_000,
            name="context",
            run_id=run_id,
        ),
    ]
    for c in calls:
        out.append(Message.assistant(tool_calls=[c], run_id=run_id))
        text = "Error: connect timeout" if c.id == "h1" else "R" * 6_000
        out.append(Message.tool(c.id, c.name, text, run_id=run_id))
    out.append(Message.assistant("SmartLab AI Digest готов. Outlook недостъпен, пратих в Discord.", run_id=run_id))
    return out


def test_the_ledger_names_what_was_done_and_folds_repeats():
    ledger = conversation_ledger([*_digest_turn(), Message.user("само прати мейла", run_id="run_b")], "run_b")
    assert ledger is not None
    assert "asked: Curate the SmartLab AI Digest" in ledger
    assert "jarvisvm.host_status FAILED" in ledger
    assert "web.search(query=AI news 0) @s0" in ledger and "web.search(query=AI news 5) @s5" in ledger
    assert "path=/workspace/smartlab_ai_digest_2026-10-02.html" in ledger
    assert "answered: SmartLab AI Digest готов" in ledger
    assert "само прати мейла" not in ledger  # the current run is not an earlier turn
    assert "H" * 100 not in ledger and len(ledger) < 2_000
    # Identical calls fold into one entry with a count.
    same = [Message.user("q", run_id="r1")]
    for i in range(10):
        same += [
            Message.assistant(tool_calls=[ToolCall(id=f"x{i}", name="workspace.shell_run")], run_id="r1"),
            Message.tool(f"x{i}", "workspace.shell_run", "ok", run_id="r1"),
        ]
    folded = conversation_ledger(same, "now")
    assert folded is not None and "workspace.shell_run x10 @x9" in folded


def test_the_ledger_stays_within_its_budget_newest_turn_in_detail():
    history: list[Message] = []
    for t in range(40):
        history += [
            Message.user(f"question {t} " + "q" * 400, run_id=f"r{t}"),
            Message.assistant(f"answer {t} " + "a" * 900, run_id=f"r{t}"),
        ]
    ledger = conversation_ledger(history, "now", max_chars=3_000)
    assert ledger is not None and len(ledger) <= 3_000
    assert "earlier turn(s) not listed" in ledger
    assert "T40 " in ledger and "answer 39 " + "a" * 500 in ledger  # the newest, in detail


def test_a_follow_up_inherits_the_previous_turns_skills():
    history = [*_digest_turn(), Message.user("само прати мейла", run_id="run_b")]
    assert last_turn_skills(history, "run_b") == ["smartlab-digest-runbook"]
    assert last_turn_skills(history, "run_a") == []  # its own context is not "the previous turn"


async def test_a_follow_up_after_a_long_run_still_knows_what_it_did(harness: Harness):
    """The turn itself is trimmed away; the ledger is not."""
    core = harness.core
    core.apply_settings(core.settings.model_copy(update={"history_token_budget": 4_000}))  # ~13k chars
    conv = await core.store.create_conversation()
    for m in (*_digest_turn(), Message.user("само прати мейла, вече имаш всичко", run_id="run_b")):
        m.conversation_id = conv.id
        await core.store.add_message(m)
    run = Run(id="run_b", conversation_id=conv.id, kind=RunKind.CHAT, input_text="само прати мейла, вече имаш всичко")
    await core.store.create_run(run)

    messages = await core.context.assemble(run)
    ledger = next(m for m in messages if m.name == "ledger")
    assert "/workspace/smartlab_ai_digest_2026-10-02.html" in ledger.content
    assert "aiprocesstransformation@postbank.bg" in ledger.content
    assert "jarvisvm.host_status FAILED" in ledger.content
    # It sits right before this run's own messages, so the cached history prefix is untouched.
    assert messages[-2] is ledger and messages[-1].content == "само прати мейла, вече имаш всичко"
