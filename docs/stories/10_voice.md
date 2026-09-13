# 10 — Voice: talking with Jarvis

The microphone is push-to-talk: press, dictate, release, the text lands in the composer. This
is the other thing — a **call**. Arsen puts the headset on, says something, Jarvis answers out
loud, Arsen says the next thing. Same conversation, same memory, same transcript; the only new
thing is the channel. A second way to hold the same conversation, not a second product.

Why it matters: today a task is dictated once and Jarvis goes off to do it. Tomorrow the task can
be *talked through* first — scoped, questioned, narrowed — before anyone does anything.

## Stories

1. **I start a call from the chat I am in.** Next to the microphone there is a headset. One tap
   and the screen becomes the call: Jarvis's name, a ring that says whether he is listening,
   thinking or speaking, and the words as they are said — mine as they are recognised, his as
   he says them. No composer, no menus, nothing to press by accident.
2. **I talk, he talks.** I speak a sentence and stop; a moment later he answers, out loud, with
   the voice the device already has (Google's Bulgarian on the phone, whatever the laptop
   offers). He starts speaking on his first sentence, not after the whole answer is written.
3. **I can cut in.** If he is going the wrong way I just start talking. He stops mid-sentence
   and listens; what I said reaches the run that is still working, so nothing restarts.
4. **He speaks like a person on the phone**, not like a document: short, plain, one question at
   a time, no bullet points, no links read aloud, no "here is a table". If something needs to
   be long, he says so and offers to write it in the chat instead.
5. **On a call he thinks and remembers; he does not act.** He has his own head, the notes and
   the knowledge graph — not mail, not the shell, not the browser. "Send it" on a call gets
   "I'll do that once we're off the call — say the word in the chat." (A setting can widen
   this; the default is deliberately narrow.)
6. **Everything said is in the chat.** Each thing I said and each thing he said is an ordinary
   message in the conversation, marked as spoken, searchable, exportable, remembered — the
   knowledge learner reads it like any other turn. When the call ends I am looking at the
   transcript of it. Starting a call with no chat open starts a new chat, the same way "+" does.
7. **At my ear, the phone does not press its own buttons.** On a touch device the call screen
   locks the moment the call starts: the screen goes near-black and nothing on it responds to
   touch except one control — *hold to unlock*. Ending the call is *hold to hang up*: a tap does
   nothing, a cheek does nothing. The screen stays awake for as long as the call lasts.
8. **It fails out loud, in words.** Whisper down: he says "I can't hear you right now — the
   speech service is not answering" and the screen says the same. Model down, microphone
   refused, no voice on this device: each has one honest sentence, spoken when it can be and
   shown always. Nothing is retried in a loop; nothing is silently dropped.
9. **The laptop has it too.** The same call screen, without the touch lock, with the laptop's
   voices. A call started on the phone and continued on the laptop is the same conversation.

## Platform truths the design is built on (not workarounds — the shape of the web)

- **A web page cannot read the proximity sensor.** No browser ships it. "The screen goes
  inactive at my ear" is therefore done the only way it can be: a **wake lock** keeps the page
  alive, a **near-black surface** takes the light down, and a **touch guard** swallows every
  touch except a deliberate hold. A phone in an Android wrapper (TWA/Capacitor) could turn the
  screen off for real; that is a later, separate step, not this one.
- **A web page cannot route audio to the earpiece.** Playback goes to the media channel — the
  loudspeaker, or the headset when one is connected (wired or Bluetooth). The feature is built
  for the headset; on the bare phone it is speakerphone, and the design says so on screen.
- **Device TTS is `speechSynthesis`.** Free, offline-capable, Bulgarian on Android and iOS. Its
  known edges are designed around: long utterances are cut off in Chrome (so speech is fed one
  sentence at a time), the first utterance must follow a user gesture on iOS (the "start call"
  tap is that gesture), and it pauses in a background tab (the wake lock and the call screen
  keep it in the foreground).
- **Recognition stays where it is: WhisperX on ardi.** It works well, it knows Bulgarian, it is
  private. The browser's own `SpeechRecognition` is Chrome-only, cloud-only and weak on
  Bulgarian; it is not used. What is added on the client is end-of-utterance detection, so
  speech is sent by the sentence rather than by the button.

## Shape

```
  phone / laptop                                     core (ardi)
  ┌──────────────────────────────┐
  │ CallScreen (UI only)         │
  │   ▲ state, captions          │
  │ CallSession  ── state machine ──── ws: run.create {channel: "voice"}
  │   ├─ Listener                │      run.steer  {channel: "voice"}   (barge-in)
  │   │   mic → VAD → utterance ──── POST /api/stt  (WhisperX, as today)
  │   └─ Speaker                 │  ◄── model.delta (text) … run.done
  │       sentences → speechSynthesis
  └──────────────────────────────┘
```

**Client — `web/src/voice/`** (a module with no React in it, so it can be tested as code):

- `segmenter.ts` — end-of-utterance detection over WebAudio energy frames: speech begins when
  energy clears an adaptive floor for ≥ 250 ms, ends after ≥ 700 ms below it; utterances under
  400 ms are dropped (a cough is not a sentence). Pure functions over `(rms, t)` samples.
- `listener.ts` — `getUserMedia({audio: {echoCancellation, noiseSuppression, autoGainControl}})`,
  `MediaRecorder` with timeslices, the segmenter deciding where one utterance ends and the next
  begins; each utterance → `POST /api/stt` → text. Gated while Jarvis speaks unless the energy
  is clearly above what the speaker's own output produces (barge-in, calibrated per call).
- `speaker.ts` — `Speaker` interface with one implementation, `DeviceSpeaker`: takes streamed
  text, cuts it at sentence boundaries, queues one `SpeechSynthesisUtterance` per sentence,
  picks the voice by script (Cyrillic → `bg-*`, otherwise the settings' first language), and
  can be cut off in one call. Progress events so the caption can highlight the sentence being
  spoken. (A `ServerSpeaker` — Piper/Kokoro on ardi — slots in here later without touching
  anything else.)
- `session.ts` — the state machine: `idle → connecting → listening → transcribing → thinking →
  speaking → listening …`, driven by the listener, the speaker and the WS run events the store
  already receives. Cut-in while `speaking`: speaker stops, listener takes the utterance; if the
  run is still working it is a `run.steer`, otherwise a new `run.create`. Every transition is a
  named event so the UI is a pure function of state.
- `CallScreen.tsx` + `TouchGuard.tsx` + `useWakeLock.ts` — the surface. Entry: a headset
  `IconButton` beside the mic in the composer, and `jarvis:call` window event so a shortcut or a
  schedule can open it later.

**Core** — small and native, the same seams incognito uses:

- `proto`: `Channel = "text" | "voice"`; `RunCreateRequest.channel`, `RunSteerRequest.channel`,
  `Message.channel` (persisted; the transcript draws the headset glyph from it).
- Voice run policy in `engine/loop.py`, next to the incognito filter: exposed tools =
  `settings.voice.namespaces` (default `notes`, `kg`, `jarvis`); planner pre-flight skipped
  (a call is never `multi_step`); thinking off unless `settings.voice.think`.
- Voice style block in the per-turn **context message** (never the stable prefix — the cache
  survives): "You are on a voice call. Answer in plain spoken sentences, briefly, one question
  at a time; no markdown, lists, links or code; if the answer needs to be long, say so and offer
  to write it in the chat." Same mechanism skills use today.
- `Settings.voice`: `namespaces: list[str]`, `think: bool`, `style: str` (the block above,
  editable). One more section in Settings — the nav rail already knows how to find it.

## What is deliberately not in this slice

- Server-side TTS (a cloned voice, one voice across devices). The `Speaker` seam is there.
- Wake word / hands-free start. A call starts with a tap.
- Actions on a call (mail, shell, browser). The namespaces setting can widen it; the default
  stays "head, notes, knowledge" until the half-duplex/echo behaviour has been lived with.
- A real screen-off at the ear. Needs a native wrapper; noted above.

## Definition of done

- **Core**: channel on runs and messages; voice policy (tool filter, no plan, think off, style
  block) with tests mirroring the incognito ones — a voice run offered only the allowed
  namespaces, a `workocholic.*` call by name refused with a spoken-style reason, the style block
  present in the context message and absent from the stable prefix.
- **Client module**: the segmenter, the sentence splitter (handles `?!.…`, decimals, `т.н.`,
  Cyrillic quotes) and the session machine under vitest, including cut-in → steer vs. create,
  and every failure path producing exactly one spoken/shown sentence.
- **Call screen**: locked-by-default on touch, hold-to-unlock and hold-to-hang-up (1.2 s, with
  a visible fill), wake lock held for the call and released after, captions in sync with the
  sentence being spoken, "speakerphone" notice when no headset can be assumed.
- **Transcript**: spoken messages carry the headset glyph; export says "(spoken)".
- **End to end**: Playwright with a fake microphone (`--use-fake-device-for-media-stream` and a
  WAV of speech) and a stubbed `speechSynthesis`, against the fake model — one full turn, one
  cut-in, one Whisper-down turn.
- **On the device**: a checklist run on the phone with a Bluetooth headset — Bulgarian in and
  out, cut-in while he speaks, the guard against a cheek, a five-minute call without the screen
  sleeping — before the headset button ships in the composer.

## Slices (each one ships, each one whole)

1. **Core channel + voice policy** (proto, loop, context, settings, tests).
2. **Voice module** (segmenter, listener, speaker, session; vitest) — no UI yet, exercised from
   the console.
3. **Call screen** (surface, touch guard, wake lock, composer entry, transcript glyph).
4. **Cut-in, cues and polish** (barge-in calibration, the short tone on "heard", voice choice by
   script, the Settings section, story sign-off on the device).
