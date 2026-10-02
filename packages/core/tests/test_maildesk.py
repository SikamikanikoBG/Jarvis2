"""The mail desk against a fake Outlook host: threads, the thread, read state, the chat beside it."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from jarvis_core.features.maildesk import (
    INSTRUCTIONS_CAP,
    MailDeskError,
    clean_subject,
    group_threads,
    thread_instructions,
)
from jarvis_core.tools import BuiltinProvider, tool
from jarvis_proto import ToolResult, TriageSettings
from tests.conftest import Harness


class _ListArgs(BaseModel):
    account: str = ""
    folder: str = "inbox"
    limit: int = 25
    preview_chars: int = 400
    unread_only: bool = False


class _OldListArgs(BaseModel, extra="forbid"):
    account: str = ""
    folder: str = "inbox"
    limit: int = 25
    preview_chars: int = 400


class _ThreadArgs(BaseModel):
    entry_id: str
    account: str = ""
    limit: int = 8
    preview_chars: int = 600


class _ReadArgs(BaseModel):
    entry_id: str
    account: str = ""
    max_chars: int = 20_000


class _MarkArgs(BaseModel):
    entry_ids: list[str]
    read: bool = True
    account: str = ""


def _mail(eid: str, conv: str, subject: str, sender: str, received: str, *, unread: bool = True, mine: bool = False):
    return {
        "entry_id": eid,
        "conversation_id": conv,
        "subject": subject,
        "from": {"name": sender, "address": f"{sender.lower()}@bank.bg"},
        "received": received,
        "preview": f"body of {eid}",
        "unread": unread,
        "mine": mine,
    }


class FakeVm(BuiltinProvider):
    name = "vm"

    def __init__(self) -> None:
        super().__init__()
        self.items = [
            _mail("a1", "C1", "Budget 2027", "Maria", "2026-09-30T09:00:00+03:00"),
            _mail("a2", "C1", "RE: Budget 2027", "Pete", "2026-09-30T11:00:00+03:00"),
            _mail("b1", "C2", "FW: Server down", "Ops", "2026-09-30T10:00:00+03:00"),
            _mail("c1", "", "Lunch?", "Ivan", "2026-09-29T12:00:00+03:00"),
            _mail("d1", "C4", "Already read", "Old", "2026-09-30T12:00:00+03:00", unread=False),
        ]
        self.lists: list[dict[str, Any]] = []
        self.marked: list[tuple[list[str], bool]] = []

    @tool("vm.outlook_list", description="list", args=_ListArgs, read_only=True)
    async def _list(self, **args: Any) -> ToolResult:
        self.lists.append(args)
        items = [i for i in self.items if i["unread"]] if args.get("unread_only") else self.items
        return ToolResult.data(json.dumps({"items": items, "cursor": None}))

    @tool("vm.outlook_thread", description="thread", args=_ThreadArgs, read_only=True)
    async def _thread(self, entry_id: str, account: str = "", limit: int = 8, preview_chars: int = 600) -> ToolResult:
        conv = next(i["conversation_id"] for i in self.items if i["entry_id"] == entry_id)
        if not conv:
            return ToolResult.data(json.dumps({"conversation": False, "items": []}))
        members = sorted((i for i in self.items if i["conversation_id"] == conv), key=lambda i: i["received"])
        mine = _mail("s1", conv, "RE: Budget 2027", "Arsen", "2026-09-30T10:00:00+03:00", unread=False, mine=True)
        if conv == "C1":
            members = sorted([*members, mine], key=lambda i: i["received"])
        return ToolResult.data(json.dumps({"conversation": True, "conversation_id": conv, "items": members}))

    @tool("vm.outlook_read", description="read", args=_ReadArgs, read_only=True)
    async def _read(self, entry_id: str, account: str = "", max_chars: int = 20_000) -> ToolResult:
        item = next(i for i in self.items if i["entry_id"] == entry_id)
        return ToolResult.data(json.dumps({**item, "body": f"full body of {entry_id}"}))

    @tool("vm.outlook_mark_read", description="mark", args=_MarkArgs)
    async def _mark(self, entry_ids: list[str], read: bool = True, account: str = "") -> ToolResult:
        self.marked.append((entry_ids, read))
        for i in self.items:
            if i["entry_id"] in entry_ids:
                i["unread"] = not read
        return ToolResult.data(json.dumps({"read": read, "updated": entry_ids, "failed": []}))


class _ThreadBodiesArgs(_ThreadArgs):
    bodies: bool = False


class _UnreadArgs(BaseModel):
    account: str = ""
    limit: int = 300
    preview_chars: int = 300


class _QueryArgs(BaseModel):
    query: str
    account: str = ""
    limit: int = 100
    days_back: int = 365


class NewVm(FakeVm):
    """A host with unread across folders, Outlook-syntax search and thread bodies (2.0.0a23)."""

    def __init__(self) -> None:
        super().__init__()
        self.items.append(
            _mail("e1", "C5", "DM-1234 approval", "Boss", "2026-09-30T13:00:00+03:00") | {"folder": "Demands"}
        )
        self.queries: list[str] = []
        self.thread_args: list[dict[str, Any]] = []

    @tool("vm.outlook_unread", description="unread", args=_UnreadArgs, read_only=True)
    async def _unread(self, **args: Any) -> ToolResult:
        items = sorted((i for i in self.items if i["unread"]), key=lambda i: i["received"], reverse=True)
        return ToolResult.data(json.dumps({"items": items, "total": len(items), "capped": False}))

    @tool("vm.outlook_query", description="query", args=_QueryArgs, read_only=True)
    async def _query(self, query: str, **_: Any) -> ToolResult:
        self.queries.append(query)
        if query.startswith("color:"):
            return ToolResult.failure("Error executing tool outlook_query: search: unknown keyword color:")
        word = query.rsplit(":", maxsplit=1)[-1].lower()
        hits = [i for i in self.items if word in i["subject"].lower()]
        return ToolResult.data(json.dumps({"items": hits, "total": len(hits), "capped": False, "days_back": 365}))

    @tool("vm.outlook_thread", description="thread", args=_ThreadBodiesArgs, read_only=True)
    async def _thread(self, entry_id: str, bodies: bool = False, **args: Any) -> ToolResult:
        self.thread_args.append({"entry_id": entry_id, "bodies": bodies, **args})
        res = json.loads((await super()._thread(entry_id, **args)).text)
        if bodies:
            for i in res["items"]:
                i["body"] = f"full body of {i['entry_id']}"
                i["preview"] = ""  # what an HTML mail's table preview looks like
        return ToolResult.data(json.dumps(res))


class OldVm(FakeVm):
    """A host from before the unread filter: its list refuses the argument."""

    @tool("vm.outlook_list", description="list", args=_OldListArgs, read_only=True)
    async def _list(self, **args: Any) -> ToolResult:
        self.lists.append(args)
        return ToolResult.data(json.dumps({"items": self.items, "cursor": None}))


async def _desk(harness: Harness, host: BuiltinProvider) -> Any:
    harness.core.registry.add(host)
    await harness.core.registry.refresh()
    harness.enable(triage=TriageSettings(host="vm", accounts=["me@bank.bg"]))
    return harness.core.maildesk


def test_subjects_lose_their_reply_prefixes():
    assert clean_subject("RE: FW: Budget") == "Budget"
    assert clean_subject("Отг: Пр: Бюджет") == "Бюджет"
    assert clean_subject("AW[2]: Plan") == "Plan"
    assert clean_subject("   ") == "(no subject)"


def test_unread_messages_group_into_threads_newest_first():
    threads = group_threads(FakeVm().items[:4])
    assert [t["subject"] for t in threads] == ["Budget 2027", "Server down", "Lunch?"]
    budget = threads[0]
    assert budget["unread_count"] == 2 and budget["entry_ids"] == ["a2", "a1"]
    assert budget["latest"]["entry_id"] == "a2" and budget["senders"] == ["Pete", "Maria"]
    assert threads[2]["key"] == "single:c1" and threads[2]["conversation_id"] is None


async def test_threads_ask_the_host_for_unread_only_and_cache_for_a_minute(harness: Harness):
    vm = FakeVm()
    desk = await _desk(harness, vm)
    res = await desk.threads()
    assert res["host"] == "vm" and res["account"] == "me@bank.bg" and res["unread"] == 4
    assert [t["subject"] for t in res["threads"]] == ["Budget 2027", "Server down", "Lunch?"]
    assert vm.lists[0]["unread_only"] is True and vm.lists[0]["account"] == "me@bank.bg"
    await desk.threads()
    assert len(vm.lists) == 1, "a second look within the minute is served from the cache"
    await desk.threads(refresh=True)
    assert len(vm.lists) == 2


async def test_an_older_host_without_the_filter_still_gives_unread_threads(harness: Harness):
    vm = OldVm()
    desk = await _desk(harness, vm)
    res = await desk.threads()
    assert res["unread"] == 4 and "Already read" not in [t["subject"] for t in res["threads"]]
    # The refused call never reached the host; the retry without the argument did.
    assert len(vm.lists) == 1 and "unread_only" not in vm.lists[0]


async def test_mark_read_clears_the_thread_and_the_cache(harness: Harness):
    vm = FakeVm()
    desk = await _desk(harness, vm)
    first = await desk.threads()
    res = await desk.mark_read(first["threads"][0]["entry_ids"])
    assert res["updated"] == ["a2", "a1"] and vm.marked == [(["a2", "a1"], True)]
    assert [t["subject"] for t in (await desk.threads())["threads"]] == ["Server down", "Lunch?"]
    with pytest.raises(MailDeskError):
        await desk.mark_read([])


async def test_a_thread_spans_sent_items_and_a_lone_message_is_its_own_thread(harness: Harness):
    desk = await _desk(harness, FakeVm())
    thread = await desk.thread("a2")
    assert thread["subject"] == "Budget 2027" and thread["conversation_id"] == "C1"
    assert [(i["entry_id"], i["mine"]) for i in thread["items"]] == [("a1", False), ("s1", True), ("a2", False)]
    lone = await desk.thread("c1")
    assert [i["entry_id"] for i in lone["items"]] == ["c1"] and lone["items"][0]["body"] == "full body of c1"


async def test_the_chat_beside_a_thread_is_one_chat_that_knows_the_thread(harness: Harness):
    desk = await _desk(harness, FakeVm())
    conv = await desk.session("a2")
    assert conv.title == "✉ Budget 2027" and conv.folder_key == "mail:me@bank.bg:C1" and conv.kind.value == "chat"
    assert "reply_to_entry_id=`a2`" in conv.instructions and "vm.outlook_send" in conv.instructions
    assert "★ Arsen" in conv.instructions and "body of a1" in conv.instructions
    # Opened again (from another message of the same thread): the same chat.
    again = await desk.session("a1")
    assert again.id == conv.id
    assert len([c for c in await harness.core.store.list_conversations() if c.folder_key == conv.folder_key]) == 1
    # And a thread that grew gets its instructions refreshed.
    desk_host: FakeVm = next(p for p in harness.core.registry._providers if isinstance(p, FakeVm))
    desk_host.items.append(_mail("a3", "C1", "RE: Budget 2027", "Maria", "2026-09-30T13:00:00+03:00"))
    grown = await desk.session("a3")
    assert grown.id == conv.id and "reply_to_entry_id=`a3`" in grown.instructions


async def test_no_host_is_a_clear_error(harness: Harness):
    harness.enable(triage=TriageSettings(host=""))
    with pytest.raises(MailDeskError, match="Triage"):
        await harness.core.maildesk.threads()


def test_instructions_stay_under_the_cap_and_keep_the_newest_message_whole():
    items = [
        _mail(f"m{n}", "C", "Long", "Maria", f"2026-09-{n + 1:02d}T09:00:00+03:00") | {"preview": "x" * 4000}
        for n in range(20)
    ]
    items[-1]["preview"] = "NEWEST " + "y" * 3000
    text = thread_instructions("vm", "me@bank.bg", {"items": items})
    assert len(text) <= INSTRUCTIONS_CAP and "NEWEST " + "y" * 3000 in text and "older messages omitted" in text


# --- host 2.0.0a23: every folder, search, bodies -------------------------------------------------


async def test_unread_comes_from_every_folder_when_the_host_can(harness: Harness):
    vm = NewVm()
    desk = await _desk(harness, vm)
    res = await desk.threads()
    assert "DM-1234 approval" in [t["subject"] for t in res["threads"]] and res["unread"] == 5
    assert vm.lists == [], "the inbox-only listing is not used when outlook_unread exists"


async def test_search_groups_hits_into_threads_read_ones_included(harness: Harness):
    vm = NewVm()
    desk = await _desk(harness, vm)
    res = await desk.search("subject:budget")
    assert vm.queries == ["subject:budget"] and res["matches"] == 2
    assert [t["subject"] for t in res["threads"]] == ["Budget 2027"] and res["threads"][0]["unread_count"] == 2
    hits = await desk.search("subject:already")
    assert hits["threads"][0]["subject"] == "Already read", "search is not limited to unread mail"
    with pytest.raises(MailDeskError, match="unknown keyword"):
        await desk.search("color:red")
    with pytest.raises(MailDeskError, match="empty"):
        await desk.search("  ")


async def test_an_old_host_says_it_cannot_search(harness: Harness):
    desk = await _desk(harness, FakeVm())
    with pytest.raises(MailDeskError, match="too old to search"):
        await desk.search("budget")


async def test_a_thread_is_read_with_bodies_once_for_screen_and_chat(harness: Harness):
    vm = NewVm()
    desk = await _desk(harness, vm)
    thread = await desk.thread("a2")
    assert [i["body"] for i in thread["items"]] == ["full body of a1", "full body of s1", "full body of a2"]
    conv = await desk.session("a2")
    assert "full body of a2" in conv.instructions
    assert len(vm.thread_args) == 1 and vm.thread_args[0]["bodies"] is True, "the session reused the read"
