"""Outlook through COM — the ``outlook_*`` and ``calendar_*`` tools.

Ported from V1's proven paths with the V1 plumbing removed. The rules that cost real incidents
are structural here, not comments:

* every listing is a ``Folder.GetTable`` read (5-34x faster than walking ``Items``; it also
  accepts filters ``Items.Restrict`` rejects) and reads columns, never per-item properties;
* table EntryIDs are short-term, so a mutation always re-resolves through
  ``Session.GetItemFromID(entry_id, store_id)`` and reports the item's own (long-term) id;
* ``FlagStatus`` is never trusted alone — ``IsMarkedAsTask`` is read too and unflagging calls
  ``ClearTaskFlag()``; flagging uses ``MarkAsTask`` (modern stores reject ``FlagStatus = 2``);
* this is a Bulgarian Outlook: default folders come from ``Store.GetDefaultFolder(id)``, never
  from an English name;
* date bounds go through DASL in UTC (``urn:schemas:httpmail:datereceived``) so the filter does
  not depend on the Windows locale; the calendar's ``Restrict`` (needed for recurrences) uses
  the locale-formatted Jet form V1 validated on this machine.

``OutlookBackend`` is synchronous and runs *on the COM thread*; ``OutlookService`` marshals it
through :class:`jarvis_host.com.ComWorker` with a per-call timeout.
"""

from __future__ import annotations

import contextlib
import ctypes
import html
import logging
import re
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from jarvis_host.com import ComWorker

log = logging.getLogger(__name__)

# olDefaultFolders
FOLDER_DELETED = 3
FOLDER_OUTBOX = 4
FOLDER_SENT = 5
FOLDER_INBOX = 6
FOLDER_CALENDAR = 9
FOLDER_CONTACTS = 10
FOLDER_TASKS = 13
FOLDER_DRAFTS = 16
FOLDER_JUNK = 23

WELL_KNOWN_FOLDERS: dict[str, int] = {
    "inbox": FOLDER_INBOX,
    "sent": FOLDER_SENT,
    "sentitems": FOLDER_SENT,
    "drafts": FOLDER_DRAFTS,
    "deleted": FOLDER_DELETED,
    "deleteditems": FOLDER_DELETED,
    "trash": FOLDER_DELETED,
    "junk": FOLDER_JUNK,
    "spam": FOLDER_JUNK,
    "outbox": FOLDER_OUTBOX,
    "calendar": FOLDER_CALENDAR,
    "tasks": FOLDER_TASKS,
    "contacts": FOLDER_CONTACTS,
}
DEFAULT_ROLES: dict[int, str] = {
    FOLDER_INBOX: "inbox",
    FOLDER_SENT: "sent",
    FOLDER_DRAFTS: "drafts",
    FOLDER_DELETED: "deleted",
    FOLDER_JUNK: "junk",
    FOLDER_OUTBOX: "outbox",
    FOLDER_CALENDAR: "calendar",
    FOLDER_TASKS: "tasks",
    FOLDER_CONTACTS: "contacts",
}
# OlItemType (Folder.DefaultItemType)
ITEM_KINDS = {0: "mail", 1: "calendar", 2: "contacts", 3: "tasks", 4: "journal", 5: "notes"}
STORE_TYPES = {0: "exchange_primary", 1: "exchange", 2: "exchange_public", 3: "other", 4: "exchange_additional"}
BUSY_STATUS = {0: "free", 1: "tentative", 2: "busy", 3: "out_of_office", 4: "working_elsewhere"}
IMPORTANCE = {0: "low", 1: "normal", 2: "high"}

OL_MAIL_ITEM = 0
OL_APPOINTMENT_ITEM = 1
OL_APPOINTMENT_CLASS = 26
OL_MARK_NO_DATE = 0
OL_RECIPIENT_REQUIRED = 1
OL_MEETING = 1

PR_HASATTACH = "http://schemas.microsoft.com/mapi/proptag/0x0E1B000B"
PR_CONVERSATION_ID = "http://schemas.microsoft.com/mapi/proptag/0x30130102"
PR_SENDER_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"
DASL_RECEIVED = '"urn:schemas:httpmail:datereceived"'
DASL_SUBJECT = '"urn:schemas:httpmail:subject"'
DASL_FROMNAME = '"urn:schemas:httpmail:fromname"'
DASL_FROMEMAIL = '"urn:schemas:httpmail:fromemail"'

DASL_BODY = "urn:schemas:httpmail:textdescription"

TABLE_COLUMNS: tuple[str, ...] = (
    "EntryID",
    "Subject",
    "SenderName",
    "SenderEmailAddress",
    "ReceivedTime",
    "UnRead",
    "FlagStatus",
    "MessageClass",
    "Size",
    PR_HASATTACH,
    PR_CONVERSATION_ID,
    # A body preview straight from the table: verified on the real mailbox 2026-09-05. It is
    # what makes triage one fast call per batch instead of a COM read per message.
    DASL_BODY,
)

PREVIEW_CHARS = 400
MAX_LIST_LIMIT = 500
MAX_BODY_CHARS = 20_000
CALENDAR_SCAN_CAP = 2_000
CALENDAR_SCAN_SECONDS = 20.0

# MessageClass prefix → coarse kind (a table can return MessageClass but not Class).
_MESSAGE_CLASS_KINDS: tuple[tuple[str, str], ...] = (
    ("IPM.SCHEDULE.MEETING.RESP", "meeting_reply"),
    ("IPM.SCHEDULE.MEETING.CANCELED", "meeting_canceled"),
    ("IPM.SCHEDULE.MEETING.REQUEST", "meeting_request"),
    ("IPM.SCHEDULE.MEETING", "meeting"),
    ("IPM.APPOINTMENT", "appointment"),
    ("IPM.TASK", "task"),
    ("IPM.POST", "post"),
    ("REPORT.", "report"),
    ("IPM.NOTE", "mail"),
)

_TRANSIENT_HRESULTS = {-2147418111, -2147417846, -2147417847}  # RPC_E_CALL_REJECTED / RETRYLATER / SERVERCALL_REJECTED
_DISCONNECTED_HRESULTS = {
    -2147023174,
    -2147023170,
    -2147417848,
}  # RPC_S_SERVER_UNAVAILABLE / CALL_FAILED / RPC_E_DISCONNECTED
_TRANSIENT_TEXT = ("rejected by callee", "server is busy", "retrylater", "message filter")
_DISCONNECTED_TEXT = ("rpc server is unavailable", "object invoked has disconnected", "remote procedure call failed")


class OutlookError(RuntimeError):
    """A clear, user-facing failure. Never converted into a fake success."""


class NotFound(OutlookError):
    pass


# --- error helpers (duck-typed: no pywintypes import so this module loads anywhere) ------------


def _hresults(exc: BaseException) -> set[int]:
    out: set[int] = set()
    args = getattr(exc, "args", ()) or ()
    if args and isinstance(args[0], int):
        out.add(args[0])
    if len(args) >= 3 and isinstance(args[2], tuple) and len(args[2]) >= 6 and isinstance(args[2][5], int):
        out.add(args[2][5])
    return out


def describe_com_error(exc: BaseException) -> str:
    args = getattr(exc, "args", ()) or ()
    if len(args) >= 3 and isinstance(args[2], tuple) and len(args[2]) >= 3 and args[2][2]:
        return str(args[2][2]).strip()
    if len(args) >= 2 and isinstance(args[1], str) and args[1].strip():
        return args[1].strip()
    text = str(exc).strip()
    return text or type(exc).__name__


def is_transient(exc: BaseException) -> bool:
    if _hresults(exc) & _TRANSIENT_HRESULTS:
        return True
    text = str(exc).lower()
    return any(t in text for t in _TRANSIENT_TEXT)


def is_disconnected(exc: BaseException) -> bool:
    if _hresults(exc) & _DISCONNECTED_HRESULTS:
        return True
    text = str(exc).lower()
    return any(t in text for t in _DISCONNECTED_TEXT)


def retry_transient(fn: Callable[[], Any], *, attempts: int = 4, base_delay: float = 0.25) -> Any:
    """Retry a read while Outlook answers "busy". Only for idempotent reads — never a send."""
    delay = base_delay
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not is_transient(exc) or i == attempts - 1:
                raise
            log.warning(
                "outlook: transient COM error (%s); retry %d/%d in %.2fs",
                describe_com_error(exc),
                i + 1,
                attempts - 1,
                delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
    raise AssertionError("unreachable")


# --- small pure helpers -------------------------------------------------------------------------


def _prop(obj: Any, name: str, default: Any = None) -> Any:
    """A COM property read that never raises (pywin32 raises for properties an item lacks)."""
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    return default if value is None else value


def _iter_com(collection: Any) -> Iterator[Any]:
    """Iterate a 1-based COM collection (Stores, Folders, Accounts, Recipients, Attachments)."""
    if collection is None:
        return
    try:
        count = int(collection.Count)
    except Exception:
        return
    for i in range(1, count + 1):
        try:
            yield collection.Item(i)
        except Exception:
            continue


def _iter_items(items: Any) -> Iterator[Any]:
    """Iterate an Items collection with GetFirst/GetNext (the only safe way with IncludeRecurrences)."""
    try:
        item = items.GetFirst()
    except Exception:
        return
    while item is not None:
        yield item
        try:
            item = items.GetNext()
        except Exception:
            return


def _text(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    return "" if s == "None" else s


def _to_dt(value: Any) -> datetime | None:
    """Normalise a COM date (pywintypes datetime, aware UTC from tables, naive local from items)."""
    if value is None:
        return None
    try:
        dt = datetime(
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            tzinfo=getattr(value, "tzinfo", None),
        )
    except Exception:
        return None
    if dt.year < 1900:  # Outlook's "no date" is 4501-01-01; anything odd is treated as absent
        return None
    if dt.year > 4000:
        return None
    return dt.astimezone()  # naive → assumed local, aware → converted to local


def iso_local(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt is not None else None


def parse_when(text: str) -> datetime:
    """ISO-8601 (date or datetime, optional offset) → aware local datetime."""
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise OutlookError(f"not an ISO-8601 date/time: {text!r}") from exc
    return dt.astimezone()


def dasl_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def dasl_str(value: str) -> str:
    return value.replace("'", "''")


def jet_date(dt: datetime) -> str:
    """Date literal for a Jet ``Restrict`` in the Windows user locale (V1's ``_format_restrict_date``)."""
    if sys.platform != "win32":
        return dt.strftime("%m/%d/%Y %H:%M")
    kernel32 = ctypes.windll.kernel32  # pyright: ignore[reportAttributeAccessIssue]
    sep_buf = ctypes.create_unicode_buffer(10)
    kernel32.GetLocaleInfoW(0x0400, 0x1D, sep_buf, 10)  # LOCALE_USER_DEFAULT, LOCALE_SDATE
    sep = sep_buf.value or "/"
    order_buf = ctypes.create_unicode_buffer(10)
    kernel32.GetLocaleInfoW(0x0400, 0x23, order_buf, 10)  # LOCALE_IDATE: 0=MDY 1=DMY 2=YMD
    order = int(order_buf.value) if order_buf.value else 0
    if order == 1:
        date = f"{dt.day}{sep}{dt.month}{sep}{dt.year}"
    elif order == 2:
        date = f"{dt.year}{sep}{dt.month}{sep}{dt.day}"
    else:
        date = f"{dt.month}{sep}{dt.day}{sep}{dt.year}"
    return f"{date} {dt.strftime('%H:%M')}"


def kind_from_message_class(message_class: str) -> str:
    mc = message_class.strip().upper()
    if not mc:
        return "mail"
    for prefix, kind in _MESSAGE_CLASS_KINDS:
        if mc.startswith(prefix):
            return kind
    return "mail"


def looks_like_entry_id(value: str) -> bool:
    v = value.strip()
    return len(v) >= 40 and all(c in "0123456789ABCDEFabcdef" for c in v)


def semicolons(addresses: str) -> str:
    """Outlook wants ';' between recipients; models write ','."""
    return ";".join(a.strip() for a in re.split(r"[;,]", addresses) if a.strip())


def split_addresses(addresses: str) -> list[str]:
    return [a.strip() for a in re.split(r"[;,]", addresses or "") if a.strip()]


def encode_cursor(before: datetime, skip: Sequence[str]) -> str:
    return f"{before.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}|{','.join(skip)}"


def decode_cursor(cursor: str) -> tuple[datetime, set[str]]:
    stamp, _, ids = cursor.partition("|")
    try:
        before = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise OutlookError(f"bad cursor {cursor!r}") from exc
    return before, {i for i in ids.split(",") if i}


class _TextExtractor(HTMLParser):
    _BLOCK = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "table",
        "section",
        "article",
        "header",
        "footer",
    }
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def text_to_html(text: str) -> str:
    return (
        "<div style=\"font-family:Calibri,'Segoe UI',Arial,sans-serif;font-size:11pt;\">"
        + html.escape(text).replace("\n", "<br>\n")
        + "</div>"
    )


def merge_reply_html(existing: str, new_html: str) -> str:
    """Put our reply above the quoted thread Outlook's ``Reply()`` pre-filled (V1 rule)."""
    if not existing:
        return new_html
    m = re.search(r"<body[^>]*>(.*)</body>", new_html, re.DOTALL | re.IGNORECASE)
    inner = m.group(1) if m else new_html
    injected, n = re.subn(r"(<body[^>]*>)", lambda mm: mm.group(1) + inner, existing, count=1, flags=re.IGNORECASE)
    return injected if n else inner + existing


@dataclass
class Row:
    entry_id: str
    subject: str
    sender_name: str
    sender_addr: str
    received: datetime | None
    unread: bool
    flag_status: int
    message_class: str
    size: int | None
    has_attachments: bool | None
    conversation_id: str
    preview: str = ""

    def as_item(self, store_id: str) -> dict[str, Any]:
        item: dict[str, Any] = {
            "entry_id": self.entry_id,
            "store_id": store_id,
            "subject": self.subject,
            "from": {"name": self.sender_name, "address": self.sender_addr},
            # Flat aliases: consumers (triage, the model) should not have to dig into `from`.
            "sender": self.sender_name or self.sender_addr,
            "sender_address": self.sender_addr,
            "preview": self.preview,
            "received": iso_local(self.received),
            "unread": self.unread,
            "flagged": self.flag_status == 2,
            "kind": kind_from_message_class(self.message_class),
        }
        if self.has_attachments is not None:
            item["has_attachments"] = self.has_attachments
        if self.size is not None:
            item["size"] = self.size
        if self.conversation_id:
            item["conversation_id"] = self.conversation_id
        return item


def parse_row(row: Sequence[Any], columns: Sequence[str]) -> Row:
    values = dict(zip(columns, row, strict=False))
    size_raw = values.get("Size")
    try:
        size = int(size_raw) if size_raw is not None else None
    except (TypeError, ValueError):
        size = None
    flag_raw = values.get("FlagStatus")
    try:
        flag_status = int(flag_raw or 0)
    except (TypeError, ValueError):
        flag_status = 0
    attach_raw = values.get(PR_HASATTACH)
    has_attach = (
        None if attach_raw is None or attach_raw == "None" else str(attach_raw).strip().lower() in ("true", "1")
    )
    conv = values.get(PR_CONVERSATION_ID)
    if isinstance(conv, bytes | bytearray | memoryview):
        conv = bytes(conv).hex().upper()
    return Row(
        entry_id=_text(values.get("EntryID")),
        subject=_text(values.get("Subject")),
        sender_name=_text(values.get("SenderName")),
        sender_addr=_text(values.get("SenderEmailAddress")),
        received=_to_dt(values.get("ReceivedTime")),
        unread=str(values.get("UnRead", "")).strip().lower() in ("true", "1"),
        flag_status=flag_status,
        message_class=_text(values.get("MessageClass")),
        size=size,
        has_attachments=has_attach,
        conversation_id=_text(conv),
        preview=" ".join(_text(values.get(DASL_BODY)).split())[:PREVIEW_CHARS],
    )


def received_filter(lower: datetime | None, upper: datetime | None, extra: str | None = None) -> str | None:
    parts: list[str] = []
    if extra:
        parts.append(extra)
    if lower is not None:
        parts.append(f"{DASL_RECEIVED} >= '{dasl_utc(lower)}'")
    if upper is not None:
        parts.append(f"{DASL_RECEIVED} <= '{dasl_utc(upper)}'")
    return "@SQL=" + " AND ".join(parts) if parts else None


def outlook_dispatch() -> Any:
    """Dispatch a live ``Outlook.Application``. Must run on the COM thread."""
    if sys.platform != "win32":
        raise OutlookError("Outlook COM is only available on Windows")
    import win32com.client  # pyright: ignore[reportMissingModuleSource]

    return win32com.client.Dispatch("Outlook.Application")


# --- the backend (COM thread) --------------------------------------------------------------------


class OutlookBackend:
    """Synchronous Outlook operations. Every method must run on the COM thread."""

    def __init__(self, dispatch: Callable[[], Any] = outlook_dispatch, accounts_allow: Sequence[str] = ()) -> None:
        self._dispatch = dispatch
        self._allow = tuple(a.strip().lower() for a in accounts_allow if a.strip())
        self._app: Any = None
        self._ns: Any = None

    # -- session ------------------------------------------------------------------------------

    def _session(self) -> Any:
        if self._ns is None:
            self._app = self._dispatch()
            self._ns = self._app.GetNamespace("MAPI")
        return self._ns

    def reset(self) -> None:
        self._app = None
        self._ns = None

    def ping(self) -> dict[str, Any]:
        ns = self._session()
        stores = self._stores()
        return {
            "connected": True,
            "stores": len(stores),
            "default": _text(_prop(_prop(ns, "DefaultStore"), "DisplayName", "")),
            "accounts": [_text(_prop(s, "DisplayName", "")) for s in stores],
        }

    # -- stores / accounts --------------------------------------------------------------------

    def _allowed(self, store: Any) -> bool:
        if not self._allow:
            return True
        return _text(_prop(store, "DisplayName", "")).lower() in self._allow

    def _stores(self) -> list[Any]:
        ns = self._session()
        return [s for s in _iter_com(ns.Stores) if self._allowed(s)]

    def _store(self, account: str = "") -> Any:
        ns = self._session()
        stores = self._stores()
        if not stores:
            raise OutlookError(f"no Outlook stores available (allow-list: {list(self._allow) or 'all'})")
        names = [_text(_prop(s, "DisplayName", "")) for s in stores]
        wanted = (account or "").strip().lower()
        if not wanted:
            default = _prop(ns, "DefaultStore")
            default_id = _text(_prop(default, "StoreID", ""))
            for s in stores:
                if _text(_prop(s, "StoreID", "")) == default_id:
                    return s
            return stores[0]
        for s, name in zip(stores, names, strict=True):
            if name.lower() == wanted:
                return s
        if looks_like_entry_id(wanted):
            for s in stores:
                if _text(_prop(s, "StoreID", "")).lower() == wanted:
                    return s
        for acct in _iter_com(_prop(ns, "Accounts")):
            if _text(_prop(acct, "SmtpAddress", "")).lower() == wanted:
                sid = _text(_prop(_prop(acct, "DeliveryStore"), "StoreID", ""))
                for s in stores:
                    if _text(_prop(s, "StoreID", "")) == sid:
                        return s
        partial = [s for s, name in zip(stores, names, strict=True) if wanted in name.lower()]
        if len(partial) == 1:
            return partial[0]
        raise NotFound(f"account {account!r} not found; available: {names}")

    def _account_for_store(self, store: Any) -> Any:
        sid = _text(_prop(store, "StoreID", ""))
        for acct in _iter_com(_prop(self._session(), "Accounts")):
            if _text(_prop(_prop(acct, "DeliveryStore"), "StoreID", "")) == sid:
                return acct
        return None

    def _default_folder(self, store: Any, const: int) -> Any:
        try:
            return store.GetDefaultFolder(const)
        except Exception:
            return None

    def accounts(self) -> list[dict[str, Any]]:
        ns = self._session()
        default_id = _text(_prop(_prop(ns, "DefaultStore"), "StoreID", ""))
        smtp_by_store: dict[str, str] = {}
        for acct in _iter_com(_prop(ns, "Accounts")):
            sid = _text(_prop(_prop(acct, "DeliveryStore"), "StoreID", ""))
            if sid:
                smtp_by_store[sid] = _text(_prop(acct, "SmtpAddress", ""))
        out: list[dict[str, Any]] = []
        for store in self._stores():
            sid = _text(_prop(store, "StoreID", ""))
            folders: dict[str, Any] = {}
            for const, role in DEFAULT_ROLES.items():
                folder = self._default_folder(store, const)
                if folder is not None:
                    folders[role] = {
                        "id": _text(_prop(folder, "EntryID", "")),
                        "name": _text(_prop(folder, "Name", "")),
                    }
            out.append(
                {
                    "name": _text(_prop(store, "DisplayName", "")),
                    "store_id": sid,
                    "type": STORE_TYPES.get(int(_prop(store, "ExchangeStoreType", 3) or 0), "other"),
                    "default": sid == default_id,
                    "smtp": smtp_by_store.get(sid, ""),
                    "folders": folders,
                }
            )
        return out

    # -- folders ------------------------------------------------------------------------------

    def _child(self, folder: Any, name: str) -> Any:
        wanted = name.strip().lower()
        for sub in _iter_com(_prop(folder, "Folders")):
            if _text(_prop(sub, "Name", "")).strip().lower() == wanted:
                return sub
        return None

    def _children_names(self, folder: Any) -> list[str]:
        return [_text(_prop(sub, "Name", "")) for sub in _iter_com(_prop(folder, "Folders"))][:40]

    def _folder(self, store: Any, spec: str) -> Any:
        """Resolve a folder by well-known role, EntryID, or ``/``-separated path (localised names)."""
        spec = (spec or "inbox").strip()
        key = spec.lower().replace(" ", "").replace("_", "")
        if key in WELL_KNOWN_FOLDERS:
            folder = self._default_folder(store, WELL_KNOWN_FOLDERS[key])
            if folder is None:
                raise NotFound(f"store {_text(_prop(store, 'DisplayName', ''))!r} has no {spec} folder")
            return folder
        if looks_like_entry_id(spec):
            try:
                return self._session().GetFolderFromID(spec, _prop(store, "StoreID"))
            except Exception as exc:
                raise NotFound(f"folder id {spec[:16]}… not found: {describe_com_error(exc)}") from exc
        parts = [p.strip() for p in re.split(r"[\\/]+", spec) if p.strip()]
        root = store.GetRootFolder()
        node = root
        start = 0
        head = parts[0].lower().replace(" ", "").replace("_", "")
        if head in WELL_KNOWN_FOLDERS:
            node = self._default_folder(store, WELL_KNOWN_FOLDERS[head])
            if node is None:
                raise NotFound(f"store has no {parts[0]} folder")
            start = 1
        elif parts[0].lower() == _text(_prop(root, "Name", "")).lower():
            start = 1
        for idx, part in enumerate(parts[start:], start):
            child = self._child(node, part)
            if child is None and idx == 0:
                inbox = self._default_folder(store, FOLDER_INBOX)
                child = self._child(inbox, part) if inbox is not None else None
            if child is None:
                raise NotFound(
                    f"folder {spec!r}: no {part!r} under {_text(_prop(node, 'FolderPath', '')) or 'root'}; children: {self._children_names(node)}"
                )
            node = child
        return node

    def folders(self, account: str = "") -> dict[str, Any]:
        store = self._store(account)
        root = store.GetRootFolder()
        roles: dict[str, str] = {}
        for const, role in DEFAULT_ROLES.items():
            folder = self._default_folder(store, const)
            if folder is not None:
                roles[_text(_prop(folder, "EntryID", ""))] = role
        out: list[dict[str, Any]] = []

        def walk(folder: Any, path: str, depth: int) -> None:
            if depth > 8:
                return
            for sub in _iter_com(_prop(folder, "Folders")):
                name = _text(_prop(sub, "Name", ""))
                p = f"{path}/{name}" if path else name
                eid = _text(_prop(sub, "EntryID", ""))
                entry: dict[str, Any] = {
                    "id": eid,
                    "name": name,
                    "path": p,
                    "kind": ITEM_KINDS.get(int(_prop(sub, "DefaultItemType", 0) or 0), "other"),
                    "unread": int(_prop(sub, "UnReadItemCount", 0) or 0),
                }
                total = _prop(_prop(sub, "Items"), "Count")
                if total is not None:
                    entry["total"] = int(total)
                if eid in roles:
                    entry["default"] = roles[eid]
                out.append(entry)
                walk(sub, p, depth + 1)

        walk(root, "", 0)
        return {
            "account": _text(_prop(store, "DisplayName", "")),
            "store_id": _text(_prop(store, "StoreID", "")),
            "folders": out,
        }

    # -- listing (GetTable) -------------------------------------------------------------------

    def _table_rows(
        self, folder: Any, filt: str | None, limit: int, skip: set[str]
    ) -> tuple[list[Row], bool, int | None]:
        try:
            tbl = folder.GetTable(filt) if filt else folder.GetTable()
        except Exception as exc:
            raise OutlookError(
                f"GetTable failed on {_text(_prop(folder, 'FolderPath', ''))}: {describe_com_error(exc)} (filter: {filt})"
            ) from exc
        columns: list[str] = []
        cols = tbl.Columns
        cols.RemoveAll()
        for name in TABLE_COLUMNS:
            try:
                cols.Add(name)
                columns.append(name)
            except Exception:
                continue  # a store that lacks the column simply yields None for it
        with contextlib.suppress(Exception):
            tbl.Sort("ReceivedTime", True)  # newest first; unsorted still beats not reading
        total: int | None
        try:
            total = int(tbl.GetRowCount())
        except Exception:
            total = None
        rows: list[Row] = []
        leftover = 0
        while len(rows) < limit:
            try:
                if tbl.EndOfTable:
                    break
                chunk = tbl.GetArray(min(250, limit - len(rows) + len(skip) + 1))
            except Exception as exc:
                raise OutlookError(f"reading table rows failed: {describe_com_error(exc)}") from exc
            if not chunk:
                break
            for i, raw in enumerate(chunk):
                row = parse_row(raw, columns)
                if not row.entry_id or row.entry_id in skip:
                    continue
                rows.append(row)
                if len(rows) >= limit:
                    leftover = len(chunk) - i - 1
                    break
        more = leftover > 0 or not bool(_prop(tbl, "EndOfTable", True))
        return rows, more, total

    @staticmethod
    def _next_cursor(rows: list[Row], more: bool, prev_upper: datetime | None, prev_skip: set[str]) -> str | None:
        if not more or not rows:
            return None
        last = rows[-1].received
        if last is None:
            return None
        boundary = last.replace(microsecond=0)
        same = [r.entry_id for r in rows if r.received is not None and r.received.replace(microsecond=0) == boundary]
        if prev_upper is not None and prev_upper.astimezone(UTC).replace(microsecond=0) == boundary.astimezone(UTC):
            same = [*prev_skip, *same]
        return encode_cursor(boundary, same)

    def list_items(
        self,
        account: str = "",
        folder: str = "inbox",
        since: str | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit or 25), MAX_LIST_LIMIT))
        store = self._store(account)
        target = self._folder(store, folder)
        lower = parse_when(since) if since else None
        upper, skip = decode_cursor(cursor) if cursor else (None, set())
        rows, more, total = self._table_rows(target, received_filter(lower, upper), limit, skip)
        store_id = _text(_prop(store, "StoreID", ""))
        result: dict[str, Any] = {
            "account": _text(_prop(store, "DisplayName", "")),
            "folder": _text(_prop(target, "FolderPath", "")) or _text(_prop(target, "Name", "")),
            "folder_id": _text(_prop(target, "EntryID", "")),
            "items": [r.as_item(store_id) for r in rows],
            "cursor": self._next_cursor(rows, more, upper, skip),
        }
        if total is not None:
            result["total"] = total
        return result

    def search(
        self,
        account: str = "",
        query: str = "",
        days_back: int = 30,
        cursor: str | None = None,
        limit: int = 25,
        folder: str = "inbox",
    ) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            raise OutlookError("query is empty")
        limit = max(1, min(int(limit or 25), MAX_LIST_LIMIT))
        store = self._store(account)
        target = self._folder(store, folder)
        lower = datetime.now().astimezone() - timedelta(days=max(1, int(days_back or 30)))
        upper, skip = decode_cursor(cursor) if cursor else (None, set())
        like = f"'%{dasl_str(q)}%'"
        match = f"({DASL_SUBJECT} LIKE {like} OR {DASL_FROMNAME} LIKE {like} OR {DASL_FROMEMAIL} LIKE {like})"
        rows, more, _ = self._table_rows(target, received_filter(lower, upper, match), limit, skip)
        # Integrity check (V1 incident: DASL matched whole folders on the Gmail store). Only rows
        # that verifiably contain the query in the columns we asked about are returned.
        needle = q.lower()
        verified = [
            r
            for r in rows
            if needle in r.subject.lower() or needle in r.sender_name.lower() or needle in r.sender_addr.lower()
        ]
        dropped = len(rows) - len(verified)
        store_id = _text(_prop(store, "StoreID", ""))
        result: dict[str, Any] = {
            "account": _text(_prop(store, "DisplayName", "")),
            "folder": _text(_prop(target, "FolderPath", "")) or _text(_prop(target, "Name", "")),
            "query": q,
            "matched_on": ["subject", "from"],
            "days_back": int(days_back or 30),
            "items": [r.as_item(store_id) for r in verified],
            "cursor": self._next_cursor(rows, more, upper, skip),
        }
        if dropped:
            result["unverified_dropped"] = dropped
        return result

    # -- single items -------------------------------------------------------------------------

    def _item(self, entry_id: str, account: str = "") -> tuple[Any, Any]:
        eid = (entry_id or "").strip()
        if not eid:
            raise OutlookError("entry_id is empty")
        ns = self._session()
        if account:
            stores = [self._store(account)]
        else:
            stores = self._stores()
            default_id = _text(_prop(_prop(ns, "DefaultStore"), "StoreID", ""))
            stores.sort(key=lambda s: _text(_prop(s, "StoreID", "")) != default_id)
        last = ""
        for store in stores:
            try:
                item = ns.GetItemFromID(eid, _prop(store, "StoreID"))
            except Exception as exc:
                if is_transient(exc) or is_disconnected(exc):
                    raise  # a busy or gone Outlook is not "not in this store"
                last = describe_com_error(exc)
                continue
            if item is not None:
                return item, store
        names = [_text(_prop(s, "DisplayName", "")) for s in stores]
        raise NotFound(f"item {eid[:16]}… not found in {names}" + (f" ({last})" if last else ""))

    def _sender_smtp(self, item: Any) -> str:
        addr = _text(_prop(item, "SenderEmailAddress", ""))
        if addr and not addr.upper().startswith("/O="):
            return addr.lower()
        try:
            smtp = item.PropertyAccessor.GetProperty(PR_SENDER_SMTP)
            if smtp and "@" in str(smtp):
                return str(smtp).lower()
        except Exception:
            pass
        try:
            user = item.Sender.GetExchangeUser()
            smtp = _text(_prop(user, "PrimarySmtpAddress", "")) if user is not None else ""
            if "@" in smtp:
                return smtp.lower()
        except Exception:
            pass
        return addr

    def read(self, entry_id: str, account: str = "", max_chars: int = MAX_BODY_CHARS) -> dict[str, Any]:
        item, store = self._item(entry_id, account)
        body = _text(_prop(item, "Body", ""))
        if not body.strip():
            markup = _text(_prop(item, "HTMLBody", ""))
            if markup:
                body = html_to_text(markup)
        truncated = len(body) > max_chars
        attachments = [
            {
                "name": _text(_prop(att, "FileName", "")) or _text(_prop(att, "DisplayName", "")),
                "size": int(_prop(att, "Size", 0) or 0),
            }
            for att in _iter_com(_prop(item, "Attachments"))
        ]
        flag_status = int(_prop(item, "FlagStatus", 0) or 0)
        is_task = bool(_prop(item, "IsMarkedAsTask", False))
        parent = _prop(item, "Parent")
        return {
            "entry_id": _text(_prop(item, "EntryID", "")) or entry_id,
            "store_id": _text(_prop(store, "StoreID", "")),
            "account": _text(_prop(store, "DisplayName", "")),
            "folder": _text(_prop(parent, "FolderPath", "")),
            "kind": kind_from_message_class(_text(_prop(item, "MessageClass", ""))),
            "subject": _text(_prop(item, "Subject", "")),
            "from": {"name": _text(_prop(item, "SenderName", "")), "address": self._sender_smtp(item)},
            "to": _text(_prop(item, "To", "")),
            "cc": _text(_prop(item, "CC", "")),
            "received": iso_local(_to_dt(_prop(item, "ReceivedTime"))),
            "sent": iso_local(_to_dt(_prop(item, "SentOn"))),
            "unread": bool(_prop(item, "UnRead", False)),
            "flagged": flag_status == 2 or is_task,
            "flag_status": flag_status,
            "is_task": is_task,
            "importance": IMPORTANCE.get(int(_prop(item, "Importance", 1) or 1), "normal"),
            "categories": _text(_prop(item, "Categories", "")),
            "conversation_id": _text(_prop(item, "ConversationID", "")),
            "attachments": attachments,
            "body": body[:max_chars],
            "body_truncated": truncated,
        }

    def move(self, entry_id: str, folder: str, account: str = "") -> dict[str, Any]:
        item, store = self._item(entry_id, account)
        target = self._folder(store, folder)
        subject = _text(_prop(item, "Subject", ""))
        try:
            moved = item.Move(target)
        except Exception as exc:
            raise OutlookError(f"move failed: {describe_com_error(exc)}") from exc
        new_id = _text(_prop(moved, "EntryID", "")) if moved is not None else ""
        if not new_id:
            raise OutlookError("move returned no item; refusing to report success")
        return {
            "entry_id": new_id,
            "previous_entry_id": entry_id,
            "subject": subject,
            "folder": _text(_prop(target, "FolderPath", "")) or _text(_prop(target, "Name", "")),
            "folder_id": _text(_prop(target, "EntryID", "")),
        }

    def flag(self, entry_id: str, flag: bool, account: str = "") -> dict[str, Any]:
        item, _ = self._item(entry_id, account)
        errors: list[str] = []
        if flag:
            try:
                item.MarkAsTask(OL_MARK_NO_DATE)
            except Exception as exc:
                errors.append(f"MarkAsTask: {describe_com_error(exc)}")
                try:
                    item.FlagStatus = 2  # olFlagMarked — pre-2013 stores only
                except Exception as exc2:
                    errors.append(f"FlagStatus: {describe_com_error(exc2)}")
                    raise OutlookError("could not flag item: " + "; ".join(errors)) from exc2
        else:
            cleared = False
            try:
                item.ClearTaskFlag()
                cleared = True
            except Exception as exc:
                errors.append(f"ClearTaskFlag: {describe_com_error(exc)}")
            try:
                item.FlagStatus = 0
                cleared = True
            except Exception as exc:
                errors.append(f"FlagStatus: {describe_com_error(exc)}")
            if not cleared:
                raise OutlookError("could not unflag item: " + "; ".join(errors))
        try:
            item.Save()
        except Exception as exc:
            raise OutlookError(f"Save failed: {describe_com_error(exc)}") from exc
        flag_status = int(_prop(item, "FlagStatus", 0) or 0)
        is_task = bool(_prop(item, "IsMarkedAsTask", False))
        now_flagged = flag_status == 2 or is_task
        if now_flagged != bool(flag):
            raise OutlookError(
                f"flag state did not change (FlagStatus={flag_status}, IsMarkedAsTask={is_task}); " + "; ".join(errors)
            )
        return {
            "entry_id": _text(_prop(item, "EntryID", "")) or entry_id,
            "flagged": now_flagged,
            "flag_status": flag_status,
            "is_task": is_task,
        }

    def send(
        self,
        account: str,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        reply_to_entry_id: str = "",
        html_body: bool = False,
        draft: bool = False,
    ) -> dict[str, Any]:
        self._session()
        store = self._store(account)
        threaded = False
        if reply_to_entry_id:
            original, _ = self._item(reply_to_entry_id, account)
            try:
                mail = original.Reply()
            except Exception as exc:
                raise OutlookError(f"Reply() failed: {describe_com_error(exc)}") from exc
            threaded = True
        else:
            mail = self._app.CreateItem(OL_MAIL_ITEM)
            mail.Subject = subject or ""
        acct = self._account_for_store(store)
        if acct is not None:
            try:
                mail.SendUsingAccount = acct
            except Exception as exc:
                log.warning("outlook: SendUsingAccount failed (%s); Outlook picks the account", describe_com_error(exc))
        if to:
            mail.To = semicolons(to)
        if cc:
            mail.CC = semicolons(cc)
        if not _text(_prop(mail, "To", "")).strip():
            raise OutlookError(
                "no recipient: pass `to` (a reply inherits the original sender only when `to` is empty and Reply() filled it)"
            )
        if threaded:
            new_html = body if html_body else text_to_html(body)
            mail.HTMLBody = merge_reply_html(_text(_prop(mail, "HTMLBody", "")), new_html)
        elif html_body:
            mail.HTMLBody = body
        else:
            mail.Body = body
        final_subject = _text(_prop(mail, "Subject", ""))
        final: dict[str, Any] = {
            "sent": not draft,
            "drafted": draft,
            "account": _text(_prop(store, "DisplayName", "")),
            "to": _text(_prop(mail, "To", "")),
            "cc": _text(_prop(mail, "CC", "")),
            "subject": final_subject,
            "threaded": threaded,
            "conversation_id": _text(_prop(mail, "ConversationID", "")),
        }
        if threaded and subject and subject.strip() != final_subject.strip():
            final["note"] = f"kept the threaded subject {final_subject!r}; changing it would fork the conversation"
        try:
            if draft:
                # Saved to Drafts, not sent: the caller decides, a human presses Send.
                mail.Save()
                final["entry_id"] = _text(_prop(mail, "EntryID", ""))
                final["note"] = "saved as a draft in Outlook; nothing was sent"
            else:
                mail.Send()
        except Exception as exc:
            verb = "Save" if draft else "Send"
            raise OutlookError(f"{verb} failed: {describe_com_error(exc)}") from exc
        return final

    # -- calendar -----------------------------------------------------------------------------

    def _calendar(self, store: Any) -> Any:
        cal = self._default_folder(store, FOLDER_CALENDAR)
        if cal is None:
            raise NotFound(f"account {_text(_prop(store, 'DisplayName', ''))!r} has no calendar")
        return cal

    def _event(self, item: Any) -> dict[str, Any]:
        attendees: list[str] = []
        for r in _iter_com(_prop(item, "Recipients")):
            attendees.append(_text(_prop(r, "Name", "")))
            if len(attendees) >= 20:
                break
        return {
            "entry_id": _text(_prop(item, "EntryID", "")),
            "subject": _text(_prop(item, "Subject", "")),
            "start": iso_local(_to_dt(_prop(item, "Start"))),
            "end": iso_local(_to_dt(_prop(item, "End"))),
            "all_day": bool(_prop(item, "AllDayEvent", False)),
            "location": _text(_prop(item, "Location", "")),
            "organizer": _text(_prop(item, "Organizer", "")),
            "busy": BUSY_STATUS.get(int(_prop(item, "BusyStatus", 2) or 0), "busy"),
            "recurring": bool(_prop(item, "IsRecurring", False)),
            "attendees": attendees,
            "categories": _text(_prop(item, "Categories", "")),
        }

    def calendar_list(self, account: str = "", days: int = 7, start: str | None = None) -> dict[str, Any]:
        store = self._store(account)
        cal = self._calendar(store)
        start_dt = (
            parse_when(start)
            if start
            else datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        )
        end_dt = start_dt + timedelta(days=max(1, min(int(days or 7), 366)))
        items = cal.Items
        items.Sort("[Start]")
        items.IncludeRecurrences = True
        restriction = f"[Start] >= '{jet_date(start_dt)}' AND [Start] <= '{jet_date(end_dt)}'"
        try:
            restricted = items.Restrict(restriction)
        except Exception as exc:
            raise OutlookError(f"calendar Restrict failed ({restriction}): {describe_com_error(exc)}") from exc
        events: list[dict[str, Any]] = []
        checked = 0
        t0 = time.monotonic()
        for item in _iter_items(restricted):
            checked += 1
            if checked > CALENDAR_SCAN_CAP or time.monotonic() - t0 > CALENDAR_SCAN_SECONDS:
                break
            if int(_prop(item, "Class", 0) or 0) != OL_APPOINTMENT_CLASS:
                continue
            events.append(self._event(item))
        source = "restrict"
        if checked == 0:
            # V1 saw Restrict silently return nothing under some locale setups; the sorted,
            # recurrence-expanded collection lets us stop as soon as Start passes the window.
            source = "scan"
            for item in _iter_items(items):
                checked += 1
                if checked > CALENDAR_SCAN_CAP or time.monotonic() - t0 > CALENDAR_SCAN_SECONDS:
                    break
                s = _to_dt(_prop(item, "Start"))
                if s is None:
                    continue
                if s > end_dt:
                    break
                if s >= start_dt and int(_prop(item, "Class", 0) or 0) == OL_APPOINTMENT_CLASS:
                    events.append(self._event(item))
        events.sort(key=lambda e: e.get("start") or "")
        return {
            "account": _text(_prop(store, "DisplayName", "")),
            "calendar": _text(_prop(cal, "Name", "")),
            "from": iso_local(start_dt),
            "to": iso_local(end_dt),
            "events": events,
            "items_checked": checked,
            "source": source,
        }

    def _overlapping(self, cal: Any, s: datetime, e: datetime) -> list[dict[str, Any]]:
        """Existing busy appointments that overlap [s, e). All-day and 'free' items do not count."""
        items = cal.Items
        items.Sort("[Start]")
        items.IncludeRecurrences = True
        # Anything starting up to a day before could still run into our slot; end bound is ours.
        restriction = f"[Start] >= '{jet_date(s - timedelta(days=1))}' AND [Start] < '{jet_date(e)}'"
        try:
            candidates = items.Restrict(restriction)
        except Exception as exc:
            raise OutlookError(f"calendar Restrict failed ({restriction}): {describe_com_error(exc)}") from exc
        hits: list[dict[str, Any]] = []
        checked = 0
        for item in _iter_items(candidates):
            checked += 1
            if checked > CALENDAR_SCAN_CAP:
                break
            if int(_prop(item, "Class", 0) or 0) != OL_APPOINTMENT_CLASS:
                continue
            if bool(_prop(item, "AllDayEvent", False)) or int(_prop(item, "BusyStatus", 2) or 0) == 0:
                continue
            i_start, i_end = _to_dt(_prop(item, "Start")), _to_dt(_prop(item, "End"))
            if i_start is None or i_end is None:
                continue
            if i_start < e and i_end > s:
                hits.append(
                    {
                        "entry_id": _text(_prop(item, "EntryID", "")),
                        "subject": _text(_prop(item, "Subject", "")),
                        "start": iso_local(i_start),
                        "end": iso_local(i_end),
                    }
                )
        return hits

    def calendar_create(
        self,
        account: str,
        subject: str,
        start: str,
        end: str = "",
        location: str = "",
        body: str = "",
        attendees: str = "",
        send_invites: bool = False,
        all_day: bool = False,
        allow_overlap: bool = False,
    ) -> dict[str, Any]:
        if not subject.strip():
            raise OutlookError("subject is empty")
        s = parse_when(start)
        e = parse_when(end) if end else s + timedelta(hours=1)
        if e <= s:
            raise OutlookError(f"end {end!r} is not after start {start!r}")
        store = self._store(account)
        cal = self._calendar(store)
        if not all_day and not allow_overlap:
            # Structural, like a unique index: a slot that already holds a busy appointment is
            # not free. On 2026-09-05 a scheduled run stacked 32 placeholder blocks on top of
            # the placeholders V1 had already placed; the model saw them in calendar_list and
            # created anyway. The tool refusing is what a prompt could not guarantee.
            clashes = self._overlapping(cal, s, e)
            if clashes:
                names = "; ".join(f"{c['subject']!r} {c['start'][11:16]}-{c['end'][11:16]}" for c in clashes[:5])
                raise OutlookError(
                    f"slot {s:%Y-%m-%d %H:%M}-{e:%H:%M} already holds {len(clashes)} appointment(s): {names}. "
                    "Nothing was created. Pick a free slot, or pass allow_overlap=true if the overlap is intended."
                )
        try:
            appt = cal.Items.Add(OL_APPOINTMENT_ITEM)
        except Exception as exc:
            raise OutlookError(
                f"cannot create an appointment in {_text(_prop(cal, 'FolderPath', ''))}: {describe_com_error(exc)}"
            ) from exc
        appt.Subject = subject
        appt.Location = location or ""
        appt.Body = body or ""
        # Strings, not datetimes: V1 found pywin32's datetime marshalling shifts by the UTC offset.
        appt.Start = s.strftime("%Y-%m-%d %H:%M")
        appt.End = e.strftime("%Y-%m-%d %H:%M")
        if all_day:
            appt.AllDayEvent = True
        names = split_addresses(attendees)
        for address in names:
            recipient = appt.Recipients.Add(address)
            recipient.Type = OL_RECIPIENT_REQUIRED
        if names:
            appt.MeetingStatus = OL_MEETING
        invites_sent = False
        try:
            if names and send_invites:
                appt.Send()
                invites_sent = True
            else:
                appt.Save()
        except Exception as exc:
            raise OutlookError(f"saving the appointment failed: {describe_com_error(exc)}") from exc
        return {
            "entry_id": _text(_prop(appt, "EntryID", "")),
            "account": _text(_prop(store, "DisplayName", "")),
            "calendar": _text(_prop(cal, "Name", "")),
            "subject": subject,
            "start": iso_local(s),
            "end": iso_local(e),
            "location": location or "",
            "attendees": names,
            "invites_sent": invites_sent,
        }

    def calendar_delete(self, entry_id: str, account: str = "") -> dict[str, Any]:
        """Delete one appointment by its durable id. Refuses anything that is not an appointment."""
        item, _ = self._item(entry_id, account)
        if int(_prop(item, "Class", 0) or 0) != OL_APPOINTMENT_CLASS:
            raise OutlookError(f"{entry_id[:24]}… is not an appointment (Class={_prop(item, 'Class', '?')})")
        summary = {
            "deleted": True,
            "subject": _text(_prop(item, "Subject", "")),
            "start": iso_local(_to_dt(_prop(item, "Start"))),
            "end": iso_local(_to_dt(_prop(item, "End"))),
            "was_meeting": int(_prop(item, "MeetingStatus", 0) or 0) != 0,
        }
        try:
            item.Delete()
        except Exception as exc:
            raise OutlookError(f"Delete failed: {describe_com_error(exc)}") from exc
        return summary


# --- async facade over the COM worker -----------------------------------------------------------


class OutlookService:
    """``OutlookBackend`` marshalled through the COM worker with per-call deadlines."""

    TIMEOUTS: dict[str, float] = {
        "ping": 10,
        "accounts": 45,
        "folders": 90,
        "list_items": 60,
        "search": 90,
        "read": 45,
        "move": 45,
        "flag": 45,
        "send": 60,
        "calendar_list": 60,
        "calendar_create": 45,
    }
    READ_ONLY = {"ping", "accounts", "folders", "list_items", "search", "read", "calendar_list"}

    def __init__(self, backend: OutlookBackend, worker: ComWorker) -> None:
        self.backend = backend
        self.worker = worker

    def _guarded(self, name: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self.backend, name)
        call = (
            (lambda: retry_transient(lambda: fn(*args, **kwargs)))
            if name in self.READ_ONLY
            else (lambda: fn(*args, **kwargs))
        )
        try:
            return call()
        except OutlookError:
            raise
        except Exception as exc:
            if is_disconnected(exc):
                self.backend.reset()
                raise OutlookError(
                    f"Outlook is not reachable ({describe_com_error(exc)}); is it running? The next call reconnects."
                ) from exc
            raise OutlookError(f"{name}: {describe_com_error(exc)}") from exc

    async def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        timeout = self.TIMEOUTS.get(name, 30.0)
        return await self.worker.acall(self._guarded, name, *args, label=f"outlook.{name}", timeout_s=timeout, **kwargs)
