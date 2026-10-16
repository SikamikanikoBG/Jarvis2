# Jarvis call over WebRTC: step 1 prototype

A throwaway prototype (branch `calls-pipecat-proto`). It tests whether a real WebRTC call fixes
three problems with the phone call in `web/src/voice/*`: echo, audio route and hold music.
Nothing in production uses it.

- **Bot** (`bot.py`): [Pipecat](https://github.com/pipecat-ai/pipecat) with the SmallWebRTC
  transport, peer to peer with no media server.
- **Pipeline:** Silero VAD and smart-turn v3 do the turn-taking and barge-in. Then Jarvis's
  Whisper (`/asr` on ardi:9110), qwen3.8-27b on vLLM, and OmniVoice "ryan" (ardi:9120).
- **Shared track:** Jarvis's voice and the hold music (`render_hold.py`, rendered from
  `web/src/voice/hold.ts`) go out on the call's single remote audio track. The browser's echo
  canceller therefore has both as its reference.
- **Page** (`static/index.html`): plain `RTCPeerConnection` with echoCancellation,
  noiseSuppression and autoGainControl on. It has call, mute, output choice and hang-up controls,
  plus a live diagnostics panel.
- **Trace:** each call writes `traces/*.jsonl`, covering browser track settings, outputs and
  route changes, getStats, every transcript, every spoken sentence, barge-ins and music on/off.
  `check_trace.py` prints it.

## Run

    uv sync
    uv run python render_hold.py assets/hold.wav
    ./run.sh                                   # 127.0.0.1:9150, log in bot.log
    tailscale serve --bg --https=8446 http://127.0.0.1:9150
    # → https://ardi.tail185cf0.ts.net:8446

## Headless check

    uv run python test/make_question.py
    CALL_SECONDS=90 docker run --rm --network host -e CALL_SECONDS -v $PWD:/work -w /work \
      mcr.microsoft.com/playwright:v1.63.0-noble \
      sh -c 'npm i --no-save --silent playwright@1.63.0 && node test/call_check.mjs'
    uv run python check_trace.py

Chromium plays the WAV as its microphone. The check proves the following:

- a reply comes back;
- the music is on the received track while Jarvis thinks (from getStats levels);
- a cut-in stops him.

Headless Chromium has no path from loudspeaker to microphone, so it cannot prove that the phone
does not hear itself. Only a real call can.
