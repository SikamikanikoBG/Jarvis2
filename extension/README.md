# Jarvis Side Panel — Brave / Chrome extension (V2)

Puts the Jarvis V2 web app in the browser sidebar and lets Jarvis read, navigate
and act in **your own tabs** — real profile, real logins, no launch flags.

The extension is a **tool provider** for the core: it connects to the core's
WebSocket, announces eight `browser.*` tools, and executes them when a run asks.
The core decides *whether* a tool may be called (its tool policy); the
extension decides *how* (the page kernel). There is no on/off switch in the
extension — disable it at `brave://extensions` if you want Jarvis blind.

## Why an extension and not CDP

Chromium ≥ 136 ignores `--remote-debugging-port` unless you also pass a
non-default `--user-data-dir`, so attaching a driver means a throwaway profile
with none of your sessions. The extension runs *inside* your normal Brave.

## Install (once, ~1 minute)

1. Have the core running (default `http://localhost:9020`); note its token.
2. Open `brave://extensions` (or `chrome://extensions`), turn on **Developer mode**.
3. **Load unpacked** → pick this `extension/` folder.
4. Pin the Jarvis button (puzzle-piece icon → pin **Jarvis Side Panel**) and click it.
5. The panel asks to be connected: **Open settings**, fill in
   - **Jarvis core URL** — `http://localhost:9020`, or the Tailscale name / LAN IP
     if the core runs elsewhere;
   - **Access token** — the core's bearer token (empty if the core runs without one);
   click **Test connection** (it calls `GET /api/health` with the bearer header),
   then **Save**.
6. Reopen the panel. The status line reads **Connected · host:port** when `/ws` is up.

## Files

| File | Role |
| --- | --- |
| `manifest.json` | MV3; permissions `sidePanel tabs scripting storage alarms`, host `<all_urls>` |
| `background.js` | service worker: WebSocket client, tool dispatch, tabs/frames, page context |
| `kernel.js` | `pageKernel(op, p)` — injected into the page per call: refs, scoring, typing, reading |
| `content.js` | reports the user's text selection (for `browser.context.selection`) |
| `sidepanel.html/js` | 1-line status bar + `<iframe src="<core>/?mode=panel&token=…">` |
| `options.html/js` | core URL, token, "Test connection" |
| `test/` | `npm test`: kernel scenarios in jsdom + the worker against a mock core |

## How the protocol works

Everything goes over one socket, `ws(s)://<core>/ws?client=browser&token=<token>`
(see `docs/API.md`, "Browser extension"):

```
ext → core  browser.hello   {agent:"jarvis-extension", version, tools: ToolSpec[]}   on every (re)connect
core → ext  browser.call    {call_id, name, arguments}
ext → core  browser.result  {call_id, kind:"data"|"empty"|"error", text, error?}
ext → core  browser.context {url, title, selection?, tab_id}   on active-tab change, only while the panel is open
both        ping / pong
```

- `browser.hello` carries the eight `ToolSpec`s (name, description, JSON Schema
  with `additionalProperties:false`, `read_only`, `destructive`, `idempotent`).
  The core registers them on hello and drops them when the socket closes, so the
  extension re-announces on every reconnect.
- Every `browser.call` gets exactly one `browser.result`: on success, on any
  exception (`kind:"error"` with a message the model can act on), and on timeout
  (30 s in the extension, so a stuck tab never leaves a call hanging). Results
  are plain text; `read`/`find` results begin with `[tab <id>] <title> — <url>`
  so the model always knows which page answered.
- `browser.screenshot` returns a short text plus an extra `image: {mime, base64}`
  field (JPEG) — the core may show it or hand it to a vision model; a core that
  ignores the field loses nothing but the picture.
- Reconnect: 1 s → 30 s exponential backoff; a `chrome.alarms` tick every 24 s
  pings the core (keeps the MV3 worker alive and detects half-open sockets) or
  reconnects if the socket is gone. When the socket never opens, the worker
  probes `/api/health` to tell "core unreachable" from "token rejected (401)" in
  the panel's status line.

### Tools

| Tool | Args | Does |
| --- | --- | --- |
| `browser.tabs` | — | lists tabs: id, title, URL, active, Jarvis work tab, protected Jarvis-app tab |
| `browser.open` | `url` | navigates **Jarvis's own work tab** (created once, reused; never the user's tabs), waits for load + content, reports where it landed (redirect note) |
| `browser.read` | `tab?`, `mode: text\|outline`, `offset?` | `text`: readable text ≤ 12 000 chars with a `[truncated … offset=N continues]` marker; `outline`: headings, links, buttons, fields with `@refs`, capped and prioritised (fields/buttons first) |
| `browser.find` | `query` | snippets around each mention + matching controls with `@refs` |
| `browser.click` | `ref` | scored resolution, scroll into view, refuses disabled/covered, reports field diffs and navigation |
| `browser.type` | `ref`, `text`, `submit?` | native setter for inputs (React sees it), `execCommand` line-by-line for rich editors, reports nearby buttons and their enabled/disabled transitions |
| `browser.scroll` | `ref?` \| `direction` | page or its main scrolling container; by ref → into view |
| `browser.screenshot` | — | visible tab as JPEG (`image` field) |

Refs (`@e12`) are `data-jarvis-ref` attributes stamped by `read`/`find`; they
stay valid until the page navigates, and a recycled node (virtualised lists) is
detected by a content signature and refused rather than clicked.

Tab targeting: the **active tab** first (so co-browsing works when you switch
to a page and say "read this"), falling back to the Jarvis work tab; the tab
running the Jarvis web app itself (same host as the core URL) is never read or
driven. `read {tab}` brings that tab to the front so the following actions land
on the same page.

Frames: when the top document has iframes, `read` folds their text in; `find`,
`click`, `type` and `scroll {ref}` fall back to the frame that last answered,
then probe every frame for the element and act in the one that owns it.

## After changing the code

- `kernel.js` or `background.js`: `brave://extensions` → **↻ reload** on the
  Jarvis card (not a reinstall; settings survive). The kernel is re-parsed by
  Chrome on every injection from `pageKernel.toString()`, so a source file JS
  cannot re-parse (a raw NUL byte once did it) silently kills every op —
  `npm test` re-parses it the same way and scans for control characters.
- `sidepanel.*` / `options.*`: close and reopen the panel / options page.

## Tests

```sh
cd extension
npm install
npm test            # node --test: kernel scenarios (jsdom) + worker vs mock core
npm run check       # node --check on every script
npm run mock-core   # a protocol-speaking fake core on :9021 (token "dev") to smoke-test the real extension
```

`test/mock-core.mjs` is also a library: it accepts `?client=browser`, checks the
token like the core does (403 at the handshake, 401 on `/api/health`), records
`browser.hello`, sends `browser.call`s and resolves with the `browser.result`.

## Limits worth knowing

- Browser-internal pages (`brave://`, `chrome://`, the Web Store, other
  extensions) cannot be read or acted on — Chromium forbids it. The result says
  so instead of failing silently.
- The panel loads the SPA in an iframe with the token in the URL: a
  `chrome-extension://` page framing the core is cross-site, so no cookie ever
  reaches the frame.
- The MV3 worker can be evicted when idle. The alarm revives it; a call landing
  in that gap is answered on the next connection — the core sees a timeout and
  retries.
