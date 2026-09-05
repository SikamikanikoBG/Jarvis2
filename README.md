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

## Run it (Docker, e.g. on ardi)

```bash
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
