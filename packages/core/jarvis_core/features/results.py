"""``jarvis.result_read`` — reach back into a tool result that context assembly has trimmed.

An older tool result rides along as a 700-char head so a long run stays affordable, and the
model was told to "note them down now or re-read it once" — with nothing to re-read it with.
Measured 2026-09-07 on "бизнес презентация": 912,217 chars of research gathered against a
76,800-char history budget, so by the step that wrote the deck at most 8.4% of what had been
found was still visible, and the deck came out thin. The full text was in the DB the whole
time. This hands it back, a window at a time, without re-running the search.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from jarvis_core.db import Store
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import ToolResult

WINDOW = 8_000
MAX_WINDOW = 40_000
MAX_HITS = 40
CONTEXT = 120
MAX_CONTEXT = 600


class _ReadArgs(BaseModel):
    ref: str = Field(description="The ref printed in the [truncated ...] marker of the result you want back.")
    offset: int = Field(default=0, description="Character offset to start from (0 = the beginning).")
    limit: int = Field(default=WINDOW, description=f"How many characters to return (max {MAX_WINDOW}).")


class _SearchArgs(BaseModel):
    ref: str = Field(description="The ref printed in the [truncated ...] marker of the result to search.")
    pattern: str = Field(description="Case-insensitive regular expression; plain text works too.")
    context: int = Field(default=CONTEXT, description=f"Characters shown on each side of a match (max {MAX_CONTEXT}).")
    max_hits: int = Field(default=MAX_HITS, description=f"Most matches to return (max {MAX_HITS}).")


class ResultsTools(BuiltinProvider):
    name = "jarvis"

    def __init__(self, store: Store) -> None:
        self.store = store
        super().__init__()

    @tool(
        "jarvis.result_read",
        description=(
            "Read back the full text of an earlier tool result in this conversation that was "
            "trimmed to a head. Use the ref from its [truncated ...] marker, and page with "
            "offset — do not re-run the original search or fetch."
        ),
        args=_ReadArgs,
        read_only=True,
        idempotent=True,
    )
    async def _result_read(self, ref: str, offset: int = 0, limit: int = WINDOW) -> ToolResult:
        found = await self.store.tool_result(ref.strip())
        if found is None:
            return ToolResult.failure(
                f"no tool result with ref {ref!r}. The ref is the value printed in the "
                "[truncated ...] marker; only results from this conversation have one."
            )
        name, text = found
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), MAX_WINDOW))
        if offset >= len(text):
            return ToolResult.empty(f"{name}: offset {offset:,} is past the end ({len(text):,} chars).")
        window = text[offset : offset + limit]
        end = offset + len(window)
        header = f"{name} [{offset:,}-{end:,} of {len(text):,} chars]"
        if end < len(text):
            header += f" — continue with offset={end}"
        return ToolResult.data(f"{header}\n{window}")

    @tool(
        "jarvis.result_search",
        description=(
            "Find text inside an earlier tool result that was trimmed to a head, without reading it "
            "all: a case-insensitive regex (or plain text) against the stored result, returning each "
            "match with some characters around it and its char offset — hand an offset to "
            "jarvis.result_read to see more there. Works on minified JSON as well as prose. Use the "
            "ref from the [truncated ...] marker."
        ),
        args=_SearchArgs,
        read_only=True,
        idempotent=True,
    )
    async def _result_search(self, ref: str, pattern: str, context: int = CONTEXT, max_hits: int = MAX_HITS) -> ToolResult:
        found = await self.store.tool_result(ref.strip())
        if found is None:
            return ToolResult.failure(
                f"no tool result with ref {ref!r}. The ref is the value printed in the "
                "[truncated ...] marker; only results from this conversation have one."
            )
        name, text = found
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)  # a bad regex is still a good substring
        context = max(0, min(int(context), MAX_CONTEXT))
        max_hits = max(1, min(int(max_hits), MAX_HITS))
        matches = [m for m in rx.finditer(text) if m.end() > m.start()]
        if not matches:
            return ToolResult.empty(f"{name}: nothing matches {pattern!r} ({len(text):,} chars searched).")
        # Windows around the matches, merged where they touch, so a dense region is one snippet.
        windows: list[tuple[int, int]] = []
        for m in matches[:max_hits]:
            lo, hi = max(0, m.start() - context), min(len(text), m.end() + context)
            if windows and lo <= windows[-1][1]:
                windows[-1] = (windows[-1][0], max(windows[-1][1], hi))
            else:
                windows.append((lo, hi))
        blocks = [f"@{lo:,}: {'…' if lo else ''}{text[lo:hi]}{'…' if hi < len(text) else ''}" for lo, hi in windows]
        header = f"{name}: {len(matches)} match{'es' if len(matches) != 1 else ''} for {pattern!r} in {len(text):,} chars"
        if len(matches) > max_hits:
            header += f", first {max_hits} shown (narrow the pattern or raise max_hits)"
        return ToolResult.data(header + "\n\n" + "\n\n".join(blocks), count=min(len(matches), max_hits), total=len(matches))
