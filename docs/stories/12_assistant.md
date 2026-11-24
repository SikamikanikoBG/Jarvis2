# 12 — One assistant, not forty chats

Every chat is its own world. That is right for work — the OLX chat should not be thinking about
the Star Hunt pipeline — and wrong for a person. On a call in the OLX chat Arsen asked "anything
else interesting from today?" and then "no, I mean another topic, not OLX"; twice Jarvis answered
about OLX, because OLX was all it could see (2026-10-13). The day had six other things going on,
all in scheduled chats he never opens.

This story makes Jarvis know the day the way an assistant does, without making any one chat
heavier, and makes the call answer faster.

## Stories

1. **He knows what happened today.** In any call, Jarvis knows in one line per chat what every
   other conversation did today: what came of it, and whether it is waiting for me. "Anything
   else?" gets the other things.
2. **He can go deeper when I ask.** "What did the Star Hunt one say exactly?" — he reads that
   chat (`sessions.read`) and tells me, on the call. The list of today's chats includes the
   scheduled ones, because that is where most of my day happens.
3. **There is one line to him.** Calling from the home screen, with no chat open, rings the
   *Jarvis* conversation: one long-lived chat, pinned at the top, for "how's my day", "remind me",
   "what's waiting on me". It always has the day's overview, by voice or typed.
4. **It does not get slower.** Nothing here is on the reply's path. The overview is written in the
   background when a run ends, on the background GPU; a call turn carries it, but a block that is
   the same as last turn's is not sent again.
5. **The call answers sooner.** Measured 2026-10-13: a call reply took 5–6 s of model time in a
   normal chat and 30 s in a long one, while speech-to-text and the first sentence of voice took
   under a second each. The fixes go where the time is (below).

## Design

### The day journal — `features/journal.py`

- When a run ends (any kind but triage, never incognito) its conversation is queued. A few seconds
  later — runs that end together are folded — the background lane writes the conversation's
  **digest**: one or two sentences on where it stands, and, if anything, what it waits on Arsen
  for. Input: the title, the previous digest, the run's request and final answer (cut). Output: a
  small JSON. One digest per conversation per day (`conversation_digests`), replaced as the day
  goes on, so it is current, not a log.
- The **overview** is the day's digests, newest first, those waiting on Arsen at the top, with
  each chat's @handle so a follow-up can read it. Capped (`assistant.overview_chars`); what does
  not fit is counted, not dropped silently. The conversation being talked in is left out — its
  history is already in front of the model.
- `sessions.today` returns the same overview as a tool, for a typed chat that asks "what happened
  today?". On a call and in the Jarvis conversation it rides the per-turn context
  (`TodayBlock`), so a question about the day needs no round trip.
- A model failure leaves the previous digest; a conversation with no digest yet shows its title
  and last answer's first sentence.

### Calls see the other sessions

- `sessions` joins the call's tool namespaces (`voice.namespaces`).
- `sessions.list(today=true)` lists every chat active today, scheduled ones included.
  `sessions.read` reads the last things *said* (it used to count tool calls among its last N, and a
  scheduled chat's last ten messages were all tool calls).

### The Jarvis conversation

- `settings.assistant.home_conversation_id` names it; `POST /api/assistant/home` returns it,
  creating it if it is missing (pinned, titled "Jarvis", never auto-renamed). The call button with
  no chat open (and incognito off) calls into it. Its system prefix carries a short block that says
  what it is for.
- It lives long, which is what compaction is for — see below.

### A block that has not changed is not sent again

The per-turn context (skills, knowledge, the voice style, the overview) is persisted with the
turn. On the next turn the old copy used to be replaced by a one-line stub, and the new turn sent
the whole thing again — for the OLX chat ~3k tokens re-read on every turn. Now each block is
stored as a block (`context_blocks`). A block identical to one already in the history is sent as
a one-line reference ("unchanged, given earlier in this chat"), and the earlier copy stays whole
for as long as a later turn refers to it. When the history is trimmed past the earlier copy, the
reference is expanded again in place.

### Compaction never blocks a reply

- It used to run inside the turn: a 180-message chat summarised before every call turn (8–11 s),
  and the new summary then invalidated the cached prefix (TTFT 21 s).
- Now: after a run, if the history is past 80% of its budget, a summary is made in the background
  on the background lane, cutting deep enough that what remains is at most half the budget — so
  the next several turns share one stable prefix. A turn that finds the history over budget
  before the summary exists trims (no model call) and the ledger keeps the record.

### The voice comes first on the GPU

Scheduled runs share the chat lane, two calls at a time. A call's turn no longer queues behind
them: interactive voice requests take the next free slot ahead of everything else, and may use
one slot more than the limit.

### Faster turns on the line

- **End of turn:** at the first pause the audio goes to Whisper as before, and also to a small
  turn model (Smart Turn v3, 8 MB, CPU, ~0.1 s, `features/turn.py`). When it is sure the sentence
  is complete, the call sends at once instead of waiting out the release and the join window
  (~0.5 s sooner). Words said after an early send join the run as a steer, as they already do.
- **Patience (2026-11-14, issue #12):** the turn model decides the other way too. A pause it hears
  as unfinished holds the turn: the less finished it sounds, the longer it waits, from the old
  1.1 s up to `voice.turn_patience_s` (Settings → Voice, 1–8 s, default 3 s) — a cut-off sentence
  (scored 0.06–0.17) waits nearly all of it, a finished one scored just under the bar no longer
  than before. With the model switched off every pause waits the patience; while it cannot say
  (loading after a deploy) the wait is the old 1.1 s. The wait counts from the start of the
  silence. What he says inside it joins the same turn, in the order he said it; a sound with no
  words in it (a cough, the room) does not hold the turn, and its clock goes on from where it
  stood. A turn is sent after 5 minutes whatever happens. Nothing is read out while a turn is held
  (answers, progress lines, a ring's result wait). Before, a pause was sent after at most 1.5 s
  whatever the model heard: 31 of 368 voice turns from 25 Oct to 14 Nov got more of his words within
  20 s of going (8.4 %, `scripts/voice_turn_stats.py`).
- **Long utterances:** speech is recognised in pieces at its pauses while Arsen is still talking,
  so at the end only the last piece is left. Each piece is given what came before it (the held
  turn's end, then the utterance's pieces) as Whisper's prompt. There is no 30 s cut any more: a
  stretch with no pause in it is cut into a piece every 20 s at its quietest moment, so every
  Whisper request stays under 28 s of audio and a five-minute monologue is one turn. The turn
  model's 8 s reach back across his pauses into the same turn, never past the last send or the end
  of Jarvis's voice.
- **Mute, route change, hang-up:** what he was saying is closed and recognised, not dropped. Mute
  sends a held turn at once; a route change stops its clock until the new microphone is up;
  hanging up sends what was recognised (waiting up to 5 s for pieces still with Whisper) and the
  answer lands in the chat. A Whisper failure on one piece keeps the others and says so once.
- **Not over his next sentence (alpha, 2026-11-14):** a turn is never sent, by the model's "sure" or
  by the patience running out, while there is sound over the floor that is not yet confirmed as
  speech (the segmenter's `onset`); the sound's first recognition decides as for speech, and a sound
  that comes to nothing (`quiet`, a release of silence, or 1.5 s without becoming speech) lets the
  clock go on with what was left. An utterance starts at its first sound, not where a dip inside a
  word restarted the attack (the 450 ms cut-in attack lost "Чакай." that way), with the pre-roll
  before it. A piece already recognised is the next piece's Whisper prompt at once. A sound that
  ends before its attack ever held (over the hold music the attack is 450 ms) is still an utterance
  if it lasted 400 ms, and Whisper decides; while he makes a sound or his words are with Whisper
  nothing is read out, and an answer written meanwhile is parked like one he talked over.
- **Only words stop the music (2026-11-24):** while Jarvis works, a sound leaves the call on Thinking,
  music and all, until Whisper finds words in it - at the first pause that has them, or at the end of
  the utterance. Then it is listening. Until then every sound that held 450 ms stopped the music, and
  one with no words left the call on "Listening" for the rest of the run (57 s in one call).
- **Incognito:** with `voice_debug` on, an incognito call writes no trace and its clips are not kept
  (`/api/stt keep=false`).
- **On the screen:** while a turn waits for him the call reads "Listening — go on" with an amber
  ring running down over the wait; the words so far stay visible. The locked (black) call screen
  carries the dark theme's colours for everything on it, whatever the theme.
- **Measuring it:** `scripts/voice_turn_stats.py` (read-only) counts voice turns that got more of
  his words within 20 s of being sent; with `voice_debug` the call trace has `turn.decide`,
  `turn.wait`, `turn.hold`, `turn.send{why}` (sure, patience, ceiling, mute, hangup), `turn.more`,
  `sound` and `piece.forced`.
- **Recognition context:** what Jarvis last said goes to Whisper as its prompt; a short answer is
  recognised as an answer to that.
- **The first sentence:** the first sentence of a reply is synthesised with fewer diffusion steps
  (`voice.omnivoice_first_steps`; 8 steps is 0.54 s against 0.92 s), the rest at full quality.
- **No preamble:** the voice style starts with the answer — no "On a voice call, so I'll keep it
  brief", which was the first sentence of most replies.
