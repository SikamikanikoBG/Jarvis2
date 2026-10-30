# Jarvis2 — how to alpha-test a build (throw-away core on ardi)

For a tester who has not seen this project before. Everything below runs against a **throw-away
core** (`jarvis2-alpha`, port 9121, its own volume) built from `main`. Production
(`jarvis2-core`, port 9020) is never touched: do not PATCH its settings, do not create runs in
it, do not restart it.

- ardi = `ardi@100.97.120.53` (ssh key auth, Docker, the GPUs). Tailscale name `ardi.tail185cf0.ts.net`.
- The laptop runs Git Bash. Set `export MSYS_NO_PATHCONV=1` before passing `/api/...` arguments.
- The production owner token is the "Access token" line in `C:\Users\arsen\Desktop\jarvis-tokens.txt`.
  It is needed only to READ the live settings in step 3. Never print it.
- The alpha's token is `alphatest`.

## 1. Build the image from main

From a clean checkout of `main` (the same files `scripts/deploy_ardi.sh` packs):

```bash
cd /c/Projects/Jarvis2-tasks            # or any clean checkout of main
git fetch && git status                  # clean, and HEAD == origin/main
ARCHIVE="$(mktemp -t jarvis2-alpha-XXXXXX.tar.gz)"
tar -czf "$ARCHIVE" \
    --exclude='.git' --exclude='.venv' --exclude='node_modules' --exclude='data' \
    --exclude='web/dist' --exclude='web/office/dist' --exclude='**/__pycache__' --exclude='*.db' \
    Dockerfile pyproject.toml uv.lock packages web
ssh ardi@100.97.120.53 "rm -rf ~/jarvis2/build-alpha && mkdir -p ~/jarvis2/build-alpha"
scp "$ARCHIVE" ardi@100.97.120.53:jarvis2/build-alpha/src.tar.gz && rm -f "$ARCHIVE"
ssh ardi@100.97.120.53 "cd ~/jarvis2/build-alpha && tar -xzf src.tar.gz && docker build -t jarvis2-core:alpha . 2>&1 | tail -3"
```

## 2. Start the instance

```bash
ssh ardi@100.97.120.53 "docker rm -f jarvis2-alpha 2>/dev/null; \
  docker run -d --name jarvis2-alpha -p 127.0.0.1:9121:9020 -v jarvis2-alpha-data:/data \
    -e JARVIS_TOKEN=alphatest -e TZ=Europe/Sofia --add-host=host.docker.internal:host-gateway \
    jarvis2-core:alpha"
ssh ardi@100.97.120.53 "for i in \$(seq 1 30); do curl -fsS -m 3 http://127.0.0.1:9121/api/health && break; sleep 1; done"
```

(`TZ` and `--add-host` as production has them: the voice line is reached at
`host.docker.internal:9150`; without it a call falls back to the page playing the voice.)

## 3. Settings: the real models and voice, nothing that leaves the house

Copy only `roles`, `voice`, `language_hint` (the "answer Arsen in English" rule) and the `stt_*` keys from production (read-only GET), switch off every
outside connection, and switch tasks on. The JSON is reshaped on the laptop (ssh in this setup
does not pass stdin through, and Git Bash's `/tmp` is not Windows Python's `/tmp`, so use a
Windows path for the files):

```bash
export MSYS_NO_PATHCONV=1
W="$LOCALAPPDATA/Temp/jarvis-alpha"; mkdir -p "$W"
cat > "$W/alpha_patch.py" <<'EOF'
import json, sys
live = json.load(open(sys.argv[1], encoding="utf-8"))
patch = {k: live[k] for k in ("roles", "voice", "language_hint", "stt_url", "stt_kind", "stt_model", "stt_languages", "stt_no_speech_max")}
patch.update(mcp_servers=[], mail_accounts=[], discord_webhook_url=None,
             triage={**live["triage"], "enabled": False}, rsvp={**live["rsvp"], "enabled": False},
             tasks={**live["tasks"], "enabled": True})
json.dump(patch, open(sys.argv[2], "w", encoding="utf-8"))
EOF
TOK=$(grep -i "Access token" /c/Users/arsen/Desktop/jarvis-tokens.txt | head -1 | sed -E 's/.*[:=][[:space:]]*//' | tr -d '\r ')
curl -s -m 10 -H "Authorization: Bearer $TOK" http://100.97.120.53:9020/api/settings > "$W/live.json"
(cd /c/Projects/Jarvis2-tasks && uv run python "$(cygpath -w "$W/alpha_patch.py")" "$(cygpath -w "$W/live.json")" "$(cygpath -w "$W/patch.json")")
rm -f "$W/live.json"
scp -q "$W/patch.json" ardi@100.97.120.53:/tmp/alpha-patch.json && rm -f "$W/patch.json"
ssh ardi@100.97.120.53 'curl -s -o /dev/null -w "patch %{http_code}\n" -X PATCH -H "Authorization: Bearer alphatest" -H "Content-Type: application/json" --data @/tmp/alpha-patch.json http://127.0.0.1:9121/api/settings; rm -f /tmp/alpha-patch.json; curl -s -H "Authorization: Bearer alphatest" http://127.0.0.1:9121/api/tasks/status'
```

Expect `{"enabled":true,"calls_open":0,"voice_style_note":null,...}`.

### Real read-only tools (for a research task judged on quality)

With `mcp_servers: []` a task can only think. Add what reads and changes nothing:

- **Web search** — `searxng_url` (ardi's SearXNG): `PATCH {"searxng_url": "http://100.97.120.53:8085"}` gives
  `web.search` (read-only).
- **Pages** — the `fetch` MCP server runs inside the container (a GET, nothing else):
  `PATCH {"mcp_servers": [{"name": "fetch", "transport": "stdio", "command": "{python}", "args": ["-m", "mcp_server_fetch"]}]}`.
  `fetch.fetch` is on `tasks.no_approval` by default, so a task reads pages without asking.
- **homelab** (ardi's monitoring MCP, `http://100.97.120.53:9810/mcp`) is optional. Its tools send no read-only
  hint, so in a task every call stops for approval — useful to see the approval card on real data, but approve
  only `get_*`/status reads.

### The approval flow without anything outward

Every non-read-only call in a task waits for approval whatever `confirmations` says. Harmless
non-read-only tools that only touch the alpha's own database: `schedule.create` (then
`schedule.delete`), `skills.*`. Ask a typed task to "set a reminder in an hour to …" and the card
turns "waiting on you" with Approve/Deny. Never configure mail accounts, Discord or a host's shell
on the alpha.

### A browser for the browser tools (optional)

Do **not** point Arsen's Brave at the alpha: its extension acts in his logged-in browser. Instead
load the extension (`extension/` in the repo) into the Playwright Chromium below with a throw-away
profile, and point it at the alpha:

```js
const ctx = await chromium.launchPersistentContext('/tmp/alpha-browser', {
  headless: true, args: ['--headless=new', '--disable-extensions-except=/w/extension', '--load-extension=/w/extension'],
});
const sw = ctx.serviceWorkers()[0] ?? (await ctx.waitForEvent('serviceworker'));
await sw.evaluate(() => chrome.storage.local.set({ coreUrl: 'http://127.0.0.1:9121', token: 'alphatest' }));
// /api/status then lists a browser provider with ok: true
```

Everything it logs into is the tester's own throw-away session.

## 4. Reach it from the laptop

```bash
ssh -f -N -L 9121:127.0.0.1:9121 ardi@100.97.120.53
# http://127.0.0.1:9121/?token=alphatest
```

## 5. Drive it like a user: Playwright on ardi

```bash
ssh ardi@100.97.120.53
mkdir -p /tmp/alpha-pw && cd /tmp/alpha-pw   # put scripts (and call.wav) here
docker run --rm --network host --add-host ardi.tail185cf0.ts.net:100.97.120.53 \
  -v /tmp/alpha-pw:/w -w /w mcr.microsoft.com/playwright:v1.63.0-noble \
  bash -c "npm i playwright@1.63.0 >/dev/null 2>&1 && node script.mjs"
```

- Open `http://127.0.0.1:9121/?token=alphatest`. Mobile: `viewport {width: 390, height: 844}`, `isMobile: true`;
  desktop 1366x900; both themes (`colorScheme: 'dark' | 'light'`).
- On a mobile viewport **Enter adds a newline in the composer: click Send**.
- URL changes are `history.pushState`: wait with `page.waitForFunction(() => location.pathname.startsWith('/c/'))`,
  not `waitForURL`.
- Runs are created over the WebSocket (`run.create`), not REST: drive the UI, or open `ws://127.0.0.1:9121/ws?token=alphatest`
  and send `{"type":"run.create","conversation_id":null,"text":"…"}`.
- What to read back: `GET /api/tasks?conversation_id=…`, `GET /api/tasks/{id}`, `GET /api/tasks/status`,
  `GET /api/conversations/{id}/messages` and `/runs` (with `Authorization: Bearer alphatest`).

### Calls with a fake microphone

Chromium flags: `--use-fake-ui-for-media-stream --use-fake-device-for-media-stream --use-file-for-fake-audio-capture=/w/call.wav`
(Chromium plays the file in a loop as the microphone). The WAV must be **48 kHz mono 16-bit**: silence, a spoken question,
silence. Make the speech with the alpha's own voice and convert it inside the alpha container (it has PyAV):

```bash
curl -s -X POST -H 'Authorization: Bearer alphatest' -H 'Content-Type: application/json' \
  -d '{"text":"Find me three hotels near the Rathaus in Vienna for fourteen to sixteen November.","lang":"en"}' \
  http://127.0.0.1:9121/api/tts -o /tmp/alpha-pw/q.audio
docker cp /tmp/alpha-pw/q.audio jarvis2-alpha:/tmp/q.audio
docker exec jarvis2-alpha /app/.venv/bin/python - <<'EOF'
import av, numpy as np, wave
frames = []
with av.open("/tmp/q.audio") as src:
    rs = av.AudioResampler(format="s16", layout="mono", rate=48000)
    for f in src.decode(audio=0):
        for r in rs.resample(f):
            frames.append(r.to_ndarray().reshape(-1))
speech = np.concatenate(frames)
silence = np.zeros(48000 * 3, dtype=np.int16)
with wave.open("/tmp/call.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
    w.writeframes(np.concatenate([silence, speech, np.zeros(48000 * 6, dtype=np.int16)]).tobytes())
EOF
docker cp jarvis2-alpha:/tmp/call.wav /tmp/alpha-pw/call.wav
```

Headless Chromium cannot do real web push; to see push attempts register a device with
endpoint `https://127.0.0.1:1/...` and read the `push to ... failed` warnings in `docker logs jarvis2-alpha`.

### Voice TTFT

Each voice turn is a run with `channel: "voice"`; its `usage.ttft_ms` is the model's time to first
token. `GET /api/conversations/{id}/runs` → take the voice runs → p50/p90. Compare a call with no
task against a call while a long typed task is going (it holds between its steps during the call).

## 6. Clean up

Root-owned `__pycache__` in mounted `/tmp` dirs (Python ran as root in a container):
```bash
docker run --rm -v /tmp:/t alpine rm -rf /t/alpha-pw
```
Remove the instance and its data:
```bash
ssh ardi@100.97.120.53 "docker rm -f jarvis2-alpha && docker volume rm jarvis2-alpha-data && docker image rm jarvis2-core:alpha; rm -rf ~/jarvis2/build-alpha"
```
Close the tunnel: `pkill -f "9121:127.0.0.1:9121"` (Git Bash) or end the ssh process.
