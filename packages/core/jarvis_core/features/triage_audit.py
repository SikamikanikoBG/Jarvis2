"""Triage quality audit: what the live triage did over the last day, checked twice.

Arsen asked for the quality of the triage to be watched, not assumed (2026-09-29: Reference had
swallowed most mail and Bosses held mail that was not from a boss, both unnoticed for weeks).
Every live decision is already recorded with its full input in the shadow log
(features/shadow.py, point ``mail``); the audit reads those rows and

* checks the structural invariants no judgement may break - nothing in a sender-gated category
  (Bosses) from a sender it does not admit, and no mail from a gated sender filed elsewhere;
* reports the mix per account - the share left in the Inbox and the share sent to the catch-all
  are the two numbers that drift first;
* asks the judge role for a second opinion on a sample (every rare category first, then the
  rest) and lists each disagreement with the reason, so the rules can be tuned from evidence.

The report goes to a ``Triage audit`` conversation (one per day) and a one-line push. Nothing
is moved: the audit only reads and reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis_core.models.base import ModelTextChunk
from jarvis_proto import ConversationKind, Message, RunKind
from jarvis_proto.events import ConversationUpdated, MessageCreated
from jarvis_proto.settings import RoleName

if TYPE_CHECKING:
    from jarvis_core.app import Core

log = logging.getLogger(__name__)

AUDIT_FOLDER_KEY = "triage-audit"
LEFT = "(left in Inbox)"

_JUDGE = """You are auditing an email filing decision made by an automatic sorter.

Rules the sorter follows:
{instructions}

Categories (name: rule):
{categories}
- none: nothing above clearly applies - the mail is left in the Inbox for the owner

Email:
From: {sender}
To: {to}
Cc: {cc}
Subject: {subject}
Preview: {preview}

The sorter chose: {chosen}

Is that the right choice under the rules? A defensible choice is "ok"; only call it "wrong" when
another answer is clearly better.
Answer JSON only: {{"verdict": "ok" or "wrong", "better": "<category name or none>", "why": "<one short sentence>"}}"""


def _address(sender: str) -> str:
    m = re.search(r"<([^>]+)>", sender)
    return (m.group(1) if m else sender).strip()


def _chosen(prod: dict[str, Any]) -> str:
    return str(prod.get("category") or "none")


class TriageAudit:
    def __init__(self, core: Core) -> None:
        self.core = core
        self._task: asyncio.Task[None] | None = None

    # --- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="triage-audit")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _seconds_to_next(self, hour: int) -> float:
        tz = ZoneInfo(self.core.settings.timezone)
        now = datetime.now(tz)
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return (nxt - now).total_seconds()

    async def _loop(self) -> None:
        while True:
            hour = self.core.settings.triage.audit_hour
            if hour < 0:
                await asyncio.sleep(3600)  # off; look again in an hour (the setting may change)
                continue
            await asyncio.sleep(self._seconds_to_next(hour))
            if self.core.settings.triage.enabled and self.core.settings.triage.audit_hour == hour:
                try:
                    await self.run(post=True)
                except Exception:
                    log.exception("triage audit failed")

    # --- the audit ---------------------------------------------------------------------

    async def _rows(self, since: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        last = 0
        while True:
            page = await self.core.shadow.rows(since_id=last, limit=1000)
            if not page:
                return out
            last = int(page[-1]["id"])
            for r in page:
                if r.get("point") != "mail" or r.get("source") != "live":
                    continue
                try:
                    at = datetime.fromisoformat(str(r["at"]))
                except ValueError:
                    continue
                if at.tzinfo is None:
                    at = at.replace(tzinfo=UTC)
                if at >= since:
                    out.append(r)

    async def _judge(self, row: dict[str, Any], rules: Any) -> dict[str, str]:
        inp = row.get("input") or {}
        address = _address(str(inp.get("from") or ""))
        offered = [c for c in rules.categories if c.get("name") and rules.admits(c, address)]
        prompt = _JUDGE.format(
            instructions=str(rules.instructions or "").strip() or "(none)",
            categories="\n".join(f"- {c['name']}: {c.get('rule', '')}" for c in offered),
            sender=inp.get("from", ""),
            to=inp.get("to", ""),
            cc=inp.get("cc", ""),
            subject=inp.get("subject", ""),
            preview=inp.get("preview", ""),
            chosen=_chosen(row.get("prod") or {}),
        )
        adapter = self.core.adapters.for_role(RoleName.JUDGE, kind=RunKind.TRIAGE)
        text = ""
        async for chunk in adapter.stream([Message.user(prompt)], [], cancel=asyncio.Event()):
            if isinstance(chunk, ModelTextChunk):
                text += chunk.text
        from jarvis_core.features.triage import _json

        data = _json(text)
        if not isinstance(data, dict):
            return {"verdict": "error", "better": "", "why": text.strip()[:120]}
        return {k: str(data.get(k) or "") for k in ("verdict", "better", "why")}

    async def run(self, *, hours: int = 24, sample: int | None = None, post: bool = False) -> dict[str, Any]:
        cfg = self.core.settings.triage
        sample = cfg.audit_sample if sample is None else sample
        since = datetime.now(UTC) - timedelta(hours=hours)
        rows = await self._rows(since)
        accounts: dict[str, dict[str, Any]] = {}
        by_account: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_account.setdefault(str((r.get("input") or {}).get("_account") or "?"), []).append(r)

        for account, arows in sorted(by_account.items()):
            rules = cfg.rules_for(account)
            folders = Counter(str((r.get("prod") or {}).get("folder") or LEFT) for r in arows)
            violations: list[str] = []
            for r in arows:
                inp, prod = r.get("input") or {}, r.get("prod") or {}
                address = _address(str(inp.get("from") or ""))
                chosen = str(prod.get("category") or "")
                subject = str(inp.get("subject") or "")[:70]
                gated = next((c for c in rules.categories if c.get("name") == chosen and c.get("senders")), None)
                if gated and not rules.admits(gated, address):
                    violations.append(f"{chosen} from a sender it does not admit: {address} — {subject}")
                owned = rules.sender_category(address, subject)
                if owned and chosen not in (owned.get("name"), "demand"):
                    violations.append(f"from {address} but filed as {chosen or 'none'}: {subject}")

            # The sample: every decision in a rare category first (they matter most and are
            # few), then a random share of the rest.
            common = {f for f, _ in folders.most_common(2)}
            rare = [r for r in arows if str((r.get("prod") or {}).get("folder") or LEFT) not in common]
            rare_ids = {r["id"] for r in rare}
            rest = [r for r in arows if r["id"] not in rare_ids]
            rng = random.Random(f"{account}{since.date()}")
            picked = (rare + rng.sample(rest, min(len(rest), max(0, sample - len(rare)))))[:sample]
            verdicts = []
            for r in picked:
                try:
                    v = await self._judge(r, rules)
                except Exception as exc:
                    v = {"verdict": "error", "better": "", "why": str(exc)[:120]}
                inp = r.get("input") or {}
                verdicts.append(
                    {
                        **v,
                        "chosen": _chosen(r.get("prod") or {}),
                        "sender": str(inp.get("from") or "")[:60],
                        "subject": str(inp.get("subject") or "")[:90],
                    }
                )
            judged = [v for v in verdicts if v["verdict"] in ("ok", "wrong")]
            ok = sum(v["verdict"] == "ok" for v in judged)
            fallback_folder = next(
                (c.get("folder") for c in rules.categories if c.get("name") == rules.fallback_category), None
            )
            accounts[account] = {
                "decisions": len(arows),
                "folders": dict(folders.most_common()),
                "left_share": round(folders.get(LEFT, 0) / len(arows), 3) if arows else 0.0,
                "catch_all": fallback_folder or "",
                "catch_all_share": round(folders.get(fallback_folder, 0) / len(arows), 3) if fallback_folder else 0.0,
                "violations": violations,
                "judged": len(judged),
                "agreement": round(ok / len(judged), 3) if judged else None,
                "disagreements": [v for v in verdicts if v["verdict"] == "wrong"],
                "judge_errors": sum(v["verdict"] == "error" for v in verdicts),
            }

        report = {"hours": hours, "since": since.isoformat(), "decisions": len(rows), "accounts": accounts}
        if post:
            await self._post(report)
        return report

    # --- delivery ------------------------------------------------------------------------

    @staticmethod
    def render(report: dict[str, Any]) -> str:
        lines = [f"Triage audit — last {report['hours']} h, {report['decisions']} live decisions"]
        if not report["accounts"]:
            lines.append(
                "No live triage decisions recorded in this window (is triage enabled and the host reachable?)."
            )
        for account, a in report["accounts"].items():
            agree = f"{a['agreement']:.0%}" if a["agreement"] is not None else "n/a"
            lines += [
                "",
                f"**{account}** — {a['decisions']} mails · judge agrees {agree} of {a['judged']} sampled"
                + (f" · {a['judge_errors']} judge errors" if a["judge_errors"] else ""),
                "Filed: " + ", ".join(f"{f} {n}" for f, n in a["folders"].items()),
                f"Left in Inbox {a['left_share']:.0%}"
                + (f" · catch-all ({a['catch_all']}) {a['catch_all_share']:.0%}" if a["catch_all"] else ""),
            ]
            if a["violations"]:
                lines.append(f"⚠ {len(a['violations'])} rule violation(s):")
                lines += [f"- {v}" for v in a["violations"][:20]]
            if a["disagreements"]:
                lines.append("Judge disagrees:")
                lines += [
                    f"- {d['sender']} — {d['subject']}: {d['chosen']} → {d['better'] or '?'} ({d['why']})"
                    for d in a["disagreements"][:25]
                ]
        return "\n".join(lines)

    async def _post(self, report: dict[str, Any]) -> None:
        tz = ZoneInfo(self.core.settings.timezone)
        day = datetime.now(tz).strftime("%Y-%m-%d")
        store = self.core.store
        convs = await store.list_conversations(include_archived=True)
        conv = next(
            (
                c
                for c in convs
                if c.kind is ConversationKind.TRIAGE and c.folder_key == AUDIT_FOLDER_KEY and c.title == day
            ),
            None,
        )
        if conv is None:
            conv = await store.create_conversation(
                kind=ConversationKind.TRIAGE, title=day, folder_key=AUDIT_FOLDER_KEY, folder_label="Triage audit"
            )
            self.core.bus.publish(ConversationUpdated(conversation=conv))
        msg = await store.add_message(
            Message.assistant(self.render(report), conversation_id=conv.id, name="triage-audit")
        )
        self.core.bus.publish(MessageCreated(message=msg))

        parts = []
        for account, a in report["accounts"].items():
            agree = f"{a['agreement']:.0%}" if a["agreement"] is not None else "n/a"
            warn = f", ⚠ {len(a['violations'])} violations" if a["violations"] else ""
            parts.append(f"{account.split('@')[0]}: {a['decisions']} mails, judge {agree}{warn}")
        text = "🧾 Triage audit — " + ("; ".join(parts) if parts else "no live decisions in the last day")
        try:
            await self.core.registry.call(
                "notify.discord",
                {"text": text},
                cancel=asyncio.Event(),
                idempotency_key=f"triage:audit:{day}",
                timeout_s=30,
            )
        except Exception as exc:
            log.warning("triage audit push not delivered: %s", exc)
