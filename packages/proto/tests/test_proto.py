import json

import pytest
from pydantic import ValidationError

from jarvis_proto import (
    ModelDelta,
    ModelSpec,
    RoleName,
    RunCreateRequest,
    Settings,
    ToolResult,
    new_id,
    parse_client_message,
)
from jarvis_proto.events import parse_server_event
from jarvis_proto.schema import build_schema


def test_ids_are_prefixed_and_time_ordered():
    a, b = new_id("run"), new_id("run")
    assert a.startswith("run_") and b.startswith("run_")
    assert a.split("_")[1] <= b.split("_")[1]


def test_server_event_roundtrip_is_discriminated():
    ev = ModelDelta(run_id="r", conversation_id="c", kind="text", text="hi")
    back = parse_server_event(json.loads(ev.model_dump_json()))
    assert isinstance(back, ModelDelta) and back.text == "hi"


def test_client_message_unknown_type_rejected():
    with pytest.raises(ValidationError):
        parse_client_message({"type": "nope"})
    msg = parse_client_message({"type": "run.create", "text": "hello"})
    assert isinstance(msg, RunCreateRequest)


def test_only_chat_role_may_think():
    Settings()  # default: chat thinks, others do not
    with pytest.raises(ValidationError, match="may not think"):
        Settings(roles={**Settings().roles, RoleName.JUDGE: ModelSpec(think=True)})


def test_tool_result_partial_tells_the_model_how_to_page():
    r = ToolResult.partial("rows", cursor="abc", count=20, total=57)
    txt = r.to_model_text()
    assert "partial" in txt and "cursor='abc'" in txt and txt.endswith("rows")


def test_schema_exports_every_event():
    schema = build_schema()
    defs = schema["$defs"]
    for name in ("ServerEvent", "ClientMessage", "Run", "Conversation", "Settings", "ToolSpec"):
        assert name in defs
    variants = defs["ServerEvent"]["oneOf"]
    assert len(variants) >= 25
