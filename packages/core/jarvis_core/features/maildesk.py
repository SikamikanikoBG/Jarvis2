"""The mail desk: unread threads on the left, one thread in the middle, Jarvis beside it.

V1 had this as an overlay with a second, thinner chat client bolted onto the same socket; its
journal (journal_email_dock.md) is the list of what that cost - no streaming, no tool cards, the
thread pasted into the visible message. Here the desk is only DATA: the threads come from the
Outlook host the triage already uses, and the conversation beside a thread is an ordinary chat
whose instructions carry the thread. The chat screen is the one every other chat uses.

Why the thread rides in the instructions and not in a tool call: the model starts already
knowing the subject, who wrote what and the id to reply to, so "draft an answer" is one step,
not five; and the instructions sit in the stable part of the system message, so they are
prefilled once and cached for every turn after.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis_core.features.mail import triage_tool
from jarvis_proto import Conversation
from jarvis_proto.events import ConversationUpdated

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

FOLDER_KEY_PREFIX = "mail:"
FOLDER_LABEL = "Mail"
# How much of the inbox the desk looks at: unread mail is usually tens, and 300 messages is
# still one table read on the host.
LIST_LIMIT = 300
LIST_CACHE_S = 60.0
THREAD_LIMIT = 30
# The thread as the model sees it: generous for the newest message, which is what an answer is
# about, and enough of the earlier ones to follow the argument. Under the 16k instructions cap.
NEWEST_CHARS = 6_000
EARLIER_CHARS = 1_500
INSTRUCTIONS_CAP = 15_500

_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg|отг|пр)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)


class MailDeskError(RuntimeError):
    """Something the caller should see as it is: no host, the host refused, Outlook is down."""


def clean_subject(subject: str) -> str:
    """'RE: FW: Budget' -> 'Budget': the thread's name, whoever answered last."""
    return _PREFIX.sub("", subject or "").strip() or "(no subject)"


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _sender(item: dict[str, Any]) -> str:
    frm = item.get("from")
    if isinstance(frm, dict):
        return str(frm.get("name") or frm.get("address") or "")
    return str(item.get("sender") or frm or "")


def _address(item: dict[str, Any]) -> str:
    frm = item.get("from")
    if isinstance(frm, dict) and frm.get("address"):
        return str(frm["address"])
    return str(item.get("sender_address") or "")


def group_threads(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unread messages -> one entry per conversation, newest thread first.

    A message without a conversation id is a thread of its own. The newest message names the
    thread (its preview, its sender, its id - the one a reply hangs on)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("entry_id"):
            continue
        key = str(item.get("conversation_id") or "") or f"single:{item['entry_id']}"
        groups.setdefault(key, []).append(item)
    threads: list[dict[str, Any]] = []
    for key, members in groups.items():
        members.sort(key=lambda i: str(i.get("received") or ""), reverse=True)
        newest = members[0]
        senders: list[str] = []
        for m in members:
            name = _sender(m)
            if name and name not in senders:
                senders.append(name)
        threads.append(
            {
                "key": key,
                "conversation_id": None if key.startswith("single:") else key,
                "subject": clean_subject(str(newest.get("subject") or "")),
                "senders": senders,
                "latest": {
                    "entry_id": str(newest["entry_id"]),
                    "sender": _sender(newest),
                    "sender_address": _address(newest),
                    "received": newest.get("received"),
                    "preview": str(newest.get("preview") or "")[:300],
                },
                "unread_count": len(members),
                "entry_ids": [str(m["entry_id"]) for m in members],
                "flagged": any(bool(m.get("flagged")) for m in members),
                "has_attachments": any(bool(m.get("has_attachments")) for m in members),
            }
        )
    threads.sort(key=lambda t: str(t["latest"]["received"] or ""), reverse=True)
    return threads


def thread_instructions(host: str, account: str, thread: dict[str, Any]) -> str:
    """The conversation instructions that put a mail thread in front of the model."""
    items: list[dict[str, Any]] = thread.get("items") or []
    newest = items[-1] if items else {}
    subject = clean_subject(str(newest.get("subject") or thread.get("subject") or ""))
    people: list[str] = []
    for it in items:
        who = f"{_sender(it)} <{_address(it)}>" if _address(it) else _sender(it)
        if who and who not in people:
            people.append(who)
    lines = [
        "This chat is about ONE mail thread, shown below. Arsen opened it from the mail desk and "
        "reads it beside this chat. Work from the text below; read the mailbox again only for "
        "something it does not show (an attachment, an older message, a full body cut here).",
        f"Mailbox: account `{account or 'default'}` on host `{host}` "
        f"(tools {host}.outlook_thread / outlook_read / outlook_send / outlook_mark_read / outlook_move / outlook_flag).",
        f"To answer in the thread: {host}.outlook_send with reply_to_entry_id=`{newest.get('entry_id', '')}` "
        "and the same account - it keeps the RE: subject and quotes the original. Save it as a draft "
        "(draft=true) unless Arsen says to send it. Write the answer in the language of the thread.",
        f"Subject: {subject}",
        f"People: {', '.join(people) or 'unknown'}",
        f"Messages ({len(items)} shown, oldest first; ★ = Arsen wrote it):",
    ]
    body = "\n".join(lines)
    blocks: list[str] = []
    for n, it in enumerate(items):
        last = n == len(items) - 1
        text = str(it.get("body") or it.get("preview") or "").strip()
        cap = NEWEST_CHARS if last else EARLIER_CHARS
        if len(text) > cap:
            text = text[:cap].rstrip() + " […]"
        mark = "★ " if it.get("mine") else ""
        head = f"--- {mark}{_sender(it)} · {it.get('received') or ''}"
        if last:
            head += f" · entry_id {it.get('entry_id', '')}"
        blocks.append(f"{head}\n{text}")
    # Oldest messages give way first when the whole does not fit: the newest is the one answered.
    while blocks and len(body) + sum(len(b) + 2 for b in blocks) > INSTRUCTIONS_CAP and len(blocks) > 1:
        blocks.pop(0)
        body_note = "(older messages omitted - outlook_thread has them)"
        if body_note not in body:
            body += f"\n{body_note}"
    return (body + "\n\n" + "\n\n".join(blocks))[:INSTRUCTIONS_CAP]


@dataclass
class _Cached:
    at: float
    data: dict[str, Any]


class MailDesk:
    def __init__(self, core: Core) -> None:
        self.core = core
        self._cache: dict[str, _Cached] = {}
        self._session_lock = asyncio.Lock()

    # -- where the mail is ---------------------------------------------------------------------

    def host(self) -> str:
        host = (self.core.settings.triage.host or "").strip()
        if not host:
            raise MailDeskError("no mail host: set Triage → Host (the jarvis-host that owns Outlook) in Settings")
        if host == "mail":
            raise MailDeskError("the mail desk reads Outlook threads; the IMAP backend keeps no conversations")
        return host

    def accounts(self) -> list[str]:
        return [a for a in self.core.settings.triage.accounts if a.strip()]

    def _account(self, account: str | None) -> str:
        if account:
            return account
        accounts = self.accounts()
        return accounts[0] if accounts else ""

    async def _call(self, op: str, args: dict[str, Any], *, timeout_s: float = 90) -> Any:
        host = self.host()
        res = await self.core.registry.call(
            triage_tool(host, op),
            args,
            cancel=asyncio.Event(),
            idempotency_key=f"maildesk:{op}:{time.time_ns()}",
            timeout_s=timeout_s,
        )
        if res.kind.value == "error":
            raise MailDeskError(res.text[:400])
        data = _json(res.text)
        if data is None:
            raise MailDeskError(f"{op}: the host answered something that is not JSON")
        return data

    # -- the desk ------------------------------------------------------------------------------

    async def threads(self, account: str | None = None, *, refresh: bool = False) -> dict[str, Any]:
        acct = self._account(account)
        cached = self._cache.get(acct)
        if cached and not refresh and time.monotonic() - cached.at < LIST_CACHE_S:
            return cached.data
        args: dict[str, Any] = {
            "account": acct,
            "folder": "inbox",
            "limit": LIST_LIMIT,
            "preview_chars": 300,
            "unread_only": True,
        }
        try:
            data = await self._call("list", args, timeout_s=120)
        except MailDeskError as exc:
            # A host older than the unread filter refuses the argument: read the page and filter here.
            if "unread_only" not in str(exc):
                raise
            args.pop("unread_only")
            data = await self._call("list", args, timeout_s=120)
        items = [i for i in (data.get("items") or []) if isinstance(i, dict) and i.get("unread")]
        result = {
            "host": self.host(),
            "account": acct,
            "accounts": self.accounts(),
            "threads": group_threads(items),
            "unread": len(items),
            "capped": bool(data.get("cursor")),
            "fetched_at": time.time(),
        }
        self._cache[acct] = _Cached(time.monotonic(), result)
        return result

    async def thread(self, entry_id: str, account: str | None = None) -> dict[str, Any]:
        acct = self._account(account)
        data = await self._call(
            "thread", {"entry_id": entry_id, "account": acct, "limit": THREAD_LIMIT, "preview_chars": 4000}
        )
        items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
        if not data.get("conversation") or not items:
            # No conversations in this store (or an empty answer): the message alone is the thread.
            one = await self._call("read", {"entry_id": entry_id, "account": acct, "max_chars": 20_000})
            one = {**one, "preview": one.get("body", "")}
            items = [one]
        newest = items[-1]
        return {
            "host": self.host(),
            "account": acct,
            "subject": clean_subject(str(newest.get("subject") or "")),
            "conversation_id": data.get("conversation_id") or newest.get("conversation_id"),
            "total": data.get("total") or len(items),
            "items": items,
        }

    async def message(self, entry_id: str, account: str | None = None) -> dict[str, Any]:
        return await self._call("read", {"entry_id": entry_id, "account": self._account(account), "max_chars": 20_000})

    async def mark_read(self, entry_ids: list[str], *, read: bool = True, account: str | None = None) -> dict[str, Any]:
        acct = self._account(account)
        ids = [e for e in entry_ids if e][:200]
        if not ids:
            raise MailDeskError("no messages given")
        result = await self._call("mark_read", {"entry_ids": ids, "read": read, "account": acct})
        self._cache.pop(acct, None)  # the list changed; the next look reads it again
        return result

    async def session(self, entry_id: str, account: str | None = None) -> Conversation:
        """The chat for this thread: found by the thread's conversation id, created the first
        time, and its instructions refreshed with the thread as it is now."""
        acct = self._account(account)
        thread = await self.thread(entry_id, acct)
        key = f"{FOLDER_KEY_PREFIX}{acct}:{thread['conversation_id'] or entry_id}"
        instructions = thread_instructions(self.host(), acct, thread)
        store = self.core.store
        async with self._session_lock:  # two quick taps must not make two chats
            existing = next(
                (c for c in await store.list_conversations(include_archived=True) if c.folder_key == key), None
            )
            if existing is None:
                conv = await store.create_conversation(
                    title=f"✉ {thread['subject']}"[:120], folder_key=key, folder_label=FOLDER_LABEL
                )
                conv = await store.update_conversation(conv.id, instructions=instructions, title_auto=False) or conv
            else:
                fields: dict[str, Any] = {}
                if existing.instructions != instructions:
                    fields["instructions"] = instructions
                if existing.archived:
                    fields["archived"] = False
                conv = await store.update_conversation(existing.id, **fields) if fields else existing
                conv = conv or existing
        self.core.bus.publish(ConversationUpdated(conversation=conv))
        return conv
