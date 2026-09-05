# Jarvis V2

Personal AI assistant, rebuilt: one core, thin hands, one protocol. See `docs/DESIGN.md` for the
architecture and the decisions, `docs/stories/` for what each phase must do, `docs/journal.md`
for how we got here.

```
packages/proto   pydantic protocol (messages, run events, settings)  ← single source of truth
packages/core    FastAPI + RunEngine + AgentLoop + model adapters + SQLite
packages/host    Windows host daemon (MCP server) — Phase 3
web/             React + TypeScript SPA (served by core; also the browser side panel)
extension/       MV3 extension — Phase 5
```

## Run it (local)

Requirements: `uv` (provisions Python 3.12 itself), Node 20.

```bash
uv sync --all-packages                 # once
(cd web && npm ci && npm run build)    # builds web/dist, served by the core
cp .env.example .env                   # set JARVIS_TOKEN before exposing the port
uv run jarvis-core                     # http://127.0.0.1:9020
```

Open http://127.0.0.1:9020, go to **Settings**, point the `chat` role at your model:

- vLLM: provider `vllm`, base_url `http://<host>:<port>/v1`, model as served (`/v1/models`).
- Ollama: provider `ollama`, base_url `http://<host>:11434`, model tag as in `ollama list`.

Only the `chat` role may have `think` on. **Status** shows whether each endpoint is reachable;
nothing falls back silently.

Dev loop: `uv run jarvis-core` in one terminal, `cd web && npm run dev` in another
(Vite proxies `/api` and `/ws` to :9020).

## Everyday running (Arsen's setup)

```
  phone / laptop browser
          |  https (Tailscale Serve, valid cert)
          v
  ardi  ── jarvis2-core (Docker, restart unless-stopped)  ← the brain: DB, memory, boards,
          |   |                                              knowledge, schedules, settings,
          |   |                                              the agent loop. Always on.
          |   +--> vader  vLLM qwen3.8-27b                 ← the models
          |   +--> ardi   homelab MCP, WhisperX (STT)
          |   +--> laptop jarvis-host (MCP)                ← Outlook, files, shell, screen
          |                                                   ONLY this needs the laptop
          +-- V1 jarvis-server still on :9010, untouched
```

Jarvis itself does not live on the laptop. With the laptop off, chat, boards, knowledge,
schedules, homelab and web all keep working; only the `workocholic` tools (mail, calendar,
local files/shell/screen) go red — visibly, not silently.

**Phone / anywhere on the tailnet (https — needed for the microphone and the PWA):**

    https://ardi.tail185cf0.ts.net:8444/?token=<JARVIS_TOKEN>

Get a scannable QR for it: `uv run python scripts/pair.py --base http://100.97.120.53:9020
--token <token>`. The https URL comes from `settings.public_url`; it is served by
`tailscale serve --bg --https=8444 http://127.0.0.1:9020` on ardi (config survives reboots;
`tailscale set --operator=ardi` was run once so this needs no sudo any more).

**STT:** ardi's Whisper is the WhisperX ASR web service, so `stt_kind` must be `asr`
(`/asr`), not `openai` (`/v1/audio/transcriptions` → 404 there). The reply names the upstream
that actually answered, so a proxy fail-over is visible instead of silent.

**Direct http (LAN/tailnet, no mic):**

    http://100.97.120.53:9020/?token=<JARVIS_TOKEN>

The **laptop** only runs `jarvis-host` — the daemon that gives Jarvis this machine's Outlook,
files, shell and screen. After a Windows restart it starts itself 40 s after logon (a per-user
Task Scheduler job, no admin, no service):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1     # register (once)
Start-ScheduledTask -TaskName JarvisHost                                   # start it now
Get-ScheduledTask -TaskName JarvisHost                                     # is it registered?
powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Remove   # undo
```

Start it by hand instead: `uv run jarvis-host` (or `powershell -File scripts\start-host.ps1`,
which is a no-op if it is already up). Check it: `curl http://127.0.0.1:9030/healthz`; its log
is `data/host.log`. When the laptop is off, the core's Status screen shows `workocholic` red
and every other tool keeps working — that is by design, not a failure.

If the core does not see the host after a restart, press **Reload tools** on Status
(`POST /api/tools/reload`); MCP servers reconnect on the next call anyway.

Keep the `.ps1` files pure ASCII: `powershell.exe` (5.1) reads a BOM-less script as ANSI, and
one em-dash makes the whole file a parse error.

## Run it (Docker, e.g. on ardi)

```bash
bash scripts/deploy_ardi.sh          # builds ON ardi from a source tarball, SPA included
# or by hand:
docker build -t jarvis2-core .
docker run -d --name jarvis2 -p 9020:9020 -v jarvis2-data:/data -e JARVIS_TOKEN=... jarvis2-core
```

## Verify against a real model

```bash
uv run python scripts/smoke.py --provider vllm --url http://<vader>:8010/v1 --model qwen3.8-27b
```

Streams a reply, makes the model call a tool, stops a run mid-stream, and checks the persisted
run events. Everything it prints happened.

## Develop

```bash
uv run ruff check packages && uv run ruff format --check packages
uv run pyright packages          # strict
uv run pytest -q                 # scripted fake model; cancel/resume/budget/judge scenarios
cd web && npm run lint && npm run test -- --run && npm run build
```

CI (`.github/workflows/ci.yml`) runs all of the above plus the Docker build on every push.
The real-model, real-browser check stays a local gate before a version bump.

## Protocol

`packages/proto/jarvis_proto/events.py` defines every WS frame. Export JSON Schema with
`uv run python -m jarvis_proto.schema web/src/protocol/schema.json`; the TypeScript mirror lives
in `web/src/protocol/types.ts`.
