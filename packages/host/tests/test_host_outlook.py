"""OutlookBackend against the fake COM model: the V1 rules as tests."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from fake_com import CALL_REJECTED, SERVER_UNAVAILABLE, Appointment, FakeComError, World

from jarvis_host.com import ComTimeout, ComWorker
from jarvis_host.outlook import (
    FOLDER_INBOX,
    NotFound,
    OutlookBackend,
    OutlookError,
    OutlookService,
    decode_cursor,
    html_to_text,
    merge_reply_html,
    parse_when,
)


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def backend(world: World) -> OutlookBackend:
    return OutlookBackend(world.dispatch)


# --- accounts & folders ---------------------------------------------------------------------


def test_accounts_list_stores_with_default_folders_by_id_not_name(backend: OutlookBackend, world: World):
    accounts = backend.accounts()
    assert [a["name"] for a in accounts] == ["aapostolov@postbank.bg", "arsen@gmail.com"]
    ex = accounts[0]
    assert ex["default"] is True and ex["type"] == "exchange_primary" and ex["smtp"] == "aapostolov@postbank.bg"
    assert ex["folders"]["inbox"] == {"id": world.inbox.EntryID, "name": "Входящи"}
    assert ex["folders"]["sent"]["name"] == "Изпратени елементи"
    assert "calendar" in ex["folders"] and "tasks" in ex["folders"]
    assert FOLDER_INBOX in world.exchange.default_calls
    gm = accounts[1]
    assert gm["type"] == "other" and gm["default"] is False and "calendar" not in gm["folders"]


def test_allow_list_hides_stores(world: World):
    backend = OutlookBackend(world.dispatch, accounts_allow=["arsen@gmail.com"])
    assert [a["name"] for a in backend.accounts()] == ["arsen@gmail.com"]
    with pytest.raises(NotFound, match=r"not found; available: \['arsen@gmail.com'\]"):
        backend.list_items("aapostolov@postbank.bg")


def test_account_resolution_exact_smtp_and_partial(backend: OutlookBackend, world: World):
    assert backend._store("").DisplayName == "aapostolov@postbank.bg"  # default store
    assert backend._store("ARSEN@GMAIL.COM").DisplayName == "arsen@gmail.com"
    assert backend._store("gmail").DisplayName == "arsen@gmail.com"
    with pytest.raises(NotFound):
        backend._store("hotmail")


def test_folder_resolution_uses_get_default_folder_never_english_names(backend: OutlookBackend, world: World):
    store = world.exchange
    for spec in ("inbox", "Inbox", "INBOX", " inbox "):
        assert backend._folder(store, spec) is world.inbox
    assert backend._folder(store, "sent") is world.sent
    assert backend._folder(store, "deleted") is world.deleted
    assert backend._folder(store, "Deleted Items") is world.deleted
    assert backend._folder(store, "Trash") is world.deleted
    assert set(store.default_calls) >= {6, 5, 3}


def test_folder_resolution_by_path_and_id(backend: OutlookBackend, world: World):
    store = world.exchange
    assert backend._folder(store, "Demands/DM-1234") is world.dm1234  # under the inbox, unqualified
    assert backend._folder(store, "inbox/Demands/DM-1234") is world.dm1234
    assert backend._folder(store, "Входящи\\Demands") is world.demands  # localised path from outlook_folders
    assert backend._folder(store, "Archive") is world.archive  # top-level under the store root
    assert backend._folder(store, world.dm1234.EntryID) is world.dm1234
    with pytest.raises(NotFound, match=r"no 'Nope' under .*children: \['Demands'\]"):
        backend._folder(store, "inbox/Nope")


def test_folder_create_adds_only_the_missing_tail(backend: OutlookBackend, world: World):
    """Triage files DM-9999 before anyone made its folder: create the tail, never a new top-level tree."""
    store = world.exchange
    before = [f.Name for f in world.demands._subfolders]
    made = backend._folder(store, "Demands/DM-9999", create=True)
    assert made.Name == "DM-9999" and made.parent is world.demands
    assert [f.Name for f in world.demands._subfolders] == [*before, "DM-9999"]
    # Idempotent: resolving again returns the same folder, no duplicate.
    assert backend._folder(store, "Demands/DM-9999", create=True) is made
    assert backend._folder(store, "Входящи/Demands/DM-9999") is made
    # An unknown FIRST segment is still an error even with create=True (a typo must not grow a tree).
    with pytest.raises(NotFound):
        backend._folder(store, "Demnads/DM-1", create=True)
    # Without create the old behaviour holds.
    with pytest.raises(NotFound):
        backend._folder(store, "Demands/DM-7777")


def test_folder_create_builds_under_the_inbox_and_is_idempotent(backend: OutlookBackend, world: World):
    store = world.exchange
    res = backend.folder_create("Important/Action", "")
    assert res["created"] == ["Important", "Action"]
    important = backend._folder(store, "inbox/Important")
    assert important.parent is world.inbox and backend._folder(store, "Important/Action").parent is important
    # Again: nothing created, same folder.
    again = backend.folder_create("Important/Action", "")
    assert again["created"] == [] and again["folder_id"] == res["folder_id"]
    # An existing top-level tree is reused, not duplicated under the inbox.
    top = backend.folder_create("Archive/2026", "")
    assert top["created"] == ["2026"] and backend._folder(store, "Archive/2026").parent is world.archive
    # Existing inbox children are reused too.
    assert backend.folder_create("Demands/DM-1234", "")["created"] == []
    with pytest.raises(OutlookError):
        backend.folder_create("   ", "")


def test_folders_tree_paths_and_roles(backend: OutlookBackend, world: World):
    tree = backend.folders("")
    assert tree["account"] == "aapostolov@postbank.bg"
    by_path = {f["path"]: f for f in tree["folders"]}
    assert by_path["Входящи"]["default"] == "inbox" and by_path["Входящи"]["kind"] == "mail"
    assert by_path["Входящи"]["unread"] == 1 and by_path["Входящи"]["total"] == 7
    assert by_path["Входящи/Demands/DM-1234"]["id"] == world.dm1234.EntryID
    assert by_path["Календар"]["kind"] == "calendar" and by_path["Календар"]["default"] == "calendar"


# --- listing ---------------------------------------------------------------------------------


def test_list_reads_one_table_pass_newest_first(backend: OutlookBackend, world: World):
    page = backend.list_items("", "inbox", limit=3)
    assert page["folder"].endswith("\\Входящи") and page["total"] == 7
    assert [i["subject"] for i in page["items"]] == ["Invoice 4471 due", "Re: DM-1234 clarification", "Weekly report"]
    first = page["items"][0]
    assert first["entry_id"] == world.mails[0].short_id, "listings hand out the table's short-term id"
    assert first["store_id"] == world.exchange.StoreID
    assert first["unread"] is True and first["has_attachments"] is True and first["kind"] == "mail"
    assert first["from"] == {"name": "billing", "address": "billing@vendor.example"}
    assert first["received"].startswith("2026-09-05T") and "+" in first["received"]
    assert page["items"][2]["flagged"] is True  # FlagStatus 2
    assert page["items"][1]["flagged"] is False  # IsMarkedAsTask alone is not visible in a table; outlook_read tells
    assert page["cursor"] is not None
    assert world.inbox.table_calls[0] == "GetTable(None)"
    assert not any(c.startswith("Restrict") for c in world.inbox.item_calls), "listings never walk Items"


def test_cursor_pages_through_everything_exactly_once_even_across_a_same_second_boundary(
    backend: OutlookBackend, world: World
):
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = backend.list_items("", "inbox", cursor=cursor, limit=2)
        pages += 1
        seen.extend(i["subject"] for i in page["items"])
        cursor = page["cursor"]
        if cursor is None:
            break
        assert pages < 10
    assert seen == [m.Subject for m in sorted(world.mails, key=lambda m: m.ReceivedTime, reverse=True)]
    assert pages == 4
    # the second page's cursor sat on the 08:20:00 second shared by three mails
    page1 = backend.list_items("", "inbox", limit=4)
    before, skip = decode_cursor(page1["cursor"])
    assert before == datetime(2026, 9, 5, 8, 20, tzinfo=UTC) and skip == {world.mails[3].short_id}


def test_since_is_a_lower_bound_in_utc(backend: OutlookBackend, world: World):
    page = backend.list_items("", "inbox", since="2026-09-05T08:35:00+00:00")
    assert [i["subject"] for i in page["items"]] == ["Invoice 4471 due", "Re: DM-1234 clarification"]
    assert page["cursor"] is None and page["total"] == 2
    page = backend.list_items("", "inbox", since="2026-09-05T11:35+03:00")  # same instant, local offset
    assert len(page["items"]) == 2
    with pytest.raises(OutlookError, match="not an ISO-8601"):
        backend.list_items("", "inbox", since="yesterday")


def test_a_store_that_rejects_a_column_still_lists(backend: OutlookBackend, world: World):
    world.exchange.rejected_columns = {"Size"}
    page = backend.list_items("", "inbox", limit=2)
    assert len(page["items"]) == 2 and "size" not in page["items"][0]
    assert page["items"][0]["subject"] == "Invoice 4471 due"


def test_search_verifies_every_match_and_drops_dasl_leaks(backend: OutlookBackend, world: World):
    res = backend.search("gmail", "invoice", days_back=30)
    assert [i["subject"] for i in res["items"]] == ["Gmail invoice"]
    assert res["unverified_dropped"] == 1 and res["matched_on"] == ["subject", "from"]
    res = backend.search("", "postbank", days_back=30)
    assert {i["subject"] for i in res["items"]} == {"Re: DM-1234 clarification", "Weekly report"}
    with pytest.raises(OutlookError, match="query is empty"):
        backend.search("", "  ")


# --- single items ------------------------------------------------------------------------------


def test_read_returns_the_long_term_id_and_a_text_body(backend: OutlookBackend, world: World):
    listed = backend.list_items("", "inbox", limit=1)["items"][0]
    msg = backend.read(listed["entry_id"])
    assert msg["entry_id"] == world.mails[0].EntryID and msg["entry_id"] != listed["entry_id"]
    assert msg["subject"] == "Invoice 4471 due" and msg["body"].startswith("Please pay invoice 4471.")
    assert msg["attachments"] == [{"name": "invoice.pdf", "size": 88_000}]
    assert msg["folder"].endswith("\\Входящи") and msg["account"] == "aapostolov@postbank.bg"
    assert world.ns.get_item_calls[-1] == (listed["entry_id"], world.exchange.StoreID)


def test_read_converts_html_when_body_is_empty_and_trusts_is_marked_as_task(backend: OutlookBackend, world: World):
    newsletter = backend.read(world.mails[6].EntryID)
    assert newsletter["body"] == "Hello there\n\nSecond para"
    legacy = backend.read(world.mails[1].short_id)
    assert legacy["flag_status"] == 0 and legacy["is_task"] is True and legacy["flagged"] is True


def test_read_unknown_id_is_not_found(backend: OutlookBackend):
    with pytest.raises(NotFound, match="not found in"):
        backend.read("F" * 48)


def test_move_re_resolves_the_short_term_id_and_returns_the_new_long_term_id(backend: OutlookBackend, world: World):
    short = backend.list_items("", "inbox", limit=1)["items"][0]["entry_id"]
    res = backend.move(short, "Demands/DM-1234")
    assert (short, world.exchange.StoreID) in world.ns.get_item_calls
    assert world.mails[0].Parent is world.dm1234 and world.mails[0] not in world.inbox._items
    assert res["entry_id"] == world.mails[0].EntryID and res["entry_id"].endswith("MOVED000")
    assert res["previous_entry_id"] == short and res["folder"].endswith("\\Demands\\DM-1234")
    with pytest.raises(NotFound):
        backend.move("0" * 48, "inbox")


def test_flag_uses_mark_as_task_and_unflag_clears_the_task_flag(backend: OutlookBackend, world: World):
    plain = world.mails[3]
    res = backend.flag(plain.short_id, True)
    assert plain.calls == ["MarkAsTask(0)"] and plain.saved == 1
    assert res == {"entry_id": plain.EntryID, "flagged": True, "flag_status": 2, "is_task": True}

    legacy = world.mails[1]  # IsMarkedAsTask=True, FlagStatus=0 — FlagStatus alone says "not flagged"
    res = backend.flag(legacy.EntryID, False)
    assert legacy.calls[0] == "ClearTaskFlag" and legacy.IsMarkedAsTask is False and legacy.FlagStatus == 0
    assert res["flagged"] is False and res["is_task"] is False and legacy.saved == 1


def test_send_new_mail_sets_subject_account_and_semicolon_recipients(backend: OutlookBackend, world: World):
    res = backend.send("", "a@x.example, b@y.example", "Hello", "Body text", cc="c@z.example")
    mail = world.app.created[-1]
    assert mail.To == "a@x.example;b@y.example" and mail.CC == "c@z.example" and mail.Subject == "Hello"
    assert mail.Body == "Body text" and mail.sent and mail.SendUsingAccount.SmtpAddress == "aapostolov@postbank.bg"
    assert res["sent"] is True and res["threaded"] is False and res["to"] == "a@x.example;b@y.example"
    with pytest.raises(OutlookError, match="no recipient"):
        backend.send("", "", "x", "y")


def test_send_reply_keeps_the_threaded_subject_and_quotes_the_original(backend: OutlookBackend, world: World):
    original = world.mails[0]
    res = backend.send("", "", "Completely different subject", "Thanks,\nArsen", reply_to_entry_id=original.short_id)
    sent = world.exchange.sent[-1]
    assert sent.Subject == "RE: Invoice 4471 due" and res["subject"] == "RE: Invoice 4471 due"
    assert "fork" in res["note"] and res["threaded"] is True and res["conversation_id"] == original.ConversationID
    assert sent.To == "billing@vendor.example", "Reply() fills the recipient when `to` is empty"
    body = sent.HTMLBody
    assert body.index("Thanks,<br>") < body.index("original text"), "our text sits above the quoted thread"
    assert body.count("<body") == 1


# --- calendar ------------------------------------------------------------------------------------


def test_calendar_list_sorts_then_includes_recurrences_then_restricts(backend: OutlookBackend, world: World):
    now = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    world.calendar.add(Appointment("Standup", now, now + timedelta(minutes=15)))
    world.calendar.add(Appointment("Not an appointment", now, now, cls=53))
    res = backend.calendar_list("", days=7)
    assert res["calendar"] == "Календар" and res["source"] == "restrict"
    assert [e["subject"] for e in res["events"]] == ["Standup"]
    ev = res["events"][0]
    assert ev["start"].startswith(now.strftime("%Y-%m-%dT10:00")) and ev["attendees"] == ["Ana", "Boris"]
    assert ev["busy"] == "busy" and ev["location"] == "Room 1" and ev["organizer"] == "Boss"
    calls = world.calendar.item_calls
    assert calls.index("Sort([Start])") < calls.index("Restrict") and "Restrict-before-IncludeRecurrences" not in calls
    with pytest.raises(NotFound, match="has no calendar"):
        backend.calendar_list("gmail")


def _appt(
    subject: str,
    hour: int,
    minutes: int = 30,
    *,
    days: int = 1,
    status: int = 0,
    response: int = 0,
    organizer: str = "",
    busy: int = 2,
) -> Appointment:
    """An appointment `days` ahead at `hour`:00 (weekday-shifted so work-hour logic applies)."""
    start = datetime.now().astimezone().replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)
    while start.weekday() >= 5:
        start += timedelta(days=1)
    a = Appointment(subject, start, start + timedelta(minutes=minutes))
    a.MeetingStatus, a.ResponseStatus, a.BusyStatus = status, response, busy
    a.organizer_address = organizer
    a.Organizer = organizer.split("@", maxsplit=1)[0].removeprefix("x500:") or "me"
    return a


def test_calendar_invites_lists_unanswered_with_committed_conflicts_only(backend: OutlookBackend, world: World):
    committed = _appt("Sprint review", 10, 60, status=3, response=3)  # received + accepted → committed
    invite = _appt("Budget sync", 10, 30, status=3, response=5, organizer="x500:maria@postbank.bg")
    other_pending = _appt("Maybe lunch", 10, 30, status=3, response=2, organizer="pete@postbank.bg")
    free_invite = _appt("1:1", 15, 30, status=3, response=5, organizer="boss@postbank.bg")
    series_dup = _appt("Budget sync", 10, 30, days=8, status=3, response=5, organizer="x500:maria@postbank.bg")
    own = _appt("Focus", 14, 60)  # own appointment: committed
    for a in (committed, invite, other_pending, free_invite, series_dup, own):
        world.calendar.add(a)
    res = backend.calendar_invites("", 14)
    assert [(i["subject"], i["response_status"]) for i in res["invites"]] == [
        ("Budget sync", "not_responded"),
        ("Maybe lunch", "tentative"),
        ("1:1", "not_responded"),
    ]
    budget, lunch, one = res["invites"]
    # Exchange-only organizer resolved through GetExchangeUser; plain SMTP taken as is.
    assert budget["organizer_address"] == "maria@postbank.bg" and lunch["organizer_address"] == "pete@postbank.bg"
    # Only the ACCEPTED meeting clashes - not the other unanswered invite, not the invite itself.
    assert [c["subject"] for c in budget["conflicts"]] == ["Sprint review"]
    assert [c["subject"] for c in lunch["conflicts"]] == ["Sprint review"]
    assert one["conflicts"] == [] and one["recurring"] is False
    assert res["items_checked"] >= 6


def test_calendar_respond_sends_and_a_decline_removes_the_appointment(backend: OutlookBackend, world: World):
    from jarvis_host.outlook import OutlookError

    invite = _appt("Budget sync", 10, 30, status=3, response=5, organizer="maria@postbank.bg")
    clash = _appt("Clash", 11, 30, status=3, response=5, organizer="pete@postbank.bg")
    world.calendar.add(invite)
    world.calendar.add(clash)
    res = backend.calendar_respond(invite.EntryID, "accept", "", "")
    assert res["sent"] is True and res["decision"] == "accept" and res["subject"] == "Budget sync"
    assert invite.ResponseStatus == 3 and invite.responses[-1].sent and not invite.deleted
    res = backend.calendar_respond(clash.EntryID, "decline", "Свободен съм утре 9:00", "")
    assert res["sent"] is True and clash.deleted and clash.responses[-1].Body.startswith("Свободен съм")
    with pytest.raises(OutlookError, match=r"accept \| tentative \| decline"):
        backend.calendar_respond(invite.EntryID, "maybe", "", "")


def test_calendar_free_slots_ignores_pending_invites_and_keeps_work_hours(backend: OutlookBackend, world: World):
    day = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)

    def at(h: int, m: int = 0) -> datetime:
        return day.replace(hour=h, minute=m)

    committed = Appointment("Committed", at(9), at(10))
    committed.MeetingStatus, committed.ResponseStatus = 3, 3
    pending = Appointment("Pending", at(10), at(10, 30))
    pending.MeetingStatus, pending.ResponseStatus = 3, 5
    own = Appointment("Own", at(11), at(12))
    for a in (committed, pending, own):
        world.calendar.add(a)
    res = backend.calendar_free_slots("", day.isoformat(), 1, 60, 9, 13, 5)
    # 09 busy; 10-11 free (an unanswered invite does not block); 11 busy; 12-13 free; 13 = end of day.
    assert [s["start"][11:16] for s in res["slots"]] == ["10:00", "12:00"]
    assert res["busy_considered"] == 2 and res["duration_min"] == 60


def test_calendar_remove_canceled_deletes_only_cancelled_meetings(backend: OutlookBackend, world: World):
    keep = _appt("Keep", 9, 30, status=3, response=3)
    gone1 = _appt("Cancelled by organizer", 10, 30, status=7)
    gone2 = _appt("Own cancelled", 11, 30, status=5)
    for a in (keep, gone1, gone2):
        world.calendar.add(a)
    res = backend.calendar_remove_canceled("", 1, 60)
    assert res["removed"] == 2 and gone1.deleted and gone2.deleted and not keep.deleted
    assert [i["subject"] for i in res["items"]] == ["Cancelled by organizer", "Own cancelled"]
    assert res["failures"] == []


def test_calendar_create_saves_without_sending_unless_asked(backend: OutlookBackend, world: World):
    res = backend.calendar_create(
        "", "Planning", "2026-09-08T10:00", "", location="Sofia", attendees="ana@x.example; boris@x.example"
    )
    appt = world.calendar._items[-1]
    assert appt.Subject == "Planning" and appt.Start == "2026-09-08 10:00" and appt.End == "2026-09-08 11:00"
    assert appt.MeetingStatus == 1 and appt.Recipients.Count == 2 and appt.saved == 1 and not appt.sent
    assert res["attendees"] == ["ana@x.example", "boris@x.example"] and res["invites_sent"] is False
    assert res["end"].startswith("2026-09-08T11:00")
    res = backend.calendar_create(
        "", "Sync", "2026-09-08T12:00", "2026-09-08T12:30", attendees="ana@x.example", send_invites=True
    )
    assert world.calendar._items[-1].sent and res["invites_sent"] is True
    with pytest.raises(OutlookError, match="is not after start"):
        backend.calendar_create("", "Bad", "2026-09-08T12:00", "2026-09-08T11:00")
    assert "Add(1)" in world.calendar.item_calls


# --- helpers -------------------------------------------------------------------------------------


def test_helpers():
    assert html_to_text("<div>a<br>b</div><script>x()</script><p>c &amp; d</p>") == "a\nb\n\nc & d"
    merged = merge_reply_html("<html><body class=x><div>old</div></body></html>", "<body><p>new</p></body>")
    assert merged == "<html><body class=x><p>new</p><div>old</div></body></html>"
    assert merge_reply_html("", "<p>new</p>") == "<p>new</p>"
    assert parse_when("2026-09-05").tzinfo is not None
    assert parse_when("2026-09-05T08:00:00Z") == datetime(2026, 9, 5, 8, tzinfo=UTC)


# --- the service through the COM worker ----------------------------------------------------------


@pytest.fixture
def service(world: World):
    worker = ComWorker(init=None)
    worker.start()
    yield OutlookService(OutlookBackend(world.dispatch), worker)
    worker.stop()


async def test_service_marshals_through_the_worker(service: OutlookService, world: World):
    accounts = await service.call("accounts")
    assert accounts[0]["name"] == "aapostolov@postbank.bg"
    assert world.app.dispatch_count == 1
    await service.call("folders", "")
    assert world.app.dispatch_count == 1, "the namespace is dispatched once and reused"


async def test_service_retries_transient_reads_and_reconnects_after_a_disconnect(service: OutlookService, world: World):
    world.ns.fail_next = [FakeComError(CALL_REJECTED, "Call was rejected by callee.")]
    msg = await service.call("read", world.mails[0].EntryID)
    assert msg["subject"] == "Invoice 4471 due"

    world.ns.fail_next = [FakeComError(SERVER_UNAVAILABLE, "The RPC server is unavailable.")]
    with pytest.raises(OutlookError, match=r"not reachable.*next call reconnects"):
        await service.call("move", world.mails[0].EntryID, "Archive")  # mutations are never retried
    assert service.backend._ns is None
    await service.call("accounts")
    assert world.app.dispatch_count == 2


async def test_service_times_out_without_blocking_later_calls(service: OutlookService, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(OutlookService.TIMEOUTS, "accounts", 0.1)

    def slow_accounts() -> list[dict[str, str]]:
        time.sleep(0.4)
        return [{"name": "late"}]

    monkeypatch.setattr(service.backend, "accounts", slow_accounts)
    with pytest.raises(ComTimeout):
        await service.call("accounts")
    assert service.worker.status().busy
    assert (await service.call("ping"))["connected"] is True
