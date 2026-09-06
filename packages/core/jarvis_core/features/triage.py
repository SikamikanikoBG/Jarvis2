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

_CLASSIFY = """Classify this email for filing.
{instructions}
Categories (name: rule):
{categories}
- none: nothing above applies

Email:
From: {sender}
To: {to}
Cc: {cc}
Subject: {subject}
Preview: {preview}

Answer JSON only: {{"category": "<name or none>"}}"""


@dataclass(slots=True)
class TriageReport:
    processed: int = 0
    routed: int = 0
    errors: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    dry_run: bool = False
    # dry run: what WOULD happen, per mail: {account, sender, subject, category, folder}
    proposed: list[dict[str, str]] = field(default_factory=list)
    # VIP / keyword alerts this pass tripped: {account, alert, sender, subject, folder}
    alerts: list[dict[str, str]] = field(default_factory=list)


_WELL_KNOWN = {"inbox", "sent", "drafts", "deleted", "trash", "junk", "spam", "outbox", "archive"}


class TriageJob:
    def __init__(self, core: Core) -> None:
        self.core = core
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._folders_ready: set[str] = set()  # accounts whose category folders exist

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

    async def _forget(self, entry_id: str, account: str) -> None:
        """Undo the pre-move record when the move did not happen, so the mail stays retryable."""
        await self.core.db.execute(
            "DELETE FROM triage_decisions WHERE entry_id = ? AND account = ?", (entry_id, account)
        )

    # --- the job -------------------------------------------------------------------------

    async def run_once(
        self, *, dry_run: bool = False, folder: str | None = None, limit: int | None = None, account: str | None = None
    ) -> TriageReport:
        async with self._lock:
            return await self._run(dry_run=dry_run, folder=folder, limit=limit, only_account=account)

    async def _run(
        self,
        *,
        dry_run: bool = False,
        folder: str | None = None,
        limit: int | None = None,
        only_account: str | None = None,
    ) -> TriageReport:
        """One pass. ``dry_run`` classifies and reports the moves it would make - nothing is
        moved, recorded, advanced or written to the triage conversation. With ``folder`` the dry
        run samples the newest ``limit`` messages of an ALREADY-SORTED folder instead of the
        inbox, so the proposal can be compared with where they actually live: that is the
        accuracy check for a new category set before it goes live."""
        cfg = self.core.settings.triage
        report = TriageReport(dry_run=dry_run)
        if folder and not dry_run:
            report.errors.append("a folder sample is only allowed as a dry run")
            return report
        if not cfg.host:
            report.errors.append("triage.host is not set (name of the jarvis-host MCP server)")
            return report
        accounts = list(cfg.accounts) or await self._discover_accounts(cfg.host, report)
        if only_account:
            wanted = only_account.strip().lower()
            accounts = [a for a in accounts if a.strip().lower() == wanted] or [only_account]
        today = datetime.now(ZoneInfo(self.core.settings.timezone)).strftime("%Y-%m-%d")
        for account in accounts:
            report.accounts.append(account)
            rules = cfg.rules_for(account)
            state = await self._state(account)
            if state.day != today:
                state.day, state.processed_today, state.routed_today = today, 0, 0
            if not dry_run:
                wanted = [*rules.folders(), *([cfg.demand_root] if rules.demand_routing else [])]
                await self._ensure_folders(cfg.host, account, wanted, report)
            try:
                if folder:
                    items, cursor = await self._list(cfg.host, account, None, folder=folder, limit=limit or 30)
                else:
                    items, cursor = await self._list(cfg.host, account, state.cursor, limit=limit or 50)
            except Exception as exc:
                state.last_error = f"list failed: {exc}"
                report.errors.append(f"{account}: {state.last_error}")
                await self._save_state(state)
                continue
            lines: list[str] = []
            stuck = 0  # mails this pass decided on but could not move
            for item in items:
                entry_id = str(item.get("entry_id") or "")
                if not entry_id or (not dry_run and await self._decided(entry_id, account)):
                    continue
                category, target = await self._route(item, cfg, account, rules)
                subject = str(item.get("subject") or "")
                # Structural, before any judgement: a VIP mail must never be missed because a
                # classifier had an opinion about it.
                alert = rules.alert_for(self._sender_address(item), self._sender(item), subject)
                if alert:
                    report.alerts.append(
                        {
                            "account": account,
                            "alert": alert,
                            "sender": self._sender(item) or self._sender_address(item),
                            "subject": subject[:120],
                            "folder": target or "Inbox",
                        }
                    )
                if dry_run:
                    report.processed += 1
                    if target:
                        report.routed += 1
                    report.proposed.append(
                        {
                            "account": account,
                            "sender": self._sender(item),
                            "subject": subject[:90],
                            "category": category or "",
                            "folder": target or "(leave in Inbox)",
                            "current_folder": folder or "Inbox",
                            "alert": alert or "",
                        }
                    )
                    continue
                action = "left"
                if target:
                    # Recorded BEFORE the move, so a crash between the two re-does nothing.
                    await self._record(entry_id, account, category, f"move:{target}")
                    failure = ""
                    try:
                        res = await self.core.registry.call(
                            f"{cfg.host}.outlook_move",
                            {"entry_id": entry_id, "folder": target, "account": account, "create": True},
                            cancel=asyncio.Event(),
                            idempotency_key=f"triage:{account}:{entry_id}",
                            timeout_s=60,
                        )
                        failure = res.text[:120] if res.kind.value == "error" else ""
                    except Exception as exc:
                        failure = str(exc)[:120]
                    if failure:
                        # The mail is still in the Inbox. Drop the decision row so the next pass
                        # can try again, and say so out loud: this used to leave a row claiming
                        # the move, and the cursor moved past the mail, so it was never seen
                        # again by anything — with report.errors empty.
                        action = f"move failed: {failure}"
                        await self._forget(entry_id, account)
                        report.errors.append(f"{account}: {subject[:60]!r} not moved to {target}: {failure}")
                        stuck += 1
                    else:
                        action = f"moved → {target}"
                        state.routed_today += 1
                        report.routed += 1
                else:
                    await self._record(entry_id, account, category, "left")
                state.processed_today += 1
                report.processed += 1
                mark = f"[ALERT {alert}] " if alert else ""
                lines.append(f"- {mark}{self._sender(item) or '?'} — {subject[:70]} → {action}")
            if dry_run:
                continue  # nothing is persisted on a dry run
            # The cursor is a high-water mark over `received`, and the host lists the NEWEST
            # mails above it — so holding it back re-offers the ones that failed without
            # starving new arrivals, while advancing it would retire them from triage for good.
            # Everything already decided is skipped by a single indexed lookup, so a held
            # cursor costs one list call, not a re-classification.
            if cursor and not stuck:
                state.cursor = cursor
            state.last_run_at = datetime.now(UTC)
            state.last_error = next((e for e in report.errors if e.startswith(f"{account}:")), None)
            await self._save_state(state)
            if lines:
                await self._append_summary(account, today, lines)
        if report.alerts and not dry_run:
            await self._push_alerts(report)
        return report

    async def _push_alerts(self, report: TriageReport) -> None:
        """One push per pass, not per mail: a VIP thread of five replies is one buzz."""
        head = f"🔔 {len(report.alerts)} mail(s) you asked to be told about:"
        body = "\n".join(f"• [{a['alert']}] {a['sender']} — {a['subject']}" for a in report.alerts[:15])
        try:
            res = await self.core.registry.call(
                "notify.discord",
                {"text": f"{head}\n{body}"},
                cancel=asyncio.Event(),
                idempotency_key=f"triage:alert:{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
                timeout_s=30,
            )
            if res.kind.value == "error":
                report.errors.append(f"alert not delivered: {res.text[:120]}")
        except Exception as exc:
            report.errors.append(f"alert not delivered: {exc}")

    async def _ensure_folders(self, host: str, account: str, folders: list[str], report: TriageReport) -> None:
        """Create the category folders once per account per process (a fresh Gmail store has
        none of them). Well-known roles (deleted, junk, …) need no creating."""
        if account in self._folders_ready:
            return
        for folder in folders:
            if not folder or ("/" not in folder and folder.lower() in _WELL_KNOWN):
                continue
            try:
                res = await self.core.registry.call(
                    f"{host}.outlook_folder_create",
                    {"path": folder, "account": account},
                    cancel=asyncio.Event(),
                    idempotency_key=f"triage:mkdir:{account}:{folder}",
                    timeout_s=60,
                )
                if res.kind.value == "error":
                    report.errors.append(f"{account}: folder {folder!r}: {res.text[:120]}")
                    return  # leave the account unmarked: try again next pass
            except Exception as exc:
                report.errors.append(f"{account}: folder {folder!r}: {exc}")
                return
        self._folders_ready.add(account)

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

    async def _list(
        self, host: str, account: str, cursor: str | None, *, folder: str = "Inbox", limit: int = 50
    ) -> tuple[list[dict[str, Any]], str | None]:
        # A long preview so the demand rule can tell a digest (many DM ids) from a thread (one).
        args: dict[str, Any] = {"account": account, "folder": folder, "limit": limit, "preview_chars": 1500}
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

    @staticmethod
    def _sender(item: dict[str, Any]) -> str:
        """The host sends ``from: {name, address}`` plus flat aliases; be tolerant of both."""
        sender = item.get("sender")
        if isinstance(sender, str) and sender.strip():
            return sender
        frm = item.get("from")
        if isinstance(frm, dict):
            return str(frm.get("name") or frm.get("address") or "")
        return str(frm or "")

    @staticmethod
    def _sender_address(item: dict[str, Any]) -> str:
        addr = item.get("sender_address")
        if isinstance(addr, str) and addr.strip():
            return addr.strip()
        frm = item.get("from")
        if isinstance(frm, dict):
            return str(frm.get("address") or "")
        return ""

    async def _body(self, host: str, account: str, entry_id: str, max_chars: int = 4000) -> str | None:
        """First ``max_chars`` of the real body, or None when it cannot be read."""
        try:
            res = await self.core.registry.call(
                f"{host}.outlook_read",
                {"entry_id": entry_id, "account": account, "max_chars": max_chars},
                cancel=asyncio.Event(),
                idempotency_key=f"triage:read:{account}:{entry_id}",
                timeout_s=60,
            )
        except Exception as exc:
            log.warning("triage body read failed: %s", exc)
            return None
        if res.kind.value == "error":
            return None
        data = _json(res.text)
        if isinstance(data, dict):
            return str(data.get("body") or data.get("text") or "")
        return res.text

    async def _route(
        self, item: dict[str, Any], cfg: Any, account: str = "", rules: Any = None
    ) -> tuple[str | None, str | None]:
        rules = rules if rules is not None else cfg.rules_for(account)
        subject = str(item.get("subject") or "")
        preview = str(item.get("preview") or item.get("body_preview") or item.get("snippet") or "")
        if rules.demand_routing and demand_ids(subject, cfg.demand_prefixes):
            return "demand", demand_folder(subject, "", prefixes=cfg.demand_prefixes, root=cfg.demand_root)
        if rules.demand_routing and demand_ids(preview, cfg.demand_prefixes):
            # A demand named only in the body: the table preview is too short to tell a thread
            # (one demand) from a digest (many), so read the body the way V1 did (4,000 chars).
            entry_id = str(item.get("entry_id") or "")
            body = await self._body(cfg.host, account, entry_id) if entry_id else None
            demand = demand_folder(subject, body or preview, prefixes=cfg.demand_prefixes, root=cfg.demand_root)
            if demand:
                return "demand", demand
        if not rules.categories:
            return None, None
        cats = "\n".join(f"- {c.get('name')}: {c.get('rule', '')}" for c in rules.categories if c.get("name"))
        instructions = str(rules.instructions or "").strip()
        address = self._sender_address(item)
        sender = f"{self._sender(item)} <{address}>" if address else self._sender(item)
        prompt = _CLASSIFY.format(
            instructions=f"\nRules:\n{instructions}\n" if instructions else "",
            categories=cats,
            sender=sender,
            to=str(item.get("to") or "")[:300],
            cc=str(item.get("cc") or "")[:300],
            subject=subject[:200],
            preview=preview[:500],
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
        for c in rules.categories:
            if c.get("name") == name and c.get("folder"):
                return name, str(c["folder"])
        # "none" or an unknown name: the configured catch-all, if there is one (inbox zero).
        fallback = str(rules.fallback_category or "")
        for c in rules.categories:
            if fallback and c.get("name") == fallback and c.get("folder"):
                return fallback, str(c["folder"])
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


def _demand_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Z0-9])(" + re.escape(prefix) + r"\d{3,7})(?!\d)", re.IGNORECASE)


def demand_ids(text: str, prefixes: list[str]) -> set[str]:
    """Distinct demand ids literally present in ``text`` (upper-cased), e.g. {"DM-1234"}."""
    out: set[str] = set()
    for prefix in prefixes:
        out.update(x.upper() for x in _demand_pattern(prefix).findall(text or ""))
    return out


def demand_folder(subject: str, body: str, *, prefixes: list[str], root: str) -> str | None:
    """Deterministic demand routing (V1's rules, kept because they were tuned on real mail).

    A demand id literally in the SUBJECT wins (the first one). A body-only match counts only when
    the body names exactly ONE distinct demand: a body naming several with none in the subject is
    a digest ("Jira Email Summary", "your demands this week") and must not be filed under the
    first id it happens to mention. Returns ``root/PREFIX-1234`` or None.
    """
    for prefix in prefixes:
        m = _demand_pattern(prefix).search(subject or "")
        if m:
            return f"{root}/{m.group(1).upper()}"
    found = demand_ids(body, prefixes)
    if len(found) == 1:
        return f"{root}/{found.pop()}"
    return None


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
