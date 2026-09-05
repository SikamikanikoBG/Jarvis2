"""Chat messages and tool results — the one format we persist.

Adapters translate *to* the Ollama / OpenAI wire format per call and *from* it into these.
Reasoning is stored beside the content and is never sent back to the model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    id: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    role: Role
    content: str = ""
    reasoning: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    partial: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str, **kw: Any) -> Message:
        return cls(role=Role.USER, content=content, **kw)

    @classmethod
    def assistant(cls, content: str = "", **kw: Any) -> Message:
        return cls(role=Role.ASSISTANT, content=content, **kw)

    @classmethod
    def tool(cls, tool_call_id: str, name: str, content: str, **kw: Any) -> Message:
        return cls(role=Role.TOOL, tool_call_id=tool_call_id, name=name, content=content, **kw)


class ToolResultKind(StrEnum):
    DATA = "data"
    EMPTY = "empty"
    PARTIAL = "partial"
    ERROR = "error"


class ToolResult(BaseModel):
    """Typed outcome of a tool call (V1's contract, adopted everywhere from day one).

    ``partial`` means "there is more" (``cursor`` pages it); an unpaged partial before the run
    ends is a structural signal for the supervisor.
    """

    kind: ToolResultKind
    text: str = ""
    count: int | None = None
    total: int | None = None
    cursor: str | None = None
    error: str | None = None

    @classmethod
    def data(cls, text: str, *, count: int | None = None, total: int | None = None) -> ToolResult:
        return cls(kind=ToolResultKind.DATA, text=text, count=count, total=total)

    @classmethod
    def empty(cls, text: str = "No results.") -> ToolResult:
        return cls(kind=ToolResultKind.EMPTY, text=text, count=0)

    @classmethod
    def partial(cls, text: str, *, cursor: str, count: int | None = None, total: int | None = None) -> ToolResult:
        return cls(kind=ToolResultKind.PARTIAL, text=text, cursor=cursor, count=count, total=total)

    @classmethod
    def failure(cls, error: str) -> ToolResult:
        return cls(kind=ToolResultKind.ERROR, text=f"Error: {error}", error=error)

    def to_model_text(self) -> str:
        """What the model reads. The kind is stated explicitly so it never has to infer it."""
        if self.kind is ToolResultKind.ERROR:
            return self.text
        if self.kind is ToolResultKind.PARTIAL:
            head = f"[partial: {self.count} of {self.total}, more available with cursor={self.cursor!r}]\n"
            return head + self.text
        if self.kind is ToolResultKind.EMPTY:
            return f"[empty] {self.text}"
        return self.text
