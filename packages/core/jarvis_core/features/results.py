"""``jarvis.result_read`` — reach back into a tool result that context assembly has trimmed.

An older tool result rides along as a 700-char head so a long run stays affordable, and the
model was told to "note them down now or re-read it once" — with nothing to re-read it with.
Measured 2026-09-07 on "бизнес презентация": 912,217 chars of research gathered against a
76,800-char history budget, so by the step that wrote the deck at most 8.4% of what had been
found was still visible, and the deck came out thin. The full text was in the DB the whole
time. This hands it back, a window at a time, without re-running the search.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from jarvis_core.db import Store
from jarvis_core.tools.builtin import BuiltinProvider, tool
from jarvis_proto import ToolResult

WINDOW = 8_000
MAX_WINDOW = 40_000


class _ReadArgs(BaseModel):
    ref: str = Field(description="The ref printed in the [truncated ...] marker of the result you want back.")
    offset: int = Field(default=0, description="Character offset to start from (0 = the beginning).")
    limit: int = Field(default=WINDOW, description=f"How many characters to return (max {MAX_WINDOW}).")


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
