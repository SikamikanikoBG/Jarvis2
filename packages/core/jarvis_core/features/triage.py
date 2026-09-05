"""Background email triage through the host's Outlook tools.

Zero-LLM cursor poll (``<host>.outlook_list since=cursor``) → deterministic demand routing
first (a ``DM-1234`` literally present in the subject/preview is a structural fact) → the
``triage`` role classifies the rest (thinking off) → ``<host>.outlook_move``. Every decision
is persisted before the move, so a crash re-does nothing; the day's triage conversation
gets one compact message per batch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis_core.models.base import ModelTextChunk
from jarvis_proto import ConversationKind, Message, TriageState
from jarvis_proto.events import ConversationUpdated, MessageCreated

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

_DEMAND = re.compile(r"(?<![A-Z0-9])(DM-\d{3,7})(?!\d)")

_CLASSIFY = """Classify this email for filing. Categories (name: rule):
{categories}
- none: nothing above applies

Email:
From: {sender}
Subject: {subject}
Preview: {preview}

Answer JSON only: {{"category": "<name or none>"}}"""


@dataclass(slots=True)
class TriageReport:
    processed: int = 0
    routed: int = 0
    errors: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)


class TriageJob:
    def __init__(self, core: Core) -> None:
        self.core = core
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="triage")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            interval = max(1, self.core.settings.triage.interval_min) * 60
            await asyncio.sleep(interval)
            if self.core.settings.triage.enabled:
                try:
                    await self.run_once()
                except Exception:
                    log.exception("triage run failed")

    # --- state -------------------------------------------------------------------------

    async def states(self) -> list[TriageState]:
        rows = await self.core.db.fetchall("SELECT * FROM triage_state ORDER BY account")
        return [
            TriageState(
                account=r["account"],
                cursor=r["cursor"],
                day=r["day"],
                processed_today=r["processed_today"],
                routed_today=r["routed_today"],
                last_run_at=datetime.fromisoformat(r["last_run_at"]) if r["last_run_at"] else None,
                last_error=r["last_error"],
            )
            for r in rows
        ]

    async def _state(self, account: str) -> TriageState:
        row = await self.core.db.fetchone("SELECT * FROM triage_state WHERE account = ?", (account,))
        if row is None:
            return TriageState(account=account)
        return (await self.states())[[s.account for s in await self.states()].index(account)]

    async def _save_state(self, s: TriageState) -> None:
        await self.core.db.execute(
            "INSERT INTO triage_state(account, cursor, day, processed_today, routed_today, last_run_at, last_error)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(account) DO UPDATE SET cursor=excluded.cursor, day=excluded.day,"
            " processed_today=excluded.processed_today, routed_today=excluded.routed_today,"
            " last_run_at=excluded.last_run_at, last_error=excluded.last_error",
            (
                s.account,
                s.cursor,
                s.day,
                s.processed_today,
                s.routed_today,
                s.last_run_at.isoformat() if s.last_run_at else None,
                s.last_error,
            ),
        )

    async def _decided(self, entry_id: str, account: str) -> bool:
        row = await self.core.db.fetchone(
            "SELECT 1 FROM triage_decisions WHERE entry_id = ? AND account = ?", (entry_id, account)
        )
        return row is not None

    async def _record(self, entry_id: str, account: str, category: str | None, action: str) -> None:
        await self.core.db.execute(
            "INSERT OR REPLACE INTO triage_decisions(entry_id, account, category, action, run_id, at) VALUES (?,?,?,?,NULL,?)",
            (entry_id, account, category, action, datetime.now(UTC).isoformat()),
        )

    # --- the job -------------------------------------------------------------------------

    async def run_once(self) -> TriageReport:
        async with self._lock:
            return await self._run()

    async def _run(self) -> TriageReport:
        cfg = self.core.settings.triage
        report = TriageReport()
        if not cfg.host:
            report.errors.append("triage.host is not set (name of the jarvis-host MCP server)")
            return report
        accounts = list(cfg.accounts) or await self._discover_accounts(cfg.host, report)
        today = datetime.now(ZoneInfo(self.core.settings.timezone)).strftime("%Y-%m-%d")
        for account in accounts:
            report.accounts.append(account)
            state = await self._state(account)
            if state.day != today:
                state.day, state.processed_today, state.routed_today = today, 0, 0
            try:
                items, cursor = await self._list(cfg.host, account, state.cursor)
            except Exception as exc:
                state.last_error = f"list failed: {exc}"
                report.errors.append(f"{account}: {state.last_error}")
                await self._save_state(state)
                continue
            lines: list[str] = []
            for item in items:
                entry_id = str(item.get("entry_id") or "")
                if not entry_id or await self._decided(entry_id, account):
                    continue
                category, folder = await self._route(item, cfg)
                action = "left"
                if folder:
                    await self._record(entry_id, account, category, f"move:{folder}")
                    try:
                        res = await self.core.registry.call(
                            f"{cfg.host}.outlook_move",
                            {"entry_id": entry_id, "folder": folder},
                            cancel=asyncio.Event(),
                            idempotency_key=f"triage:{account}:{entry_id}",
                            timeout_s=60,
                        )
                        action = f"moved → {folder}" if res.kind.value != "error" else f"move failed: {res.text[:80]}"
                        if res.kind.value != "error":
                            state.routed_today += 1
                    except Exception as exc:
                        action = f"move failed: {exc}"
                else:
                    await self._record(entry_id, account, category, "left")
                state.processed_today += 1
                report.processed += 1
                lines.append(f"- {item.get('sender', '?')} — {str(item.get('subject', ''))[:70]} → {action}")
            report.routed = state.routed_today
            if cursor:
                state.cursor = cursor
            state.last_run_at = datetime.now(UTC)
            state.last_error = None
            await self._save_state(state)
            if lines:
                await self._append_summary(account, today, lines)
        return report

    async def _discover_accounts(self, host: str, report: TriageReport) -> list[str]:
        res = await self.core.registry.call(
            f"{host}.outlook_accounts", {}, cancel=asyncio.Event(), idempotency_key="triage:accounts"
        )
        if res.kind.value == "error":
            report.errors.append(f"outlook_accounts: {res.text[:120]}")
            return []
        data = _json(res.text)
        accounts = data.get("accounts") if isinstance(data, dict) else data
        out: list[str] = []
        for a in accounts or []:
            name = a.get("name") if isinstance(a, dict) else str(a)
            if name:
                out.append(str(name))
        return out

    async def _list(self, host: str, account: str, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        args: dict[str, Any] = {"account": account, "folder": "Inbox", "limit": 50}
        if cursor:
            args["since"] = cursor
        res = await self.core.registry.call(
            f"{host}.outlook_list",
            args,
            cancel=asyncio.Event(),
            idempotency_key=f"triage:list:{account}",
            timeout_s=120,
        )
        if res.kind.value == "error":
            raise RuntimeError(res.text[:200])
        data = _json(res.text)
        if isinstance(data, dict):
            items = [i for i in data.get("items", []) if isinstance(i, dict)]
            newest = data.get("newest") or data.get("cursor_since")
            next_cursor = newest or max((str(i.get("received", "")) for i in items), default=None) or cursor
            return items, next_cursor
        if isinstance(data, list):
            items = [i for i in data if isinstance(i, dict)]
            return items, max((str(i.get("received", "")) for i in items), default=cursor)
        return [], cursor

    async def _route(self, item: dict[str, Any], cfg: Any) -> tuple[str | None, str | None]:
        subject = str(item.get("subject") or "")
        preview = str(item.get("preview") or item.get("body_preview") or "")
        for prefix in cfg.demand_prefixes:
            pattern = re.compile(r"(?<![A-Z0-9])(" + re.escape(prefix) + r"\d{3,7})(?!\d)")
            m = pattern.search(subject) or pattern.search(preview)
            if m:
                return "demand", f"{cfg.demand_root}/{m.group(1)}"
        if not cfg.categories:
            return None, None
        cats = "\n".join(f"- {c.get('name')}: {c.get('rule', '')}" for c in cfg.categories if c.get("name"))
        prompt = _CLASSIFY.format(
            categories=cats, sender=item.get("sender", ""), subject=subject[:200], preview=preview[:500]
        )
        try:
            from jarvis_proto.settings import RoleName

            adapter = self.core.adapters.for_role(RoleName.TRIAGE)
            text = ""
            async for chunk in adapter.stream([Message.user(prompt)], [], cancel=asyncio.Event()):
                if isinstance(chunk, ModelTextChunk):
                    text += chunk.text
            data = _json(text)
            name = str(data.get("category", "none")).strip() if isinstance(data, dict) else "none"
        except Exception as exc:
            log.warning("triage classify skipped: %s", exc)
            return None, None
        for c in cfg.categories:
            if c.get("name") == name and c.get("folder"):
                return name, str(c["folder"])
        return None, None

    async def _append_summary(self, account: str, day: str, lines: list[str]) -> None:
        store = self.core.store
        convs = await store.list_conversations(include_archived=True)
        conv = next(
            (c for c in convs if c.kind is ConversationKind.TRIAGE and c.folder_key == account and c.title == day), None
        )
        if conv is None:
            conv = await store.create_conversation(
                kind=ConversationKind.TRIAGE, title=day, folder_key=account, folder_label=account
            )
            self.core.bus.publish(ConversationUpdated(conversation=conv))
        text = (
            f"Triage {datetime.now(UTC).astimezone(ZoneInfo(self.core.settings.timezone)).strftime('%H:%M')} — {len(lines)} new\n"
            + "\n".join(lines[:60])
        )
        msg = await store.add_message(Message.assistant(text, conversation_id=conv.id, name="triage"))
        self.core.bus.publish(MessageCreated(message=msg))
        conv = await store.get_conversation(conv.id)
        if conv:
            self.core.bus.publish(ConversationUpdated(conversation=conv))


def _json(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(text[start : end + 1])
    return None
