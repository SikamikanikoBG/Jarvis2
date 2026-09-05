"""Skills: markdown files with YAML frontmatter, detected per message, injected as context."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

    async def detect(self, message: str) -> list[str]:
        skills = [s for s in await self.store.list() if s.enabled]
        if not skills or len(message.strip()) < 8:
            return []
        lowered = message.lower()
        # Structural first: a trigger phrase literally present needs no model.
        structural = [s.name for s in skills if any(t.lower() in lowered for t in s.triggers if len(t) >= 4)]
        if structural:
            return structural[:2]
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
