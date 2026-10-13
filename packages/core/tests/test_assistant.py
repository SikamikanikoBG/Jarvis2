"""One assistant, not forty chats (docs/stories/12_assistant.md).

The day journal and its overview, a call that sees the rest of the day, the Jarvis conversation,
a context block that is not sent twice, the GPU slot a call does not queue for, and the speech
side of a faster turn.
"""

from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis_core.app import Core, create_app
from jarvis_core.config import CoreConfig
from jarvis_core.engine.context import CARRIED_NOTE, ContextBlock, render_context
from jarvis_core.engine.current import current_conversation_id
from jarvis_core.features.sessions import SessionsTools
from jarvis_core.features.turn import TurnDetector, _log_mel, _pcm16
from jarvis_core.models.base import BACKGROUND, TYPED, VOICE, EndpointGate, reset_endpoint_semaphores
from jarvis_core.models.fake import FakeAdapter, FakeTurn
from jarvis_proto import AssistantSettings, Channel, ConversationKind, Message, RoleName, VoiceSettings
from tests.conftest import Harness


def _journal_on(harness: Harness) -> FakeAdapter:
    """The journal writes with its own (housekeeping) model: a fake of its own, so the chat's
    scripted turns stay the chat's."""
    harness.enable(assistant=AssistantSettings(journal=True))
    writer = FakeAdapter()
    harness.core.adapters.fakes[RoleName.CLASSIFIER] = writer
    return writer


async def _turn(harness: Harness, conversation_id: str, text: str, reply: str, **kw: object) -> None:
    harness.chat.push(FakeTurn(text=reply))
    sub = harness.subscribe(conversation_id)
    await harness.core.engine.create_run(text=text, conversation_id=conversation_id, **kw)  # type: ignore[arg-type]
    await harness.wait_for(sub, "run.done")


async def _call(tools: SessionsTools, name: str, **args: object) -> str:
    result = await tools.call(name, args, cancel=asyncio.Event(), idempotency_key="k", timeout_s=30)
    return result.text or result.error or ""


# --- the day journal ----------------------------------------------------------------------------


async def test_a_finished_run_leaves_a_line_in_the_day_and_the_newest_line_wins(harness: Harness):
    core = harness.core
    writer = _journal_on(harness)
    olx = await core.store.create_conversation(title="Олх обяви")
    writer.push(
        FakeTurn(
            text='{"summary": "Petar confirmed the card works; waiting on the styrofoam.", "waiting": "", "quiet": false}'
        )
    )
    await _turn(harness, olx.id, "какво стана с обявата", "Petar confirmed the card works.")
    await core.journal.flush()
    rows = await core.journal.digests()
    assert [(d.conversation_id, d.summary) for d in rows] == [
        (olx.id, "Petar confirmed the card works; waiting on the styrofoam.")
    ]
    # The model saw the run's request and answer, and what the entry said before (nothing yet).
    prompt = writer.calls[0][0][0].content
    assert "какво стана с обявата" in prompt and "Petar confirmed the card works." in prompt
    assert "(none yet)" in prompt

    writer.push(FakeTurn(text='{"summary": "Card sold to Petar.", "waiting": "send him the IBAN", "quiet": false}'))
    await _turn(harness, olx.id, "продадох ли я", "Yes, sold.")
    await core.journal.flush()
    rows = await core.journal.digests()
    assert len(rows) == 1 and rows[0].summary == "Card sold to Petar." and rows[0].waiting == "send him the IBAN"
    assert "Petar confirmed the card works; waiting on the styrofoam." in writer.calls[1][0][0].content


async def test_runs_ending_together_are_written_up_once(harness: Harness):
    core = harness.core
    writer = _journal_on(harness)
    conv = await core.store.create_conversation(title="Домо")
    await _turn(harness, conv.id, "първо", "One.")
    await _turn(harness, conv.id, "второ", "Two.")
    writer.push(FakeTurn(text='{"summary": "Two things done.", "waiting": "", "quiet": false}'))
    await core.journal.flush()
    assert len(writer.calls) == 1
    assert "второ" in writer.calls[0][0][0].content  # the latest run is the one written up


async def test_a_model_that_does_not_answer_leaves_a_plain_line_not_a_hole(harness: Harness):
    core = harness.core
    writer = _journal_on(harness)
    conv = await core.store.create_conversation(title="Пътуване до Рим")
    writer.push(FakeTurn(text="I am not JSON"))
    await _turn(harness, conv.id, "билети?", "Booked the 9:40 flight to Rome. Seat 14A.")
    await core.journal.flush()
    (row,) = await core.journal.digests()
    assert row.summary.startswith("Booked the 9:40 flight to Rome.")


async def test_incognito_and_triage_never_reach_the_journal(harness: Harness):
    core = harness.core
    writer = _journal_on(harness)
    secret = await core.store.create_conversation(title="Лично", incognito=True)
    triage = await core.store.create_conversation(title="Triage 2026-10-13", kind=ConversationKind.TRIAGE)
    await _turn(harness, secret.id, "между нас", "OK.")
    await _turn(harness, triage.id, "sort", "Sorted.")
    await core.journal.flush()
    assert writer.calls == [] and await core.journal.digests() == []


async def test_the_overview_puts_what_waits_on_arsen_first_and_leaves_out_this_chat(harness: Harness):
    core = harness.core
    writer = _journal_on(harness)
    olx = await core.store.create_conversation(title="Олх обяви")
    hunt = await core.store.create_conversation(title="Star Hunt · 2026-10-13 10:00", kind=ConversationKind.SCHEDULED)
    gh = await core.store.create_conversation(title="GitHub check · 2026-10-13 20:00", kind=ConversationKind.SCHEDULED)
    here = await core.store.create_conversation(title="Here")
    for conv, digest in (
        (olx, '{"summary": "Card sold to Petar.", "waiting": "", "quiet": false}'),
        (hunt, '{"summary": "Submitted to two directories.", "waiting": "tap the GitHub passkey", "quiet": false}'),
        (gh, '{"summary": "No new notifications.", "waiting": "", "quiet": true}'),
        (here, '{"summary": "This chat.", "waiting": "", "quiet": false}'),
    ):
        writer.push(FakeTurn(text=digest))
        await _turn(harness, conv.id, "go", "Done.")
        await core.journal.flush()

    text = await core.journal.overview(exclude=here.id)
    assert text is not None
    lines = text.splitlines()
    assert lines[0].startswith("## Today in Arsen's other conversations")
    body = [ln for ln in lines if ln.startswith("- ")]
    # Waiting on Arsen first, with what it waits for; then the rest; the routine one at the end.
    assert "@star-hunt-2026-10-13-10-00" in body[0] and "WAITING ON ARSEN: tap the GitHub passkey." in body[0]
    assert "@олх-обяви" in body[1] and "Card sold to Petar." in body[1]
    assert body[-1].startswith("- Routine, nothing new: @github-check")
    assert "This chat." not in text  # it is in front of the model already

    # Capped: what does not fit is named, not dropped in silence.
    short = await core.journal.overview(exclude=here.id, max_chars=400)
    assert short is not None and len(short) < 700 and "@олх-обяви" in short


async def test_nothing_happened_today_is_no_block_at_all(harness: Harness):
    _journal_on(harness)
    assert await harness.core.journal.overview() is None


# --- a call sees the day; the Jarvis conversation always does ------------------------------------


async def _with_a_day(harness: Harness) -> str:
    writer = _journal_on(harness)
    olx = await harness.core.store.create_conversation(title="Олх обяви")
    writer.push(FakeTurn(text='{"summary": "Card sold to Petar.", "waiting": "", "quiet": false}'))
    await _turn(harness, olx.id, "go", "Done.")
    await harness.core.journal.flush()
    return olx.id


async def test_a_call_carries_the_day_and_a_typed_turn_does_not(harness: Harness):
    await _with_a_day(harness)
    conv = await harness.core.store.create_conversation(title="Друго")
    await _turn(harness, conv.id, "нещо друго днес?", "Yes - the OLX card sold.", channel=Channel.VOICE)
    context = next(m for m in harness.chat.calls[-1][0] if m.name == "context")
    assert "## Today in Arsen's other conversations" in context.content and "Card sold to Petar." in context.content

    typed = await harness.core.store.create_conversation(title="Typed")
    await _turn(harness, typed.id, "hi", "Hi.")
    assert not any("Today in Arsen" in m.content for m in harness.chat.calls[-1][0])


async def test_a_call_may_read_the_other_chats(harness: Harness):
    assert "sessions" in VoiceSettings().namespaces
    await _with_a_day(harness)
    conv = await harness.core.store.create_conversation(title="Call")
    await _turn(harness, conv.id, "какво друго", "The card sold.", channel=Channel.VOICE)
    offered = {t.name for t in harness.chat.calls[-1][1]}
    assert {"sessions.today", "sessions.read", "sessions.list"} <= offered


async def test_the_jarvis_conversation_is_made_once_pinned_and_always_has_the_day(harness: Harness):
    core = harness.core
    await _with_a_day(harness)
    home = await core.home_conversation()
    assert home.pinned and home.title == "Jarvis" and not home.title_auto
    assert core.settings.assistant.home_conversation_id == home.id
    assert (await core.store.load_settings()).assistant.home_conversation_id == home.id  # survives a restart
    assert (await core.home_conversation()).id == home.id  # asked again: the same one

    await _turn(harness, home.id, "how's my day", "The card sold.")
    sent = harness.chat.calls[-1][0]
    assert "## The Jarvis conversation" in sent[0].content  # its own block, in its cached prefix
    context = next(m for m in sent if m.name == "context")
    assert "Card sold to Petar." in context.content  # the day, typed and not only on a call

    # Deleted: the next call makes a new one rather than failing.
    await core.delete_conversation(home.id)
    again = await core.home_conversation()
    assert again.id != home.id and again.pinned


# --- sessions: today, and reading what was said -------------------------------------------------


async def test_sessions_today_and_list_today_include_the_scheduled_chats(harness: Harness):
    core = harness.core
    await _with_a_day(harness)
    tools = core.sessions
    hunt = await core.store.create_conversation(title="Star Hunt · 2026-10-13", kind=ConversationKind.SCHEDULED)
    await _turn(harness, hunt.id, "run", "Two directories.")

    listed = await _call(tools, "sessions.list")
    assert "Star Hunt" not in listed  # the plain list is the chats he names, as before
    listed = await _call(tools, "sessions.list", today=True)
    assert "@star-hunt-2026-10-13" in listed and "@олх-обяви" in listed

    token = current_conversation_id.set(hunt.id)
    try:
        today = await _call(tools, "sessions.today")
    finally:
        current_conversation_id.reset(token)
    assert "Card sold to Petar." in today and "@star-hunt" not in today  # not itself


async def test_reading_a_session_shows_what_was_said_not_its_tool_calls(harness: Harness):
    from jarvis_proto import ToolCall

    core = harness.core
    there = await core.store.create_conversation(title="Star Hunt", kind=ConversationKind.SCHEDULED)
    # Three steps of tools (six messages) after the request: the last three messages are all
    # tool traffic, which is all the old read showed of a scheduled chat.
    calls = [
        FakeTurn(tool_calls=[ToolCall(id="c1", name="notes.boards", arguments={})]),
        FakeTurn(tool_calls=[ToolCall(id="c2", name="kg.who_is", arguments={"name": "Petar"})]),
        FakeTurn(tool_calls=[ToolCall(id="c3", name="notes.list", arguments={})]),
    ]
    harness.chat.push(*calls, FakeTurn(text="Submitted to openalternative."))
    sub = harness.subscribe(there.id)
    await core.engine.create_run(text="daily run", conversation_id=there.id)
    await harness.wait_for(sub, "run.done", timeout=20)
    read = await _call(core.sessions, "sessions.read", of=there.id, limit=3)
    assert "Jarvis: Submitted to openalternative." in read
    assert "Arsen: daily run" in read
    assert "Context for the request" not in read


# --- a context block is given once ----------------------------------------------------------------


def _ctx(mid: str, run: str, *blocks: ContextBlock) -> tuple[Message, list[ContextBlock]]:
    return Message.user("x", name="context", id=mid, run_id=run), list(blocks)


def test_a_carried_block_keeps_its_first_copy_whole_and_is_written_out_when_that_is_gone():
    skill = ContextBlock("SkillsBlock", "d1", "## Skill: olx\nhow to answer buyers", carried=False)
    again = ContextBlock("SkillsBlock", "d1", skill.body, carried=True)
    m1, b1 = _ctx("m1", "r1", skill)
    m2, b2 = _ctx("m2", "r2", again)
    m3, b3 = _ctx("m3", "r3", again)
    blocks = {"m1": b1, "m2": b2, "m3": b3}
    history = [
        Message.user("q1", run_id="r1"),
        m1,
        Message.user("q2", run_id="r2"),
        m2,
        Message.user("q3", run_id="r3"),
        m3,
    ]

    out = {m.id: m.content for m in render_context(history, blocks, "r3") if m.id}
    assert "how to answer buyers" in out["m1"]  # the copy everyone refers to stays whole
    assert "## Skill: olx" in out["m2"] and CARRIED_NOTE in out["m2"] and "how to answer" not in out["m2"]
    assert CARRIED_NOTE in out["m3"]

    # The first copy trimmed away: the earliest reference left is written out in full instead.
    out = {m.id: m.content for m in render_context(history[2:], blocks, "r3") if m.id}
    assert "how to answer buyers" in out["m2"] and CARRIED_NOTE in out["m3"]


def test_a_block_nobody_refers_to_any_more_is_stubbed_as_before():
    old = ContextBlock("SkillsBlock", "d1", "## Skill: olx\nhow to answer buyers", carried=False)
    new = ContextBlock("SkillsBlock", "d2", "## Skill: github\nhow to make a token", carried=False)
    m1, b1 = _ctx("m1", "r1", old)
    m2, b2 = _ctx("m2", "r2", new)
    out = {m.id: m.content for m in render_context([m1, m2], {"m1": b1, "m2": b2}, "r2") if m.id}
    assert out["m1"].startswith("[The context block for the request above is not repeated here")
    assert "olx" in out["m1"] and "how to make a token" in out["m2"]


async def test_a_call_does_not_re_send_what_it_was_already_given(harness: Harness):
    """The call style (and any block that has not changed) rides once; the later turns of a call
    are a prefix of each other, so the server's cache holds them (2026-10-13: ~3k tokens re-read
    every turn, most of the 5 s before the first word)."""
    core = harness.core
    conv = await core.store.create_conversation(title="Call")
    for text in ("first", "second", "third"):
        await _turn(harness, conv.id, text, f"Answer to {text}.", channel=Channel.VOICE)
    second, third = harness.chat.calls[1][0], harness.chat.calls[2][0]
    full = [m for m in third if m.name == "context" and "read aloud" in m.content]
    assert len(full) == 1, "the call style is in the prompt once"
    assert sum(CARRIED_NOTE in m.content for m in third if m.name == "context") == 2
    # Everything the second turn sent, the third sends again unchanged, in the same place - up
    # to where the second turn's own ledger stood (the ledger is rebuilt every turn by design).
    shared = [m for m in second if m.name != "ledger"]
    same = [m for m in third if m.name != "ledger"][: len(shared)]
    assert [(m.role, m.name, m.content) for m in same[:-2]] == [(m.role, m.name, m.content) for m in shared[:-2]]


# --- the GPU slot a call does not wait for ------------------------------------------------------


async def test_a_call_goes_ahead_of_everything_waiting_and_may_take_one_slot_more():
    gate = EndpointGate(1)
    order: list[str] = []
    await gate.acquire(BACKGROUND)  # a scheduled step holds the only slot

    async def wants(name: str, urgency: int) -> None:
        await gate.acquire(urgency)
        order.append(name)

    waiting_bg = asyncio.create_task(wants("scheduled", BACKGROUND))
    waiting_typed = asyncio.create_task(wants("typed", TYPED))
    await asyncio.sleep(0)
    voice = asyncio.create_task(wants("voice", VOICE))
    await asyncio.wait_for(voice, 1)  # the extra slot: no waiting for the scheduled step
    assert order == ["voice"] and gate.busy == 2
    await gate.release()
    await gate.release()
    await asyncio.wait_for(waiting_typed, 1)  # the chat he is looking at before the background
    assert order == ["voice", "typed"] and not waiting_bg.done()
    await gate.release()
    await asyncio.wait_for(waiting_bg, 1)
    assert order == ["voice", "typed", "scheduled"]


async def test_a_waiter_that_gives_up_does_not_hold_the_others_back():
    gate = EndpointGate(1)
    await gate.acquire(BACKGROUND)
    await gate.acquire(VOICE)
    impatient = asyncio.create_task(gate.acquire(TYPED))
    later = asyncio.create_task(gate.acquire(BACKGROUND))
    await asyncio.sleep(0)
    impatient.cancel()
    await asyncio.gather(impatient, return_exceptions=True)
    await gate.release()
    await gate.release()
    await asyncio.wait_for(later, 1)
    assert gate.busy == 1


# --- speech: Whisper's context, the end of a turn, the first sentence ----------------------------


@pytest.fixture
def client(tmp_path: Path):
    reset_endpoint_semaphores()
    core = Core(CoreConfig(home=tmp_path, token="secret"))
    fake = FakeAdapter()
    core.adapters.fakes = {r: fake for r in RoleName}
    app = create_app(core.config, core=core)
    with TestClient(app) as c:
        yield c


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def _wav(seconds: float = 1.0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\x01\x00" * int(16_000 * seconds))
    return buf.getvalue()


def test_stt_passes_what_was_said_before_and_scores_the_turn_when_asked(client: TestClient, monkeypatch):
    from jarvis_core.features.stt import SttResult

    core: Core = client.app.state.core  # type: ignore[attr-defined]
    seen: dict[str, object] = {}

    async def transcribe(audio: bytes, **kw: object) -> SttResult:
        seen.update(kw)
        return SttResult(text="Не съм.", language="bg", backend="test", duration_ms=5)

    async def score(wav: bytes) -> float | None:
        return 0.93

    monkeypatch.setattr(core.transcriber, "transcribe", transcribe)
    monkeypatch.setattr(core.turns, "score", score)
    files = {"audio": ("u.wav", _wav(), "audio/wav")}
    r = client.post(
        "/api/stt",
        files=files,
        data={"language": "bg", "prompt": "Are you waiting on me?", "turn": "true"},
        headers=_h(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "Не съм." and body["turn_complete"] == 0.93
    assert seen["prompt"] == "Are you waiting on me?" and seen["language"] == "bg"
    # Not asked: not scored. Switched off in the settings: not scored either.
    assert client.post("/api/stt", files=files, headers=_h()).json()["turn_complete"] is None
    core.settings.voice.turn_detection = False
    assert client.post("/api/stt", files=files, data={"turn": "true"}, headers=_h()).json()["turn_complete"] is None


async def test_the_transcriber_sends_the_prompt_as_whispers_initial_prompt(tmp_path: Path):
    import httpx

    from jarvis_core.features.stt import Transcriber
    from jarvis_proto import Settings

    settings = Settings(stt_url="http://whisper:9000", stt_kind="asr")
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"text": "ok", "language": "bg"})

    t = Transcriber(lambda: settings)
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # pyright: ignore[reportPrivateUsage]
    await t.transcribe(b"RIFF", filename="a.wav", mime="audio/wav", language="bg", prompt="  " + "x" * 600)
    params = sent[0].url.params
    assert params["language"] == "bg" and params["initial_prompt"] == "x" * 400  # its tail, capped
    await t.transcribe(b"RIFF", filename="a.wav", mime="audio/wav", language="bg")
    assert "initial_prompt" not in sent[1].url.params
    await t.aclose()


def test_the_first_sentence_of_a_reply_can_be_made_with_fewer_steps(client: TestClient, monkeypatch):
    from jarvis_core.features import tts

    core: Core = client.app.state.core  # type: ignore[attr-defined]
    core.settings.voice.engine = "omnivoice"
    core.settings.voice.omnivoice_first_steps = 8
    steps: list[int] = []

    async def fake_omnivoice(url: str, text: str, lang: str, voice: str, n: int) -> bytes:
        steps.append(n)
        return b"ID3" + text.encode()

    monkeypatch.setattr(tts, "_omnivoice", fake_omnivoice)
    client.post("/api/tts", json={"text": "First.", "lang": "en", "first": True}, headers=_h())
    client.post("/api/tts", json={"text": "Second.", "lang": "en"}, headers=_h())
    # The same words as a later sentence are another synthesis, not the cached first one.
    client.post("/api/tts", json={"text": "First.", "lang": "en"}, headers=_h())
    assert steps == [8, 16, 16]


async def test_the_turn_model_reads_the_calls_own_wav_and_says_nothing_without_a_model(tmp_path: Path):
    samples = _pcm16(_wav(0.5))
    assert samples is not None and samples.shape == (8_000,)
    with pytest.raises(wave.Error):
        _pcm16(b"not a wav at all")  # score() turns this into "cannot say"
    features = _log_mel(__import__("numpy").zeros(128_000, dtype="float32"))
    assert features.shape == (80, 800)
    detector = TurnDetector(tmp_path)  # no model on the disk, not allowed to fetch one
    assert await detector.score(_wav()) is None
    assert await detector.score(b"garbage") is None
    assert not detector.ready


def test_the_journal_is_off_in_the_settings_the_tests_run_with(tmp_path: Path):
    """A guard for the suite itself: every scripted test would lose a turn to it otherwise."""
    from tests.conftest import fake_settings

    assert fake_settings().assistant.journal is False


def test_whisper_hears_only_the_part_not_yet_recognised(client: TestClient, monkeypatch):
    """A long utterance is sent with up to 8 s of what came before for the turn model; Whisper is
    given the audio from the offset on (docs/stories/12_assistant.md)."""
    from jarvis_core.features.stt import SttResult
    from jarvis_core.features.turn import wav_from

    core: Core = client.app.state.core  # type: ignore[attr-defined]
    heard: list[bytes] = []
    scored: list[bytes] = []

    async def transcribe(audio: bytes, **kw: object) -> SttResult:
        heard.append(audio)
        return SttResult(text="обади ми се", language="bg", backend="test", duration_ms=5)

    async def score(wav: bytes) -> float | None:
        scored.append(wav)
        return 0.5

    monkeypatch.setattr(core.transcriber, "transcribe", transcribe)
    monkeypatch.setattr(core.turns, "score", score)
    whole = _wav(3.0)
    files = {"audio": ("u.wav", whole, "audio/wav")}
    r = client.post("/api/stt", files=files, data={"turn": "true", "stt_offset_ms": "2000"}, headers=_h())
    assert r.status_code == 200
    assert scored == [whole]  # the turn model hears all of it
    with wave.open(io.BytesIO(heard[0])) as w:  # Whisper, the last second
        assert w.getnframes() == 16_000 and w.getframerate() == 16_000
    assert wav_from(b"not a wav", 500) == b"not a wav"


def test_a_settings_form_saved_with_a_stale_assistant_section_keeps_the_jarvis_conversation(client: TestClient):
    """F-02 (alpha-test): the Settings screen sends its whole draft of a section. One loaded before
    the Jarvis conversation existed carried home_conversation_id null; saving it made the next call
    open a second Jarvis conversation."""
    stale = client.get("/api/settings", headers=_h()).json()["assistant"]
    home = client.post("/api/assistant/home", headers=_h()).json()
    saved = client.patch("/api/settings", json={"assistant": {**stale, "journal": False}}, headers=_h()).json()
    assert saved["assistant"]["journal"] is False
    assert saved["assistant"]["home_conversation_id"] == home["id"]
    assert client.post("/api/assistant/home", headers=_h()).json()["id"] == home["id"]
