"""Skills: markdown files with YAML frontmatter, detected per message, injected as context."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field

from jarvis_core.db import Database
from jarvis_core.engine.bus import EventBus
from jarvis_core.models.base import ModelAdapter, ModelTextChunk
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import Message, Skill, ToolResult
from jarvis_proto.events import SkillsChanged

log = logging.getLogger(__name__)

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def skill_document(
    *,
    description: str,
    triggers: list[str],
    body: str,
    sites: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """A skill file from its parts: frontmatter the store will parse back, then the body."""
    meta: dict[str, Any] = {"description": description, "triggers": triggers}
    if sites:
        meta["sites"] = sites
    if extra:
        meta.update(extra)
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{front}\n---\n\n{body.strip()}\n"


def _host(url_or_host: str) -> str:
    s = url_or_host.strip().lower()
    host = urlparse(s).hostname if "://" in s else s.split("/")[0]
    return (host or "").removeprefix("www.")


class SkillStore:
    def __init__(self, directory: Path, db: Database, bus: EventBus | None = None) -> None:
        self.dir = directory
        self.db = db
        self.bus = bus
        self.dir.mkdir(parents=True, exist_ok=True)

    def _changed(self) -> None:
        if self.bus is not None:
            self.bus.publish(SkillsChanged())

    @staticmethod
    def parse(content: str) -> tuple[dict[str, Any], str]:
        m = _FRONTMATTER.match(content)
        if not m:
            return {}, content
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid frontmatter: {exc}") from exc
        if not isinstance(meta, dict):
            raise ValueError("frontmatter must be a mapping")
        return meta, content[m.end() :]

    async def _enabled_map(self) -> dict[str, bool]:
        rows = await self.db.fetchall("SELECT name, enabled FROM skills_state")
        return {r["name"]: bool(r["enabled"]) for r in rows}

    def _path(self, name: str) -> Path:
        if not _NAME.match(name):
            raise ValueError("skill names are lowercase letters, digits, - and _")
        return self.dir / f"{name}.md"

    async def list(self) -> list[Skill]:
        enabled = await self._enabled_map()
        out: list[Skill] = []
        for path in sorted(self.dir.glob("*.md")):
            name = path.stem
            try:
                meta, _ = self.parse(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                meta = {}
            triggers = meta.get("triggers") or []
            out.append(
                Skill(
                    name=name,
                    description=str(meta.get("description") or "").strip(),
                    triggers=[str(t) for t in triggers] if isinstance(triggers, list) else [],
                    enabled=enabled.get(name, True),
                    size=path.stat().st_size,
                    updated_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                )
            )
        return out

    async def get(self, name: str) -> str | None:
        path = self._path(name)
        return path.read_text(encoding="utf-8") if path.exists() else None

    async def body(self, name: str) -> str | None:
        content = await self.get(name)
        if content is None:
            return None
        _, body = self.parse(content)
        return body.strip()

    async def for_site(self, url_or_host: str) -> list[tuple[str, str]]:
        """Enabled playbooks whose ``sites:`` cover this host — ``(name, body)`` pairs.

        A skill for a SITE is read when the agent arrives there, not when Arsen mentions it:
        the trigger is the URL in a browser tool's result. ``sites: [dskbank.bg]`` also matches
        ``chatbot.dskbank.bg``."""
        host = _host(url_or_host)
        if not host:
            return []
        enabled = await self._enabled_map()
        out: list[tuple[str, str]] = []
        for path in sorted(self.dir.glob("*.md")):
            name = path.stem
            if not enabled.get(name, True):
                continue
            try:
                meta, body = self.parse(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            sites = meta.get("sites") or []
            if not isinstance(sites, list):
                continue
            for site in sites:
                s = _host(str(site))
                if s and (host == s or host.endswith("." + s)):
                    out.append((name, body.strip()))
                    break
        return out

    async def put(self, name: str, content: str) -> Skill:
        meta, _ = self.parse(content)  # validates
        if "description" not in meta:
            raise ValueError("frontmatter needs a description")
        self._path(name).write_text(content, encoding="utf-8")
        self._changed()
        skill = next((s for s in await self.list() if s.name == name), None)
        assert skill is not None
        return skill

    async def set_enabled(self, name: str, enabled: bool) -> Skill | None:
        if not self._path(name).exists():
            return None
        await self.db.execute(
            "INSERT INTO skills_state(name, enabled) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled",
            (name, int(enabled)),
        )
        self._changed()
        return next((s for s in await self.list() if s.name == name), None)

    async def delete(self, name: str) -> bool:
        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        await self.db.execute("DELETE FROM skills_state WHERE name = ?", (name,))
        self._changed()
        return True


_DETECT_PROMPT = """Pick the skills (instruction sheets) relevant to this request, if any.
Skills:
{index}

Request: {message}

Answer with JSON only: {{"skills": ["<name>", ...]}} — at most 2, empty list if none apply."""


class SkillDetector:
    def __init__(self, store: SkillStore, classifier: Any) -> None:
        self.store = store
        self._classifier = classifier  # Callable[[], ModelAdapter]

    async def detect(
        self, message: str, *, allow_model: bool = True, trace: dict[str, object] | None = None
    ) -> list[str]:
        """``allow_model=False`` keeps the free half only: a trigger phrase that is literally there.
        ``trace`` (optional) is told how the answer was reached: ``by`` = trigger | model | none.

        The paid half is a whole model round trip before the real answer begins — measured at
        about a second on a call (2026-09-20), which is a second of a human being listening to
        silence. The planner was already skipped on a call for the same reason.
        """
        skills = [s for s in await self.store.list() if s.enabled]
        if not skills or len(message.strip()) < 8:
            return []
        lowered = message.lower()
        # Structural first: a trigger phrase literally present needs no model.
        structural = [s.name for s in skills if any(t.lower() in lowered for t in s.triggers if len(t) >= 4)]
        if structural:
            if trace is not None:
                trace["by"] = "trigger"
            return structural[:2]
        if not allow_model:
            return []
        if trace is not None:
            trace["by"] = "model"
        index = "\n".join(f"- {s.name}: {s.description or '(no description)'}" for s in skills)
        prompt = _DETECT_PROMPT.format(index=index, message=message[:1500])
        try:
            adapter: ModelAdapter = self._classifier()
            text = ""
            async for chunk in adapter.stream([Message.user(prompt)], [], cancel=asyncio.Event()):
                if isinstance(chunk, ModelTextChunk):
                    text += chunk.text
            start, end = text.find("{"), text.rfind("}")
            data = json.loads(text[start : end + 1]) if start != -1 and end > start else {}
            names = {s.name for s in skills}
            picked = [str(n) for n in data.get("skills", []) if str(n) in names]
            return picked[:2]
        except Exception as exc:
            log.warning("skill detection skipped: %s", exc)
            return []

    async def context_block(self, names: list[str], *, max_chars: int) -> str | None:
        parts: list[str] = []
        for name in names:
            body = await self.store.body(name)
            if body:
                parts.append(f"## Skill: {name}\n{body[:max_chars]}")
        return "\n\n".join(parts) if parts else None


class _UseArgs(BaseModel):
    name: str = Field(description="Skill name as listed")


class _LearnArgs(BaseModel):
    name: str = Field(description="Slug: lowercase letters, digits, - and _ (e.g. webim-chat-dskbank)")
    description: str = Field(description="One line: what this playbook covers")
    body: str = Field(
        description="Markdown, imperative, the working recipe first: which control does what, the event or tool that worked, timings, session quirks, what NOT to do"
    )
    triggers: list[str] = Field(default_factory=list, description="2-4 word phrases a request about this would contain")
    sites: list[str] = Field(
        default_factory=list,
        description="Hosts this applies to (dskbank.bg matches its subdomains); the playbook is shown automatically when a browser tool lands on one of them",
    )


class SkillsTools(BuiltinProvider):
    name = "skills"

    def __init__(self, store: SkillStore) -> None:
        self.store = store
        super().__init__()

    @tool(
        "skills.list",
        description="List available skills (instruction sheets) and what they cover.",
        read_only=True,
        idempotent=True,
    )
    async def _list(self) -> ToolResult:
        skills = [s for s in await self.store.list() if s.enabled]
        if not skills:
            return ToolResult.empty("No skills installed.")
        return ToolResult.data("\n".join(f"- {s.name}: {s.description}" for s in skills), count=len(skills))

    @tool(
        "skills.use",
        description="Read one skill's full instructions before doing that kind of task.",
        args=_UseArgs,
        read_only=True,
        idempotent=True,
    )
    async def _use(self, name: str) -> ToolResult:
        body = await self.store.body(name)
        return ToolResult.data(body) if body else ToolResult.failure(f"no skill {name!r}")

    @tool(
        "skills.learn",
        description=(
            "Write or update a playbook skill from what you just learned: a way of doing something on a site or "
            "app that failed, and the way that then worked. Call it as soon as you have the working recipe — the "
            "next run reads it before its first click (sites: shows it automatically on that host) instead of "
            "rediscovering. Facts only: controls by class/ref shape, the event or tool that worked, timings, "
            "session quirks, what not to do."
        ),
        args=_LearnArgs,
        idempotent=True,
    )
    async def _learn(self, name: str, description: str, body: str, triggers: list[str], sites: list[str]) -> ToolResult:
        if not body.strip():
            return ToolResult.failure("a playbook needs a body")
        existing = await self.store.get(name)
        extra: dict[str, Any] = {"learned": True, "learned_at": datetime.now(UTC).isoformat(timespec="seconds")}
        if existing:
            try:
                meta, _ = self.store.parse(existing)
                if not meta.get("learned"):
                    return ToolResult.failure(
                        f"{name!r} is a hand-written skill, not a learned playbook — pick another name, or tell Arsen what to change in it"
                    )
            except ValueError:
                pass
        try:
            skill = await self.store.put(
                name,
                skill_document(description=description.strip(), triggers=triggers, sites=sites, body=body, extra=extra),
            )
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        verb = "updated" if existing else "learned"
        where = f" for {', '.join(sites)}" if sites else ""
        return ToolResult.data(
            f"Playbook {skill.name} {verb}{where} ({skill.size} bytes). Arsen can read and edit it under Skills."
        )
