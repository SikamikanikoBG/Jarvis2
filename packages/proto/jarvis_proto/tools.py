"""Tool descriptions as the registry and the model see them."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    """One callable tool. ``name`` is namespaced: ``outlook.list``, ``browser.click``.

    The hints mirror MCP tool annotations and drive engine policy:
    ``read_only`` → safe to retry/resume blind; ``destructive`` → confirmation unless the run
    kind is unattended and the tool is allow-listed; ``idempotent`` → replay-safe.
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    provider: str = "builtin"

    @property
    def namespace(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def op(self) -> str:
        return self.name.split(".", 1)[1] if "." in self.name else self.name
