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
    VoiceSettings,
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


def test_turn_patience_defaults_to_3s_and_is_held_to_1_to_8():
    """A call's patience at a pause that sounds unfinished (voice.turn_patience_s): 3 s for a
    settings file saved before it existed, and a value outside 1-8 s is refused, not clamped."""
    assert VoiceSettings().turn_patience_s == 3.0
    old = Settings().model_dump(mode="json")
    old["voice"].pop("turn_patience_s")
    assert Settings.model_validate(old).voice.turn_patience_s == 3.0
    assert VoiceSettings(turn_patience_s=5).turn_patience_s == 5.0
    for bad in (0.5, 9):
        with pytest.raises(ValidationError):
            VoiceSettings(turn_patience_s=bad)


# --- agent admin: settings paths and risk markers (issue #14) --------------------------------


def test_setting_risk_comes_from_the_field_markers():
    from jarvis_proto import settings_paths as sp

    assert sp.resolve("roles.chat.base_url").risk == {"lifeline"}
    assert sp.resolve("roles.chat.temperature").risk == set()
    assert sp.resolve("mcp_servers[name=fetch].headers").risk == {"lifeline", "secret"}
    assert sp.resolve("mail_accounts[address=a@b.bg].app_password").risk == {"secret"}
    assert sp.resolve("confirmations.mode").risk == {"safety"}
    assert sp.resolve("agent_admin.enabled").risk == {"safety"}
    assert sp.resolve("voice.hold_volume").risk == set()


def test_settings_paths_set_diff_and_mask_round_trip():
    from jarvis_proto import MailAccount, Settings
    from jarvis_proto import settings_paths as sp

    old = Settings(mail_accounts=[MailAccount(address="a@gmail.com", app_password="abcd1234")]).model_dump(mode="json")
    new = sp.deep(old)
    sp.set_at(new, "mcp_servers[name=fetch].enabled", False)
    sp.set_at(new, "budgets.task.max_steps", 70)
    sp.remove_at(new, "mail_accounts[address=a@gmail.com]")
    leaves = {leaf.path: leaf for leaf in sp.diff(old, new)}
    assert leaves["mcp_servers[name=fetch].enabled"].risk == {"lifeline"}
    assert leaves["budgets.task.max_steps"].new == 70
    gone = leaves["mail_accounts[address=a@gmail.com]"]
    assert gone.op == "remove" and gone.secret and "delete" in gone.risk
    shown = sp.mask(old)
    assert "abcd1234" not in str(shown) and sp.keep_masked(shown, old) == old
    try:
        sp.resolve("voice.hold_volumee")
    except sp.PathError as exc:
        assert "voice.hold_volume" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a typo must fail")


def test_confirm_requested_and_tool_result_stay_compatible():
    from jarvis_proto import ToolResult
    from jarvis_proto.events import parse_server_event

    ev = parse_server_event(
        {
            "type": "tool.confirm_requested",
            "run_id": "r",
            "conversation_id": "c",
            "call_id": "x",
            "name": "n",
            "arguments": {},
            "reason": "r",
        }
    )
    assert ev.preview is None and ev.risk is None
    assert ToolResult.data("x").model_dump()["change"] is None
