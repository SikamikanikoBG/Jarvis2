"""A long tool result as sections with @refs - the view every tool's output gets (2026-09-20).

Before: a result that did not fit rode as its first N chars plus a marker, and the rest was
reachable by character offset. The first 700 chars of an outlook_list are a hex store_id; a 12k
head of a 43k folder tree told the model so little it fished with sixty calls. Here the same
results arrive as an outline the model can reason about and read back by section.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jarvis_core.engine.views import (
    AGED_VIEW_CHARS,
    admit,
    aged,
    locate,
    parse,
    parse_ref,
    read,
    render,
    short_ref,
    view_of,
)
from jarvis_proto import Message, ToolCall, ToolResultKind
from tests.conftest import Harness
from tests.test_loop import FakeTurn, with_tools

STORE_ID = "00000000" * 60  # 480 hex chars, the same on every item
MAILS = {
    "account": "arsen@example.com",
    "folder": "\\\\arsen@example.com\\Inbox\\Jira",
    "items": [
        {
            "entry_id": f"EF00{i:04d}D6052F34B5DEEB489B9BAE577B57448AE41236{i:02d}",
            "store_id": STORE_ID,
            "subject": f"[JIRA] Ticket {i}: something broke in module {i}",
            "from": f"Reporter {i}",
            "received": f"2026-09-{10 + i:02d}T10:51:47+03:00",
            "unread": i % 2 == 0,
            "size": 40_000 + i,
        }
        for i in range(1, 13)
    ],
    "cursor": None,
    "total": 12,
}


def _mails_json() -> str:
    return json.dumps(MAILS, ensure_ascii=False)


# --- refs ---------------------------------------------------------------------------------


def test_refs_drop_the_vllm_prefix_and_parse_back_with_sections_and_ranges():
    assert short_ref("chatcmpl-tool-a254a08e1df37916") == "@a254a08e1df37916"
    assert short_ref("c1") == "@c1"
    ids, path = parse_ref("@a254a08e1df37916.3.1-4")
    assert ids == ["a254a08e1df37916", "chatcmpl-tool-a254a08e1df37916"]
    assert path == [3, (1, 4)]
    assert parse_ref("c1") == (["c1", "chatcmpl-tool-c1"], [])
    with pytest.raises(ValueError):
        parse_ref("@c1.x")


# --- JSON ---------------------------------------------------------------------------------


def test_a_json_list_becomes_items_with_the_common_values_hoisted_and_ids_shortened():
    raw = _mails_json()
    doc = parse(raw, "@m")
    assert doc.shape == "list" and len(doc.sections) == 13  # envelope + 12 mails
    env = doc.sections[0]
    assert env.ref == "@m.0" and "common: store_id" in env.title and STORE_ID in env.body
    first = doc.sections[1]
    assert first.ref == "@m.1"
    assert first.title.startswith("subject: [JIRA] Ticket 1")
    assert "EF000001D6052F34" not in first.title, "the preview names the mail, not its ids"
    ids = parse(json.dumps([{"id": f"{i:02d}" * 40, "token": f"{i:02d}" * 40, "n": i} for i in range(3)]), "@i")
    assert ids.sections[0].title == "token: …00000000, n: 0", (
        "ids stay out of previews; other opaque values keep their tail"
    )
    assert "entry_id: EF000001D6052F34B5DEEB489B9BAE577B57448AE4123601" in first.body, "...and whole in the body"
    assert "store_id" not in first.body, "hoisted, not repeated twelve times"
    # Compact and whole, it is a third shorter than the minified JSON - and fits where the raw did not.
    view = render(doc, 7_000)
    assert len(raw) > 7_000 > len(view) and len(view) < 0.6 * len(raw)
    assert "shown: whole (list, compact)" in view
    assert view.count("subject: [JIRA] Ticket") == 12 and view.count(STORE_ID) == 1


def test_a_big_list_arrives_as_an_outline_and_ages_into_grouped_ranges():
    folders = {
        "account": "arsen@example.com",
        "folders": [
            {
                "id": f"{i:08x}" * 12,
                "name": f"Folder {i}",
                "path": f"Root/Area {i // 20}/Folder {i}",
                "kind": "mail",
                "unread": i % 7,
                "total": i * 3,
            }
            for i in range(300)
        ],
    }
    raw = json.dumps(folders)
    assert len(raw) > 40_000
    doc = parse(raw, "@f")
    view = render(doc, 16_000)
    assert len(view) <= 16_200
    assert "@f: 301 sections (" in view and "— outline:" in view
    lines = [ln for ln in view.split("\n") if ln.startswith("@f.")]
    assert lines[0].startswith("@f.0 account: arsen@example.com")
    assert lines[1].startswith("@f.1 path: Root/Area 0/Folder 0, unread: 0, total: 0"), (
        "the path says the name; kind is common"
    )
    assert "more sections: @f." in view, "what did not fit is named, not dropped"
    small = render(doc, AGED_VIEW_CHARS)
    assert len(small) <= AGED_VIEW_CHARS + 120
    assert "shown: grouped outline" in small
    assert "@f.0-" in small and "…" in small, "ranges from the envelope on, with the first and last title"
    assert small.rstrip().endswith("finds inside]")


def test_a_json_object_without_a_list_is_one_section_per_key():
    doc = parse(json.dumps({"path": "C:/x", "size": 12, "entries": {"a": 1}, "note": "ok"}), "@o")
    assert doc.shape == "object" and [s.ref for s in doc.sections] == ["@o.1", "@o.2", "@o.3", "@o.4"]
    assert doc.sections[3].title == "note: ok" and doc.sections[3].body == "note: ok"


def test_a_partial_head_line_is_kept_in_front_of_the_view():
    text = "[partial: 12 of 40, more available with cursor='abc']\n" + _mails_json()
    view = render(parse(text, "@p"), 4_000)
    assert view.startswith("[partial: 12 of 40, more available with cursor='abc']\n@p: ")


# --- text ---------------------------------------------------------------------------------


def test_markdown_splits_at_headings_and_a_long_section_reads_back_as_its_own_outline():
    intro = "Fetched https://example.com/readme\n"
    parts = [intro]
    for i in range(1, 9):
        parts.append(f"## Chapter {i}\n" + (f"Paragraph of chapter {i}. " * 40 + "\n\n") * (12 if i == 3 else 2))
    text = "".join(parts)
    doc = parse(text, "@r")
    assert doc.shape == "text" and len(doc.sections) == 9
    assert doc.sections[0].title.startswith("Fetched https://example.com/readme")
    assert doc.sections[3].title == "Chapter 3" and doc.sections[3].size > 11_000
    assert text[doc.sections[3].start : doc.sections[3].end].startswith("## Chapter 3")
    view = render(doc, 3_000)
    assert "@r.4 [" in view and "Chapter 3" in view and "shown: outline" in view
    one = read(text, "@r", [2], 8_000)
    assert one.startswith("@r.2 (") and "## Chapter 1" in one
    big = read(text, "@r", [4], 4_000)
    assert "@r.4: " in big and "@r.4.1" in big and "shown:" in big, "a long section is viewed with sub-refs"
    sub = read(text, "@r", [4, 1], 8_000)
    assert sub.startswith("@r.4.1 (") and "Paragraph of chapter 3" in sub
    rng = read(text, "@r", [(5, 7)], 40_000)
    assert rng.count("## Chapter") == 3 and "@r.5\n" in rng and "@r.7\n" in rng
    short = read(text, "@r", [(1, 9)], 2_500)
    assert "[stopped before @r." in short and "continue with" in short
    with pytest.raises(IndexError):
        read(text, "@r", [42], 8_000)


def test_plain_text_without_headings_is_chunked_and_one_line_walls_are_cut():
    prose = "\n\n".join(f"Paragraph {i}. " + "words " * 120 for i in range(20))  # ~13k, blank-line separated
    doc = parse(prose, "@t")
    assert 3 <= len(doc.sections) <= 6
    assert all(s.title.startswith("Paragraph") for s in doc.sections)
    assert "".join(s.body for s in doc.sections).replace("\n", "") == prose.replace("\n", "")
    wall = "x" * 20_000  # minified something that is not JSON
    doc = parse(wall, "@w")
    assert len(doc.sections) == 4 and all(s.size <= 6_000 for s in doc.sections)


# --- the view of a message ----------------------------------------------------------------


def test_the_view_only_shrinks_and_is_never_reparsed_as_a_document():
    msg = Message.tool("chatcmpl-tool-a254a08e1df37916", "workocholic.outlook_list", _mails_json(), run_id="r")
    assert "shown: whole (list, compact)" in admit(msg, 6_000).content, "compact, it fits where the raw did not"
    admitted = admit(msg, 3_000)
    assert admitted.content.startswith("@a254a08e1df37916: 13 sections")
    assert "shown: outline" in admitted.content and len(admitted.content) <= 3_100
    again = view_of(admitted, 3_000)
    assert again is admitted, "fits: untouched"
    smaller = aged(admitted)
    assert len(smaller.content) <= AGED_VIEW_CHARS + 60
    assert "shown: first" in smaller.content and "more lines of this view" in smaller.content
    assert "[@a254a08e1df37916: " in smaller.content, "the marker still names the ref and the size"
    assert aged(smaller) is smaller
    assert msg.content == _mails_json(), "copies; the original (and the DB) keep the text"


def test_small_results_are_left_alone_at_both_sizes():
    small = Message.tool("c1", "x", "y" * 1_900)
    assert aged(small) is small and admit(small, 16_000) is small
    medium = Message.tool("c2", "x", "y" * 2_500)
    assert aged(medium) is not medium and admit(medium, 16_000) is medium


def test_locate_names_the_section_a_match_falls_in():
    text = "## A\nalpha alpha\n\n## B\nbeta beta\n"
    doc = parse(text, "@x")
    assert locate(doc, text.index("beta"), "beta").ref == "@x.2"
    doc = parse(_mails_json(), "@m")
    assert locate(doc, 0, "Ticket 7:").ref == "@m.7"
    assert locate(doc, 0, "no such text") is None


# --- through the engine -------------------------------------------------------------------


async def test_a_list_result_is_admitted_as_a_view_and_read_back_by_section(harness: Harness):
    """The model gets the outline, asks for one mail by ref, gets its whole entry_id; search
    names the section; the raw window still works; the vLLM-style ref resolves with and
    without its prefix."""
    tools = await with_tools(harness)
    harness.enable(tool_result_admit_chars=6_000)

    async def listing(text: str = "") -> ToolResult:
        return ToolResult.data(_mails_json())

    from jarvis_proto import ToolResult

    tools._entries["test.echo"].fn = listing
    harness.chat.push(
        FakeTurn(
            tool_calls=[ToolCall(id="chatcmpl-tool-9d052fe8b127c2dd", name="test.echo", arguments={"text": "list"})]
        ),
        FakeTurn(text="twelve tickets"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="list jira", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    second = harness.chat.calls[1][0]
    shown = next(m for m in second if m.role.value == "tool")
    assert shown.content.startswith("@9d052fe8b127c2dd: 13 sections")
    assert 'jarvis.result_read(ref="@9d052fe8b127c2dd.N")' in shown.content

    async def call(name: str, **args):
        return await harness.core.registry.call(name, args, cancel=asyncio.Event(), idempotency_key=name + str(args))

    one = await call("jarvis.result_read", ref="@9d052fe8b127c2dd.7")
    assert one.kind is ToolResultKind.DATA
    assert "entry_id: EF000007D6052F34B5DEEB489B9BAE577B57448AE4123607" in one.text and "Ticket 7" in one.text
    env = await call("jarvis.result_read", ref="@9d052fe8b127c2dd.0")
    assert STORE_ID in env.text, "the hoisted value is one read away"
    rng = await call("jarvis.result_read", ref="9d052fe8b127c2dd.2-3")
    assert "Ticket 2" in rng.text and "Ticket 3" in rng.text and "Ticket 4" not in rng.text
    outline = await call("jarvis.result_read", ref="@9d052fe8b127c2dd")
    assert "13 sections" in outline.text
    raw = await call("jarvis.result_read", ref="@9d052fe8b127c2dd", offset=10, limit=50)
    assert "[10-60 of" in raw.text and "continue with offset=60" in raw.text
    hit = await call("jarvis.result_search", ref="@9d052fe8b127c2dd", pattern="module 11")
    assert hit.kind is ToolResultKind.DATA and "@9d052fe8b127c2dd.11 @" in hit.text
    bad = await call("jarvis.result_read", ref="@9d052fe8b127c2dd.99")
    assert bad.kind is ToolResultKind.ERROR and "not 99" in bad.text
    nope = await call("jarvis.result_read", ref="@nope.1")
    assert nope.kind is ToolResultKind.ERROR and "no tool result" in nope.text


async def test_a_long_page_is_stored_whole_and_arrives_as_headings(harness: Harness):
    """browser.read used to cut a page at 12k in the extension; now the whole page reaches the
    core and the model sees its headings, the way it sees every other long result."""
    tools = await with_tools(harness)
    harness.enable(tool_result_admit_chars=4_000)
    page = "[tab 1] Docs — https://example.com/docs\n" + "".join(
        f"# Part {i}\n" + f"Text of part {i}. " * 200 + "\n\n" for i in range(1, 7)
    )
    from jarvis_proto import ToolResult

    async def read_page(text: str = "") -> ToolResult:
        return ToolResult.data(page)

    tools._entries["test.echo"].fn = read_page
    harness.chat.push(
        FakeTurn(tool_calls=[ToolCall(id="pg", name="test.echo", arguments={"text": "read"})]),
        FakeTurn(text="six parts"),
    )
    conv = await harness.core.store.create_conversation()
    sub = harness.subscribe(conv.id)
    await harness.core.engine.create_run(text="read the docs", conversation_id=conv.id)
    await harness.wait_for(sub, "run.done")
    shown = next(m for m in harness.chat.calls[1][0] if m.role.value == "tool")
    assert len(shown.content) < 4_200 and "@pg.3 [" in shown.content and "Part 2" in shown.content
    whole = await harness.core.store.tool_result("pg")
    assert whole is not None and whole[1] == page
