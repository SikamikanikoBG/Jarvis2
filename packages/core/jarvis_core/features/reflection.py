"""Post-run reflection: what a run learned about a site becomes a playbook skill.

The alternative is the maintainer patching the tools after every failed run — the svg send
button, the missing wait, the outline that lost the composer (DSK Bank, 13 Sep 2026). Each of
those was knowledge the run itself had by the end: what did not work, what did. This module
asks for that knowledge once, in writing, and files it where the next run on the same site
will read it before its first click (``SkillStore.for_site``).

Never blocks the reply; never raises; skips more often than it writes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from jarvis_core.engine.supervision import StepRecord, render_steps
from jarvis_core.features.skills import SkillStore, skill_document
from jarvis_core.models.base import ModelAdapter, ModelTextChunk
from jarvis_proto import Message, ToolResultKind

log = logging.getLogger(__name__)

_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_MIN_FAILURES = 2
_MAX_STEPS = 40

_PROMPT = """You are writing a PLAYBOOK for an AI assistant that drives web pages through browser tools.
Below are the browser steps of a run it just finished on {host}: what failed, what worked afterwards.
Distil ONLY what a future run on this site needs in order not to rediscover it: which control does
what (by CSS class / ref shape), the event or tool that actually worked, timings (how long replies
take), session quirks (idle timeouts, re-open steps), and what NOT to do. Facts from the steps only —
no guesses, no praise, no narrative.

{existing}Steps (oldest first):
{steps}

Answer with JSON only:
{{"name": "<lowercase-slug, e.g. webim-chat-dskbank>", "description": "<one line: site + what the playbook covers>",
  "sites": ["{host}"], "triggers": ["<2-4 words a request about this site would contain>", ...],
  "body": "<markdown, 8-25 lines, imperative, the working recipe first>"}}
or {{"skip": "<why there is nothing worth keeping>"}} when the run taught nothing reusable."""


class PlaybookReflector:
    def __init__(self, store: SkillStore, classifier: Callable[[], ModelAdapter]) -> None:
        self.store = store
        self._classifier = classifier
        self._tasks: set[asyncio.Task[None]] = set()

    # --- entry ---------------------------------------------------------------------

    def worth_it(self, steps: Sequence[StepRecord]) -> str | None:
        """The host this run struggled with and then got somewhere on, or None."""
        browser = [s for s in steps if s.tool.startswith("browser.")]
        if len(browser) < 4:
            return None
        failures = [i for i, s in enumerate(browser) if s.kind is ToolResultKind.ERROR]
        if len(failures) < _MIN_FAILURES:
            return None
        after = browser[failures[-1] + 1 :]
        if not any(s.kind in (ToolResultKind.DATA, ToolResultKind.PARTIAL) and s.tool != "browser.read" for s in after):
            return None
        host = _host_of(browser)
        return host

    def schedule(
        self,
        steps: Sequence[StepRecord],
        *,
        on_learned: Callable[[str, str], Any] | None = None,
    ) -> None:
        host = self.worth_it(steps)
        if host is None:
            return
        task = asyncio.create_task(self._reflect(host, list(steps), on_learned))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- the work ------------------------------------------------------------------

    async def _reflect(self, host: str, steps: list[StepRecord], on_learned: Callable[[str, str], Any] | None) -> None:
        try:
            existing = await self.store.for_site(host)
            existing_block = ""
            if existing:
                name, body = existing[0]
                existing_block = (
                    f"There is already a playbook `{name}` for this site; UPDATE it (keep its name, keep what "
                    f"still holds, correct what this run contradicted):\n{body[:2500]}\n\n"
                )
            browser = [s for s in steps if s.tool.startswith("browser.")][-_MAX_STEPS:]
            prompt = _PROMPT.format(host=host, existing=existing_block, steps=render_steps(browser, width=260))
            adapter = self._classifier()
            text = ""
            async for chunk in adapter.stream([Message.user(prompt)], [], cancel=asyncio.Event()):
                if isinstance(chunk, ModelTextChunk):
                    text += chunk.text
            data = _json_object(text)
            if not data or data.get("skip") or not data.get("body"):
                log.info("playbook reflection for %s skipped: %s", host, (data or {}).get("skip", "no usable answer"))
                return
            name = _slug(str(data.get("name") or f"playbook-{host}"))
            if existing:
                name = existing[0][0]
            sites = [str(s) for s in data.get("sites") or [host]] or [host]
            triggers = [str(t) for t in data.get("triggers") or []][:6]
            description = str(data.get("description") or f"Playbook for {host}").strip()
            content = skill_document(
                description=description,
                triggers=triggers,
                sites=sites,
                body=str(data["body"]).strip(),
                extra={"learned": True, "learned_at": datetime.now(UTC).isoformat(timespec="seconds")},
            )
            await self.store.put(name, content)
            log.info("playbook %s %s for %s", "updated" if existing else "learned", name, host)
            if on_learned is not None:
                res = on_learned(name, description)
                if asyncio.iscoroutine(res):
                    await res
        except Exception as exc:  # never the run's problem
            log.warning("playbook reflection failed for %s: %s", host, exc)


# --- helpers ---------------------------------------------------------------------------


def _host_of(steps: Sequence[StepRecord]) -> str | None:
    for s in reversed(steps):
        for text in (s.summary, s.args_summary):
            m = _URL.search(text or "")
            if m:
                host = urlparse(m.group(0)).hostname or ""
                if host:
                    return host.removeprefix("www.")
    return None


def _json_object(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    return (s or "playbook")[:64]
