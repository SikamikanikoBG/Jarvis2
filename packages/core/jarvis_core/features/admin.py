"""Jarvis reading and changing its own settings - and the trail of every change (issue #14).

docs/stories/14_agent_admin.md has the rules; in short:

- **One write path.** ``SettingsAdmin`` is the only code that saves settings: ``PATCH /api/settings``
  from the UI, the ``settings.*`` tools, a Revert and an applied proposal all go through it, under
  one lock, with one revision number (the UI's ``If-Match``) and one row in ``admin_changes``.
- **Risk is data.** Whether a change asks first comes from the markers on the settings fields
  (``jarvis_proto.settings.marks``): lifeline, safety, secret - plus "delete" for an item removed.
  The tool answers the loop's per-call risk hook (``assess``) with that, and the loop's existing
  confirmation paths do the asking.
- **Lifelines are probed.** A change that can cut Jarvis off (a lane's endpoint, an MCP host) is
  tried before it is saved and refused if it does not answer; once saved it is checked again after
  ``agent_admin.probe_window_s`` and put back on its own if that fails - also across a restart.
- **Nobody watching, nothing risky.** In a scheduled, triage, meeting or system run a risky change
  becomes a *proposal*: recorded, not applied, with Apply in the chat and the Settings trail.
- **Secrets** are masked unless asked for in clear; a run that has seen one in clear asks before
  any call that would carry it out of the core (engine/loop.py ``_leaks_secret``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast, get_args, get_origin

import httpx
from pydantic import BaseModel, Field, ValidationError

from jarvis_core.engine.current import current_conversation_id, current_run_id
from jarvis_core.models.base import ProbeResult
from jarvis_core.models.factory import current_run_kind
from jarvis_core.tools.builtin import BuiltinProvider, CallRisk, tool
from jarvis_proto import ChangeField, ChangeRecord, McpServerSpec, ModelSpec, Run, RunKind, Settings, ToolResult, new_id
from jarvis_proto import settings_paths as sp
from jarvis_proto.events import ChangeRecorded, SettingsChanged
from jarvis_proto.settings import RoleName

if TYPE_CHECKING:
    from jarvis_core.app import Core
    from jarvis_core.db import Database
    from jarvis_core.engine.bus import EventBus

log = logging.getLogger(__name__)

# Runs nobody can answer a question in (engine/loop._UNATTENDED): their risky changes are proposals.
UNATTENDED = {RunKind.SCHEDULED, RunKind.TRIAGE, RunKind.MEETING, RunKind.SYSTEM}
_PROBE_TIMEOUT_S = 15.0
_MCP_PROBE_TIMEOUT_S = 20.0
# A value longer than this is shown by its size in a section listing; the leaf path gives it whole.
_LONG = 240
_RISK_WORDS = {
    "lifeline": "can cut Jarvis off",
    "safety": "loosens what stops for Arsen",
    "secret": "writes a secret",
    "delete": "removes something",
}


class SettingsInvalid(ValueError):
    """The validator said no. ``errors`` is pydantic's list (or one message), nothing was changed."""

    def __init__(self, errors: list[dict[str, Any]] | str) -> None:
        self.errors = errors
        super().__init__(errors if isinstance(errors, str) else "; ".join(_error_text(e) for e in errors))


class SettingsConflict(Exception):
    """The UI saved over a revision that is not the current one (409)."""

    def __init__(self, rev: int) -> None:
        self.rev = rev
        super().__init__(f"settings changed since they were loaded (now rev {rev}); reload them")


class ChangeRefused(Exception):
    """A change that may not be made as asked: a probe failed, a revert would clobber a later change."""


def _error_text(err: dict[str, Any]) -> str:
    loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body",))
    return f"{loc}: {err.get('msg', '')}" if loc else str(err.get("msg", ""))


def merge_settings(model: type[BaseModel], current: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """PATCH semantics for one settings document: a key that is a settings SECTION (a sub-model,
    e.g. `tasks`, `voice`) merges field by field - `{"tasks": {"max_running": 2}}` leaves `enabled`
    as it is (alpha F-13 found it reset to the default). Everything else - a list, a dict keyed by
    name like `account_rules`, a scalar - is replaced whole, so an entry can still be removed."""
    out = dict(current)
    for key, value in body.items():
        field = model.model_fields.get(key)
        sub = field.annotation if field is not None else None
        if (
            isinstance(sub, type)
            and issubclass(sub, BaseModel)
            and isinstance(value, dict)
            and isinstance(out.get(key), dict)
        ):
            out[key] = merge_settings(sub, out[key], value)
        else:
            out[key] = value
    return out


# --- who is changing it -------------------------------------------------------------------


@dataclass(slots=True)
class Who:
    actor: str = "arsen"
    run_id: str | None = None
    run_kind: RunKind | None = None
    conversation_id: str | None = None
    call_id: str | None = None
    label: str = ""

    @property
    def unattended(self) -> bool:
        return self.run_kind in UNATTENDED

    @property
    def by_run(self) -> bool:
        return self.actor == "jarvis"

    @property
    def author(self) -> str | None:
        """Who a schedule prompt written now counts as written by: None (Arsen) outside a run and
        in his own chat - he asked for it there - else the run (docs/stories/14_agent_admin.md)."""
        return None if self.run_id is None or self.run_kind is RunKind.CHAT else self.run_id


async def who_now(core: Core, idempotency_key: str | None = None) -> Who:
    """The actor of a change made now: Arsen outside any run, else Jarvis in this run."""
    run_id = current_run_id.get()
    if run_id is None:
        return Who()
    kind = current_run_kind.get()
    conv_id = current_conversation_id.get()
    label = kind.value if kind is not None else "run"
    conv = await core.store.get_conversation(conv_id) if conv_id else None
    if conv is not None:
        label = "incognito chat" if conv.incognito else (conv.folder_label or conv.title or label)
    call_id = idempotency_key.split(":", 1)[1].split(":", 1)[0] if idempotency_key and ":" in idempotency_key else None
    return Who("jarvis", run_id, kind, conv_id, call_id, label)


# --- the trail ------------------------------------------------------------------------------


def shown(rec: ChangeRecord) -> ChangeRecord:
    """A record as it may leave the core: every secret masked."""
    return rec.model_copy(update={"fields": [shown_field(f, rec.area) for f in rec.fields]})


def _mask(area: str, path: str, value: Any) -> Any:
    if area == "settings":
        masked = sp.mask_at(path, value)
        return masked if masked != value or value in (None, "", {}) else sp.masked(json.dumps(value))
    return sp.masked(json.dumps(value)) if value else value


class ChangeLog:
    """``admin_changes``: who changed what, when, old -> new, and what became of it."""

    def __init__(self, db: Database, bus: EventBus | None = None) -> None:
        self.db = db
        self.bus = bus

    def _publish(self, rec: ChangeRecord) -> None:
        if self.bus is not None:
            self.bus.publish(ChangeRecorded(change=shown(rec)))

    @staticmethod
    def _row(r: Any) -> ChangeRecord:
        return ChangeRecord(
            id=r["id"],
            at=datetime.fromisoformat(r["at"]),
            area=r["area"],
            target=r["target"],
            actor=r["actor"],
            actor_label=r["actor_label"] or "",
            run_id=r["run_id"],
            run_kind=r["run_kind"],
            conversation_id=r["conversation_id"],
            call_id=r["call_id"],
            fields=[ChangeField.model_validate(f) for f in json.loads(r["fields"] or "[]")],
            risk=json.loads(r["risk"] or "[]"),
            reason=r["reason"] or "",
            status=r["status"],
            rev=r["rev"],
            reverts=r["reverts"],
            reverted_by=r["reverted_by"],
            note=r["note"] or "",
            revertible=_revertible(r["area"], r["status"], r["fields"]),
        )

    async def record(
        self,
        *,
        area: str,
        target: str,
        who: Who,
        fields: list[ChangeField],
        risk: list[str] | None = None,
        reason: str = "",
        status: str = "applied",
        rev: int | None = None,
        reverts: str | None = None,
        note: str = "",
        request: dict[str, Any] | None = None,
        probe_deadline: datetime | None = None,
    ) -> ChangeRecord:
        cid = new_id("chg")
        await self.db.execute(
            "INSERT INTO admin_changes(id, at, area, target, actor, actor_label, run_id, run_kind, conversation_id,"
            " call_id, fields, risk, reason, status, rev, reverts, reverted_by, note, request, probe_deadline)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)",
            (
                cid,
                datetime.now(UTC).isoformat(),
                area,
                target,
                who.actor,
                who.label,
                who.run_id,
                who.run_kind.value if who.run_kind else None,
                who.conversation_id,
                who.call_id,
                json.dumps([f.model_dump(mode="json") for f in fields], ensure_ascii=False, default=str),
                json.dumps(sorted(set(risk or []))),
                reason[:500],
                status,
                rev,
                reverts,
                note,
                json.dumps(request, ensure_ascii=False, default=str) if request is not None else None,
                probe_deadline.isoformat() if probe_deadline else None,
            ),
        )
        rec = await self.get(cid)
        assert rec is not None
        log.info("change %s by %s (%s): %s %s [%s]", cid, who.actor, who.label or "-", area, target, status)
        self._publish(rec)
        return rec

    async def update(self, change_id: str, **fields: Any) -> ChangeRecord | None:
        allowed = {"status", "reverted_by", "note", "rev", "probe_deadline", "fields", "risk"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if "fields" in sets:
            sets["fields"] = json.dumps([f.model_dump(mode="json") for f in sets["fields"]], ensure_ascii=False)
        if "risk" in sets:
            sets["risk"] = json.dumps(sorted(set(sets["risk"])))
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            await self.db.execute(f"UPDATE admin_changes SET {cols} WHERE id = ?", (*sets.values(), change_id))
        rec = await self.get(change_id)
        if rec is not None:
            self._publish(rec)
        return rec

    async def get(self, change_id: str) -> ChangeRecord | None:
        row = await self.db.fetchone("SELECT * FROM admin_changes WHERE id = ?", (change_id,))
        return self._row(row) if row else None

    async def request(self, change_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT request FROM admin_changes WHERE id = ?", (change_id,))
        return json.loads(row["request"]) if row and row["request"] else None

    async def list(
        self,
        *,
        limit: int = 50,
        area: str | None = None,
        conversation_id: str | None = None,
        status: str | None = None,
        path: str | None = None,
    ) -> list[ChangeRecord]:
        where, args = [], []
        for col, val in (("area", area), ("conversation_id", conversation_id), ("status", status)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        if path:
            where.append("fields LIKE ?")
            args.append(f'%"path": "{path}%')
        sql = "SELECT * FROM admin_changes" + (" WHERE " + " AND ".join(where) if where else "")
        rows = await self.db.fetchall(sql + " ORDER BY at DESC, rowid DESC LIMIT ?", (*args, max(1, min(limit, 500))))
        return [self._row(r) for r in rows]

    async def probing(self) -> list[tuple[ChangeRecord, datetime | None]]:
        rows = await self.db.fetchall("SELECT * FROM admin_changes WHERE status = 'probing'")
        return [
            (self._row(r), datetime.fromisoformat(r["probe_deadline"]) if r["probe_deadline"] else None) for r in rows
        ]

    async def later(self, rec: ChangeRecord) -> list[ChangeRecord]:
        """Changes to the same area and target recorded after ``rec`` that are in force."""
        rows = await self.db.fetchall(
            "SELECT * FROM admin_changes WHERE area = ? AND target = ? AND rowid > (SELECT rowid FROM admin_changes"
            " WHERE id = ?) AND status IN ('applied', 'probing') ORDER BY rowid",
            (rec.area, rec.target, rec.id),
        )
        return [self._row(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM admin_changes WHERE status IN ('probing', 'proposed') GROUP BY status"
        )
        reverted = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM admin_changes WHERE status = 'auto_reverted' AND at >= ?", (since,)
        )
        last = await self.db.fetchone("SELECT at FROM admin_changes ORDER BY at DESC LIMIT 1")
        by = {r["status"]: r["n"] for r in rows}
        return {
            "probing": by.get("probing", 0),
            "proposed": by.get("proposed", 0),
            "auto_reverted_24h": reverted["n"] if reverted else 0,
            "last_change_at": last["at"] if last else None,
        }

    async def forget_conversation(self, conversation_id: str) -> None:
        """A deleted chat leaves its changes in the trail (they are not chat content), unlinked."""
        await self.db.execute(
            "UPDATE admin_changes SET conversation_id = NULL WHERE conversation_id = ?", (conversation_id,)
        )


def _revertible(area: str, status: str, fields: str | None) -> bool:
    """Settings changes can be undone field by field; a schedule edit too (its create and delete
    are undone by the opposite action, not by Revert); an api change never - it is trail only."""
    if status not in ("applied", "probing"):
        return False
    if area == "settings":
        return True
    if area != "schedule":
        return False
    items = json.loads(fields or "[]")
    return any(f.get("old") is not None for f in items) and any(f.get("new") is not None for f in items)


def change_fields(leaves: list[sp.Leaf]) -> list[ChangeField]:
    return [ChangeField(path=leaf.path, old=leaf.old, new=leaf.new, op=leaf.op, secret=leaf.secret) for leaf in leaves]  # type: ignore[arg-type]


def risk_of(leaves: list[sp.Leaf]) -> list[str]:
    out: set[str] = set()
    for leaf in leaves:
        out |= leaf.risk
        if leaf.secret and leaf.op != "remove":
            out.add("secret")
    return sorted(out)


def describe_risk(leaves: list[sp.Leaf]) -> str:
    """One plain sentence: what about this change makes it ask."""
    parts: list[str] = []
    for cls in ("lifeline", "secret", "safety", "delete"):
        hit = [
            leaf.path
            for leaf in leaves
            if cls in leaf.risk or (cls == "secret" and leaf.secret and leaf.op != "remove")
        ]
        if hit:
            parts.append(f"{_RISK_WORDS[cls]} ({', '.join(hit[:3])}{'…' if len(hit) > 3 else ''})")
    return "this change " + "; ".join(parts) if parts else ""


def diff_lines(fields: list[ChangeField], area: str = "settings") -> list[str]:
    out = []
    for f in fields:
        old = _mask(area, f.path, f.old) if f.secret else f.old
        new = _mask(area, f.path, f.new) if f.secret else f.new
        if f.op == "add":
            out.append(f"+ {f.path}: {_short(new)}")
        elif f.op == "remove":
            out.append(f"- {f.path} (was {_short(old)})")
        else:
            out.append(f"{f.path}: {_short(old)} → {_short(new)}")
    return out


def _short(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- the one settings write path ----------------------------------------------------------


@dataclass(slots=True)
class Plan:
    """A change worked out but not saved: the candidate settings and what differs."""

    new: Settings
    leaves: list[sp.Leaf]

    @property
    def keys(self) -> set[str]:
        return {sp.parse(leaf.path)[0].name or "" for leaf in self.leaves}


ProbeSpec = Callable[[ModelSpec], Awaitable[ProbeResult]]
ProbeMcp = Callable[[McpServerSpec], Awaitable[tuple[bool, str]]]


class SettingsAdmin:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.changes = ChangeLog(core.db, core.bus)
        self._lock = asyncio.Lock()
        self._watches: set[asyncio.Task[None]] = set()
        self._revealed: dict[str, set[str]] = {}
        # Injectable for tests: how a model endpoint and an MCP server are tried.
        self.probe_spec: ProbeSpec = self._probe_spec
        self.probe_mcp: ProbeMcp = self._probe_mcp

    # -- revision, epoch, secrets seen ---------------------------------------------------------

    async def rev(self) -> int:
        row = await self.core.db.fetchone("SELECT rev FROM settings_meta WHERE id = 1")
        return int(row["rev"]) if row else 0

    async def _bump(self) -> int:
        await self.core.db.execute("UPDATE settings_meta SET rev = rev + 1 WHERE id = 1")
        return await self.rev()

    def policy_epoch(self) -> int:
        return self.core.policy_epoch

    def revealed(self, run_id: str) -> set[str]:
        return self._revealed.get(run_id, set())

    def note_revealed(self, run_id: str | None, values: list[str]) -> None:
        if run_id and values:
            self._revealed.setdefault(run_id, set()).update(v for v in values if v)

    def forget_run(self, run_id: str) -> None:
        self._revealed.pop(run_id, None)

    def secret_values(self) -> list[str]:
        """Every secret stored in the settings now, for keeping them out of the journal and the KG."""
        dump = self.core.settings.model_dump(mode="json")
        found: list[str] = []
        _collect(dump, sp.mask(dump), found)
        return [v for v in found if len(v) >= 4]

    def redact(self, text: str) -> str:
        """``text`` with any stored secret replaced by the mask: what is never written to the day's
        journal or learned into the knowledge graph, whatever the model repeated."""
        for value in self.secret_values():
            if value in text:
                text = text.replace(value, sp.MASK)
        return text

    async def strict(self, run: Run) -> bool:
        conv = await self.core.store.get_conversation(run.conversation_id)
        if conv is None or not conv.folder_key:
            return False
        schedule = await self.core.schedules.get(conv.folder_key)
        return schedule is not None and schedule.prompt_by is not None

    # -- planning ----------------------------------------------------------------------------

    def plan(
        self,
        set_: dict[str, Any] | None = None,
        add: dict[str, Any] | None = None,
        remove: list[str] | None = None,
        *,
        body: dict[str, Any] | None = None,
    ) -> Plan:
        """The candidate settings for these operations (or a REST body), validated like the UI's.
        Raises SettingsInvalid or sp.PathError; changes nothing."""
        current = self.core.settings.model_dump(mode="json")
        if body is not None:
            draft = merge_settings(Settings, sp.deep(current), body)
        else:
            draft = sp.deep(current)
            for path, value in (set_ or {}).items():
                sp.set_at(draft, path, value)
            for path, item in (add or {}).items():
                sp.add_at(draft, path, item)
            for path in remove or []:
                sp.remove_at(draft, path)
        draft = sp.keep_masked(draft, current)
        try:
            new = Settings.model_validate(draft)
        except ValidationError as exc:
            raise SettingsInvalid(
                cast(list[dict[str, Any]], exc.errors(include_url=False, include_context=False, include_input=False))
            ) from exc
        # Which conversation is the Jarvis one is the core's to know, not the form's: a Settings screen
        # opened before it was made would save its stale null and the next call would make a second one.
        new.assistant.home_conversation_id = self.core.settings.assistant.home_conversation_id
        leaves = sp.diff(current, new.model_dump(mode="json"))
        return Plan(new, leaves)

    def _rebase(self, plan: Plan) -> Plan:
        current = self.core.settings.model_dump(mode="json")
        draft = sp.deep(current)
        for leaf in plan.leaves:
            try:
                if leaf.op == "remove":
                    with contextlib.suppress(sp.PathError):
                        sp.remove_at(draft, leaf.path)
                    continue
                parent = _parent(leaf.path) if leaf.op == "add" else ""
                target = sp.get_at(draft, parent) if parent else None
                if isinstance(target, list) and _value_at(draft, leaf.path) is None:
                    target.append(leaf.new)
                    continue
                sp.set_at(draft, leaf.path, leaf.new)
            except sp.PathError as exc:
                raise ChangeRefused(f"not changed: the settings changed meanwhile ({exc})") from exc
        try:
            new = Settings.model_validate(draft)
        except ValidationError as exc:
            raise SettingsInvalid(
                cast(list[dict[str, Any]], exc.errors(include_url=False, include_context=False, include_input=False))
            ) from exc
        new.assistant.home_conversation_id = self.core.settings.assistant.home_conversation_id
        return Plan(new, sp.diff(current, new.model_dump(mode="json")))

    def assess_plan(self, plan: Plan) -> tuple[list[str], str]:
        return risk_of(plan.leaves), describe_risk(plan.leaves)

    # -- applying ----------------------------------------------------------------------------

    async def apply(
        self,
        plan: Plan,
        *,
        who: Who,
        reason: str = "",
        reverts: str | None = None,
        record_id: str | None = None,
    ) -> ChangeRecord | None:
        """Save a planned change: probe a lifeline, save, apply, record, announce. None when the
        plan changes nothing. Raises ChangeRefused when a probe says the change would cut Jarvis off."""
        if not plan.leaves:
            return None
        async with self._lock:
            # Planned against the settings of a moment ago: its fields are put onto what is in force
            # NOW, so a save that landed in between (the UI's, another run's) is not undone (EC-3).
            plan = self._rebase(plan)
            leaves = plan.leaves
            if not leaves:
                return None
            risk = risk_of(leaves)
            note = ""
            lifeline = [leaf for leaf in leaves if "lifeline" in leaf.risk]
            if lifeline:
                ok, note = await self._probe(plan.new, lifeline, before=self.core.settings, strict=who.by_run)
                if not ok:
                    raise ChangeRefused(f"not changed: {note}")
            keys = {sp.parse(leaf.path)[0].name or "" for leaf in leaves}
            await self.core.store.save_settings(plan.new, only_keys=keys)
            rev = await self._bump()
            self.core.apply_settings(plan.new, by_run=who.by_run)
            if "mcp_servers" in keys:
                await self.core.reload_tools()
            window = self.core.settings.agent_admin.probe_window_s
            deadline = datetime.now(UTC) + timedelta(seconds=window) if lifeline else None
            status = "probing" if lifeline else "applied"
            fields = change_fields(leaves)
            if record_id is not None:
                rec = await self.changes.update(
                    record_id, status=status, rev=rev, fields=fields, risk=risk, note=note, probe_deadline=deadline
                )
                assert rec is not None
            else:
                rec = await self.changes.record(
                    area="settings",
                    target="settings",
                    who=who,
                    fields=fields,
                    risk=risk,
                    reason=reason,
                    status=status,
                    rev=rev,
                    reverts=reverts,
                    note=note,
                    probe_deadline=deadline,
                )
            self.core.bus.publish(
                SettingsChanged(rev=rev, paths=[leaf.path for leaf in leaves], change_id=rec.id, actor=rec.actor)
            )
        if lifeline:
            self._watch(rec.id, window)
        return rec

    async def apply_body(self, body: dict[str, Any], *, base_rev: int | None = None) -> Settings:
        """``PATCH /api/settings``: the UI's own save, through the same path as Jarvis's."""
        if base_rev is not None and base_rev != await self.rev():
            raise SettingsConflict(await self.rev())
        plan = self.plan(body=body)
        try:
            await self.apply(plan, who=Who())
        except ChangeRefused as exc:
            raise SettingsInvalid(str(exc)) from exc
        return self.core.settings

    async def save_system(self, new: Settings, keys: set[str]) -> None:
        """The core's own bookkeeping in the settings (the Jarvis conversation's id, a one-off style
        upgrade): saved and announced with a new revision, no trail row - nobody changed anything."""
        async with self._lock:
            await self.core.store.save_settings(new, only_keys=keys)
            rev = await self._bump()
            self.core.apply_settings(new)
            self.core.bus.publish(SettingsChanged(rev=rev, paths=sorted(keys), actor="system"))

    # -- probes -------------------------------------------------------------------------------

    async def _probe_spec(self, spec: ModelSpec) -> ProbeResult:
        adapter: Any = self.core.adapters.detached(spec)
        try:
            return await asyncio.wait_for(adapter.probe(), timeout=_PROBE_TIMEOUT_S)
        except TimeoutError:
            return ProbeResult(False, int(_PROBE_TIMEOUT_S * 1000), f"no answer in {_PROBE_TIMEOUT_S:.0f}s")
        finally:
            closer = getattr(adapter, "aclose", None)
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer()

    async def _probe_mcp(self, spec: McpServerSpec) -> tuple[bool, str]:
        from jarvis_core.tools.mcp_provider import McpProvider  # heavy; only when a host changes

        provider = McpProvider(spec)
        try:
            await asyncio.wait_for(provider.start(), timeout=_MCP_PROBE_TIMEOUT_S)
            specs = await asyncio.wait_for(provider.list_tools(), timeout=_MCP_PROBE_TIMEOUT_S)
            if provider.error or not specs:
                return False, f"MCP server {spec.name!r} {provider.error or 'offered no tools'}"
            return True, f"MCP server {spec.name!r} answered with {len(specs)} tools"
        except Exception as exc:
            return False, f"MCP server {spec.name!r} did not answer ({type(exc).__name__}: {exc})"
        finally:
            with contextlib.suppress(Exception):
                await provider.stop()

    async def _probe(
        self, new: Settings, leaves: list[sp.Leaf], *, before: Settings | None, strict: bool = False
    ) -> tuple[bool, str]:
        """Try what a lifeline change points at. (ok, what was found). A target that was already
        down before the change is not held against it - the change may be the fix - and neither is
        a host Arsen adds in the UI while it is asleep; ``strict`` (a run's change, the re-check)
        holds every failure against it."""
        notes: list[str] = []
        roles = {
            step.name
            for leaf in leaves
            if (steps := sp.parse(leaf.path)) and steps[0].name == "roles" and len(steps) > 1
            for step in [steps[1]]
        }
        for name in sorted(r for r in roles if r):
            role = RoleName(name.lower()) if name.lower() in {r.value for r in RoleName} else None
            if role is None or role not in new.roles:
                continue
            res = await self.probe_spec(new.roles[role])
            if res.ok:
                notes.append(f"{role.value} lane answered ({res.latency_ms} ms)")
                continue
            old_ok = before is not None and role in before.roles and (await self.probe_spec(before.roles[role])).ok
            if old_ok or before is None:
                return False, f"the {role.value} model at {new.roles[role].base_url} did not answer ({res.detail})"
            notes.append(f"{role.value} lane does not answer ({res.detail}), but it did not before either - kept")
        named = {step.key[1] for leaf in leaves if len(steps := sp.parse(leaf.path)) > 1 and (step := steps[1]).key}
        whole = any(len(sp.parse(leaf.path)) == 1 and leaf.path == "mcp_servers" for leaf in leaves)
        old_servers = {s.name: s for s in (before.mcp_servers if before else [])}
        health = {p["name"]: p["ok"] for p in self.core.registry.provider_health()}
        for spec in new.mcp_servers:
            if not spec.enabled or (before is not None and old_servers.get(spec.name) == spec):
                continue
            if before is None and spec.name.lower() not in {n.lower() for n in named} and not whole:
                continue
            ok, detail = await self.probe_mcp(spec)
            if ok:
                notes.append(detail)
            elif strict or health.get(spec.name):
                return False, detail
            else:
                notes.append(f"{detail} - it was not serving before either, kept")
        if any(sp.parse(leaf.path)[0].name == "stt_url" for leaf in leaves) and new.stt_url:
            try:
                async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
                    await client.get(new.stt_url)
                notes.append("speech-to-text answered")
            except Exception as exc:
                detail = f"speech-to-text at {new.stt_url} did not answer ({type(exc).__name__})"
                if strict or (before is not None and before.stt_url):
                    return False, detail
                notes.append(f"{detail}; there was none before, kept")
        return True, "; ".join(notes)

    def _watch(self, change_id: str, window: float, *, now: bool = False) -> None:
        async def go() -> None:
            if not now:
                await asyncio.sleep(window)
            await self.recheck(change_id)

        task = asyncio.create_task(go(), name=f"admin-watch:{change_id}")
        self._watches.add(task)
        task.add_done_callback(self._watches.discard)

    async def recheck(self, change_id: str) -> ChangeRecord | None:
        """The second look at a lifeline change: kept if what it points at answers now, else put
        back - with nobody needing a working model to do it."""
        rec = await self.changes.get(change_id)
        if rec is None or rec.status != "probing":
            return rec
        leaves = [sp.Leaf(f.path, f.old, f.new, f.op, {"lifeline"}, f.secret) for f in rec.fields]
        ok, note = await self._probe(self.core.settings, leaves, before=None, strict=True)
        health = {p["name"]: p for p in self.core.registry.provider_health()}
        for spec in self.core.settings.mcp_servers:
            if spec.enabled and any(f.path.startswith(f"mcp_servers[name={spec.name}]") for f in rec.fields):
                state = health.get(spec.name)
                if state is not None and not state["ok"]:
                    ok, note = False, f"MCP server {spec.name!r}: {state['error']}"
        if ok:
            return await self.changes.update(change_id, status="applied", note=note or rec.note)
        log.warning("change %s failed its re-check (%s): reverting", change_id, note)
        try:
            back = await self.revert(change_id, who=Who("probe-revert", label="re-check"), force=True, probe=False)
        except (ChangeRefused, SettingsInvalid, sp.PathError) as exc:
            return await self.changes.update(change_id, note=f"re-check failed ({note}) and could not revert: {exc}")
        await self.changes.update(change_id, status="auto_reverted", note=f"re-check failed: {note}")
        return back

    async def recover(self) -> None:
        """At start: a lifeline change still on probation when the core stopped is checked now."""
        for rec, _deadline in await self.changes.probing():
            self._watch(rec.id, 0, now=True)
        await self.expire_proposals()

    async def stop(self) -> None:
        for task in list(self._watches):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- revert, proposals ----------------------------------------------------------------------

    async def revert(self, change_id: str, *, who: Who, force: bool = False, probe: bool = True) -> ChangeRecord:
        rec = await self.changes.get(change_id)
        if rec is None:
            raise ChangeRefused(f"no change {change_id!r}")
        if rec.status not in ("applied", "probing"):
            raise ChangeRefused(f"{change_id} is {rec.status.replace('_', ' ')}; there is nothing to revert")
        if rec.area == "schedule":
            return await self.revert_schedule(rec, who=who, force=force)
        if rec.area != "settings":
            raise ChangeRefused(
                "only settings and schedule changes can be reverted; undo this one with the opposite call"
            )
        current = self.core.settings.model_dump(mode="json")
        draft = sp.deep(current)
        for f in reversed(rec.fields):
            here = _value_at(current, f.path)
            expected = None if f.op == "remove" else f.new
            if not force and here != expected:
                shown_now = sp.mask_at(f.path, here) if f.secret else here
                raise ChangeRefused(
                    f"{f.path} has changed since (it is now {_short(shown_now)}); revert the later change first"
                )
            _undo(draft, f)
        plan = self.plan(body={k: draft[k] for k in {sp.parse(f.path)[0].name or "" for f in rec.fields}})
        # Going back to what worked after a failed re-check needs no probe and no watch; a revert
        # Arsen or Jarvis asks for is a change like any other.
        back = (
            await self.apply(plan, who=who, reverts=rec.id)
            if probe
            else await self._apply_unprobed(plan, who=who, reverts=rec.id)
        )
        if back is None:
            await self.changes.update(rec.id, status="reverted")
            refreshed = await self.changes.get(rec.id)
            assert refreshed is not None
            return refreshed
        await self.changes.update(rec.id, status="reverted", reverted_by=back.id)
        return back

    async def _apply_unprobed(self, plan: Plan, *, who: Who, reverts: str) -> ChangeRecord | None:
        async with self._lock:
            plan = self._rebase(plan)
            leaves = plan.leaves
            if not leaves:
                return None
            keys = {sp.parse(leaf.path)[0].name or "" for leaf in leaves}
            await self.core.store.save_settings(plan.new, only_keys=keys)
            rev = await self._bump()
            self.core.apply_settings(plan.new)
            if "mcp_servers" in keys:
                await self.core.reload_tools()
            rec = await self.changes.record(
                area="settings",
                target="settings",
                who=who,
                fields=change_fields(leaves),
                risk=risk_of(leaves),
                status="applied",
                rev=rev,
                reverts=reverts,
            )
            self.core.bus.publish(
                SettingsChanged(rev=rev, paths=[leaf.path for leaf in leaves], change_id=rec.id, actor=who.actor)
            )
            return rec

    async def revert_schedule(self, rec: ChangeRecord, *, who: Who, force: bool = False) -> ChangeRecord:
        schedule = await self.core.schedules.get(rec.target)
        if schedule is None:
            raise ChangeRefused("that schedule no longer exists")
        now = schedule.model_dump(mode="json")
        back: dict[str, Any] = {}
        for f in rec.fields:
            if not force and now.get(f.path) != f.new:
                raise ChangeRefused(f"{f.path} of {schedule.name!r} has changed since; revert the later change first")
            back[f.path] = f.old
        try:
            updated = await self.core.schedules.update(rec.target, **_schedule_fields(back))
        except ValueError as exc:
            raise ChangeRefused(f"cannot put the schedule back: {exc}") from exc
        assert updated is not None
        after = updated.model_dump(mode="json")
        fields = [ChangeField(path=k, old=now.get(k), new=after.get(k)) for k in back if now.get(k) != after.get(k)]
        new = await self.changes.record(area="schedule", target=rec.target, who=who, fields=fields, reverts=rec.id)
        await self.changes.update(rec.id, status="reverted", reverted_by=new.id)
        return new

    async def propose(
        self,
        plan: Plan,
        *,
        who: Who,
        reason: str,
        request: dict[str, Any],
        area: str = "settings",
        target: str = "settings",
    ) -> ChangeRecord:
        risk, why = (risk_of(plan.leaves), describe_risk(plan.leaves)) if area == "settings" else (["delete"], "")
        return await self.changes.record(
            area=area,
            target=target,
            who=who,
            fields=change_fields(plan.leaves),
            risk=risk,
            reason=reason,
            status="proposed",
            note=why,
            request=request,
        )

    async def apply_proposal(self, change_id: str) -> ChangeRecord:
        """Arsen pressed Apply on a change a run proposed: it runs through the whole path now -
        validated against today's settings, probed - as his own."""
        rec = await self.changes.get(change_id)
        if rec is None or rec.status != "proposed":
            raise ChangeRefused(f"{change_id} is not waiting to be applied")
        ttl = self.core.settings.agent_admin.proposal_ttl_s
        if datetime.now(UTC) - rec.at > timedelta(seconds=ttl):
            await self.changes.update(change_id, status="expired")
            raise ChangeRefused("this proposal has lapsed; ask for it again")
        req = await self.changes.request(change_id) or {}
        who = Who("arsen", label="Apply")
        if rec.area == "api":
            status, data = await self.core.api_tools.dispatch(
                str(req.get("method")), str(req.get("path")), req.get("body"), actor="arsen"
            )
            if status >= 400:
                raise ChangeRefused(f"HTTP {status}: {_short(data, 300)}")
            out = await self.changes.update(change_id, status="applied", note=f"applied by Arsen: HTTP {status}")
            assert out is not None
            return out
        plan = self.plan(req.get("set"), req.get("add"), req.get("remove"))
        out = await self.apply(plan, who=who, record_id=change_id)
        if out is None:
            out = await self.changes.update(change_id, status="discarded", note="nothing left to change")
        assert out is not None
        return out

    async def discard(self, change_id: str) -> ChangeRecord:
        rec = await self.changes.get(change_id)
        if rec is None or rec.status != "proposed":
            raise ChangeRefused(f"{change_id} is not waiting to be applied")
        out = await self.changes.update(change_id, status="discarded")
        assert out is not None
        return out

    async def expire_proposals(self) -> None:
        ttl = self.core.settings.agent_admin.proposal_ttl_s
        cutoff = (datetime.now(UTC) - timedelta(seconds=ttl)).isoformat()
        rows = await self.core.db.fetchall(
            "SELECT id FROM admin_changes WHERE status = 'proposed' AND at < ?", (cutoff,)
        )
        for r in rows:
            await self.changes.update(r["id"], status="expired")


def _collect(value: Any, masked: Any, out: list[str]) -> None:
    """The strings in ``value`` that ``masked`` hides (the same shape, secrets replaced)."""
    if isinstance(value, str) and isinstance(masked, str) and value != masked and sp.is_masked(masked):
        out.append(value)
    elif isinstance(value, dict) and isinstance(masked, dict):
        for k, v in value.items():
            _collect(v, masked.get(k), out)
    elif isinstance(value, list) and isinstance(masked, list):
        for v, m in zip(value, masked, strict=False):
            _collect(v, m, out)


def _value_at(dump: dict[str, Any], path: str) -> Any:
    try:
        return sp.get_at(dump, path)
    except sp.PathError:
        return None


def _parent(path: str) -> str:
    """``mcp_servers[name=x]`` → ``mcp_servers``; ``budgets.task`` → ``budgets``."""
    if path.endswith("]"):
        return path[: path.rfind("[")]
    return path.rsplit(".", 1)[0] if "." in path else ""


def _undo(draft: dict[str, Any], f: ChangeField) -> None:
    if f.op == "add":
        sp.remove_at(draft, f.path)
    elif f.op == "remove":
        parent = _parent(f.path)
        target = sp.get_at(draft, parent) if parent else draft
        if isinstance(target, list):
            target.append(f.old)
        else:
            sp.set_at(draft, f.path, f.old)
    else:
        sp.set_at(draft, f.path, f.old)


_SCHEDULE_FIELDS = (
    "name",
    "prompt",
    "cron",
    "at",
    "tz",
    "enabled",
    "catch_up",
    "think",
    "think_level",
    "call_me",
    "prompt_by",
    "project_id",
)


def _schedule_fields(values: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in values.items() if k in _SCHEDULE_FIELDS}
    if isinstance(out.get("at"), str):
        out["at"] = datetime.fromisoformat(out["at"])
    return out


# --- the tools: settings.* ----------------------------------------------------------------


class _GetArgs(BaseModel):
    path: str = Field(
        default="",
        description="'' = the list of sections; 'voice' = a section; 'voice.max_turn_s' = one setting; "
        "list items by key: 'mcp_servers[name=fetch]'.",
    )
    reveal: bool = Field(default=False, description="Show secrets in clear. Only when Arsen asks for one.")


class _SetArgs(BaseModel):
    set: dict[str, Any] = Field(default_factory=dict, description="path -> new value, e.g. {'voice.hold_volume': 0.3}")
    add: dict[str, Any] = Field(default_factory=dict, description="list path -> item to append")
    remove: list[str] = Field(default_factory=list, description="item paths to remove, e.g. 'mcp_servers[name=x]'")
    reason: str = Field(default="", description="One line: why (shown on the change card).")
    dry_run: bool = Field(default=False, description="Validate and show the diff; change nothing.")


class _RevertArgs(BaseModel):
    change_id: str = Field(description="The chg_… id from a change card or /api/changes.")
    reason: str = ""


class SettingsTools(BuiltinProvider):
    """``settings.get / set / revert``: the precise tools for Jarvis's own settings."""

    name = "settings"

    def __init__(self, core: Core) -> None:
        self.core = core
        super().__init__()

    @property
    def admin(self) -> SettingsAdmin:
        return self.core.admin

    @property
    def configured(self) -> bool:
        return self.core.settings.agent_admin.enabled

    @tool(
        "settings.get",
        description=(
            "Read Jarvis's own settings - anything on the Settings screen - with what each one means. Start "
            "with no path for the sections."
        ),
        args=_GetArgs,
        read_only=True,
        idempotent=True,
        owner_only=True,
    )
    async def _get(self, path: str = "", reveal: bool = False) -> ToolResult:
        dump = self.core.settings.model_dump(mode="json")
        try:
            if not path.strip():
                return ToolResult.data(_index(dump))
            value = sp.get_at(dump, path)
            res = sp.resolve(path)
        except sp.PathError as exc:
            return ToolResult.failure(str(exc))
        shown_value = value if reveal else sp.mask(value, res.tp, secret="secret" in res.risk)
        if reveal:
            found: list[str] = []
            _collect(value, sp.mask(value, res.tp, secret="secret" in res.risk), found)
            self.admin.note_revealed(current_run_id.get(), found)
        return ToolResult.data(_render(path, shown_value, res))

    async def assess(self, name: str, arguments: dict[str, Any]) -> CallRisk | None:
        if name == "settings.set":
            try:
                args = _SetArgs.model_validate(arguments)
                plan = self.admin.plan(args.set, args.add, args.remove)
            except (ValidationError, SettingsInvalid, sp.PathError):
                return None  # it fails at call time with the validator's own words
            if args.dry_run:
                return None
            risk, why = self.admin.assess_plan(plan)
            if not risk:
                return None
            preview = [shown_field(f) for f in change_fields(plan.leaves)]
            request = {"set": args.set, "add": args.add, "remove": args.remove}

            async def propose() -> ToolResult:
                rec = await self.admin.propose(plan, who=await who_now(self.core), reason=args.reason, request=request)
                return _proposed(rec, why)

            return CallRisk(reason=why, risk=risk, preview=preview, propose=propose)
        if name == "settings.revert":
            rec = await self.admin.changes.get(str(arguments.get("change_id", "")))
            if rec is None:
                return None
            risk = sorted(
                {r for r in rec.risk if r != "delete"}
                | ({"delete"} if any(f.op == "add" for f in rec.fields) else set())
            )
            if not risk:
                return None
            return CallRisk(
                reason=f"reverting {rec.id} undoes a change that " + ", ".join(_RISK_WORDS.get(r, r) for r in risk),
                risk=risk,
                preview=[shown_field(f.model_copy(update={"old": f.new, "new": f.old})) for f in rec.fields],
            )
        return None

    @tool(
        "settings.set",
        description=(
            "Change Jarvis's own settings, validated like the Settings screen. Paths as in settings.get. When "
            "Arsen names no value, propose one and ask instead of guessing. Risky changes ask him first; "
            "everything applies at once with a change card and Revert."
        ),
        args=_SetArgs,
        owner_only=True,
    )
    async def _set(
        self,
        set: dict[str, Any] | None = None,
        add: dict[str, Any] | None = None,
        remove: list[str] | None = None,
        reason: str = "",
        dry_run: bool = False,
        idempotency_key: str = "",
    ) -> ToolResult:
        try:
            plan = self.admin.plan(set or {}, add or {}, remove or [])
        except SettingsInvalid as exc:
            return ToolResult.failure(f"nothing was changed - {exc}")
        except sp.PathError as exc:
            return ToolResult.failure(f"nothing was changed - {exc}")
        if not plan.leaves:
            return ToolResult.empty("Nothing to change: the settings already have these values.")
        if dry_run:
            lines = diff_lines(change_fields(plan.leaves))
            risk, why = self.admin.assess_plan(plan)
            tail = f"\nIt would ask Arsen first: {why}." if risk else ""
            return ToolResult.data("Would change:\n" + "\n".join(lines) + tail)
        try:
            rec = await self.admin.apply(plan, who=await who_now(self.core, idempotency_key), reason=reason)
        except ChangeRefused as exc:
            return ToolResult.failure(str(exc))
        if rec is None:
            return ToolResult.empty("Nothing to change.")
        return _changed(rec)

    @tool(
        "settings.revert",
        description="Undo a settings or schedule change by its change id, field by field. Refused if changed since.",
        args=_RevertArgs,
        owner_only=True,
    )
    async def _revert(self, change_id: str, reason: str = "", idempotency_key: str = "") -> ToolResult:
        try:
            rec = await self.admin.revert(change_id, who=await who_now(self.core, idempotency_key))
        except (ChangeRefused, SettingsInvalid, sp.PathError) as exc:
            return ToolResult.failure(str(exc))
        return _changed(rec, head=f"Reverted {change_id}.")


def shown_field(f: ChangeField, area: str = "settings") -> ChangeField:
    if not f.secret:
        return f
    return f.model_copy(update={"old": _mask(area, f.path, f.old), "new": _mask(area, f.path, f.new)})


def _changed(rec: ChangeRecord, *, head: str | None = None) -> ToolResult:
    lines = diff_lines(rec.fields, rec.area)
    first = head or ("Changed " + (lines[0] if len(lines) == 1 else f"{len(lines)} settings") + ".")
    tail = ""
    if rec.status == "probing":
        tail = f"\nIt is checked again in a moment and put back on its own if it stops answering. {rec.note}".rstrip()
    body = "\n".join(lines) if len(lines) > 1 or head else ""
    result = ToolResult.data(
        f"{first}\n{body}\n(change {rec.id}; Arsen can revert it from the card){tail}".replace("\n\n", "\n")
    )
    return result.model_copy(update={"change": shown(rec).model_dump(mode="json")})


def _proposed(rec: ChangeRecord, why: str) -> ToolResult:
    lines = diff_lines(rec.fields, rec.area)
    text = (
        f"Proposed, NOT applied ({rec.id}): {why or 'it needs Arsen'}. Nobody can say yes in this run, so it waits "
        "for Arsen's Apply in the chat or Settings. Say so in your report.\n" + "\n".join(lines)
    )
    return ToolResult.data(text).model_copy(update={"change": shown(rec).model_dump(mode="json")})


def _index(dump: dict[str, Any]) -> str:
    lines = ["Settings sections (settings.get path=<name> for one):"]
    for name in Settings.model_fields:
        value = dump.get(name)
        res = sp.resolve(name)
        if isinstance(value, dict):
            inner = res.tp
            what = f"{len(value)} entries" if get_origin(inner) is dict else f"{len(value)} fields"
            lines.append(f"- {name}: {what}")
        elif isinstance(value, list):
            key = res.key
            names = [str(v.get(key)) for v in value if isinstance(v, dict)] if key else []
            lines.append(f"- {name}: {len(value)} items" + (f" ({', '.join(names[:8])})" if names else ""))
        else:
            lines.append(f"- {name} = {_short(sp.mask(value, res.tp, secret='secret' in res.risk), 80)}")
    return "\n".join(lines)


def _limits(path: str) -> str:
    steps = sp.parse(path)
    if not steps or steps[-1].name is None:
        return ""
    try:
        owner = sp.unwrap(sp.resolve(steps[:-1]).tp) if len(steps) > 1 else Settings
    except sp.PathError:
        return ""
    if get_origin(owner) is list:
        owner = sp.unwrap(get_args(owner)[0])
    if not (isinstance(owner, type) and issubclass(owner, BaseModel)) or steps[-1].name not in owner.model_fields:
        return ""
    info = owner.model_fields[steps[-1].name]
    bits = []
    with contextlib.suppress(Exception):
        bits.append(
            f"default {_short(json.loads(json.dumps(info.get_default(call_default_factory=True), default=str)), 60)}"
        )
    for meta in info.metadata:
        for attr, word in (("ge", "≥"), ("gt", ">"), ("le", "≤"), ("lt", "<")):
            if (v := getattr(meta, attr, None)) is not None:
                bits.append(f"{word} {v}")
    ann = sp.unwrap(info.annotation)
    if get_origin(ann) is not None and str(get_origin(ann)).endswith("Literal"):
        bits.append("one of " + ", ".join(repr(a) for a in get_args(ann)))
    elif hasattr(ann, "__members__"):
        bits.append("one of " + ", ".join(repr(m.value) for m in ann))  # type: ignore[union-attr]
    return "; ".join(bits)


def _render(path: str, value: Any, res: sp.Resolved) -> str:
    meaning = sp.meaning(path)
    head = f"{path}" + (f" [{', '.join(sorted(res.risk))}]" if res.risk else "")
    fields = sp.section_fields(path) if isinstance(value, dict) else []
    if fields:
        lines = [head + (f" — {meaning}" if meaning else "")]
        for name in fields:
            sub = f"{path}.{name}"
            text = _short(value.get(name), 10_000)
            if len(text) > _LONG:
                text = f"<{len(text)} chars> (settings.get path={sub} for the whole value)"
            what = sp.meaning(sub)
            lines.append(f"- {name} = {text}" + (f"  — {what[:200]}" if what else ""))
        return "\n".join(lines)
    out = [f"{head} = {json.dumps(value, ensure_ascii=False, default=str)}"]
    if limits := _limits(path):
        out.append(f"({limits})")
    if meaning:
        out.append(meaning)
    return "\n".join(out)
