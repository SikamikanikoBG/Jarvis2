"""Tool exposure policy: what the model sees vs. what the registry has.

Derived facades: one tool per namespace with an ``op`` enum and an ``args`` object, generated
from the live tool list — an op cannot exist without a backing tool (V1's static facades drifted).
Everything else in the engine keeps using canonical ``namespace.op`` names.
"""

from __future__ import annotations

import json
from typing import Any

from jarvis_proto import ToolCall, ToolSpec

_MIN_GROUP = 3  # namespaces with fewer tools stay flat: a facade would not save anything


def _signature(spec: ToolSpec) -> str:
    props: dict[str, Any] = spec.input_schema.get("properties", {}) or {}
    required = set(spec.input_schema.get("required", []) or [])
    parts: list[str] = []
    for name, schema in props.items():
        typ = schema.get("type") if isinstance(schema, dict) else None
        if isinstance(typ, list):
            typ = "|".join(str(t) for t in typ)
        if isinstance(schema, dict) and "enum" in schema:
            typ = "|".join(str(v) for v in schema["enum"][:6])
        parts.append(f"{name}{'' if name in required else '?'}: {typ or 'any'}")
    return ", ".join(parts)


def build_facades(specs: list[ToolSpec]) -> tuple[list[ToolSpec], dict[str, dict[str, ToolSpec]]]:
    """Return (model-facing specs, facade map namespace → {op: inner spec})."""
    groups: dict[str, list[ToolSpec]] = {}
    for spec in specs:
        groups.setdefault(spec.namespace, []).append(spec)
    exposed: list[ToolSpec] = []
    facades: dict[str, dict[str, ToolSpec]] = {}
    for ns, members in groups.items():
        if len(members) < _MIN_GROUP:
            exposed.extend(members)
            continue
        ops = {m.op: m for m in members}
        facades[ns] = ops
        lines = [f"{ns} tools. Call with op and args. Ops:"]
        for op, m in ops.items():
            desc = (m.description or "").strip().splitlines()[0][:140] if m.description else ""
            lines.append(f"- {op}({_signature(m)}) — {desc}")
        exposed.append(
            ToolSpec(
                name=ns,
                description="\n".join(lines),
                input_schema={
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": list(ops)},
                        "args": {"type": "object", "description": "Arguments for the chosen op"},
                    },
                    "required": ["op"],
                    "additionalProperties": False,
                },
                read_only=all(m.read_only for m in members),
                destructive=any(m.destructive for m in members),
                idempotent=all(m.idempotent for m in members),
                provider=members[0].provider,
            )
        )
    return exposed, facades


class ExposurePolicy:
    def __init__(self, mode: str = "auto", threshold: int = 12) -> None:
        self.mode = mode
        self.threshold = threshold
        self._facades: dict[str, dict[str, ToolSpec]] = {}

    def expose(self, specs: list[ToolSpec]) -> list[ToolSpec]:
        use_facades = self.mode == "facade" or (self.mode == "auto" and len(specs) > self.threshold)
        if not use_facades:
            self._facades = {}
            return list(specs)
        exposed, self._facades = build_facades(specs)
        return exposed

    @property
    def active(self) -> bool:
        return bool(self._facades)

    def resolve(self, call: ToolCall) -> ToolCall:
        """Map a facade call (``homelab`` + op) to the canonical inner call; pass others through."""
        ops = self._facades.get(call.name)
        if ops is None:
            return call
        args = dict(call.arguments)
        op = str(args.pop("op", "") or "")
        inner_args = args.pop("args", None)
        if isinstance(inner_args, str):
            try:
                inner_args = json.loads(inner_args) if inner_args.strip() else {}
            except json.JSONDecodeError:
                inner_args = {"__raw__": inner_args}
        if not isinstance(inner_args, dict):
            inner_args = {}
        inner_args = {**args, **inner_args}  # tolerate flat args next to op
        if op not in ops:
            return ToolCall(
                id=call.id,
                name=f"{call.name}.{op or '?'}",
                arguments=inner_args,
            )
        return ToolCall(id=call.id, name=ops[op].name, arguments=inner_args)
