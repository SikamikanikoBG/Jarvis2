"""The web's protocol types are a hand-written mirror of jarvis_proto (web/src/protocol/types.ts).

A value added here and forgotten there surfaces only at runtime, on the phone: web RunKind had
no 'mail' for weeks. This reads the TypeScript file and checks every wire literal is in it.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest

from jarvis_proto import ConversationKind, RunKind, TaskState
from jarvis_proto.events import ClientMessage, ServerEvent

TYPES = Path(__file__).resolve().parents[3] / "web" / "src" / "protocol" / "types.ts"


def _literals(union: object) -> list[str]:
    members = typing.get_args(typing.get_args(union)[0])
    return [m.model_fields["type"].default for m in members]


def _union(ts: str, name: str) -> set[str]:
    m = re.search(rf"export type {name} =([^;]+);", ts)
    assert m, f"{name} is not declared in types.ts"
    return set(re.findall(r"'([a-z_.]+)'", m.group(1)))


@pytest.fixture(scope="module")
def ts() -> str:
    if not TYPES.exists():
        pytest.skip("web/ is not checked out next to the packages")
    return TYPES.read_text(encoding="utf-8")


def test_every_server_event_and_client_message_type_is_mirrored(ts: str):
    declared = set(re.findall(r"type: '([a-z_.]+)';", ts))
    missing = [t for t in _literals(ServerEvent) + _literals(ClientMessage) if t not in declared]
    assert missing == [], f"not in web/src/protocol/types.ts: {missing}"


@pytest.mark.parametrize(
    ("enum", "name"), [(RunKind, "RunKind"), (ConversationKind, "ConversationKind"), (TaskState, "TaskState")]
)
def test_every_kind_and_state_is_mirrored(ts: str, enum: type, name: str):
    assert {e.value for e in enum} == _union(ts, name)  # type: ignore[attr-defined]
