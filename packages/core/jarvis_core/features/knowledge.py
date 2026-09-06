"""Knowledge graph: entities, aliases, edges, mentions; learned in the background."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from jarvis_core.db import Database
from jarvis_core.engine.bus import EventBus
from jarvis_core.models.base import ModelAdapter, ModelTextChunk
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import Edge, Entity, EntityDetail, Graph, Message, ToolResult, new_id
from jarvis_proto.events import KgChanged
from jarvis_proto.features import EdgeWithOther, EntityType, Mention

log = logging.getLogger(__name__)

_TYPES: set[str] = {"person", "org", "project", "place", "thing", "topic"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class KnowledgeStore:
    def __init__(self, db: Database, bus: EventBus | None = None) -> None:
        self.db = db
        self.bus = bus

    def _changed(self, ids: list[str]) -> None:
        if self.bus is not None and ids:
            self.bus.publish(KgChanged(entity_ids=ids))

    # --- read -------------------------------------------------------------------------

    async def _entity(self, row: Any) -> Entity:
        aliases = await self.db.fetchall(
            "SELECT alias FROM kg_aliases WHERE entity_id = ? ORDER BY alias", (row["id"],)
        )
        return Entity(
            id=row["id"],
            name=row["name"],
            type=row["type"] if row["type"] in _TYPES else "thing",
            summary=row["summary"],
            aliases=[a["alias"] for a in aliases],
            mention_count=row["mention_count"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def get(self, entity_id: str) -> Entity | None:
        row = await self.db.fetchone("SELECT * FROM kg_entities WHERE id = ?", (entity_id,))
        return await self._entity(row) if row else None

    async def find(self, name: str) -> Entity | None:
        """Exact name or alias match, case-insensitive."""
        row = await self.db.fetchone("SELECT * FROM kg_entities WHERE name = ? COLLATE NOCASE", (name.strip(),))
        if row is None:
            alias = await self.db.fetchone("SELECT entity_id FROM kg_aliases WHERE alias = ?", (name.strip(),))
            if alias is None:
                return None
            row = await self.db.fetchone("SELECT * FROM kg_entities WHERE id = ?", (alias["entity_id"],))
        return await self._entity(row) if row else None

    async def search(self, q: str, *, limit: int = 50) -> list[Entity]:
        if q.strip():
            like = f"%{q.strip()}%"
            rows = await self.db.fetchall(
                "SELECT DISTINCT e.* FROM kg_entities e LEFT JOIN kg_aliases a ON a.entity_id = e.id"
                " WHERE e.name LIKE ? COLLATE NOCASE OR a.alias LIKE ? COLLATE NOCASE"
                " ORDER BY e.mention_count DESC, e.name LIMIT ?",
                (like, like, limit),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM kg_entities ORDER BY mention_count DESC, updated_at DESC LIMIT ?", (limit,)
            )
        return [await self._entity(r) for r in rows]

    async def detail(self, entity_id: str) -> EntityDetail | None:
        entity = await self.get(entity_id)
        if entity is None:
            return None
        rows = await self.db.fetchall(
            "SELECT * FROM kg_edges WHERE src = ? OR dst = ? ORDER BY weight DESC LIMIT 100", (entity_id, entity_id)
        )
        edges: list[EdgeWithOther] = []
        for r in rows:
            other_id = r["dst"] if r["src"] == entity_id else r["src"]
            other = await self.get(other_id)
            if other is None:
                continue
            edges.append(
                EdgeWithOther(
                    src=r["src"],
                    dst=r["dst"],
                    relation=r["relation"],
                    weight=r["weight"],
                    evidence=r["evidence"],
                    other=other,
                )
            )
        mrows = await self.db.fetchall(
            "SELECT * FROM kg_mentions WHERE entity_id = ? ORDER BY at DESC LIMIT 20", (entity_id,)
        )
        mentions = [
            Mention(
                conversation_id=m["conversation_id"],
                message_id=m["message_id"],
                snippet=m["snippet"],
                at=datetime.fromisoformat(m["at"]),
            )
            for m in mrows
        ]
        return EntityDetail(**entity.model_dump(), edges=edges, mentions=mentions)

    async def graph(self, *, center: str | None = None, depth: int = 1, limit: int = 80) -> Graph:
        if center:
            # Insertion-ordered, so trimming to `limit` drops the OUTSIDE of the neighbourhood.
            # A set gave no order at all: a graph centred on an entity with more neighbours than
            # `limit` usually did not contain that entity, and the UI drew a ring around nothing.
            ids: dict[str, None] = {center: None}
            frontier = {center}
            for _ in range(max(1, depth)):
                if not frontier or len(ids) >= limit:
                    break
                marks = ",".join("?" for _ in frontier)
                rows = await self.db.fetchall(
                    f"SELECT src, dst FROM kg_edges WHERE src IN ({marks}) OR dst IN ({marks})",
                    [*frontier, *frontier],
                )
                nxt = {r["src"] for r in rows} | {r["dst"] for r in rows}
                frontier = nxt - ids.keys()
                for eid in sorted(frontier):  # stable: the same query gives the same picture
                    ids[eid] = None
            id_list = list(ids)[:limit]
        else:
            rows = await self.db.fetchall("SELECT id FROM kg_entities ORDER BY mention_count DESC LIMIT ?", (limit,))
            id_list = [r["id"] for r in rows]
        if not id_list:
            return Graph(nodes=[], edges=[])
        marks = ",".join("?" for _ in id_list)
        nodes = [
            await self._entity(r)
            for r in await self.db.fetchall(f"SELECT * FROM kg_entities WHERE id IN ({marks})", id_list)
        ]
        erows = await self.db.fetchall(
            f"SELECT * FROM kg_edges WHERE src IN ({marks}) AND dst IN ({marks})", [*id_list, *id_list]
        )
        edges = [
            Edge(src=r["src"], dst=r["dst"], relation=r["relation"], weight=r["weight"], evidence=r["evidence"])
            for r in erows
        ]
        return Graph(nodes=nodes, edges=edges)

    async def match_in_text(self, text: str, *, limit: int = 8) -> list[Entity]:
        """Entities whose name or alias literally occurs in ``text`` (structural, no model)."""
        if not text.strip():
            return []
        rows = await self.db.fetchall(
            "SELECT e.id, e.name AS term FROM kg_entities e UNION ALL SELECT a.entity_id AS id, a.alias AS term FROM kg_aliases a"
        )
        lowered = text.lower()
        hits: dict[str, int] = {}
        for r in rows:
            term = str(r["term"]).strip()
            if len(term) < 3:
                continue
            if re.search(r"(?<!\w)" + re.escape(term.lower()) + r"(?!\w)", lowered):
                hits[r["id"]] = max(hits.get(r["id"], 0), len(term))
        ordered = sorted(hits, key=lambda i: -hits[i])[:limit]
        out: list[Entity] = []
        for eid in ordered:
            e = await self.get(eid)
            if e is not None:
                out.append(e)
        return out

    # --- write ------------------------------------------------------------------------

    async def upsert(
        self, name: str, *, type: str = "thing", summary: str | None = None, aliases: list[str] | None = None
    ) -> Entity:
        existing = await self.find(name)
        if existing is None:
            for alias in aliases or []:
                existing = await self.find(alias)
                if existing:
                    break
        now = _now()
        if existing is None:
            eid = new_id("ent")
            await self.db.execute(
                "INSERT INTO kg_entities(id, name, type, summary, mention_count, created_at, updated_at) VALUES (?,?,?,?,0,?,?)",
                (eid, name.strip(), type if type in _TYPES else "thing", (summary or "").strip(), now, now),
            )
        else:
            eid = existing.id
            new_summary = (summary or "").strip()
            if new_summary and new_summary != existing.summary:
                merged = new_summary if len(new_summary) > len(existing.summary) else existing.summary
                await self.db.execute(
                    "UPDATE kg_entities SET summary = ?, updated_at = ? WHERE id = ?", (merged, now, eid)
                )
            if type in _TYPES and existing.type == "thing" and type != "thing":
                await self.db.execute("UPDATE kg_entities SET type = ? WHERE id = ?", (type, eid))
        for alias in aliases or []:
            a = alias.strip()
            if a and a.lower() != name.strip().lower():
                await self.db.execute("INSERT OR IGNORE INTO kg_aliases(alias, entity_id) VALUES (?, ?)", (a, eid))
        self._changed([eid])
        entity = await self.get(eid)
        assert entity is not None
        return entity

    async def add_edge(
        self, src: str, dst: str, relation: str, *, weight: float = 1.0, evidence: str | None = None
    ) -> None:
        if src == dst:
            return
        await self.db.execute(
            "INSERT INTO kg_edges(src, dst, relation, weight, evidence, updated_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(src, dst, relation) DO UPDATE SET weight = kg_edges.weight + 0.5,"
            " evidence = COALESCE(excluded.evidence, kg_edges.evidence), updated_at = excluded.updated_at",
            (src, dst, relation.strip().lower(), weight, evidence, _now()),
        )
        self._changed([src, dst])

    async def add_mention(
        self, entity_id: str, *, conversation_id: str | None, message_id: str | None, snippet: str | None
    ) -> None:
        await self.db.execute(
            "INSERT INTO kg_mentions(entity_id, conversation_id, message_id, snippet, at) VALUES (?,?,?,?,?)",
            (entity_id, conversation_id, message_id, (snippet or "")[:200] or None, _now()),
        )
        await self.db.execute(
            "UPDATE kg_entities SET mention_count = mention_count + 1, updated_at = ? WHERE id = ?", (_now(), entity_id)
        )

    async def update(
        self, entity_id: str, *, name: str | None = None, type: str | None = None, summary: str | None = None
    ) -> Entity | None:
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name.strip())
        if type is not None and type in _TYPES:
            sets.append("type = ?")
            params.append(type)
        if summary is not None:
            sets.append("summary = ?")
            params.append(summary.strip())
        if sets:
            sets.append("updated_at = ?")
            params.extend([_now(), entity_id])
            await self.db.execute(f"UPDATE kg_entities SET {', '.join(sets)} WHERE id = ?", params)
            self._changed([entity_id])
        return await self.get(entity_id)

    async def delete(self, entity_id: str) -> None:
        await self.db.execute("DELETE FROM kg_entities WHERE id = ?", (entity_id,))
        self._changed([entity_id])

    async def merge(self, entity_id: str, into: str) -> Entity | None:
        if entity_id == into:
            return await self.get(into)
        source = await self.get(entity_id)
        target = await self.get(into)
        if source is None or target is None:
            return None
        async with self.db.transaction():
            await self.db.execute(
                "INSERT OR IGNORE INTO kg_aliases(alias, entity_id) VALUES (?, ?)", (source.name, into)
            )
            await self.db.execute("UPDATE kg_aliases SET entity_id = ? WHERE entity_id = ?", (into, entity_id))
            await self.db.execute("UPDATE OR IGNORE kg_edges SET src = ? WHERE src = ?", (into, entity_id))
            await self.db.execute("UPDATE OR IGNORE kg_edges SET dst = ? WHERE dst = ?", (into, entity_id))
            await self.db.execute("DELETE FROM kg_edges WHERE src = dst")
            await self.db.execute("UPDATE kg_mentions SET entity_id = ? WHERE entity_id = ?", (into, entity_id))
            await self.db.execute(
                "UPDATE kg_entities SET mention_count = mention_count + ?, summary = CASE WHEN summary = '' THEN ? ELSE summary END,"
                " updated_at = ? WHERE id = ?",
                (source.mention_count, source.summary, _now(), into),
            )
            await self.db.execute("DELETE FROM kg_entities WHERE id = ?", (entity_id,))
        self._changed([entity_id, into])
        return await self.get(into)

    # --- context ----------------------------------------------------------------------

    async def context_block(self, text: str, *, max_chars: int = 3000) -> str | None:
        entities = await self.match_in_text(text)
        if not entities:
            return None
        lines = ["## Known context (from Jarvis's knowledge graph)"]
        for e in entities:
            detail = await self.detail(e.id)
            rel = ""
            if detail and detail.edges:
                rel = " Related: " + "; ".join(f"{x.other.name} ({x.relation})" for x in detail.edges[:5])
            summary = f": {e.summary}" if e.summary else ""
            lines.append(f"- **{e.name}** ({e.type}){summary}.{rel}")
        block = "\n".join(lines)
        return block[:max_chars]


# --- learner ----------------------------------------------------------------------------

_EXTRACT_PROMPT = """Extract durable knowledge from this exchange between {user} and the assistant.
Return JSON only:
{{"entities": [{{"name": "...", "type": "person|org|project|place|thing|topic", "summary": "<one sentence>", "aliases": []}}],
 "relations": [{{"from": "<entity name>", "to": "<entity name>", "relation": "<short verb phrase>"}}]}}
Rules: only named people, organisations, projects, places, recurring topics; no generic words;
summaries state facts from the text, never guesses; at most 6 entities and 6 relations; empty
lists if nothing durable was said.

USER: {user_text}
ASSISTANT: {assistant_text}"""


class KnowledgeLearner:
    """Runs after a run finishes; never blocks the reply; never raises."""

    def __init__(self, store: KnowledgeStore, classifier: Any, *, user_name: str = "Arsen") -> None:
        self.store = store
        self._classifier = classifier  # Callable[[], ModelAdapter]
        self.user_name = user_name
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, conversation_id: str, user_text: str, assistant_text: str, message_id: str | None) -> None:
        if len(user_text) + len(assistant_text) < 60:
            return
        task = asyncio.create_task(self._learn(conversation_id, user_text, assistant_text, message_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _learn(self, conversation_id: str, user_text: str, assistant_text: str, message_id: str | None) -> None:
        try:
            adapter: ModelAdapter = self._classifier()
            prompt = _EXTRACT_PROMPT.format(
                user=self.user_name, user_text=user_text[:3000], assistant_text=assistant_text[:3000]
            )
            text = ""
            async for chunk in adapter.stream([Message.user(prompt)], [], cancel=asyncio.Event()):
                if isinstance(chunk, ModelTextChunk):
                    text += chunk.text
            data = _parse_json(text)
            if not data:
                return
            names: dict[str, str] = {}
            for ent in data.get("entities", [])[:6]:
                name = str(ent.get("name", "")).strip()
                if len(name) < 2:
                    continue
                entity = await self.store.upsert(
                    name,
                    type=str(ent.get("type", "thing")),
                    summary=str(ent.get("summary", "")),
                    aliases=[str(a) for a in ent.get("aliases", []) if isinstance(a, str)][:5],
                )
                names[name.lower()] = entity.id
                await self.store.add_mention(
                    entity.id, conversation_id=conversation_id, message_id=message_id, snippet=user_text[:200]
                )
            for rel in data.get("relations", [])[:6]:
                a = await self._resolve(names, str(rel.get("from", "")))
                b = await self._resolve(names, str(rel.get("to", "")))
                relation = str(rel.get("relation", "")).strip()
                if a and b and relation:
                    await self.store.add_edge(a, b, relation, evidence=user_text[:160])
        except Exception as exc:
            log.warning("kg learning skipped: %s", exc)

    async def _resolve(self, extracted: dict[str, str], name: str) -> str | None:
        """An extracted entity by name, else an entity already in the graph; never a new one."""
        key = name.strip().lower()
        if not key:
            return None
        if key in extracted:
            return extracted[key]
        existing = await self.store.find(name)
        return existing.id if existing else None


def _parse_json(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# --- tools ------------------------------------------------------------------------------


class _NameArgs(BaseModel):
    name: str = Field(description="Entity name or alias")


class _RelationArg(BaseModel):
    to: str
    relation: str


class _RememberArgs(BaseModel):
    name: str
    type: EntityType = "thing"
    summary: str = Field(description="One factual sentence")
    aliases: list[str] = Field(default_factory=list)
    relations: list[_RelationArg] = Field(default_factory=list)


class KnowledgeTools(BuiltinProvider):
    name = "kg"

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store
        super().__init__()

    @tool(
        "kg.who_is",
        description="What Jarvis knows about a person, org, project or topic.",
        args=_NameArgs,
        read_only=True,
        idempotent=True,
    )
    async def _who_is(self, name: str) -> ToolResult:
        e = await self.store.find(name)
        if e is None:
            hits = await self.store.search(name, limit=5)
            if not hits:
                return ToolResult.empty(f"Nothing known about {name!r}.")
            return ToolResult.data(
                "Closest matches:\n" + "\n".join(f"- {h.name} ({h.type}): {h.summary}" for h in hits)
            )
        d = await self.store.detail(e.id)
        rel = "\n".join(f"- {x.relation} → {x.other.name}" for x in (d.edges if d else [])[:10])
        return ToolResult.data(
            f"{e.name} ({e.type}): {e.summary or 'no summary'}\nAliases: {', '.join(e.aliases) or '-'}\n{rel}"
        )

    @tool(
        "kg.related_to",
        description="Entities connected to a given one and how.",
        args=_NameArgs,
        read_only=True,
        idempotent=True,
    )
    async def _related(self, name: str) -> ToolResult:
        e = await self.store.find(name)
        if e is None:
            return ToolResult.empty(f"Nothing known about {name!r}.")
        d = await self.store.detail(e.id)
        if not d or not d.edges:
            return ToolResult.empty(f"{e.name} has no recorded relations.")
        return ToolResult.data(
            "\n".join(f"- {e.name} {x.relation} {x.other.name} ({x.other.type})" for x in d.edges), count=len(d.edges)
        )

    @tool(
        "kg.remember", description="Store a durable fact about an entity (and optional relations).", args=_RememberArgs
    )
    async def _remember(
        self,
        name: str,
        summary: str,
        type: EntityType = "thing",
        aliases: list[str] | None = None,
        relations: list[dict[str, str]] | None = None,
    ) -> ToolResult:
        entity = await self.store.upsert(name, type=type, summary=summary, aliases=aliases or [])
        for rel in relations or []:
            # A relation with no target used to create an entity literally called "?" and wire
            # the fact to it. Skip it: half a relation is not knowledge.
            to = str(rel.get("to", "")).strip()
            if not to:
                continue
            other = await self.store.upsert(to, type="thing")
            await self.store.add_edge(entity.id, other.id, str(rel.get("relation", "related to")))
        return ToolResult.data(f"Remembered {entity.name} ({entity.type}).")
