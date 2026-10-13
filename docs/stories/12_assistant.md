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
  (~0.5 s sooner). When it is not sure, nothing changes. Words said after an early send join the
  run as a steer, as they already do.
- **Long utterances:** speech is recognised in pieces at its pauses while Arsen is still talking,
  so at the end only the last piece is left. Each piece is given what came before it as Whisper's
  prompt.
- **Recognition context:** what Jarvis last said goes to Whisper as its prompt; a short answer is
  recognised as an answer to that.
- **The first sentence:** the first sentence of a reply is synthesised with fewer diffusion steps
  (`voice.omnivoice_first_steps`; 8 steps is 0.54 s against 0.92 s), the rest at full quality.
- **No preamble:** the voice style starts with the answer — no "On a voice call, so I'll keep it
  brief", which was the first sentence of most replies.
