"""Unread across folders, Outlook-syntax search and threads with bodies, against the fake COM world."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fake_com import Mail, World

from jarvis_host.mailsearch import QueryError, date_range, parse_query, query, thread_full, unread
from jarvis_host.outlook import OutlookBackend

NOW = datetime(2026, 10, 2, 10, 30).astimezone()  # a Friday


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def backend(world: World) -> OutlookBackend:
    return OutlookBackend(world.dispatch)


# --- the syntax -----------------------------------------------------------------------------


def test_keywords_map_onto_dasl_properties():
    q = parse_query("from:maria subject:budget hasattachments:yes is:unread", NOW)
    assert q.conditions == [
        "(\"urn:schemas:httpmail:fromname\" LIKE '%maria%' OR \"urn:schemas:httpmail:fromemail\" LIKE '%maria%')",
        "(\"urn:schemas:httpmail:subject\" LIKE '%budget%')",
        '"urn:schemas:httpmail:hasattachment" = 1',
        '"urn:schemas:httpmail:read" = 0',
    ]
    assert q.verify == [("from", "maria"), ("subject", "budget")]


def test_plain_words_and_phrases_search_everything_and_quotes_are_escaped():
    q = parse_query('"budget 2027" o\'neil', NOW)
    assert len(q.conditions) == 2
    assert "textdescription\" LIKE '%budget 2027%'" in q.conditions[0]
    assert "LIKE '%o''neil%'" in q.conditions[1] and q.conditions[1].count(" OR ") == 5


def test_or_joins_neighbours_and_not_negates():
    q = parse_query("from:maria OR from:pete -subject:newsletter NOT is:read", NOW)
    assert q.conditions[0].startswith("((") and " OR (" in q.conditions[0]
    assert q.conditions[1].startswith("NOT (") and "newsletter" in q.conditions[1]
    assert q.conditions[2] == 'NOT ("urn:schemas:httpmail:read" = 1)'
    assert q.verify == [], "an OR makes a term optional, so no row check"


def test_dates_follow_outlooks_words_and_ranges():
    today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    assert date_range("today", NOW) == (today, today + timedelta(days=1))
    lo, hi = date_range("this week", NOW)
    assert lo.weekday() == 0 and (hi - lo).days == 7 and lo <= NOW < hi
    lo, hi = date_range("last month", NOW)
    assert (lo.month, lo.day, hi.month, hi.day) == (9, 1, 10, 1)
    assert date_range(">=2026-09-01", NOW)[1] is None
    lo, hi = date_range("2026-09-01..2026-09-30", NOW)
    assert (lo.day, hi.month, hi.day) == (1, 10, 1)
    q = parse_query("received:this week", NOW)
    assert q.has_date and "datereceived\" >=" in q.conditions[0] and "datereceived\" <" in q.conditions[0]
    assert parse_query("received:30.09.2026", NOW).has_date


def test_folder_narrows_the_scan_and_bad_queries_say_why():
    q = parse_query("folder:demands invoice", NOW)
    assert q.folders == ["demands"] and len(q.conditions) == 1
    with pytest.raises(QueryError, match="unknown keyword"):
        parse_query("color:red", NOW)
    with pytest.raises(QueryError, match="not a date"):
        parse_query("received:someday", NOW)
    with pytest.raises(QueryError, match="empty"):
        parse_query("   ", NOW)


# --- unread across folders ------------------------------------------------------------------------


def test_unread_reads_every_mail_folder_with_unread_items_and_names_the_folder(backend: OutlookBackend, world: World):
    t0 = world.mails[0].ReceivedTime
    world.dm1234.add(Mail("DM-1234 approval", "pm@postbank.bg", t0 + timedelta(hours=2), unread=True))
    world.archive.add(Mail("Archived but unread", "x@y.example", t0 + timedelta(hours=1), unread=True))
    world.deleted.add(Mail("Deleted unread", "x@y.example", t0 + timedelta(hours=3), unread=True))
    world.sent.add(Mail("Sent unread?", "me@example.bg", t0 + timedelta(hours=4), unread=True))
    res = unread(backend)
    assert [(i["subject"], i["folder"]) for i in res["items"]] == [
        ("DM-1234 approval", "Входящи/Demands/DM-1234"),
        ("Archived but unread", "Archive"),
        ("Invoice 4471 due", "Входящи"),
    ]
    assert res["folders_with_unread"] == 3
    # Folders with nothing unread are not read at all; calendars and tasks are not mail folders.
    assert world.demands.table_calls == [] and world.calendar.table_calls == []
    assert world.deleted.table_calls == [] and world.sent.table_calls == []


# --- search -----------------------------------------------------------------------------------------


def test_search_spans_folders_including_sent_but_not_deleted(backend: OutlookBackend, world: World):
    t0 = world.mails[0].ReceivedTime
    world.dm1234.add(Mail("Invoice for DM-1234", "pm@postbank.bg", t0 + timedelta(hours=1)))
    world.sent.add(Mail("RE: Invoice 4471 due", "aapostolov@postbank.bg", t0 + timedelta(hours=2)))
    world.deleted.add(Mail("Invoice spam", "spam@x.example", t0 + timedelta(hours=3)))
    res = query(backend, "subject:invoice")
    assert [(i["subject"], i["folder"]) for i in res["items"]] == [
        ("RE: Invoice 4471 due", "Изпратени елементи"),
        ("Invoice for DM-1234", "Входящи/Demands/DM-1234"),
        ("Invoice 4471 due", "Входящи"),
    ]
    assert world.deleted.table_calls == []
    assert "datereceived\" >=" in world.inbox.table_calls[0], "a year back unless a date is given"


def test_search_folder_keyword_and_the_gmail_dasl_leak(backend: OutlookBackend, world: World):
    res = query(backend, "folder:demands subject:invoice")
    assert res["items"] == [] and res["folders_scanned"] == 2  # Demands and DM-1234
    gm = query(backend, "subject:invoice", account="arsen@gmail.com")
    assert [i["subject"] for i in gm["items"]] == ["Gmail invoice"] and gm["unverified_dropped"] == 2  # one leak per Gmail folder


# --- threads ----------------------------------------------------------------------------------------


def test_thread_full_carries_each_body_even_when_the_table_preview_is_empty(backend: OutlookBackend, world: World):
    first = world.mails[0]
    reply = world.sent.add(Mail("RE: Invoice 4471 due", "aapostolov@postbank.bg", first.ReceivedTime + timedelta(hours=1)))
    reply.ConversationID = first.ConversationID
    reply.HTMLBody = "<p>Paid <b>today</b>.</p>"
    res = thread_full(backend, first.EntryID)
    bodies = [(i["subject"], i["body"]) for i in res["items"]]
    assert bodies == [("Invoice 4471 due", "Please pay invoice 4471.\r\nRegards"), ("RE: Invoice 4471 due", "Paid today.")]
    assert res["items"][0]["attachments"] == [{"name": "invoice.pdf", "size": 88_000}]
    world.exchange.conversations = False
    lone = thread_full(backend, first.EntryID)
    assert [i["body"] for i in lone["items"]] == ["Please pay invoice 4471.\r\nRegards"]
