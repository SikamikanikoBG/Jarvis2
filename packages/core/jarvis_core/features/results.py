"""``jarvis.result_read`` / ``jarvis.result_search`` — reach back into a tool result the prompt
shows as a view.

A long result rides in the prompt as sections with refs (``engine/views.py``); the full text is
in the DB the whole time. These two hand it back by ref: a section, a range of sections, the
outline again, a raw character window when nothing else fits, and search hits that name the
section they fall in. Measured 2026-09-07 on "бизнес презентация": 912,217 chars of research
gathered against a 76,800-char history budget, so by the step that wrote the deck at most 8.4%
of what had been found was still visible — with nothing to read it back with. Now it is one
call away, without re-running the search.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from jarvis_core.db import Store
from jarvis_core.engine.views import locate, parse, parse_ref, read
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import ToolResult

WINDOW = 8_000
MAX_WINDOW = 40_000
MAX_HITS = 40
CONTEXT = 120
MAX_CONTEXT = 600


class _ReadArgs(BaseModel):
    ref: str = Field(
        description=(
            "A ref from a result's outline: '@x' for the whole (viewed again), '@x.3' for section 3, "
            "'@x.3-5' for sections 3 to 5, '@x.3.2' for part 2 of a long section 3."
        )
    )
    offset: int = Field(default=0, description="Raw mode: character offset to start from, ignoring sections.")
    limit: int = Field(default=WINDOW, description=f"How many characters to return at most (max {MAX_WINDOW}).")


class _SearchArgs(BaseModel):
    ref: str = Field(
        description="The '@x' ref of the result to search (a section path is ignored; the whole is searched)."
    )
    pattern: str = Field(description="Case-insensitive regular expression; plain text works too.")
    context: int = Field(default=CONTEXT, description=f"Characters shown on each side of a match (max {MAX_CONTEXT}).")
    max_hits: int = Field(default=MAX_HITS, description=f"Most matches to return (max {MAX_HITS}).")


class ResultsTools(BuiltinProvider):
    name = "jarvis"

    def __init__(self, store: Store) -> None:
        self.store = store
        super().__init__()

    async def _find(self, ref: str) -> tuple[str, str, str, list[int | tuple[int, int]]] | ToolResult:
        """(tool name, full text, base ref as '@x', section path) for a ref, or the failure to return."""
        try:
            candidates, path = parse_ref(ref)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        for cid in candidates:
            found = await self.store.tool_result(cid)
            if found is not None:
                return found[0], found[1], "@" + candidates[0], path
        return ToolResult.failure(
            f"no tool result with ref {ref!r}. The ref is the '@…' printed in a result's outline or "
            "its last line; only results from this conversation have one."
        )

    @tool(
        "jarvis.result_read",
        description=(
            "Read back an earlier tool result in this conversation that arrived as an outline. By ref: "
            "'@x.3' returns section 3 whole, '@x.3-5' sections 3 to 5, '@x' the outline again; a long "
            "section comes back as its own outline with refs like '@x.3.2'. Do not re-run the original "
            "tool to see more of it."
        ),
        args=_ReadArgs,
        read_only=True,
        idempotent=True,
    )
    async def _result_read(self, ref: str, offset: int = 0, limit: int = WINDOW) -> ToolResult:
        found = await self._find(ref)
        if isinstance(found, ToolResult):
            return found
        name, text, base, path = found
        limit = max(1, min(int(limit), MAX_WINDOW))
        offset = max(0, int(offset))
        if offset > 0 or (not path and limit != WINDOW):
            # Raw window, by character — the fallback for text no structure can be read from.
            if offset >= len(text):
                return ToolResult.empty(f"{name}: offset {offset:,} is past the end ({len(text):,} chars).")
            window = text[offset : offset + limit]
            end = offset + len(window)
            header = f"{name} {base} [{offset:,}-{end:,} of {len(text):,} chars]"
            if end < len(text):
                header += f" — continue with offset={end}"
            return ToolResult.data(f"{header}\n{window}")
        try:
            body = read(text, base, path, max(limit, 500))
        except (IndexError, ValueError) as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.data(f"{name} {ref.strip()}:\n{body}" if path else body)

    @tool(
        "jarvis.result_search",
        description=(
            "Find text inside an earlier tool result without reading it all: a case-insensitive regex "
            "(or plain text) over the stored result, returning each match with some characters around "
            "it, its char offset and the section it is in — then jarvis.result_read(ref='@x.7') shows "
            "that section. Works on JSON as well as prose."
        ),
        args=_SearchArgs,
        read_only=True,
        idempotent=True,
    )
    async def _result_search(
        self, ref: str, pattern: str, context: int = CONTEXT, max_hits: int = MAX_HITS
    ) -> ToolResult:
        found = await self._find(ref)
        if isinstance(found, ToolResult):
            return found
        name, text, base, _path = found
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)  # a bad regex is still a good substring
        context = max(0, min(int(context), MAX_CONTEXT))
        max_hits = max(1, min(int(max_hits), MAX_HITS))
        matches = [m for m in rx.finditer(text) if m.end() > m.start()]
        if not matches:
            return ToolResult.empty(f"{name}: nothing matches {pattern!r} ({len(text):,} chars searched).")
        doc = parse(text, base)
        # Windows around the matches, merged where they touch, so a dense region is one snippet.
        windows: list[tuple[int, int, str]] = []
        for m in matches[:max_hits]:
            lo, hi = max(0, m.start() - context), min(len(text), m.end() + context)
            if windows and lo <= windows[-1][1]:
                windows[-1] = (windows[-1][0], max(windows[-1][1], hi), windows[-1][2])
            else:
                windows.append((lo, hi, m.group(0)))
        blocks = []
        for lo, hi, needle in windows:
            section = locate(doc, lo + context if lo else 0, needle)
            where = f"{section.ref} " if section is not None else ""
            blocks.append(f"{where}@{lo:,}: {'…' if lo else ''}{text[lo:hi]}{'…' if hi < len(text) else ''}")
        header = f"{name} {base}: {len(matches)} match{'es' if len(matches) != 1 else ''} for {pattern!r} in {len(text):,} chars"
        if len(matches) > max_hits:
            header += f", first {max_hits} shown (narrow the pattern or raise max_hits)"
        return ToolResult.data(
            header + "\n\n" + "\n\n".join(blocks), count=min(len(matches), max_hits), total=len(matches)
        )
