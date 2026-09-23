"""Shadow decisions: a second opinion that is recorded, never obeyed (Settings.shadow).

Production decides exactly as before. Afterwards the same input goes to Laya - a 421M typed-
decision encoder on ardi that answers choice / score / yes-no questions with probabilities in
one forward pass - and both answers are written side by side to ``<home>/shadow.db``, a file of
its own so the experiment can be copied, inspected or deleted without touching jarvis2.db.

Four points are observed (``ShadowSettings.points``):

* ``mail``      - triage: the category the 27B chose (or the DM-regex) and the alert the rules
                  matched; Laya gets both questions in one pass.
* ``rsvp``      - the policy's answer to an invite (accept / decline / VIP / external).
* ``preflight`` - before a run: simple vs multi_step, and which skill applies.
* ``guardrail`` - a message about to leave on Arsen's behalf; production has no guard, so the
                  row records what one would have said.

Every row keeps the full input production saw, so any model (the 27B on the rule-based points,
a fine-tuned or calibrated Laya) can be replayed over the same rows later. The recorder never
raises into its caller and never waits on Laya: each observation is a task of its own, capped
at ``max_pending``; past the cap it is dropped and counted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import httpx

from jarvis_proto import Settings

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  point TEXT NOT NULL,
  ref TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'live',
  input TEXT NOT NULL,
  questions TEXT NOT NULL,
  prod TEXT NOT NULL,
  prod_ms REAL,
  laya TEXT,
  laya_ms REAL,
  laya_rtt_ms REAL,
  laya_error TEXT,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS shadow_point_at ON shadow(point, at);
"""


class ShadowRecorder:
    def __init__(self, path: Path, settings: Callable[[], Settings], client: httpx.AsyncClient | None = None) -> None:
        self.path = path
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        self._conn: aiosqlite.Connection | None = None
        self._write = asyncio.Lock()
        self._pending: set[asyncio.Task[None]] = set()
        self.dropped = 0

    # --- lifecycle -----------------------------------------------------------------

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(_SCHEMA)

    async def close(self) -> None:
        await self.drain()
        await self._client.aclose()
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def drain(self, timeout: float = 15.0) -> None:
        """Wait for the observations in flight (tests, and a clean shutdown)."""
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=timeout)

    # --- the one entry point ---------------------------------------------------------

    def observe(
        self,
        point: str,
        *,
        ref: str,
        input: dict[str, Any],
        questions: dict[str, Any],
        prod: dict[str, Any],
        prod_ms: float | None = None,
        source: str = "live",
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Record production's decision and ask Laya for its own. Returns at once, never raises."""
        try:
            cfg = self._settings().shadow
            if not cfg.observes(point) or self._conn is None:
                return
            if len(self._pending) >= cfg.max_pending:
                self.dropped += 1
                return
            task = asyncio.create_task(
                self._observe(point, ref, source, input, questions, prod, prod_ms, meta or {}), name=f"shadow:{point}"
            )
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        except Exception:
            log.exception("shadow observe failed (ignored)")

    async def _observe(
        self,
        point: str,
        ref: str,
        source: str,
        input: dict[str, Any],
        questions: dict[str, Any],
        prod: dict[str, Any],
        prod_ms: float | None,
        meta: dict[str, Any],
    ) -> None:
        laya: Any = None
        laya_ms: float | None = None
        rtt: float | None = None
        error: str | None = None
        cfg = self._settings().shadow
        if cfg.laya_url and questions:
            t = time.perf_counter()
            try:
                resp = await self._client.post(
                    cfg.laya_url.rstrip("/") + "/predict",
                    json={"state": laya_state(input), "questions": questions},
                    timeout=cfg.timeout_s,
                )
                rtt = (time.perf_counter() - t) * 1000
                if resp.status_code >= 400:
                    error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                else:
                    laya = resp.json()
                    ms = laya.get("ms") if isinstance(laya, dict) else None  # pyright: ignore[reportUnknownMemberType]
                    laya_ms = float(ms) if isinstance(ms, int | float) else None
            except Exception as exc:
                rtt = (time.perf_counter() - t) * 1000
                error = f"{type(exc).__name__}: {exc}"[:300]
        try:
            async with self._write:
                assert self._conn is not None
                await self._conn.execute(
                    "INSERT INTO shadow(at, point, ref, source, input, questions, prod, prod_ms, laya, laya_ms,"
                    " laya_rtt_ms, laya_error, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        datetime.now(UTC).isoformat(),
                        point,
                        ref,
                        source,
                        _dump(input),
                        _dump(questions),
                        _dump(prod),
                        prod_ms,
                        _dump(laya) if laya is not None else None,
                        laya_ms,
                        rtt,
                        error,
                        _dump(meta),
                    ),
                )
        except Exception:
            log.exception("shadow row not written (ignored)")

    def outgoing(self, name: str, arguments: dict[str, Any], *, ref: str, source: str = "live") -> None:
        """A message about to leave on Arsen's behalf: what would a guard have said? Production has
        no guard, so ``prod`` only says it went. Callers skip incognito chats."""
        try:
            cfg = self._settings().shadow
            if not cfg.observes("guardrail") or not cfg.guards(name):
                return
            if (inp := guardrail_input(name, arguments)) is not None:
                self.observe(
                    "guardrail",
                    ref=ref,
                    source=source,
                    input=inp,
                    questions=GUARDRAIL_QUESTIONS,
                    prod={"sent": True, "by": "none"},
                    meta={"tool": name},
                )
        except Exception:
            log.exception("shadow outgoing failed (ignored)")

    # --- read side (API) ----------------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        if self._conn is None:
            return {"open": False}
        cur = await self._conn.execute(
            "SELECT point, source, COUNT(*) n, SUM(laya IS NOT NULL) answered, SUM(laya_error IS NOT NULL) errors,"
            " AVG(laya_ms) laya_ms, AVG(laya_rtt_ms) rtt_ms, AVG(prod_ms) prod_ms, MIN(at) first, MAX(at) last"
            " FROM shadow GROUP BY point, source ORDER BY point, source"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        await cur.close()
        return {
            "open": True,
            "path": str(self.path),
            "pending": len(self._pending),
            "dropped": self.dropped,
            "points": rows,
        }

    async def rows(self, *, since_id: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        cur = await self._conn.execute("SELECT * FROM shadow WHERE id > ? ORDER BY id LIMIT ?", (since_id, limit))
        out = []
        for r in await cur.fetchall():
            row = dict(r)
            for k in ("input", "questions", "prod", "laya", "meta"):
                if row.get(k):
                    row[k] = json.loads(row[k])
            out.append(row)
        await cur.close()
        return out


# --- the questions: pure functions, shared with the offline replay -----------------------------

NONE_RULE = "nothing above applies"


def laya_state(input: dict[str, Any]) -> dict[str, Any]:
    """What Laya reads as the state: the fields of the input that describe the THING being decided
    about. Policy text lives in the questions; keys starting with ``_`` are recorded only."""
    return {k: v for k, v in input.items() if not k.startswith("_") and v not in (None, "", [])}


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


def mail_input(
    *, sender: str, to: str, cc: str, subject: str, preview: str, account: str, rules: Any
) -> dict[str, Any]:
    """The mail as Laya's state. The owner's filing rules and alert rules ride in the STATE, last:
    a question's head has its own 192/256-token budget that already holds every category, and a
    state that overflows the window is cut from the end - so the rules give way before the mail."""
    alerts = [d for a in rules.alerts if (d := _describe_alert(a))]
    return {
        "from": sender,
        "to": to[:300],
        "cc": cc[:300],
        "subject": subject[:200],
        "preview": preview[:500],
        "filing_rules": rules.instructions.strip(),
        "alert_rules": "; ".join(alerts),
        "_account": account,
        "_categories": rules.categories,
        "_fallback": rules.fallback_category,
    }


def mail_questions(categories: list[dict[str, str]], alerts: list[Any]) -> dict[str, Any]:
    """The triage prompt as typed questions: one choice over the categories (+ none), and - when
    the account has alert rules - one yes/no on whether this mail trips one of them."""
    criteria = {str(c["name"]): _clip(c.get("rule", "") or c["name"], 300) for c in categories if c.get("name")}
    criteria["none"] = NONE_RULE
    qs: dict[str, Any] = {
        "category": {
            "type": "choice",
            "instructions": "Which folder should this email be filed in, following `filing_rules`?",
            "criteria": criteria,
        }
    }
    if any(_describe_alert(a) for a in alerts):
        qs["alert"] = {
            "type": "noul",
            "instructions": "Does this email match one of the `alert_rules` (a named sender or a subject keyword)?",
        }
    return qs


def _describe_alert(alert: Any) -> str:
    if not getattr(alert, "enabled", True):
        return ""
    parts = []
    if senders := getattr(alert, "senders", []):
        parts.append("from " + ", ".join(senders))
    if keywords := getattr(alert, "keywords", []):
        parts.append("subject contains " + " or ".join(repr(k) for k in keywords))
    if not parts:
        return ""
    tail = " (automatic replies excluded)" if getattr(alert, "skip_auto_replies", True) else ""
    return f"{getattr(alert, 'name', '') or 'alert'}: " + " and ".join(parts) + tail


RSVP_OPTIONS = {
    "accept": "the organizer is inside the allowed domains and the slot does not clash with any committed meeting",
    "decline": "the slot clashes with a committed meeting and the organizer is not a VIP",
    "accept_vip_conflict": "the slot clashes with a committed meeting but the organizer is a VIP, who is never declined",
    "left_external": "the organizer is outside the allowed domains and not a VIP, so it is left for Arsen",
}


def rsvp_policy(allowed_domains: list[str], vip: list[str]) -> str:
    return (
        f"Allowed organizer domains: {', '.join(allowed_domains) or 'none'}. "
        f"VIPs (never declined): {', '.join(vip) or 'none'}. "
        "Only the listed conflicts are committed meetings; tentative ones are not listed."
    )


RSVP_QUESTIONS: dict[str, Any] = {
    "decision": {
        "type": "choice",
        "instructions": "How should Arsen answer this meeting invite under `policy`?",
        "criteria": dict(RSVP_OPTIONS),
    }
}


PREFLIGHT_TIERS = {
    "simple": "can be answered or done in one or two tool calls",
    "multi_step": "needs several dependent steps: research then write, check several sources, a multi-part deliverable",
}


def preflight_questions(skills: list[tuple[str, str]]) -> dict[str, Any]:
    """Tier as a choice. Skills as a choice too (+ none): the 27B may pick two, Laya picks the most
    likely one - the comparison is on the first pick. Past ~20 options the service shortlists by
    embedding first (laya.shortlist_choice); that step is part of what is being measured."""
    qs: dict[str, Any] = {
        "tier": {
            "type": "choice",
            "instructions": "How much work is this request for an AI assistant with tools?",
            "criteria": dict(PREFLIGHT_TIERS),
        }
    }
    if skills:
        crit = {name: _clip(desc or name, 200) for name, desc in skills}
        crit["none"] = "no skill applies"
        qs["skill"] = {"type": "choice", "instructions": "Which playbook applies to this request?", "criteria": crit}
    return qs


GUARDRAIL_QUESTIONS: dict[str, Any] = {
    "names_assistant": {
        "type": "noul",
        "instructions": "Does this outgoing message say or reveal that an AI assistant (Jarvis) wrote or sent it?",
    },
    "leaks_sensitive": {
        "type": "noul",
        "instructions": "Does this outgoing message expose credentials, tokens, internal IDs or confidential "
        "information the recipient should not get?",
    },
    "safe_to_send": {
        "type": "noul",
        "instructions": "Is this message fine to send as-is on Arsen's behalf: correct tone for the recipient, "
        "no invented facts or commitments?",
    },
}


def guardrail_input(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """The text that would leave, from whichever send tool it is. None when there is no text."""
    text = ""
    for key in ("body", "text", "comment", "message", "content", "html"):
        if isinstance(args.get(key), str) and args[key].strip():
            text = args[key]
            break
    if not text:
        return None
    to = args.get("to") or args.get("recipients") or args.get("recipient") or ""
    return {
        "channel": tool,
        "to": ", ".join(map(str, to)) if isinstance(to, list) else str(to),
        "subject": str(args.get("subject") or ""),
        "text": text[:4000],
        "_decision": str(args.get("decision") or ""),
    }


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
