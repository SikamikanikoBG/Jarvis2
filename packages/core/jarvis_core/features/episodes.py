"""Episodes: what happened, as a person would keep it (memory-track PROPOSAL §6.2).

A conversation is not a memory: it is 3,000 messages. What stays with a person is that on Friday
the talk with Gyuzelev was about A-Flow, who was there, what came of it and whether it was good
news. An episode is that: one stretch of one conversation about one thing, on one day. The day
journal writes them with the same background call that writes its line (features/journal.py): no
model call is added. The model decides whether the new turn goes on with the conversation's last
episode or begins another; the messages themselves stay where they are.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from jarvis_core.db import Database

if TYPE_CHECKING:
    import aiosqlite

#: What an episode meant to him. Anything the model writes outside it is neutral.
VALENCES = ("threat", "risk", "opportunity", "reward", "loss", "neutral")
TITLE_CHARS = 120
WHAT_CHARS = 1_500
NAMES_MAX = 12

#: What the writer of an episode is asked for, after the journal's own line.
ASK = """- episode: what a person would remember of this stretch of the conversation; null for a routine
  check that found nothing. An object:
  - continues: true when the latest runs go on with the last episode above (the same talk, the same
    thing done); false when they are about something else, or there is no episode yet.
  - title: what it was about, up to 10 words, with the names.
  - what: what happened, was said, decided, promised or felt, with names, dates and numbers; at most
    120 words. When it continues, the whole episode, not only the new part: keep what it already says.
  - people: everyone named in it but {user}, each once, in every spelling used ("Nikolay Gyuzelev (Ники Гюзелев)").
  - places: where, when a place was named.
  - importance: 1 routine, 2 ordinary, 3 he may ask about it, 4 matters for weeks, 5 for years.
  - valence: for {user}, one of threat, risk, opportunity, reward, loss, neutral."""


@dataclass(slots=True)
class Episode:
    conversation_id: str
    day: str
    started_at: datetime
    ended_at: datetime
    title: str
    what: str
    people: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)
    importance: int = 2
    valence: str = "neutral"
    first_run_id: str | None = None
    last_run_id: str | None = None
    id: int | None = None


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        name = " ".join(str(v).split())[:80]
        if name and name not in out:
            out.append(name)
    return out[:NAMES_MAX]


def from_model(
    data: Any,
    *,
    conversation_id: str,
    day: str,
    started_at: datetime,
    ended_at: datetime,
    first_run_id: str,
    last_run_id: str,
    last: Episode | None,
) -> Episode | None:
    """The episode the model described, joined to the last one when it continues it. None when it
    described none (routine) or nothing usable: what is not certain is not kept, and the messages are there."""
    if not isinstance(data, dict):
        return None
    title = " ".join(str(data.get("title") or "").split())[:TITLE_CHARS]
    what = " ".join(str(data.get("what") or "").split())[:WHAT_CHARS]
    if not title or not what:
        return None
    try:
        importance = min(5, max(1, int(data.get("importance") or 2)))
    except (TypeError, ValueError):
        importance = 2
    valence = str(data.get("valence") or "").strip().lower()
    episode = Episode(
        conversation_id=conversation_id,
        day=day,
        started_at=started_at,
        ended_at=ended_at,
        title=title,
        what=what,
        people=_names(data.get("people")),
        places=_names(data.get("places")),
        importance=importance,
        valence=valence if valence in VALENCES else "neutral",
        first_run_id=first_run_id,
        last_run_id=last_run_id,
    )
    if last is not None and data.get("continues") is True:
        episode.id = last.id
        episode.started_at, episode.first_run_id = last.started_at, last.first_run_id
        # A name heard once stays, whatever the rewrite leaves out.
        episode.people = _names([*last.people, *episode.people])
        episode.places = _names([*last.places, *episode.places])
        episode.importance = max(last.importance, episode.importance)
    return episode


class EpisodeStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def of_day(self, conversation_id: str, day: str) -> list[Episode]:
        rows = await self.db.fetchall(
            "SELECT * FROM episodes WHERE conversation_id = ? AND day = ? ORDER BY started_at, id",
            (conversation_id, day),
        )
        return [_episode(r) for r in rows]

    async def save(self, e: Episode) -> None:
        values = (
            e.day,
            e.started_at.astimezone(UTC).isoformat(),
            e.ended_at.astimezone(UTC).isoformat(),
            e.title,
            e.what,
            json.dumps(e.people, ensure_ascii=False),
            json.dumps(e.places, ensure_ascii=False),
            e.importance,
            e.valence,
            e.first_run_id,
            e.last_run_id,
            datetime.now(UTC).isoformat(),
        )
        if e.id is not None:
            await self.db.execute(
                "UPDATE episodes SET day = ?, started_at = ?, ended_at = ?, title = ?, what = ?, people = ?,"
                " places = ?, importance = ?, valence = ?, first_run_id = ?, last_run_id = ?, updated_at = ?"
                " WHERE id = ?",
                (*values, e.id),
            )
            return
        await self.db.execute(
            "INSERT INTO episodes(day, started_at, ended_at, title, what, people, places, importance, valence,"
            " first_run_id, last_run_id, updated_at, conversation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*values, e.conversation_id),
        )

    async def search(
        self,
        query: str,
        *,
        lo: str | None,
        hi: str | None,
        limit: int,
        exclude_run: str | None = None,
        not_kinds: tuple[str, ...] = (),
    ) -> list[aiosqlite.Row]:
        """Episodes with their conversation's title; ``query`` in FTS5 syntax, empty = the window's
        episodes, oldest first, without the conversations of ``not_kinds``. A private chat never appears."""
        where = ["c.incognito = 0"]
        params: list[object] = []
        if query:
            where.append("episodes_fts MATCH ?")
            params.append(query)
        elif not_kinds:
            where.append(f"c.kind NOT IN ({','.join('?' * len(not_kinds))})")
            params.extend(not_kinds)
        # An episode is in the window when any part of it is.
        if lo:
            where.append("e.ended_at >= ?")
            params.append(lo)
        if hi:
            where.append("e.started_at < ?")
            params.append(hi)
        if exclude_run:
            where.append("(e.first_run_id IS NULL OR e.first_run_id <> ?)")
            params.append(exclude_run)
        source = "episodes_fts JOIN episodes e ON e.id = episodes_fts.rowid" if query else "episodes e"
        order = "bm25(episodes_fts, 3.0, 1.0, 2.0, 1.0), e.id DESC" if query else "e.started_at, e.id"
        return await self.db.fetchall(
            f"SELECT e.*, c.title AS conversation FROM {source}"
            " JOIN conversations c ON c.id = e.conversation_id"
            f" WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
            (*params, limit),
        )


def _episode(row: Any) -> Episode:
    def at(value: str) -> datetime:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)

    return Episode(
        conversation_id=row["conversation_id"],
        day=row["day"],
        started_at=at(row["started_at"]),
        ended_at=at(row["ended_at"]),
        title=row["title"],
        what=row["what"],
        people=list(json.loads(row["people"] or "[]")),
        places=list(json.loads(row["places"] or "[]")),
        importance=row["importance"],
        valence=row["valence"],
        first_run_id=row["first_run_id"],
        last_run_id=row["last_run_id"],
        id=row["id"],
    )
