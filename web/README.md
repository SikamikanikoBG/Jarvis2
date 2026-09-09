# Jarvis V2 — web

The single-page app served by the core (`/`), also loaded by the browser extension's side
panel with `?mode=panel`. Mobile-first PWA; React 19 + TypeScript (strict) + Vite; `zustand`
store fed by one pure WS-event reducer; plain CSS with design tokens ported from V1.

## Run

```sh
npm install
npm run dev            # http://localhost:5173 — proxies /api and /ws to http://127.0.0.1:9020
npm run build          # type-check (tsc -b) + vite build → dist/
npm run lint           # eslint, zero warnings allowed
npm run test           # vitest (reducer / selectors / transcript)
npm run e2e            # playwright against a running core (BASE_URL=… to override)
```

Without a core you can drive every screen with the scripted mock (REST + WS, a fake model
that streams, calls tools, asks for confirmation, fails, or gets stopped by the judge):

```sh
npm run mock                                   # mock core on :9021
JARVIS_BACKEND=http://127.0.0.1:9021 npm run dev
node scripts/shots.mjs http://localhost:5173   # optional: screenshots of every flow → scripts/shots/
```

Message keywords the mock reacts to: `tools`, `confirm`, `fail`, `judge`, `long`, `plan`. The mock
also serves boards, knowledge, skills, schedules (run now creates a scheduled conversation), triage,
STT (any recording ≥ 200 bytes transcribes; smaller → 502), meetings (live segments every 2.5 s),
collab keys and pairing (`mock/features.mjs`).

Feature screens refetch on the server's `*.changed` events: `store/features.ts` folds them into
per-area version counters and `useLoader(loader, version)` re-runs the fetch when the version moves.
`meeting.segment` is the one live payload kept in the store (merged with the REST detail by `seq`).

Auth: the core's bearer token comes from `?token=…` on first load (stripped from the URL and
kept in `localStorage.jarvis_token`), then sent as `Authorization: Bearer` on REST and
`?token=` on the WebSocket. With no token configured the client sends none.

## Folder layout

```
public/            manifest.webmanifest, sw.js (caches the shell only — never /api or /ws), icons/
src/
  main.tsx         boots the store (token, WS, initial loads), mounts <App/>, registers the SW in prod
  protocol/        types.ts — hand-written mirror of packages/proto/jarvis_proto (see below)
  api/             client.ts (REST, ApiError, 422 → field errors) · ws.ts (reconnect 1s→30s, per-frame batching)
  store/           state.ts (ChatState) · reducer.ts (pure applyServerEvent) · selectors.ts (sidebar, active run,
                   pending confirm) · transcript.ts (messages + run events → transcript items) · features.ts
                   (pure fold of *.changed / meeting.segment into refetch versions) · store.ts (zustand, actions,
                   routing side effects) · reducer.test.ts · features.test.ts
  chat/            ChatScreen, Transcript (autoscroll + jump pill), MessageItem, StreamBubble, ReasoningFold,
                   ToolCard, ConfirmCard, Note, RunChip, Composer
  sidebar/         Sidebar (Chats flat + Arsen's own folders + folders by kind → folder_label ?? folder_key;
                   multi-select bar, "only what's waiting for me" toggle), ConversationRow (menu, inline
                   rename, inline delete confirm, activity dot, drag to file), ChatFolderRow, SelectionBar,
                   SearchBox + SearchResults (local titles + server titles/messages, click a message hit to
                   land on it), ActivityDot, dnd.ts
  runs/            RunInspector (timeline with Δt, model/tool pairs, tok/s, payload JSON), RunsScreen, timeline.ts
  settings/        SettingsScreen — roles × ModelSpec (think + level), context/behaviour, MCP servers editor,
                   triage block, collaborator keys, budgets; PATCHes only dirty top-level keys
  status/          StatusScreen — endpoint cards, tool providers + Reload, grouped Tools list, phone pairing (QR)
  boards/          BoardsScreen — boards as columns, sticky notes (colour, move, inline edit), live via board.changed
  knowledge/       KnowledgeScreen + GraphView — search, entity detail (aliases, edges, mentions), edit/merge/delete,
                   SVG neighbourhood graph; live via kg.changed
  skills/          SkillsScreen — list with enable toggles, monospace editor (PUT), new/delete; live via skills.changed
  schedules/       SchedulesScreen — cron/one-shot form with human cron preview, run now, fires; live via schedule.changed
  meetings/        MeetingsScreen — start on a host, live transcript from meeting.segment, frames, stop → summary
  triage/          TriageScreen — per-account cursor table + Run now (settings live in Settings → Triage)
  shell/           App (layout, drawers, inspector column), TopBar + BottomNav, nav.ts, useMediaQuery
  components/      Icon (one inline SVG set), primitives (IconButton, Switch, Menu, InlineConfirm, Drawer,
                   RelativeTime), Markdown (marked + DOMPurify), Toast, useTicker
  lib/             format, markdown, theme, token, router, runs
  styles/          tokens.css (palette, radii, spacing, type) · base.css (reset + primitives) · layout.css ·
                   chat.css · panels.css
mock/server.mjs    scripted mock core for development
scripts/           gen-icons.mjs (PNG icons, no deps), shots.mjs (visual smoke run → scripts/shots/, git-ignored)
e2e/               playwright specs (run against the real core)
```

## Protocol types

`src/protocol/types.ts` mirrors the pydantic models in `packages/proto/jarvis_proto/*.py`
field-for-field. Rules: `X | None` → `X | null` (pydantic always emits the key), `datetime` →
ISO string, `dict[str, Any]` → `Record<string, unknown>`, `StrEnum` → string-literal union,
discriminators are the `type` literal. When the JSON-Schema export
(`python -m jarvis_proto.schema web/src/protocol/schema.json`) lands, generate from it and
snapshot-test both sides; until then a change in `jarvis_proto` is a change here.

The REST-only shapes (`/api/health`, `/api/status`) live at the bottom of the same file.

## How the UI thinks

- **One reducer.** Every WS frame goes through `applyServerEvent(state, event, now)`; the WS
  client batches frames per animation frame so a burst of `model.delta` costs one render.
  `model.delta` is never stored as an event — it only grows the run's `StreamState`.
- **Transcript = messages + run events.** `buildTranscript` groups by run: user message →
  assistant messages / tool cards / system notes merged by server timestamp → live stream →
  run chip. Tool cards are keyed by `call_id` and merge the assistant `tool_calls`, the tool
  message and the `tool.call` / `tool.result` / `tool.confirm_*` events (when loaded). Events
  for non-terminal runs are fetched when a conversation opens (that is how a pending confirm
  card survives a reload); terminal runs load their events lazily in the inspector.
- **Stop is honest.** Pressing Stop sends `run.cancel` and marks the run "stopping"; the
  "Stopped" note appears only when `run.cancelled` arrives. Partial replies carry
  `partial: true` and a marker.
- **Optimistic user message** is bound to its run on `run.queued` (`run_id`,
  `user_message_id`) and replaced by the persisted copy on `message.created`.
- **Reconnect** re-subscribes and refetches messages + runs of the open conversation.

## Theme

Dark by default, light follows `prefers-color-scheme`; the toggle stores `jarvis_theme`
(`light` | `dark`) in localStorage and stamps `data-theme` on `<html>`; Settings → Appearance
has the three-way control. Both palettes were checked for WCAG AA on body text:
`--fg-2`/`--fg-muted` ≥ 4.5:1 on `--bg-surface`; `--fg-dim` is decorative only; filled
controls use `--accent-fill` with `--accent-ink` (dark ink on cyan in dark mode, white on a
darkened cyan in light mode). `prefers-reduced-motion` disables all animation.

## Backend contract (what the client sends and expects)

REST under `/api`, WebSocket at `/ws`; see the task brief for the route list. The frames are
`ClientMessage` / `ServerEvent` from `jarvis_proto.events`. The assumptions the client makes
beyond the written contract are listed in the delivery report and in comments where they
apply (`store.ts`, `reducer.ts`).
