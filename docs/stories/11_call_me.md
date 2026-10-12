# 11 — Call me when you're done

Story 10 lets Arsen call Jarvis. This is the other direction. He gives Jarvis a task, says
"call me when you're done" (or taps the bell), and puts the phone away. When the task ends, the
phone rings: a notification from Jarvis. Tapping it opens the app on an incoming call in that
chat. He answers, and Jarvis tells him what the task came to. The rest is in the chat as always.

It is not a phone call. A web page cannot make one. It is the app's own web push, carrying a
notification that opens the call the app already knows how to hold.

## Stories

0. **It is on by default.** Settings → Calls from Jarvis says which kinds of task ring when
   they end without being asked (chats and scheduled prompts unless changed;
   `settings.call_me_default`). A default ring stays quiet while the chat is open in front of
   me, and a call's own turns never ring, since I am on the line. Asked for (the bell, or
   "call me"), it rings whatever I am doing.
1. **I ask for it with the task.** In the composer, next to the incognito and disappearing-chat
   toggles, is a bell: on, the next message's run rings when it ends. While a run works, the
   bell is that run's. On a call it is on the call screen too, so I can say the task, tap the
   bell and hang up. Or I just say it: "call me when you're done" makes Jarvis set it himself
   (`jarvis.call_me`).
2. **My phone rings when it ends.** The app is closed, the phone locked: it still gets a
   notification, "Jarvis is done · <chat>", with the start of the answer, sounding and
   vibrating, with Answer and Decline. A run that fails rings too ("it failed"), and so does
   one that stops to ask me something ("Jarvis needs you"), because I am the one it waits for.
3. **I answer.** Answer on the notification opens the app in that chat, straight into the
   call: Jarvis reads the start of his reply, then listens like any call. Only where the page
   may not start sound without a tap of its own (a browser that blocks autoplay for the site)
   does the incoming screen ask for one more tap, and a ring by link (Discord) always does.
4. **I can decline.** Decline on the notification, or on the incoming screen, and nothing
   happens; the answer is in the chat.
5. **One ring, not a pile.** Two tasks ending together ring once, for the later one, and the
   call says both. A phone that was off gets the latest ring, not every one.
6. **It reaches me even without push.** No device rings (the app was never allowed to notify,
   or every device is gone): the ring goes to the Discord webhook with the same link into the
   chat, and the link opens the same incoming call.

## How it works

- **The run carries it.** Untouched, `settings.call_me_default` decides by the run's kind and
  `Run.call_me_default` marks it. `Run.call_me` is set with the message (`run.create {call_me}`), by the
  bell while the run works (`run.call_me`), or by the run itself (`jarvis.call_me`). It is
  stored, so a restart keeps it.
- **`features/callme.py` decides when and what.** It watches the bus (like the Office) for a run
  with `call_me` that is done, failed or waiting on a question, gathers rings for a few seconds,
  and rings with the start of the reply in plain words.
- **`features/notify.py` carries it.** One channel, two carriers: web push to every subscribed
  device, Discord when none takes it. `notify.discord` is the same channel's tool.
- **`features/webpush.py` is the standard.** The core's own VAPID key (made once, kept in
  `push_identity`, never sent to the UI) signs each push. The payload is encrypted for each
  device (RFC 8291), so the push service carries what it cannot read. A device the push service
  says is gone is forgotten.
- **The app.** `push/push.ts` subscribes this device (from a tap: the bell or Settings → Calls
  from Jarvis, which also lists the devices and has a test ring). `sw.js` shows the ring and,
  when it is tapped, tells an open window or opens one at `/c/<chat>?ring=<run>`. The store shows
  the incoming call (`voice/IncomingCall.tsx`), and answering starts the story-10 call, which
  says the result first.

## What a web page cannot do (and this does not pretend to)

- Ring full-screen like a phone call, or play its own ringtone. The ring is the phone's
  notification sound and a vibration, as loud as the phone allows notifications to be.
- On an iPhone, push works only for the app added to the home screen (iOS 16.4+).
