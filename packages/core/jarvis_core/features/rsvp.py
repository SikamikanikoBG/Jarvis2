"""Meeting auto-RSVP through the host's calendar tools (docs/stories/08_meeting_rsvp.md).

Every ``interval_min``: drop cancelled meetings → ``<host>.calendar_invites`` (pending invites,
each with the committed meetings that clash) → per occurrence not yet in the ledger:

* organizer outside ``allowed_domains`` (and not a VIP) → left for Arsen, recorded as such;
* no clash → accept;
* VIP + clash → accept anyway and say so (a boss is never declined);
* clash → decline, proposing up to N free alternatives from ``<host>.calendar_free_slots``.

The ledger key is ``organizer|subject|start`` (migration 0003 explains why not the EntryID).
A dry run walks the same path and reports the decisions without calling anything that sends.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis_proto import ConversationKind, Message, RsvpDecision, RsvpState
from jarvis_proto.events import ConversationUpdated, MessageCreated

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

ANSWERED = {"accept", "decline", "accept_vip_conflict"}
_BG_DOW = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


def ledger_key(organizer: str, subject: str, start: str) -> str:
    return f"{organizer.strip().lower()}|{subject.strip().lower()}|{start[:16]}"


@dataclass(slots=True)
class RsvpReport:
    dry_run: bool = False
    account: str = ""
    pending: int = 0
    decisions: list[RsvpDecision] = field(default_factory=list)
    removed_canceled: int = 0
    errors: list[str] = field(default_factory=list)


class RsvpJob:
    def __init__(self, core: Core) -> None:
        self.core = core
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="rsvp")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(max(1, self.core.settings.rsvp.interval_min) * 60)
            if self.core.settings.rsvp.enabled:
                try:
                    await self.run_once()
                except Exception:
                    log.exception("rsvp run failed")

    # --- state -------------------------------------------------------------------------

    async def state(self) -> RsvpState | None:
        account = self.core.settings.rsvp.account or ""
        row = await self.core.db.fetchone("SELECT * FROM rsvp_state WHERE account = ?", (account,))
        if row is None:
            return None
        return RsvpState(
            account=row["account"],
            last_run_at=datetime.fromisoformat(row["last_run_at"]) if row["last_run_at"] else None,
            last_error=row["last_error"],
            answered_total=row["answered_total"],
            removed_canceled_total=row["removed_canceled_total"],
        )

    async def decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.core.db.fetchall("SELECT * FROM rsvp_decisions ORDER BY at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    async def _save_state(self, s: RsvpState) -> None:
        await self.core.db.execute(
            "INSERT INTO rsvp_state(account, last_run_at, last_error, answered_total, removed_canceled_total)"
            " VALUES (?,?,?,?,?) ON CONFLICT(account) DO UPDATE SET last_run_at=excluded.last_run_at,"
            " last_error=excluded.last_error, answered_total=excluded.answered_total,"
            " removed_canceled_total=excluded.removed_canceled_total",
            (
                s.account,
                s.last_run_at.isoformat() if s.last_run_at else None,
                s.last_error,
                s.answered_total,
                s.removed_canceled_total,
            ),
        )

    async def _decided(self, key: str) -> bool:
        return await self.core.db.fetchone("SELECT 1 FROM rsvp_decisions WHERE key = ?", (key,)) is not None

    async def _record(self, key: str, d: RsvpDecision) -> None:
        await self.core.db.execute(
            "INSERT OR REPLACE INTO rsvp_decisions(key, account, subject, organizer, start, decision, detail, at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                key,
                d.account,
                d.subject,
                d.organizer_address or d.organizer,
                d.start,
                d.decision,
                d.detail,
                datetime.now(UTC).isoformat(),
            ),
        )

    # --- the job -------------------------------------------------------------------------

    async def run_once(self, *, dry_run: bool = False) -> RsvpReport:
        async with self._lock:
            return await self._run(dry_run=dry_run)

    async def _call(self, host: str, tool: str, args: dict[str, Any], *, key: str, timeout_s: float) -> Any:
        res = await self.core.registry.call(
            f"{host}.{tool}", args, cancel=asyncio.Event(), idempotency_key=key, timeout_s=timeout_s
        )
        if res.kind.value == "error":
            raise RuntimeError(f"{tool}: {res.text[:200]}")
        return _data(res.text)

    async def _run(self, *, dry_run: bool) -> RsvpReport:
        cfg = self.core.settings.rsvp
        report = RsvpReport(dry_run=dry_run, account=cfg.account)
        if not cfg.host:
            report.errors.append("rsvp.host is not set (name of the jarvis-host MCP server that owns the calendar)")
            return report
        if not cfg.allowed_domains and not cfg.vip:
            report.errors.append("rsvp.allowed_domains is empty: every invite would be left for you (fail closed)")
        state = await self.state() or RsvpState(account=cfg.account)
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")

        if cfg.remove_canceled and not dry_run:
            try:
                data = await self._call(
                    cfg.host,
                    "calendar_remove_canceled",
                    {"account": cfg.account, "days_back": 1, "days_ahead": 60},
                    key=f"rsvp:canceled:{stamp}",
                    timeout_s=150,
                )
                report.removed_canceled = int((data or {}).get("removed", 0) or 0)
                state.removed_canceled_total += report.removed_canceled
            except Exception as exc:
                report.errors.append(str(exc)[:200])

        try:
            data = await self._call(
                cfg.host,
                "calendar_invites",
                {"account": cfg.account, "days": cfg.lookahead_days},
                key=f"rsvp:invites:{stamp}",
                timeout_s=120,
            )
        except Exception as exc:
            state.last_error = str(exc)[:200]
            report.errors.append(state.last_error)
            if not dry_run:
                await self._save_state(state)
            return report
        invites = [i for i in (data or {}).get("invites", []) if isinstance(i, dict) and i.get("entry_id")]
        report.pending = len(invites)

        reserved: list[tuple[datetime, datetime]] = []  # slots already offered this pass
        lines: list[str] = []
        for inv in invites:
            key = ledger_key(
                str(inv.get("organizer_address") or inv.get("organizer") or ""),
                str(inv.get("subject") or ""),
                str(inv.get("start") or ""),
            )
            if not dry_run and await self._decided(key):
                continue
            decision = await self._decide(cfg, inv, reserved, key=key, dry_run=dry_run)
            report.decisions.append(decision)
            if dry_run:
                continue
            if decision.decision == "failed":
                # The invite is still unanswered, so it must NOT enter the ledger: a ledger row
                # is what stops the next pass from trying again. One COM hiccup used to retire
                # an invite for good, silently — nothing in the ledger says "failed" out loud
                # and report.errors was empty.
                report.errors.append(f"{decision.subject!r} from {decision.organizer}: {decision.detail}")
            else:
                await self._record(key, decision)
                if decision.decision in ANSWERED:
                    state.answered_total += 1
            lines.append(self._line(decision))

        if not dry_run:
            state.last_run_at = datetime.now(UTC)
            # A pass that could not answer something is not a clean pass: the status the UI reads
            # has to say so, or a stuck invite is invisible until someone misses the meeting.
            state.last_error = report.errors[0][:200] if report.errors else None
            await self._save_state(state)
            if lines:
                await self._append_summary(cfg.account, lines)
        return report

    async def _decide(
        self, cfg: Any, inv: dict[str, Any], reserved: list[tuple[datetime, datetime]], *, key: str, dry_run: bool
    ) -> RsvpDecision:
        address = str(inv.get("organizer_address") or "")
        conflicts = [c for c in inv.get("conflicts", []) if isinstance(c, dict)]
        d = RsvpDecision(
            account=cfg.account,
            subject=str(inv.get("subject") or "(no subject)"),
            organizer=str(inv.get("organizer") or ""),
            organizer_address=address,
            start=str(inv.get("start") or ""),
            end=str(inv.get("end") or ""),
            decision="",
            conflicts=[
                f"{c.get('subject', '')} {str(c.get('start', ''))[11:16]}-{str(c.get('end', ''))[11:16]}"
                for c in conflicts
            ],
        )
        if not cfg.is_allowed(address):
            d.decision = "left_external"
            d.detail = (
                "organizer outside allowed_domains; left for you"
                if address
                else "organizer address could not be resolved; left for you"
            )
            return d
        if not conflicts:
            d.decision = "accept"
            d.detail = "slot is free"
        elif cfg.is_vip(address):
            d.decision = "accept_vip_conflict"
            d.detail = "VIP invite accepted despite a clash - the double-booking is yours to resolve"
        else:
            d.decision = "decline"
            slots = await self._free_slots(cfg, inv, reserved)
            d.proposals = [f"{_BG_DOW[s.weekday()]} {s:%d.%m} {s:%H:%M}-{e:%H:%M}" for s, e in slots]
            d.detail = (
                f"clashes with {len(conflicts)} committed meeting(s); {len(slots)} alternative(s) proposed"
                if slots
                else f"clashes with {len(conflicts)} committed meeting(s); no free alternative in the lookahead window"
            )
        if dry_run:
            return d
        comment = self._decline_comment(d) if d.decision == "decline" else ""
        try:
            await self._call(
                cfg.host,
                "calendar_respond",
                {
                    "entry_id": str(inv["entry_id"]),
                    "decision": "decline" if d.decision == "decline" else "accept",
                    "comment": comment,
                    "account": cfg.account,
                },
                key=f"rsvp:respond:{key}",
                timeout_s=60,
            )
        except Exception as exc:
            d.decision = "failed"
            d.detail = str(exc)[:200]
        return d

    async def _free_slots(
        self, cfg: Any, inv: dict[str, Any], reserved: list[tuple[datetime, datetime]]
    ) -> list[tuple[datetime, datetime]]:
        try:
            s = datetime.fromisoformat(str(inv.get("start")))
            e = datetime.fromisoformat(str(inv.get("end")))
            duration = max(15, int((e - s).total_seconds() // 60))
        except ValueError:
            return []
        want = max(1, int(cfg.propose_slots))
        try:
            data = await self._call(
                cfg.host,
                "calendar_free_slots",
                {
                    "account": cfg.account,
                    "start": inv.get("start"),
                    "days": cfg.lookahead_days,
                    "duration_min": duration,
                    "work_start_hour": cfg.work_start_hour,
                    "work_end_hour": cfg.work_end_hour,
                    "limit": want + len(reserved) + 2,
                },
                key=f"rsvp:slots:{inv.get('entry_id')}",
                timeout_s=90,
            )
        except Exception as exc:
            log.warning("free slots unavailable: %s", exc)
            return []
        candidates: list[tuple[datetime, datetime]] = []
        for slot in (data or {}).get("slots", []):
            try:
                ss, ee = datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"])
            except (KeyError, ValueError):
                continue
            if any(ss < r_e and ee > r_s for r_s, r_e in reserved):
                continue  # already offered to another organizer this pass
            candidates.append((ss, ee))
        # Spread the offer: three consecutive half-hours are one option, not three. Prefer slots at
        # least two hours apart (or on another day); fill from the rest only if that leaves gaps.
        out: list[tuple[datetime, datetime]] = []
        for ss, ee in candidates:
            if len(out) >= want:
                break
            if all(ss.date() != o_s.date() or abs((ss - o_s).total_seconds()) >= 7200 for o_s, _ in out):
                out.append((ss, ee))
        for ss, ee in candidates:
            if len(out) >= want:
                break
            if (ss, ee) not in out:
                out.append((ss, ee))
        out.sort()
        reserved.extend(out)
        return out

    @staticmethod
    def _decline_comment(d: RsvpDecision) -> str:
        lines = ["Здравейте,", "", "за съжаление в този час имам друг ангажимент, който не мога да преместя."]
        if d.proposals:
            lines += ["Свободен съм в следващите часове:"] + [f"  - {p}" for p in d.proposals]
            lines.append("Ако някой от тях е удобен, моля преместете срещата там.")
        lines += ["", "Поздрави,", "Арсен", "(автоматичен отговор от Jarvis според календара)"]
        return "\n".join(lines)

    @staticmethod
    def _line(d: RsvpDecision) -> str:
        who = f"{d.organizer} <{d.organizer_address}>" if d.organizer_address else d.organizer
        head = f"{d.start[:16]} {d.subject!r} from {who}: {d.decision}"
        extra = f" - {d.detail}" if d.detail else ""
        prop = f"; proposed {', '.join(d.proposals)}" if d.proposals else ""
        return head + extra + prop

    async def _append_summary(self, account: str, lines: list[str]) -> None:
        store = self.core.store
        tz = ZoneInfo(self.core.settings.timezone)
        day = datetime.now(tz).strftime("%Y-%m-%d")
        folder_key = f"rsvp:{account or 'default'}"
        convs = await store.list_conversations(include_archived=True)
        conv = next(
            (c for c in convs if c.kind is ConversationKind.TRIAGE and c.folder_key == folder_key and c.title == day),
            None,
        )
        if conv is None:
            conv = await store.create_conversation(
                kind=ConversationKind.TRIAGE, title=day, folder_key=folder_key, folder_label="Calendar RSVP"
            )
            self.core.bus.publish(ConversationUpdated(conversation=conv))
        text = f"RSVP {datetime.now(tz).strftime('%H:%M')} — {len(lines)} invite(s)\n" + "\n".join(lines[:40])
        msg = await store.add_message(Message.assistant(text, conversation_id=conv.id, name="rsvp"))
        self.core.bus.publish(MessageCreated(message=msg))
        conv = await store.get_conversation(conv.id)
        if conv:
            self.core.bus.publish(ConversationUpdated(conversation=conv))


def _data(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(text[start : end + 1])
        return {}
