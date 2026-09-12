"""A duck-typed stand-in for the Outlook COM object model — enough to test the backend anywhere.

Folder names are Bulgarian on purpose: anything that resolves a default folder by an English
name fails here exactly as it failed in production.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class FakeComError(Exception):
    """Shaped like ``pywintypes.com_error``: (hresult, text, (wcode, source, description, ...), argerror)."""

    def __init__(self, hresult: int, description: str) -> None:
        super().__init__(hresult, description, (0, "Microsoft Outlook", description, None, 0, hresult), None)


NOT_FOUND = -2147221233  # MAPI_E_NOT_FOUND
CALL_REJECTED = -2147418111  # RPC_E_CALL_REJECTED
SERVER_UNAVAILABLE = -2147023174  # RPC_S_SERVER_UNAVAILABLE


class Collection:
    def __init__(self, items: Iterable[Any] = ()) -> None:
        self._items = list(items)

    @property
    def Count(self) -> int:
        return len(self._items)

    def Item(self, i: int) -> Any:
        return self._items[i - 1]

    def Add(self, value: Any) -> Any:
        item = Recipient(value) if isinstance(value, str) else value  # Recipients.Add(address) → Recipient
        self._items.append(item)
        return item


class Attachment:
    def __init__(self, name: str, size: int) -> None:
        self.FileName = name
        self.DisplayName = name
        self.Size = size


class Attachments(Collection):
    """``Attachments.Add(path)`` reads the file like Outlook does: a missing file is a COM error."""

    def Add(self, value: Any) -> Any:
        if isinstance(value, str):
            path = Path(value)
            if not path.is_file():
                raise FakeComError(
                    -2147024894, f"Cannot find this file. Verify the path and file name are correct: {value}"
                )
            value = Attachment(path.name, path.stat().st_size)
        self._items.append(value)
        return value


class OleObj:
    """The raw IDispatch pywin32 hides behind ``_oleobj_``.

    Real Outlook ignores a by-value ``mail.SendUsingAccount = acct`` (a PROPERTYPUT) and only
    honours a PROPERTYPUTREF invoke on dispid 64209; the fake does the same so the test proves
    the backend uses the form that works.
    """

    def __init__(self, mail: Mail) -> None:
        self._mail = mail

    def Invoke(self, dispid: int, lcid: int, flags: int, ret: int, *args: Any) -> None:
        if dispid == 64209 and flags == 8:
            self._mail._send_using_account = args[0]
            return
        raise FakeComError(-2147352573, f"Member not found (dispid {dispid}, flags {flags})")


class Recipient:
    def __init__(self, name: str) -> None:
        self.Name = name
        self.Address = name
        self.Type = 0


class Mail:
    _counter = 0

    def __init__(
        self,
        subject: str,
        sender: str,
        received: datetime,
        *,
        unread: bool = False,
        flag_status: int = 0,
        is_task: bool = False,
        body: str = "",
        html_body: str = "",
        message_class: str = "IPM.Note",
        attachments: Iterable[Attachment] = (),
        size: int = 1234,
    ) -> None:
        Mail._counter += 1
        n = Mail._counter
        self.EntryID = f"00000000LONGTERM{n:04d}" + "A" * 120  # 140 chars like Exchange long-term ids
        self.short_id = f"EF000000{n:04d}" + "B" * 36  # 48 chars like a table's short-term id
        self.Subject = subject
        self.SenderName = sender.split("@", maxsplit=1)[0]
        self.SenderEmailAddress = sender
        self.ReceivedTime = received
        self.SentOn = received - timedelta(minutes=1)
        self.UnRead = unread
        self._flag_status = flag_status
        self.IsMarkedAsTask = is_task
        self.MessageClass = message_class
        self.Size = size
        self.Body = body
        self.HTMLBody = html_body
        self.To = "me@example.bg"
        self.CC = ""
        self.Importance = 1
        self.Categories = ""
        self.ConversationID = f"CONV{n:04d}"
        self.Attachments = Attachments(attachments)
        self.Recipients = Collection()
        self.Parent: Folder | None = None
        self._send_using_account: Any = None
        self._oleobj_ = OleObj(self)
        self.saved = 0
        self.sent = False
        self.calls: list[str] = []

    @property
    def SendUsingAccount(self) -> Any:
        return self._send_using_account

    @SendUsingAccount.setter
    def SendUsingAccount(self, value: Any) -> None:
        pass  # what Outlook does with a by-value put: nothing, silently

    @property
    def FlagStatus(self) -> int:
        return self._flag_status

    @FlagStatus.setter
    def FlagStatus(self, value: int) -> None:
        store = self.Parent.Store if self.Parent is not None else None
        if value == 2 and store is not None and store.modern:
            raise FakeComError(-2147352567, "Property 'GetItemFromID.FlagStatus' can not be set.")
        self._flag_status = value

    def MarkAsTask(self, interval: int) -> None:
        self.calls.append(f"MarkAsTask({interval})")
        self.IsMarkedAsTask = True
        self._flag_status = 2

    def ClearTaskFlag(self) -> None:
        self.calls.append("ClearTaskFlag")
        self.IsMarkedAsTask = False
        self._flag_status = 0

    def Save(self) -> None:
        self.saved += 1

    def Move(self, folder: Folder) -> Mail:
        if self.Parent is not None:
            self.Parent._items.remove(self)
        folder._items.append(self)
        self.Parent = folder
        self.EntryID = self.EntryID[:-8] + "MOVED000"
        return self

    def Reply(self) -> Mail:
        reply = Mail("RE: " + self.Subject, "me@example.bg", datetime.now(UTC))
        reply.To = self.SenderEmailAddress
        reply.HTMLBody = "<html><body><div class='quoted'>original text</div></body></html>"
        reply.ConversationID = self.ConversationID
        reply.Parent = self.Parent
        return reply

    def Send(self) -> None:
        if self.Parent is not None:
            self.Parent.Store.sent.append(self)
        self.sent = True


class Appointment:
    Class = 26

    def __init__(self, subject: str, start: datetime, end: datetime, *, cls: int = 26) -> None:
        self.Subject = subject
        self.Start = start
        self.End = end
        self.Class = cls
        self.EntryID = "APPT" + subject.upper().replace(" ", "")[:8].ljust(8, "0") + "0" * 40
        self.Location = "Room 1"
        self.Organizer = "Boss"
        self.BusyStatus = 2
        self.AllDayEvent = False
        self.IsRecurring = False
        self.Categories = ""
        self.Recipients = Collection([Recipient("Ana"), Recipient("Boris")])
        self.MeetingStatus = 0
        self.ResponseStatus = 0
        self.Body = ""
        self.saved = 0
        self.sent = False
        self.deleted = False
        self.organizer_address = ""  # what GetOrganizer() resolves to ("" = unresolvable)
        self.responses: list[MeetingResponse] = []

    def Save(self) -> None:
        self.saved += 1

    def Send(self) -> None:
        self.sent = True

    def Delete(self) -> None:
        self.deleted = True

    def GetOrganizer(self) -> AddressEntry | None:
        return AddressEntry(self.organizer_address) if self.organizer_address else None

    @property
    def PropertyAccessor(self) -> PropertyAccessor:
        return PropertyAccessor({})

    def Respond(self, code: int, no_ui: bool = False) -> MeetingResponse:
        # olMeetingAccepted=3 / olMeetingTentative=2 / olMeetingDeclined=4 → ResponseStatus
        # olResponseAccepted=3 / olResponseTentative=2 / olResponseDeclined=4 (same numbers).
        self.ResponseStatus = code
        resp = MeetingResponse(self, code)
        self.responses.append(resp)
        return resp


class MeetingResponse:
    def __init__(self, appt: Appointment, code: int) -> None:
        self.appt = appt
        self.code = code
        self.Body = ""
        self.sent = False

    def Send(self) -> None:
        self.sent = True
        if self.code == 4:  # a decline removes the appointment from the calendar
            self.appt.deleted = True


class ExchangeUser:
    def __init__(self, smtp: str) -> None:
        self.PrimarySmtpAddress = smtp


class AddressEntry:
    def __init__(self, address: str) -> None:
        # "x500:" marks an Exchange-only entry whose SMTP is reachable via GetExchangeUser.
        self.Address = address if "@" in address and not address.startswith("x500:") else "/o=Exchange/cn=Recipients"
        self._smtp = address[5:] if address.startswith("x500:") else address

    def GetExchangeUser(self) -> ExchangeUser | None:
        return ExchangeUser(self._smtp) if "@" in self._smtp else None


class PropertyAccessor:
    def __init__(self, props: dict[str, Any]) -> None:
        self._props = props

    def GetProperty(self, name: str) -> Any:
        if name not in self._props:
            raise FakeComError(-2147221233, "The property does not exist.")
        return self._props[name]


class Items:
    def __init__(self, folder: Folder) -> None:
        self._folder = folder
        self.IncludeRecurrences = False
        self.calls: list[str] = []
        self._cursor = 0
        self._view: list[Any] | None = None

    @property
    def Count(self) -> int:
        return len(self._folder._items)

    def _all(self) -> list[Any]:
        return self._view if self._view is not None else list(self._folder._items)

    def Sort(self, prop: str, descending: bool = False) -> None:
        self.calls.append(f"Sort({prop})")
        self._folder.item_calls.append(f"Sort({prop})")

    def Restrict(self, filt: str) -> Items:
        self._folder.item_calls.append("Restrict")
        if not self.IncludeRecurrences:
            self._folder.item_calls.append("Restrict-before-IncludeRecurrences")
        view = Items(self._folder)
        view._view = list(self._folder._items)
        return view

    def GetFirst(self) -> Any:
        self._cursor = 0
        return self.GetNext()

    def GetNext(self) -> Any:
        items = self._all()
        if self._cursor >= len(items):
            return None
        item = items[self._cursor]
        self._cursor += 1
        return item

    def Add(self, item_type: int) -> Appointment:
        self._folder.item_calls.append(f"Add({item_type})")
        appt = Appointment("", datetime.now(), datetime.now())
        appt.Recipients = Collection()  # a brand-new item has no attendees
        self._folder._items.append(appt)
        return appt


_DASL_GE = re.compile(r'datereceived" >= \'([^\']+)\'')
_DASL_LE = re.compile(r'datereceived" <= \'([^\']+)\'')
_DASL_LIKE = re.compile(r"LIKE '%([^']*)%'")


class Table:
    """Mimics ``Outlook.Table``: DASL date bounds in UTC, LIKE on subject/sender, chunked GetArray."""

    def __init__(self, folder: Folder, filt: str | None) -> None:
        self.folder = folder
        self.filter = filt
        self.columns: list[str] = list(folder.store.default_columns)
        self.Columns = self
        rows = [m for m in folder._items if isinstance(m, Mail)]
        if filt:
            rows = [m for m in rows if self._matches(m, filt)]
            if folder.store.dasl_leaks and _DASL_LIKE.search(filt):
                leak = Mail("Weekly digest", "noreply@leak.example", datetime.now(UTC) - timedelta(hours=1))
                leak.Parent = folder
                rows.append(leak)  # the Gmail-store bug: DASL matched the whole folder
        self._rows = rows
        self._pos = 0

    @staticmethod
    def _matches(m: Mail, filt: str) -> bool:
        recv = m.ReceivedTime.astimezone(UTC).replace(microsecond=0)
        ge = _DASL_GE.search(filt)
        if ge and recv < datetime.strptime(ge.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC):
            return False
        le = _DASL_LE.search(filt)
        if le and recv > datetime.strptime(le.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC):
            return False
        like = _DASL_LIKE.search(filt)
        if like:
            q = like.group(1).replace("''", "'").lower()
            return q in m.Subject.lower() or q in m.SenderName.lower() or q in m.SenderEmailAddress.lower()
        return True

    # Columns interface
    def RemoveAll(self) -> None:
        self.columns = []

    def Add(self, name: str) -> None:
        if name in self.folder.store.rejected_columns:
            raise FakeComError(-2147352567, f"The property '{name}' is unknown or cannot be found.")
        self.columns.append(name)

    def Sort(self, prop: str, descending: bool = False) -> None:
        self._rows.sort(key=lambda m: m.ReceivedTime, reverse=descending)
        self.folder.table_calls.append(f"Sort({prop},{descending})")

    def GetRowCount(self) -> int:
        return len(self._rows)

    @property
    def EndOfTable(self) -> bool:
        return self._pos >= len(self._rows)

    def _value(self, m: Mail, col: str) -> Any:
        match col:
            case "EntryID":
                return m.short_id
            case "Subject":
                return m.Subject
            case "SenderName":
                return m.SenderName
            case "SenderEmailAddress":
                return m.SenderEmailAddress or "None"
            case "ReceivedTime":
                return m.ReceivedTime.astimezone(UTC)
            case "UnRead":
                return m.UnRead
            case "FlagStatus":
                return m.FlagStatus
            case "MessageClass":
                return m.MessageClass
            case "Size":
                return m.Size
            case "http://schemas.microsoft.com/mapi/proptag/0x0E1B000B":
                return m.Attachments.Count > 0
            case "http://schemas.microsoft.com/mapi/proptag/0x30130102":
                return m.ConversationID
            case _:
                return None

    def GetArray(self, n: int) -> tuple[tuple[Any, ...], ...]:
        chunk = self._rows[self._pos : self._pos + n]
        self._pos += len(chunk)
        self.folder.table_calls.append(f"GetArray({n})")
        return tuple(tuple(self._value(m, c) for c in self.columns) for m in chunk)


class FolderCollection(Collection):
    """``Folder.Folders``: iterable like any collection, and ``Add(name)`` creates a real subfolder."""

    def __init__(self, folder: Folder) -> None:
        super().__init__(folder._subfolders)
        self._folder = folder
        self._items = folder._subfolders  # live view, not a copy

    def Add(self, value: Any) -> Any:
        if any(f.Name.lower() == str(value).lower() for f in self._folder._subfolders):
            raise FakeComError(-2147352567, "Cannot create the folder. A folder with this name already exists.")
        return self._folder.sub(str(value))


class Folder:
    def __init__(self, name: str, store: Store, *, default_item_type: int = 0, items: Iterable[Any] = ()) -> None:
        self.Name = name
        self.store = store
        self.Store = store
        self.DefaultItemType = default_item_type
        self.EntryID = f"FOLDER{store.short}{name.upper()}".encode().hex().upper().ljust(48, "0")
        self._items: list[Any] = []
        self._subfolders: list[Folder] = []
        self.parent: Folder | None = None
        self.item_calls: list[str] = []
        self.table_calls: list[str] = []
        for it in items:
            self.add(it)

    def add(self, item: Any) -> Any:
        self._items.append(item)
        if isinstance(item, Mail):
            item.Parent = self
        return item

    def sub(self, name: str, **kw: Any) -> Folder:
        f = Folder(name, self.store, **kw)
        f.parent = self
        self._subfolders.append(f)
        return f

    @property
    def Folders(self) -> FolderCollection:
        return FolderCollection(self)

    @property
    def FolderPath(self) -> str:
        parts: list[str] = []
        node: Folder | None = self
        while node is not None:
            parts.append(node.Name)
            node = node.parent
        return "\\\\" + "\\".join(reversed(parts))

    @property
    def Items(self) -> Items:
        return Items(self)

    @property
    def UnReadItemCount(self) -> int:
        return sum(1 for m in self._items if isinstance(m, Mail) and m.UnRead)

    def GetTable(self, filt: str | None = None) -> Table:
        self.table_calls.append(f"GetTable({filt})")
        return Table(self, filt)

    def walk(self) -> Iterable[Folder]:
        yield self
        for s in self._subfolders:
            yield from s.walk()


class Store:
    def __init__(self, display_name: str, short: str, *, exchange_type: int = 0, modern: bool = True) -> None:
        self.DisplayName = display_name
        self.short = short
        self.StoreID = f"STORE{short}".encode().hex().upper().ljust(48, "0")
        self.ExchangeStoreType = exchange_type
        self.modern = modern
        self.sent: list[Mail] = []
        self.default_calls: list[int] = []
        self.default_columns = ["EntryID", "Subject", "CreationTime", "LastModificationTime", "MessageClass"]
        self.rejected_columns: set[str] = set()
        self.dasl_leaks = False
        self.root = Folder(display_name, self)
        self.defaults: dict[int, Folder] = {}

    def GetRootFolder(self) -> Folder:
        return self.root

    def GetDefaultFolder(self, const: int) -> Folder:
        self.default_calls.append(const)
        try:
            return self.defaults[const]
        except KeyError:
            raise FakeComError(NOT_FOUND, "The operation failed. An object could not be found.") from None

    def folders(self) -> Iterable[Folder]:
        return self.root.walk()


class Account:
    def __init__(self, smtp: str, store: Store) -> None:
        self.SmtpAddress = smtp
        self.DisplayName = smtp
        self.DeliveryStore = store


class Namespace:
    def __init__(self, stores: list[Store], accounts: list[Account], default: Store) -> None:
        self._stores = stores
        self.Stores = Collection(stores)
        self.Accounts = Collection(accounts)
        self.DefaultStore = default
        self.get_item_calls: list[tuple[str, str | None]] = []
        self.fail_next: list[Exception] = []

    def _stores_for(self, store_id: str | None) -> list[Store]:
        if store_id is None:
            return [self.DefaultStore]
        return [s for s in self._stores if s.StoreID == store_id]

    def GetItemFromID(self, entry_id: str, store_id: str | None = None) -> Any:
        self.get_item_calls.append((entry_id, store_id))
        if self.fail_next:
            raise self.fail_next.pop(0)
        for store in self._stores_for(store_id):
            for folder in store.folders():
                for it in folder._items:
                    if entry_id in (getattr(it, "EntryID", None), getattr(it, "short_id", None)):
                        return it
        raise FakeComError(NOT_FOUND, "The message you specified cannot be found.")

    def GetFolderFromID(self, entry_id: str, store_id: str | None = None) -> Folder:
        for store in self._stores_for(store_id):
            for folder in store.folders():
                if folder.EntryID == entry_id:
                    return folder
        raise FakeComError(NOT_FOUND, "The folder cannot be found.")


class Application:
    def __init__(self, ns: Namespace) -> None:
        self._ns = ns
        self.created: list[Mail] = []
        self.dispatch_count = 0
        self.create_hook: Callable[[Mail], None] | None = None

    def GetNamespace(self, kind: str) -> Namespace:
        assert kind == "MAPI"
        return self._ns

    def CreateItem(self, item_type: int) -> Mail:
        assert item_type == 0
        mail = Mail("", "me@example.bg", datetime.now(UTC))
        mail.To = ""
        mail.Parent = self._ns.DefaultStore.defaults[6]
        if self.create_hook is not None:
            self.create_hook(mail)
        self.created.append(mail)
        return mail


class World:
    """One Exchange store (Bulgarian folder names) + one Gmail IMAP store, with fixture mail."""

    def __init__(self) -> None:
        Mail._counter = 0
        ex = Store("aapostolov@postbank.bg", "EX", exchange_type=0, modern=True)
        self.inbox = ex.root.sub("Входящи")
        self.sent = ex.root.sub("Изпратени елементи")
        self.drafts = ex.root.sub("Чернови")
        self.deleted = ex.root.sub("Изтрити елементи")
        self.calendar = ex.root.sub("Календар", default_item_type=1)
        self.tasks = ex.root.sub("Задачи", default_item_type=3)
        self.demands = self.inbox.sub("Demands")
        self.dm1234 = self.demands.sub("DM-1234")
        self.archive = ex.root.sub("Archive")
        ex.defaults = {6: self.inbox, 5: self.sent, 16: self.drafts, 3: self.deleted, 9: self.calendar, 13: self.tasks}

        base = datetime(2026, 9, 5, 8, 0, 0, tzinfo=UTC)
        self.mails: list[Mail] = []
        specs = [
            ("Invoice 4471 due", "billing@vendor.example", base + timedelta(minutes=50), {"unread": True}),
            (
                "Re: DM-1234 clarification",
                "pm@postbank.bg",
                base + timedelta(minutes=40),
                {"is_task": True},
            ),  # flagged the legacy way
            ("Weekly report", "boss@postbank.bg", base + timedelta(minutes=30), {"flag_status": 2}),
            ("Same-second A", "a@x.example", base + timedelta(minutes=20), {}),
            ("Same-second B", "b@x.example", base + timedelta(minutes=20), {}),
            ("Same-second C", "c@x.example", base + timedelta(minutes=20), {}),
            (
                "Old newsletter",
                "news@list.example",
                base - timedelta(days=3),
                {"html_body": "<html><body><p>Hello <b>there</b></p><p>Second para</p></body></html>"},
            ),
        ]
        for subject, sender, when, kw in specs:
            m = Mail(subject, sender, when, **kw)
            self.inbox.add(m)
            self.mails.append(m)
        self.mails[0].Attachments = Attachments([Attachment("invoice.pdf", 88_000)])
        self.mails[0].Body = "Please pay invoice 4471.\r\nRegards"

        gm = Store("arsen@gmail.com", "GM", exchange_type=3, modern=False)
        self.gm_inbox = gm.root.sub("Inbox")
        gm.defaults = {6: self.gm_inbox, 5: gm.root.sub("Sent")}
        gm.dasl_leaks = True
        self.gm_inbox.add(Mail("Gmail invoice", "shop@gmail.example", base + timedelta(minutes=5)))

        self.exchange = ex
        self.gmail = gm
        self.ns = Namespace([ex, gm], [Account("aapostolov@postbank.bg", ex), Account("arsen@gmail.com", gm)], ex)
        self.app = Application(self.ns)

    def dispatch(self) -> Application:
        self.app.dispatch_count += 1
        return self.app
