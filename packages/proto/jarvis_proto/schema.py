"""Export the protocol as JSON Schema for the web client's generated TypeScript types.

uv run python -m jarvis_proto.schema web/src/protocol/schema.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from jarvis_proto import events, messages, runs, settings, tools

_ROOTS: list[type[BaseModel]] = [
    messages.Message,
    messages.ToolCall,
    messages.ToolResult,
    runs.Run,
    runs.Conversation,
    runs.Plan,
    runs.ModelUsage,
    runs.RunBudget,
    settings.Settings,
    settings.ModelSpec,
    tools.ToolSpec,
]


def build_schema() -> dict[str, Any]:
    server = TypeAdapter(events.ServerEvent).json_schema(ref_template="#/$defs/{model}")
    client = TypeAdapter(events.ClientMessage).json_schema(ref_template="#/$defs/{model}")
    defs: dict[str, Any] = {}
    defs.update(server.pop("$defs", {}))
    defs.update(client.pop("$defs", {}))
    for model in _ROOTS:
        s = model.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(s.pop("$defs", {}))
        defs[model.__name__] = s
    defs["ServerEvent"] = server
    defs["ClientMessage"] = client
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "JarvisProtocol",
        "type": "object",
        "properties": {name: {"$ref": f"#/$defs/{name}"} for name in defs},
        "$defs": defs,
    }


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else None
    text = json.dumps(build_schema(), indent=2, sort_keys=True) + "\n"
    if out is None:
        sys.stdout.write(text)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        sys.stdout.write(f"wrote {out} ({len(text)} bytes)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
