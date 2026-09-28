"""Mailboxes Jarvis reaches directly over IMAP/SMTP - no Outlook, no Windows host.

Until 2026-09-28 every mailbox went through classic Outlook on a Windows host (COM). When that
Outlook sat on an Office sign-in dialog, triage, drafts and mail tools were down for a whole night,
Gmail included - although Gmail needs no Outlook at all. An account listed in
``Settings.mail_accounts`` (Gmail with an app password, or any IMAP/SMTP server) is served here,
from the core itself.

The tools answer in the same shapes as the host's ``outlook_*`` tools (``items`` with
``entry_id``/``subject``/``from``/``preview``/``received``; ``body`` on read), so triage and the
model treat both backends alike. One short-lived IMAP connection per call: nothing to go stale
between triage passes, and a failed login is reported on the call that needed it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import email
import email.header
import email.policy
import email.utils
import html
import imaplib
import json
import logging
import re
import smtplib
import ssl
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage, Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from pydantic import BaseModel, Field

from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import MailAccount, Settings, ToolResult

log = logging.getLogger(__name__)

PROVIDER = "mail"
FETCH_BYTES = 65_536  # enough of each message for headers + a preview; the full body on read
CONNECT_TIMEOUT_S = 30

# Folder roles triage and the model use by name, mapped to IMAP special-use flags (RFC 6154).
_ROLE_FLAGS = {
    "deleted": "\\Trash",
    "trash": "\\Trash",
    "junk": "\\Junk",
    "spam": "\\Junk",
    "sent": "\\Sent",
    "drafts": "\\Drafts",
    "archive": "\\All",
}


class MailError(RuntimeError):
    pass


# --- IMAP modified UTF-7 (RFC 3501 5.1.3): Cyrillic folder names ------------------------------


def imap_utf7_encode(name: str) -> str:
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(name: str) -> str:
    def repl(m: re.Match[str]) -> str:
        chunk = m.group(1)
        if not chunk:
            return "&"
        b64 = chunk.replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        return base64.b64decode(b64).decode("utf-16-be")

    return re.sub(r"&([A-Za-z0-9+,]*)-", repl, name)


def _quote(name: str) -> str:
    return '"' + imap_utf7_encode(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


# --- message helpers -------------------------------------------------------------------------


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "table"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    p = _Text()
    p.feed(markup)
    text = html.unescape("".join(p.parts))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\xa0]+", " ", text)).strip()


def _body_text(msg: Message) -> str:
    plain = html_part = None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html_part is None:
            html_part = text
    if plain and plain.strip():
        return plain.strip()
    return html_to_text(html_part) if html_part else ""


def _addr(value: str | None) -> dict[str, str]:
    name, address = parseaddr(value or "")
    return {"name": str(email.header.make_header(email.header.decode_header(name))) if name else "", "address": address}


def _addrs(value: str | None) -> str:
    return ", ".join(a for _, a in getaddresses([value or ""]) if a)


def _header(msg: Message, key: str) -> str:
    raw = msg.get(key)
    if raw is None:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(str(raw))))
    except Exception:
        return str(raw)


def _received(msg: Message, internal: str | None) -> str:
    try:
        if internal:
            return datetime.strptime(internal, "%d-%b-%Y %H:%M:%S %z").isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(msg.get("Date", "")).isoformat()
    except Exception:
        return ""


def encode_id(folder: str, uidvalidity: str, uid: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([folder, uidvalidity, uid]).encode()).decode().rstrip("=")


def decode_id(entry_id: str) -> tuple[str, str, str]:
    try:
        raw = base64.urlsafe_b64decode(entry_id + "=" * (-len(entry_id) % 4))
        folder, validity, uid = json.loads(raw)
        return str(folder), str(validity), str(uid)
    except Exception as exc:
        raise MailError(f"not a mail entry_id: {entry_id!r}") from exc


# --- one mailbox -----------------------------------------------------------------------------


class Mailbox:
    """Blocking IMAP/SMTP operations on one account; call from a worker thread."""

    def __init__(self, account: MailAccount, connect: Callable[[MailAccount], Any] | None = None) -> None:
        self.account = account
        self._connect = connect or _imap_connect

    @contextmanager
    def session(self) -> Generator[Any]:
        try:
            imap = self._connect(self.account)
        except imaplib.IMAP4.error as exc:
            raise MailError(
                f"IMAP login to {self.account.imap_host} failed for {self.account.address}: {exc}. "
                "For Gmail this needs an app password (Settings -> Mail accounts)."
            ) from exc
        except OSError as exc:
            raise MailError(f"cannot reach {self.account.imap_host}:{self.account.imap_port}: {exc}") from exc
        try:
            yield imap
        finally:
            with contextlib.suppress(Exception):
                imap.logout()

    # -- folders --

    def _folders(self, imap: Any) -> list[dict[str, Any]]:
        typ, data = imap.list()
        if typ != "OK":
            raise MailError(f"LIST failed: {data}")
        out: list[dict[str, Any]] = []
        for line in data or []:
            if not line:
                continue
            text = line.decode() if isinstance(line, bytes) else str(line)
            m = re.match(r'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$', text)
            if not m:
                continue
            name = m.group("name").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            delim = m.group("delim").strip('"') if m.group("delim") != "NIL" else "/"
            flags = m.group("flags").split()
            out.append({"path": imap_utf7_decode(name), "delimiter": delim or "/", "flags": flags})
        return out

    def _resolve(self, imap: Any, folder: str) -> str:
        """A folder as triage/the model name it ('Inbox', 'deleted', 'Leadership/Bosses') -> server path."""
        f = (folder or "Inbox").strip()
        if f.lower() == "inbox":
            return "INBOX"
        flag = _ROLE_FLAGS.get(f.lower())
        folders = self._folders(imap)
        if flag:
            for entry in folders:
                if flag in entry["flags"]:
                    return entry["path"]
        delim = folders[0]["delimiter"] if folders else "/"
        wanted = f.replace("/", delim)
        for entry in folders:
            if entry["path"].lower() == wanted.lower():
                return entry["path"]
        return wanted

    def _select(self, imap: Any, path: str, *, readonly: bool) -> str:
        typ, data = imap.select(_quote(path), readonly=readonly)
        if typ != "OK":
            raise MailError(f"folder {path!r} not found ({data})")
        typ, resp = imap.response("UIDVALIDITY")
        return (resp[0].decode() if resp and isinstance(resp[0], bytes) else str(resp[0])) if resp and resp[0] else "0"

    # -- operations --

    def accounts(self) -> dict[str, Any]:
        with self.session() as imap:
            roles = {
                role: e["path"]
                for e in self._folders(imap)
                for role, flag in _ROLE_FLAGS.items()
                if flag in e["flags"] and role in ("deleted", "junk", "sent", "drafts", "archive")
            }
        return {
            "name": self.account.address,
            "type": "imap",
            "smtp": self.account.address,
            "folders": {"inbox": "INBOX", **roles},
        }

    def folders(self) -> list[dict[str, Any]]:
        with self.session() as imap:
            return [{"path": e["path"], "flags": e["flags"]} for e in self._folders(imap)]

    def list_items(self, folder: str = "Inbox", limit: int = 25, preview_chars: int = 400) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        with self.session() as imap:
            path = self._resolve(imap, folder)
            validity = self._select(imap, path, readonly=True)
            typ, data = imap.uid("SEARCH", None, "ALL")
            uids = (data[0] or b"").split() if typ == "OK" and data else []
            chosen = list(reversed(uids[-limit:]))
            items = [self._summary(imap, path, validity, uid.decode(), preview_chars) for uid in chosen]
        return {"items": [i for i in items if i], "cursor": None, "total": len(uids)}

    def _fetch(self, imap: Any, uid: str, *, partial: bool) -> tuple[Message, list[str], str | None]:
        part = f"BODY.PEEK[]<0.{FETCH_BYTES}>" if partial else "BODY.PEEK[]"
        typ, data = imap.uid("FETCH", uid, f"(UID FLAGS INTERNALDATE {part})")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise MailError(f"message {uid} not found")
        meta = data[0][0].decode(errors="replace") if isinstance(data[0][0], bytes) else str(data[0][0])
        flags = re.findall(r"\\\w+", (re.search(r"FLAGS \(([^)]*)\)", meta) or [None, ""])[1] or "")
        internal = (re.search(r'INTERNALDATE "([^"]+)"', meta) or [None, None])[1]
        msg = email.message_from_bytes(data[0][1], policy=email.policy.compat32)
        return msg, flags, internal

    def _summary(self, imap: Any, path: str, validity: str, uid: str, preview_chars: int) -> dict[str, Any] | None:
        try:
            msg, flags, internal = self._fetch(imap, uid, partial=True)
        except MailError:
            return None
        frm = _addr(_header(msg, "From"))
        preview = " ".join(_body_text(msg).split())[: max(0, min(int(preview_chars), 4000))]
        return {
            "entry_id": encode_id(path, validity, uid),
            "subject": _header(msg, "Subject"),
            "from": frm,
            "sender": frm["name"] or frm["address"],
            "sender_address": frm["address"],
            "to": _addrs(_header(msg, "To")),
            "cc": _addrs(_header(msg, "Cc")),
            "received": _received(msg, internal),
            "unread": "\\Seen" not in flags,
            "flagged": "\\Flagged" in flags,
            "preview": preview,
        }

    def read(self, entry_id: str, max_chars: int = 20_000) -> dict[str, Any]:
        path, validity, uid = decode_id(entry_id)
        with self.session() as imap:
            if self._select(imap, path, readonly=True) != validity:
                raise MailError("the folder was rebuilt since this id was issued; list it again")
            msg, flags, internal = self._fetch(imap, uid, partial=False)
        attachments = [
            {"name": p.get_filename(), "size": len(p.get_payload(decode=True) or b"")}  # pyright: ignore[reportArgumentType]
            for p in msg.walk()
            if p.get_filename()
        ]
        return {
            "entry_id": entry_id,
            "folder": path,
            "subject": _header(msg, "Subject"),
            "from": _addr(_header(msg, "From")),
            "to": _addrs(_header(msg, "To")),
            "cc": _addrs(_header(msg, "Cc")),
            "received": _received(msg, internal),
            "body": _body_text(msg)[: max(200, min(int(max_chars), 20_000))],
            "attachments": attachments,
            "flagged": "\\Flagged" in flags,
        }

    def search(self, query: str, days_back: int = 30, folder: str = "Inbox", limit: int = 25) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(days=max(1, int(days_back)))).strftime("%d-%b-%Y")
        with self.session() as imap:
            path = self._resolve(imap, folder)
            validity = self._select(imap, path, readonly=True)
            q = query.strip()
            if q and not q.isascii():
                imap.literal = q.encode()
                typ, data = imap.uid("SEARCH", "CHARSET", "UTF-8", "SINCE", since, "TEXT")
            elif q:
                safe = '"' + q.replace("\\", "\\\\").replace('"', '\\"') + '"'
                typ, data = imap.uid("SEARCH", None, "SINCE", since, "OR", "SUBJECT", safe, "FROM", safe)
            else:
                typ, data = imap.uid("SEARCH", None, "SINCE", since)
            uids = (data[0] or b"").split() if typ == "OK" and data else []
            chosen = list(reversed(uids[-max(1, min(int(limit), 100)) :]))
            items = [self._summary(imap, path, validity, u.decode(), 300) for u in chosen]
        return {"items": [i for i in items if i], "total": len(uids)}

    def folder_create(self, path: str) -> dict[str, Any]:
        with self.session() as imap:
            existing = self._folders(imap)
            delim = existing[0]["delimiter"] if existing else "/"
            target = path.strip().strip("/").replace("/", delim)
            if any(e["path"].lower() == target.lower() for e in existing):
                return {"path": target, "created": False}
            typ, data = imap.create(_quote(target))
            if typ != "OK" and b"ALREADYEXISTS" not in b"".join(d for d in data or [] if isinstance(d, bytes)):
                raise MailError(f"CREATE {target!r} failed: {data}")
            return {"path": target, "created": True}

    def move(self, entry_id: str, folder: str, create: bool = True) -> dict[str, Any]:
        src, validity, uid = decode_id(entry_id)
        if create and folder.lower() not in _ROLE_FLAGS and folder.lower() != "inbox":
            self.folder_create(folder)
        with self.session() as imap:
            target = self._resolve(imap, folder)
            if self._select(imap, src, readonly=False) != validity:
                raise MailError("the folder was rebuilt since this id was issued; list it again")
            caps = {
                c.decode().upper() if isinstance(c, bytes) else str(c).upper()
                for c in getattr(imap, "capabilities", ())
            }
            if "MOVE" in caps:
                typ, data = imap.uid("MOVE", uid, _quote(target))
            else:
                typ, data = imap.uid("COPY", uid, _quote(target))
                if typ == "OK":
                    imap.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")
                    imap.expunge()
            if typ != "OK":
                raise MailError(f"move to {target!r} failed: {data}")
        return {"moved": True, "folder": target}

    def flag(self, entry_id: str, flagged: bool = True) -> dict[str, Any]:
        path, validity, uid = decode_id(entry_id)
        with self.session() as imap:
            if self._select(imap, path, readonly=False) != validity:
                raise MailError("the folder was rebuilt since this id was issued; list it again")
            typ, data = imap.uid("STORE", uid, "+FLAGS" if flagged else "-FLAGS", "(\\Flagged)")
            if typ != "OK":
                raise MailError(f"flag failed: {data}")
        return {"entry_id": entry_id, "flagged": flagged}

    def send(
        self, to: str, subject: str, body: str, *, cc: str = "", html_body: bool = False, draft: bool = False
    ) -> dict[str, Any]:
        msg = EmailMessage()
        msg["From"] = self.account.address
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        if html_body:
            msg.set_content(html_to_text(body))
            msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body)
        if draft:
            with self.session() as imap:
                drafts = self._resolve(imap, "drafts")
                typ, data = imap.append(
                    _quote(drafts), "(\\Draft)", imaplib.Time2Internaldate(datetime.now(UTC)), msg.as_bytes()
                )
                if typ != "OK":
                    raise MailError(f"saving the draft failed: {data}")
            return {"draft": True, "folder": drafts}
        acct = self.account
        with smtplib.SMTP_SSL(
            acct.smtp_host, acct.smtp_port, timeout=CONNECT_TIMEOUT_S, context=ssl.create_default_context()
        ) as s:
            s.login(acct.username or acct.address, acct.app_password)
            s.send_message(msg)
        return {"sent": True, "to": to}

    def check(self) -> dict[str, Any]:
        with self.session() as imap:
            path = "INBOX"
            self._select(imap, path, readonly=True)
            typ, data = imap.uid("SEARCH", None, "ALL")
            count = len((data[0] or b"").split()) if typ == "OK" and data else 0
            folders = len(self._folders(imap))
        return {"ok": True, "inbox_messages": count, "folders": folders}


def _imap_connect(account: MailAccount) -> Any:
    imap = imaplib.IMAP4_SSL(
        account.imap_host, account.imap_port, ssl_context=ssl.create_default_context(), timeout=CONNECT_TIMEOUT_S
    )
    imap.login(account.username or account.address, account.app_password.replace(" ", ""))
    return imap


# --- tools -----------------------------------------------------------------------------------


class _Account(BaseModel):
    account: str = Field(default="", description="The mailbox address; '' = the first configured account.")


class _ListArgs(_Account):
    folder: str = Field(
        default="Inbox", description="'Inbox', a path like 'Leadership/Bosses', or a role: sent, drafts, deleted, junk."
    )
    limit: int = Field(default=25, ge=1, le=200)
    preview_chars: int = Field(default=400, ge=0, le=4000)
    cursor: str | None = Field(default=None, description="Unused: the newest `limit` messages are returned.")


class _ReadArgs(_Account):
    entry_id: str
    max_chars: int = Field(default=20_000, ge=200, le=20_000)


class _SearchArgs(_Account):
    query: str = Field(default="", description="Text in the subject or sender (any language).")
    days_back: int = Field(default=30, ge=1, le=3650)
    folder: str = "Inbox"
    limit: int = Field(default=25, ge=1, le=100)


class _MoveArgs(_Account):
    entry_id: str
    folder: str = Field(description="Target folder path ('Action Hub/To-Do') or a role (deleted, junk, archive).")
    create: bool = Field(default=True, description="Create the folder if it does not exist.")


class _FolderArgs(_Account):
    path: str = Field(description="Folder path, '/'-separated, e.g. 'Leadership/Bosses'.")


class _FlagArgs(_Account):
    entry_id: str
    flagged: bool = True


class _SendArgs(_Account):
    to: str = Field(description="Recipients, comma-separated.")
    subject: str
    body: str
    cc: str = ""
    html: bool = Field(default=False, description="body is HTML.")
    draft: bool = Field(default=False, description="Save to Drafts instead of sending.")


class MailTools(BuiltinProvider):
    """``mail.*``: the IMAP/SMTP accounts of ``Settings.mail_accounts``."""

    name = PROVIDER

    def __init__(self, settings: Callable[[], Settings], connect: Callable[[MailAccount], Any] | None = None) -> None:
        self._settings = settings
        self._connect = connect
        super().__init__()

    def mailbox(self, account: str = "") -> Mailbox:
        accounts = [a for a in self._settings().mail_accounts if a.enabled]
        if not accounts:
            raise MailError("no mail account configured (Settings -> Mail accounts)")
        if not account:
            return Mailbox(accounts[0], self._connect)
        for a in accounts:
            if a.address.strip().lower() == account.strip().lower():
                return Mailbox(a, self._connect)
        raise MailError(f"account {account!r} is not configured; configured: {[a.address for a in accounts]}")

    async def _run(self, account: str, op: Callable[[Mailbox], Any]) -> ToolResult:
        try:
            box = self.mailbox(account)
            result = await asyncio.to_thread(op, box)
        except MailError as exc:
            return ToolResult.failure(str(exc))
        except (imaplib.IMAP4.error, smtplib.SMTPException, OSError) as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")
        return ToolResult.data(json.dumps(result, ensure_ascii=False, default=str))

    @tool(
        "mail.accounts",
        description="IMAP mailboxes Jarvis reaches directly (no Outlook): address and folder roles.",
        read_only=True,
        idempotent=True,
    )
    async def _accounts(self) -> ToolResult:
        out = []
        for a in self._settings().mail_accounts:
            if a.enabled:
                out.append({"name": a.address, "type": "imap", "imap": a.imap_host})
        return ToolResult.data(json.dumps({"accounts": out}))

    @tool(
        "mail.folders",
        description="Folder tree of an IMAP mailbox (paths and special-use flags).",
        args=_Account,
        read_only=True,
        idempotent=True,
    )
    async def _folders(self, account: str = "") -> ToolResult:
        return await self._run(account, lambda b: b.folders())

    @tool(
        "mail.list",
        description="Newest messages of a folder of an IMAP mailbox (Gmail etc.), newest first: entry_id, subject, from, to, cc, received, unread, flagged, preview.",
        args=_ListArgs,
        read_only=True,
    )
    async def _list(
        self,
        account: str = "",
        folder: str = "Inbox",
        limit: int = 25,
        preview_chars: int = 400,
        cursor: str | None = None,
    ) -> ToolResult:
        return await self._run(account, lambda b: b.list_items(folder, limit, preview_chars))

    @tool(
        "mail.read",
        description="One message of an IMAP mailbox: headers, plain-text body (HTML converted), attachment names.",
        args=_ReadArgs,
        read_only=True,
        idempotent=True,
    )
    async def _read(self, entry_id: str, account: str = "", max_chars: int = 20_000) -> ToolResult:
        return await self._run(account, lambda b: b.read(entry_id, max_chars))

    @tool(
        "mail.search",
        description="Messages whose subject or sender contains `query` within the last `days_back` days of one folder.",
        args=_SearchArgs,
        read_only=True,
    )
    async def _search(
        self, account: str = "", query: str = "", days_back: int = 30, folder: str = "Inbox", limit: int = 25
    ) -> ToolResult:
        return await self._run(account, lambda b: b.search(query, days_back, folder, limit))

    @tool(
        "mail.move",
        description="Move a message to another folder (created if missing) - how triage files mail.",
        args=_MoveArgs,
    )
    async def _move(self, entry_id: str, folder: str, account: str = "", create: bool = True) -> ToolResult:
        return await self._run(account, lambda b: b.move(entry_id, folder, create))

    @tool(
        "mail.folder_create",
        description="Create a folder (and its parents) if it does not exist.",
        args=_FolderArgs,
        idempotent=True,
    )
    async def _folder_create(self, path: str, account: str = "") -> ToolResult:
        return await self._run(account, lambda b: b.folder_create(path))

    @tool("mail.flag", description="Set or clear the flag (star) on a message.", args=_FlagArgs, idempotent=True)
    async def _flag(self, entry_id: str, account: str = "", flagged: bool = True) -> ToolResult:
        return await self._run(account, lambda b: b.flag(entry_id, flagged))

    @tool(
        "mail.send",
        description="Send a message from an IMAP/SMTP account, or save it to Drafts with draft=true.",
        args=_SendArgs,
        destructive=True,
    )
    async def _send(
        self, to: str, subject: str, body: str, account: str = "", cc: str = "", html: bool = False, draft: bool = False
    ) -> ToolResult:
        return await self._run(account, lambda b: b.send(to, subject, body, cc=cc, html_body=html, draft=draft))


def triage_tool(host: str, op: str) -> str:
    """Triage's tool name for `op` on `host`: ``mail.list`` for IMAP accounts, ``<host>.outlook_list`` otherwise."""
    return f"{PROVIDER}.{op}" if host == PROVIDER else f"{host}.outlook_{op}"
