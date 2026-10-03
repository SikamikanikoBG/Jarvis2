"""Unread mail across every folder, Outlook-style search, and threads with their bodies.

Triage files mail into subfolders (Bosses, Demands/DM-1234, Newsletters...), so "the unread mail"
is not the Inbox: it is every mail folder that says it has unread items. The folder tree already
knows its unread counts, so only those folders are read - one table each, filtered by the store.

Search speaks the syntax of Outlook's own search box (the subset that maps onto DASL):

    from:maria  to:pete  cc:ops  subject:budget  body:invoice   "exact phrase"   plain words
    hasattachments:yes  has:attachment  is:unread  is:read  is:flagged
    received:today | yesterday | "this week" | "last week" | "this month" | "last month"
    received:2026-09-01  received:>=2026-09-01  received:<2026-10-01  received:2026-09-01..2026-09-30
    after:2026-09-01  before:2026-10-01  folder:Demands   OR between terms   NOT / -term

Plain words match the subject, the sender, the recipients and the body, like Outlook's "All".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis_host.outlook import OutlookBackend

DASL_SUBJECT = '"urn:schemas:httpmail:subject"'
DASL_FROMNAME = '"urn:schemas:httpmail:fromname"'
DASL_FROMEMAIL = '"urn:schemas:httpmail:fromemail"'
DASL_TO = '"urn:schemas:httpmail:displayto"'
DASL_CC = '"urn:schemas:httpmail:displaycc"'
DASL_BODY = '"urn:schemas:httpmail:textdescription"'
DASL_READ = '"urn:schemas:httpmail:read"'
DASL_ATTACH = '"urn:schemas:httpmail:hasattachment"'
DASL_RECEIVED = '"urn:schemas:httpmail:datereceived"'
DASL_FLAG = '"http://schemas.microsoft.com/mapi/proptag/0x10900003"'  # PR_FLAG_STATUS, 2 = flagged

# Default folders that are never "unread mail" and not searched unless named with folder:.
FOLDER_DELETED, FOLDER_OUTBOX, FOLDER_SENT, FOLDER_DRAFTS, FOLDER_JUNK = 3, 4, 5, 16, 23
FOLDER_CONFLICTS, FOLDER_SYNC_ISSUES, FOLDER_LOCAL_FAILURES, FOLDER_SERVER_FAILURES, FOLDER_RSS = 19, 20, 21, 22, 25
NOT_UNREAD = (FOLDER_DELETED, FOLDER_OUTBOX, FOLDER_SENT, FOLDER_DRAFTS, FOLDER_JUNK)
NOT_SEARCHED = (FOLDER_DELETED, FOLDER_OUTBOX, FOLDER_JUNK)
NEVER = (FOLDER_CONFLICTS, FOLDER_SYNC_ISSUES, FOLDER_LOCAL_FAILURES, FOLDER_SERVER_FAILURES, FOLDER_RSS)
OL_MAIL_ITEM = 0
MAX_DEPTH = 10
MAX_RESULTS = 500
SEARCH_DAYS_DEFAULT = 365

FIELDS: dict[str, tuple[str, ...]] = {
    "from": (DASL_FROMNAME, DASL_FROMEMAIL),
    "to": (DASL_TO,),
    "cc": (DASL_CC,),
    "subject": (DASL_SUBJECT,),
    "body": (DASL_BODY,),
}
ALL_TEXT = (DASL_SUBJECT, DASL_FROMNAME, DASL_FROMEMAIL, DASL_TO, DASL_CC, DASL_BODY)
YES = {"yes", "true", "1", "y", "да"}


class QueryError(ValueError):
    """The query cannot be turned into a search; the message says which part and why."""


@dataclass
class ParsedQuery:
    """A search: one DASL condition per AND-ed group, plus what DASL cannot say."""

    conditions: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)  # folder: substrings, case-insensitive
    has_date: bool = False
    # Positive subject:/from: terms, checked against the row afterwards: DASL LIKE has matched
    # whole folders on the Gmail store before (V1 incident), and these two columns are in the row.
    verify: list[tuple[str, str]] = field(default_factory=list)

    def dasl(self) -> str | None:
        return " AND ".join(self.conditions) if self.conditions else None


_TOKEN = re.compile(r'(-)?(?:([A-Za-z]+):)?("([^"]*)"|\S+)')
_PERIOD = re.compile(r"\b(received|date|sent):(this|last)\s+(week|month)\b", re.IGNORECASE)


def _like(prop: str, value: str) -> str:
    return f"{prop} LIKE '%{value.replace(chr(39), chr(39) * 2)}%'"


def _any_like(props: tuple[str, ...], value: str) -> str:
    return "(" + " OR ".join(_like(p, value) for p in props) + ")"


def _utc(dt: datetime) -> str:
    from datetime import UTC

    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _day(text: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).astimezone()
        except ValueError:
            continue
    raise QueryError(f"not a date: {text!r} (use 2026-09-30 or 30.09.2026)")


def date_range(value: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    """received:… -> [lower, upper) in local time."""
    v = value.strip().lower()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    named = {
        "today": (today, today + timedelta(days=1)),
        "днес": (today, today + timedelta(days=1)),
        "yesterday": (today - timedelta(days=1), today),
        "вчера": (today - timedelta(days=1), today),
        "this week": (week, week + timedelta(days=7)),
        "last week": (week - timedelta(days=7), week),
        "this month": (month, (month + timedelta(days=32)).replace(day=1)),
        "last month": ((month - timedelta(days=1)).replace(day=1), month),
    }
    if v in named:
        return named[v]
    if ".." in v:
        a, b = v.split("..", 1)
        return _day(a), _day(b) + timedelta(days=1)
    for op in (">=", "<=", ">", "<"):
        if v.startswith(op):
            d = _day(v[len(op) :])
            return {
                ">=": (d, None),
                ">": (d + timedelta(days=1), None),
                "<=": (None, d + timedelta(days=1)),
                "<": (None, d),
            }[op]
    d = _day(v)
    return d, d + timedelta(days=1)


def _term(key: str | None, value: str, now: datetime, q: ParsedQuery, negate: bool) -> str | None:
    """One term as a DASL condition, or None when it only narrows the folders."""
    k = (key or "").lower()
    if k in ("", "all"):
        return _any_like(ALL_TEXT, value)
    if k in FIELDS:
        if not negate and k in ("subject", "from"):
            q.verify.append((k, value.lower()))
        return _any_like(FIELDS[k], value)
    if k in ("hasattachment", "hasattachments", "attachments"):
        return f"{DASL_ATTACH} = {1 if value.lower() in YES else 0}"
    if k == "has":
        if value.lower() in ("attachment", "attachments"):
            return f"{DASL_ATTACH} = 1"
        raise QueryError(f"has:{value} - only has:attachment is understood")
    if k in ("is", "isread", "read", "isflagged", "flagged"):
        v = value.lower()
        if k == "is" and v in ("unread", "read"):
            return f"{DASL_READ} = {0 if v == 'unread' else 1}"
        if k in ("isread", "read"):
            return f"{DASL_READ} = {1 if v in YES else 0}"
        if (k == "is" and v == "flagged") or (k in ("isflagged", "flagged") and v in YES):
            return f"{DASL_FLAG} = 2"
        if k in ("isflagged", "flagged"):
            return f"{DASL_FLAG} <> 2"
        raise QueryError(f"{k}:{value} - use is:unread, is:read or is:flagged")
    if k in ("received", "date", "sent", "after", "before"):
        q.has_date = True
        if k == "after":
            lo, hi = _day(value) + timedelta(days=1), None
        elif k == "before":
            lo, hi = None, _day(value)
        else:
            lo, hi = date_range(value, now)
        parts = []
        if lo is not None:
            parts.append(f"{DASL_RECEIVED} >= '{_utc(lo)}'")
        if hi is not None:
            parts.append(f"{DASL_RECEIVED} < '{_utc(hi)}'")
        return "(" + " AND ".join(parts) + ")"
    if k == "folder":
        if negate:
            raise QueryError("NOT folder: is not supported; name the folder to search instead")
        q.folders.append(value.lower())
        return None
    raise QueryError(
        f"unknown keyword {key}: - use from:, to:, cc:, subject:, body:, hasattachments:, is:, received:, folder:"
    )


def parse_query(text: str, now: datetime | None = None) -> ParsedQuery:
    """Outlook search syntax -> DASL. Terms are AND-ed; ``OR`` joins its two neighbours;
    ``NOT term`` or ``-term`` negates one."""
    now = now or datetime.now().astimezone()
    q = ParsedQuery()
    groups: list[list[str]] = []
    pending_or = False
    pending_not = False
    # received:this week reads as one value, as Outlook's box takes it.
    text = _PERIOD.sub(lambda m: f'{m.group(1)}:"{m.group(2)} {m.group(3)}"', text or "")
    for m in _TOKEN.finditer(text):
        minus, key, raw, quoted = m.group(1), m.group(2), m.group(3), m.group(4)
        value = quoted if quoted is not None else raw
        if key is None and quoted is None and raw in ("OR", "AND", "NOT"):
            if raw == "OR":
                pending_or = bool(groups)
            elif raw == "NOT":
                pending_not = True
            continue
        negate = bool(minus) or pending_not
        pending_not = False
        cond = _term(key, value, now, q, negate)
        if cond is None:
            pending_or = False
            continue
        if negate:
            cond = f"NOT ({cond})"
            q.verify = [v for v in q.verify if v != ((key or "").lower(), value.lower())]
        if pending_or and groups:
            groups[-1].append(cond)
        else:
            groups.append([cond])
        pending_or = False
    for g in groups:
        q.conditions.append(g[0] if len(g) == 1 else "(" + " OR ".join(g) + ")")
    if any(len(g) > 1 for g in groups):
        q.verify = []  # an OR makes a single term optional; the row check would be wrong
    if not q.conditions and not q.folders:
        raise QueryError("the search is empty")
    return q


def _row_matches(item: dict[str, Any], verify: list[tuple[str, str]]) -> bool:
    for key, value in verify:
        if key == "subject" and value not in str(item.get("subject") or "").lower():
            return False
        if key == "from":
            frm = item.get("from") or {}
            hay = f"{frm.get('name', '')} {frm.get('address', '')}".lower()
            if value not in hay:
                return False
    return True


def mail_folders(backend: OutlookBackend, store: Any, skip: tuple[int, ...]) -> list[tuple[Any, str]]:
    """Every mail folder of a store with its path, minus the default folders in ``skip``.

    Reads only Name, DefaultItemType and EntryID per folder - not Items.Count, which is what
    makes a full folder listing of a 200-folder mailbox slow."""
    from jarvis_host.outlook import _iter_com, _prop, _text

    skipped = set()
    for const in (*skip, *NEVER):
        f = backend._default_folder(store, const)
        if f is not None:
            skipped.add(_text(_prop(f, "EntryID", "")))
    out: list[tuple[Any, str]] = []

    def walk(folder: Any, path: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        for sub in _iter_com(_prop(folder, "Folders")):
            name = _text(_prop(sub, "Name", ""))
            p = f"{path}/{name}" if path else name
            if _text(_prop(sub, "EntryID", "")) in skipped:
                continue
            if int(_prop(sub, "DefaultItemType", 0) or 0) == OL_MAIL_ITEM:
                out.append((sub, p))
            walk(sub, p, depth + 1)

    walk(store.GetRootFolder(), "", 0)
    return out


def _rows(backend: OutlookBackend, folder: Any, filt: str | None, limit: int, preview_chars: int) -> list[Any]:
    rows, _more, _total = backend._table_rows(folder, filt, limit, set(), preview_chars)
    return rows


def unread(backend: OutlookBackend, account: str = "", limit: int = 300, preview_chars: int = 300) -> dict[str, Any]:
    """Unread mail of every mail folder (not Sent, Drafts, Deleted, Junk, Outbox), newest first."""
    from jarvis_host.outlook import _prop, _text

    limit = max(1, min(int(limit or 300), MAX_RESULTS))
    store = backend._store(account)
    store_id = _text(_prop(store, "StoreID", ""))
    items: list[dict[str, Any]] = []
    scanned = 0
    with_unread = 0
    for folder, path in mail_folders(backend, store, NOT_UNREAD):
        scanned += 1
        if int(_prop(folder, "UnReadItemCount", 0) or 0) <= 0:
            continue
        with_unread += 1
        for row in _rows(backend, folder, f"@SQL={DASL_READ} = 0", limit, preview_chars):
            if row.unread:
                items.append({**row.as_item(store_id), "folder": path})
    items.sort(key=lambda i: str(i.get("received") or ""), reverse=True)
    return {
        "account": _text(_prop(store, "DisplayName", "")),
        "items": items[:limit],
        "total": len(items),
        "capped": len(items) > limit,
        "folders_scanned": scanned,
        "folders_with_unread": with_unread,
    }


def query(
    backend: OutlookBackend,
    query_text: str,
    account: str = "",
    limit: int = 100,
    days_back: int = SEARCH_DAYS_DEFAULT,
    preview_chars: int = 300,
) -> dict[str, Any]:
    """Search every mail folder (Sent Items too) with Outlook's search syntax, newest first."""
    from jarvis_host.outlook import _prop, _text

    parsed = parse_query(query_text)
    limit = max(1, min(int(limit or 100), MAX_RESULTS))
    conditions = list(parsed.conditions)
    if not parsed.has_date and days_back:
        lower = datetime.now().astimezone() - timedelta(days=max(1, int(days_back)))
        conditions.append(f"{DASL_RECEIVED} >= '{_utc(lower)}'")
    # GetTable takes DASL only behind @SQL=; a bare condition is "Condition is not valid".
    filt = "@SQL=" + " AND ".join(conditions) if conditions else None
    store = backend._store(account)
    store_id = _text(_prop(store, "StoreID", ""))
    folders = mail_folders(backend, store, () if parsed.folders else NOT_SEARCHED)
    if parsed.folders:
        folders = [(f, p) for f, p in folders if any(n in p.lower() for n in parsed.folders)]
        if not folders:
            raise QueryError(f"no folder matches {parsed.folders}")
    items: list[dict[str, Any]] = []
    dropped = 0
    for folder, path in folders:
        for row in _rows(backend, folder, filt, limit, preview_chars):
            item = {**row.as_item(store_id), "folder": path}
            if parsed.verify and not _row_matches(item, parsed.verify):
                dropped += 1
                continue
            items.append(item)
    items.sort(key=lambda i: str(i.get("received") or ""), reverse=True)
    result: dict[str, Any] = {
        "account": _text(_prop(store, "DisplayName", "")),
        "query": query_text,
        "days_back": None if parsed.has_date else days_back,
        "items": items[:limit],
        "total": len(items),
        "capped": len(items) > limit,
        "folders_scanned": len(folders),
    }
    if dropped:
        result["unverified_dropped"] = dropped
    return result


def thread_full(
    backend: OutlookBackend, entry_id: str, account: str = "", limit: int = 20, body_chars: int = 8000
) -> dict[str, Any]:
    """``thread`` with each message's real body (a table preview can be empty for HTML mail)."""
    res = backend.thread(entry_id, account, limit, 400)
    items = res.get("items") or []
    if not items:
        one = backend.read(entry_id, account, body_chars)
        one["body_truncated"] = bool(one.get("body_truncated"))
        return {**res, "items": [one]}
    for item in items:
        try:
            full = backend.read(str(item["entry_id"]), account, body_chars)
        except Exception as exc:  # one unreadable message must not hide the thread
            item["body_error"] = str(exc)[:200]
            continue
        item["body"] = full.get("body", "")
        item["body_truncated"] = bool(full.get("body_truncated"))
        item["attachments"] = full.get("attachments", [])
        item["entry_id"] = full.get("entry_id") or item["entry_id"]
    return res
