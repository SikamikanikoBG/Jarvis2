"""The core's own IMAP/SMTP mail backend (features/mail.py) against a fake Gmail-like IMAP server."""

from __future__ import annotations

import imaplib
import json
from email.message import EmailMessage
from typing import Any

import pytest

from jarvis_core.features.mail import (
    Mailbox,
    MailError,
    MailTools,
    decode_id,
    imap_utf7_decode,
    imap_utf7_encode,
    triage_tool,
)
from jarvis_proto import MailAccount, Settings, TriageSettings

GMAIL = MailAccount(address="arsen.apostolov@gmail.com", app_password="abcd efgh ijkl mnop")


def _msg(subject: str, sender: str, body: str, *, html: bool = False) -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "arsen.apostolov@gmail.com"
    m["Subject"] = subject
    m["Date"] = "Mon, 28 Sep 2026 06:00:00 +0300"
    if html:
        m.set_content(body, subtype="html")  # HTML only, as many newsletters and Outlook mails are
    else:
        m.set_content(body)
    return m.as_bytes()


class FakeImap:
    """Just enough of imaplib.IMAP4 for Mailbox, behaving like Gmail (special-use folders, MOVE)."""

    def __init__(self) -> None:
        self.capabilities = ("IMAP4REV1", "MOVE", "UIDPLUS")
        self.folders: dict[str, dict[str, Any]] = {}
        for path, flags in [
            ("INBOX", []),
            ("[Gmail]/Trash", ["\\Trash"]),
            ("[Gmail]/Drafts", ["\\Drafts"]),
            ("[Gmail]/Sent Mail", ["\\Sent"]),
            ("[Gmail]/Spam", ["\\Junk"]),
            ("Лични", []),
        ]:
            self.folders[path] = {"flags": flags, "msgs": {}, "next": 1}
        self.current: str | None = None
        self.literal: bytes | None = None
        self.logged_out = False

    def add(self, folder: str, raw: bytes, flags: tuple[str, ...] = ()) -> str:
        f = self.folders[folder]
        uid = str(f["next"])
        f["next"] += 1
        f["msgs"][uid] = (raw, list(flags))
        return uid

    @staticmethod
    def _name(quoted: str) -> str:
        return imap_utf7_decode(quoted.strip('"').replace('\\"', '"'))

    def list(self) -> tuple[str, list[bytes]]:
        lines = []
        for path, f in self.folders.items():
            flags = " ".join(["\\HasNoChildren", *f["flags"]])
            lines.append(f'({flags}) "/" "{imap_utf7_encode(path)}"'.encode())
        return "OK", lines

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        name = self._name(mailbox)
        if name not in self.folders:
            return "NO", [b"no such mailbox"]
        self.current = name
        return "OK", [str(len(self.folders[name]["msgs"])).encode()]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        return code, [b"7"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        assert self.current is not None
        box = self.folders[self.current]["msgs"]
        cmd = command.upper()
        if cmd == "SEARCH":
            return "OK", [" ".join(sorted(box, key=int)).encode()]
        if cmd == "FETCH":
            uid = args[0]
            if uid not in box:
                return "OK", [None]
            raw, flags = box[uid]
            meta = f'{uid} (UID {uid} FLAGS ({" ".join(flags)}) INTERNALDATE "28-Sep-2026 06:00:00 +0300" BODY[] {{{len(raw)}}}'
            return "OK", [(meta.encode(), raw), b")"]
        if cmd == "MOVE":
            uid, target = args
            self.folders[self._name(target)]["msgs"][uid] = box.pop(uid)
            return "OK", [b"moved"]
        if cmd == "STORE":
            uid, op, flags = args
            raw, current = box[uid]
            f = flags.strip("()")
            box[uid] = (raw, [*current, f] if op.startswith("+") else [x for x in current if x != f])
            return "OK", [b"stored"]
        raise AssertionError(f"unexpected UID {command}")

    def create(self, mailbox: str) -> tuple[str, list[bytes]]:
        self.folders.setdefault(self._name(mailbox), {"flags": [], "msgs": {}, "next": 1})
        return "OK", [b"created"]

    def append(self, mailbox: str, flags: str, date: Any, raw: bytes) -> tuple[str, list[bytes]]:
        self.add(self._name(mailbox), raw, tuple(flags.strip("()").split()))
        return "OK", [b"appended"]

    def expunge(self) -> tuple[str, list[bytes]]:
        return "OK", []

    def logout(self) -> None:
        self.logged_out = True


@pytest.fixture
def server() -> FakeImap:
    s = FakeImap()
    s.add("INBOX", _msg("Old one", "Ivan <ivan@example.com>", "first"), ("\\Seen",))
    s.add(
        "INBOX",
        _msg("Среща утре", "Petia Dimitrova <petia@postbank.bg>", "<p>Здравей,<br>утре в <b>10</b>?</p>", html=True),
    )
    return s


def box(server: FakeImap) -> Mailbox:
    return Mailbox(GMAIL, connect=lambda _a: server)


def test_utf7_folder_names_round_trip():
    for name in ["Лични/Семейство", "A & B", "INBOX", "Réponses"]:
        assert imap_utf7_decode(imap_utf7_encode(name)) == name
    assert imap_utf7_encode("A & B") == "A &- B"


def test_list_is_newest_first_with_the_fields_triage_reads(server: FakeImap):
    res = box(server).list_items("Inbox", limit=10, preview_chars=200)
    first, second = res["items"]
    assert first["subject"] == "Среща утре" and second["subject"] == "Old one"
    assert first["from"] == {"name": "Petia Dimitrova", "address": "petia@postbank.bg"}
    assert first["sender_address"] == "petia@postbank.bg" and first["unread"] and not second["unread"]
    assert "утре в 10?" in first["preview"]  # HTML converted to text
    assert first["received"].startswith("2026-09-28T06:00:00")
    assert decode_id(first["entry_id"]) == ("INBOX", "7", "2")
    assert server.logged_out


def test_read_returns_the_body(server: FakeImap):
    item = box(server).list_items()["items"][0]
    msg = box(server).read(item["entry_id"])
    assert "Здравей" in msg["body"] and msg["from"]["address"] == "petia@postbank.bg"


def test_move_creates_the_folder_and_roles_map_to_gmail_folders(server: FakeImap):
    items = box(server).list_items()["items"]
    res = box(server).move(items[0]["entry_id"], "Leadership/Bosses")
    assert res == {"moved": True, "folder": "Leadership/Bosses"}
    assert len(server.folders["Leadership/Bosses"]["msgs"]) == 1 and len(server.folders["INBOX"]["msgs"]) == 1
    res = box(server).move(items[1]["entry_id"], "deleted")
    assert res["folder"] == "[Gmail]/Trash" and not server.folders["INBOX"]["msgs"]


def test_cyrillic_folder_is_found_by_its_real_name(server: FakeImap):
    item = box(server).list_items()["items"][0]
    assert box(server).move(item["entry_id"], "Лични")["folder"] == "Лични"


def test_a_draft_lands_in_gmail_drafts(server: FakeImap):
    res = box(server).send("vera@example.com", "News digest", "<b>hi</b>", html_body=True, draft=True)
    assert res == {"draft": True, "folder": "[Gmail]/Drafts"}
    raw, flags = next(iter(server.folders["[Gmail]/Drafts"]["msgs"].values()))
    assert b"News digest" in raw and "\\Draft" in flags


def test_a_rejected_login_says_what_to_fix():
    def refuse(_a: MailAccount) -> Any:
        raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    with pytest.raises(MailError, match="app password"):
        box_ = Mailbox(GMAIL, connect=refuse)
        box_.check()


async def test_the_tools_speak_json_and_pick_the_account(server: FakeImap):
    settings = Settings(mail_accounts=[GMAIL])
    tools = MailTools(lambda: settings, connect=lambda _a: server)
    res = await tools.call(
        "mail.list",
        {"account": "Arsen.Apostolov@gmail.com", "limit": 5},
        cancel=None,
        idempotency_key="k",
        timeout_s=10,
    )  # type: ignore[arg-type]
    assert json.loads(res.text)["items"][0]["subject"] == "Среща утре"
    missing = await tools.call(
        "mail.list", {"account": "someone@else.com"}, cancel=None, idempotency_key="k", timeout_s=10
    )  # type: ignore[arg-type]
    assert missing.kind.value == "error" and "not configured" in missing.text


def test_triage_routes_imap_accounts_to_the_mail_backend():
    tri = TriageSettings(host="jarvisvm")
    assert tri.host_for("arsen.apostolov@gmail.com", [GMAIL]) == "mail"
    assert tri.host_for("AApostolov@postbank.bg", [GMAIL]) == "jarvisvm"
    assert tri.host_for("arsen.apostolov@gmail.com", [GMAIL.model_copy(update={"enabled": False})]) == "jarvisvm"
    assert triage_tool("mail", "list") == "mail.list"
    assert triage_tool("jarvisvm", "list") == "jarvisvm.outlook_list"
