// Jarvis side panel — background service worker (V2).
//
// One WebSocket to the Jarvis core: /ws?client=browser&token=… . The extension
// is a TOOL PROVIDER (docs/API.md, "Browser extension"):
//
//   ext → core  browser.hello   {agent, version, tools: ToolSpec[]}   on every (re)connect
//   core → ext  browser.call    {call_id, name, arguments}
//   ext → core  browser.result  {call_id, kind: data|empty|error, text, error?}
//   ext → core  browser.context {url, title, selection?}              on tab change, panel open
//   both        ping / pong
//
// Every browser.call gets exactly one browser.result — on success, on any
// exception, and on timeout — because an unanswered call stalls a run.
//
// Everything runs in the user's normal profile: real logins, no launch flags,
// no CDP (Chromium >= 136 kills the debug port on the default profile). The
// page-side logic lives in kernel.js and is injected per call with
// chrome.scripting.executeScript; this file owns tabs, frames and transport.

importScripts("kernel.js");

const DEFAULTS = { coreUrl: "", token: "" };
const VERSION = chrome.runtime.getManifest().version;

// Timing knobs. The test harness overrides them through self.__JARVIS_TIMEOUTS;
// production never sets it.
const TUNE = (typeof self !== "undefined" && self.__JARVIS_TIMEOUTS) || {};
const CALL_TIMEOUT_MS = TUNE.call || 30000;          // one answer per call, always
const INJECT_BUDGET_MS = TUNE.inject || 15000;       // a tab that never settles must not hang us
const LOAD_TIMEOUT_MS = TUNE.load || 15000;
const SETTLE_BUDGET_MS = TUNE.settle || 6000;        // client-rendered content after "complete"
const RETRY_MIN_MS = TUNE.retryMin || 1000;
const RETRY_MAX_MS = TUNE.retryMax || 30000;
const AFTER_ACTION_MS = TUNE.afterAction === undefined ? 400 : TUNE.afterAction;
// browser.wait: poll the page text until it changes and settles (or a phrase
// appears). The model has no other clock — before this it slept through
// workocholic.shell_run and re-read the page, and the identical reads tripped
// the supervisor while a chatbot took two minutes to answer (13 Sep 2026).
const WAIT_DEFAULT_S = TUNE.waitDefault || 30;
const WAIT_MAX_S = TUNE.waitMax || 120;
const WAIT_POLL_MS = TUNE.waitPoll || 1000;
const WAIT_SETTLE_MS = TUNE.waitSettle === undefined ? 1500 : TUNE.waitSettle;
const CONTEXT_DEBOUNCE_MS = TUNE.contextDebounce === undefined ? 300 : TUNE.contextDebounce;
const READ_CAP = 12000;                              // chars of page text per read

// Pages where chrome.scripting is refused by the browser itself. Naming them
// gives the model a real reason instead of an opaque injection failure.
const RESTRICTED = /^(chrome|brave|edge|about|devtools|chrome-extension|moz-extension|view-source):/i;
const WEBSTORE = /^https:\/\/(chromewebstore\.google\.com|chrome\.google\.com\/webstore)/i;

// ---------------------------------------------------------------------------
// Tools, exactly as the model sees them (ToolSpec: packages/proto/.../tools.py).
// The description is the only guidance a local 27B model gets — say WHEN.
// ---------------------------------------------------------------------------

const TOOLS = [
  {
    name: "browser.tabs",
    description:
      "List the tabs open in the user's browser: id, title, URL and which one is active. " +
      "Call it first when the user says 'this page', 'the tab I have open' or names a site they already opened.",
    input_schema: { type: "object", properties: {}, additionalProperties: false },
    read_only: true, destructive: false, idempotent: true,
  },
  {
    name: "browser.open",
    description:
      "Open a full http(s) URL in Jarvis's own work tab (never in the user's tabs) and wait for it to load; " +
      "follow with browser.read. Reports where the browser actually landed.",
    input_schema: {
      type: "object",
      properties: {
        url: { type: "string", minLength: 1, description: "Absolute URL, e.g. https://example.com/page" },
      },
      required: ["url"],
      additionalProperties: false,
    },
    read_only: false, destructive: false, idempotent: false,
  },
  {
    name: "browser.read",
    description:
      "Read the page in the active tab (or the tab id given). mode=text returns the readable text, up to 12000 " +
      "characters, with an offset to continue; mode=outline returns headings, links, buttons and fields with " +
      "@refs that browser.click, browser.type and browser.scroll accept.",
    input_schema: {
      type: "object",
      properties: {
        tab: { type: "integer", description: "Tab id from browser.tabs; that tab is brought to the front. Omit for the active tab." },
        mode: { type: "string", enum: ["text", "outline"], default: "text" },
        offset: { type: "integer", minimum: 0, default: 0, description: "text mode only: continue from this character offset, as given by a previous [truncated] marker." },
      },
      additionalProperties: false,
    },
    read_only: true, destructive: false, idempotent: true,
  },
  {
    name: "browser.find",
    description:
      "Find something on the current page by words it shows — a phrase in the text, a link, a button label, " +
      "a field placeholder. Returns the surrounding text and the matching elements with @refs to act on; " +
      "cheaper than reading the whole outline.",
    input_schema: {
      type: "object",
      properties: { query: { type: "string", minLength: 1, description: "Words that appear on the page." } },
      required: ["query"],
      additionalProperties: false,
    },
    read_only: true, destructive: false, idempotent: true,
  },
  {
    name: "browser.click",
    description:
      "Click a link, button, tab, checkbox or other control by its @ref from browser.read mode=outline or " +
      "browser.find (its exact visible text also works). Scrolls it into view, refuses disabled controls and " +
      "ones covered by a modal, and reports what changed. Never click a chip whose label reads like a message — it sends it.",
    input_schema: {
      type: "object",
      properties: { ref: { type: "string", minLength: 1, description: "@ref such as @e12, or the control's visible text." } },
      required: ["ref"],
      additionalProperties: false,
    },
    read_only: false, destructive: false, idempotent: false,
  },
  {
    name: "browser.type",
    description:
      "Type into a text field, textarea or rich-text editor by its @ref, replacing what it holds; " +
      "submit=true presses Enter / submits the form afterwards. Reports the field's content and the buttons " +
      "next to it (e.g. Send) so the next browser.click is by ref.",
    input_schema: {
      type: "object",
      properties: {
        ref: { type: "string", minLength: 1, description: "@ref of the field, from browser.read mode=outline or browser.find." },
        text: { type: "string", description: "The text to type. Newlines are kept." },
        submit: { type: "boolean", default: false, description: "Press Enter / submit the form after typing." },
      },
      required: ["ref", "text"],
      additionalProperties: false,
    },
    read_only: false, destructive: false, idempotent: false,
  },
  {
    name: "browser.scroll",
    description:
      "Scroll the page or its main scrolling list one screen: direction up, down, top or bottom; or give a @ref " +
      "to bring that element into view. Reports the position and whether the bottom was reached. Scrolling does " +
      "not change what browser.read mode=text returns — use its offset for long text.",
    input_schema: {
      type: "object",
      properties: {
        ref: { type: "string", description: "@ref of an element to scroll into view." },
        direction: { type: "string", enum: ["up", "down", "top", "bottom"], description: "Ignored when ref is given. Default: down." },
      },
      additionalProperties: false,
    },
    read_only: false, destructive: false, idempotent: false,
  },
  {
    name: "browser.screenshot",
    description:
      "Capture what is visible in the active tab as a JPEG image — for layout, charts, images or anything the " +
      "text misses. Prefer browser.read for text.",
    input_schema: { type: "object", properties: {}, additionalProperties: false },
    read_only: true, destructive: false, idempotent: true,
  },
  {
    name: "browser.eval",
    description:
      "Run your own JavaScript in the page (the work tab, in the frame last read) and get the returned value " +
      "back as JSON. The escape hatch when the typed tools do not fit: inspect a control the outline mis-read, " +
      "dispatch the exact event a widget wants, read state only the page knows, click by CSS selector. Write the " +
      "body of an async function and `return` the value. Helpers: $(sel), $$(sel), $ref('e9') resolves an outline " +
      "@ref to its element, describe(el) and evidence(el) give the kernel's view of a node. Runs in the " +
      "extension's isolated world (full DOM, no page variables); set page_world=true to run in the page's own " +
      "JavaScript world (its globals, subject to its CSP). Output capped at 20k characters.",
    input_schema: {
      type: "object",
      properties: {
        code: { type: "string", description: "JavaScript — the body of an async function. `return` what you want to see." },
        page_world: { type: "boolean", description: "Run in the page's main world (access to its JS globals). Default false." },
      },
      required: ["code"],
      additionalProperties: false,
    },
    read_only: false, destructive: false, idempotent: false,
  },
  {
    name: "browser.wait",
    description:
      "Wait for the page to change — a chatbot's reply, a search result, a form's confirmation — and return " +
      "the new text when it arrives. Blocks until the visible text changes and holds still, or until `text` " +
      "appears, or until timeout_s runs out (default 30, max 120). Use this after a click or type that starts " +
      "something slow, instead of sleeping and re-reading; a timeout means nothing changed, not an error.",
    input_schema: {
      type: "object",
      properties: {
        text: { type: "string", description: "Return as soon as this phrase appears on the page (case-insensitive)." },
        timeout_s: { type: "integer", minimum: 1, maximum: 120, description: "How long to wait at most. Default 30." },
      },
      additionalProperties: false,
    },
    read_only: true, destructive: false, idempotent: true,
  },
];

// ---------------------------------------------------------------------------
// Config and status
// ---------------------------------------------------------------------------

function getConfig() {
  return chrome.storage.local.get(DEFAULTS);
}

function wsUrlFrom(coreUrl, token) {
  const u = new URL(coreUrl);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  u.pathname = u.pathname.replace(/\/+$/, "") + "/ws";
  u.hash = "";
  // client=browser tells the core this socket is a tool provider, not a chat client.
  u.search = "?client=browser" + (token ? "&token=" + encodeURIComponent(token) : "");
  return u.toString();
}

const status = { connected: false, error: "", coreUrl: "", attempts: 0, version: VERSION };
const panelPorts = new Set();

function setStatus(patch) {
  Object.assign(status, patch);
  const snapshot = Object.assign({}, status);
  for (const port of panelPorts) {
    try { port.postMessage({ type: "status", status: snapshot }); } catch (e) {}
  }
}

// ---------------------------------------------------------------------------
// Connection
// ---------------------------------------------------------------------------

let ws = null;
let connecting = false;
let generation = 0;            // bumped by reconnectNow(); a connect() from an older generation gives up
let retryDelay = RETRY_MIN_MS;
let retryTimer = null;
let pendingPings = 0;

async function connect() {
  if (connecting || (ws && ws.readyState <= 1)) return;
  // Claim the slot BEFORE the first await: the options page fires a storage
  // change and a reconnect message within the same tick, and two connects
  // racing past this guard would leave an orphaned socket registering tools.
  connecting = true;
  const gen = generation;
  const cfg = await getConfig();
  if (gen !== generation) {
    connecting = false;        // config changed while reading it — start over with the new one
    return connect();
  }
  if (!cfg.coreUrl) {
    connecting = false;
    setStatus({ connected: false, error: "not configured — set the core URL in the extension settings", coreUrl: "" });
    return;
  }
  let url;
  try {
    url = wsUrlFrom(cfg.coreUrl, cfg.token);
  } catch (e) {
    connecting = false;
    setStatus({ connected: false, error: "bad core URL: " + cfg.coreUrl, coreUrl: cfg.coreUrl });
    return;
  }
  let sock;
  try {
    sock = new WebSocket(url);
  } catch (e) {
    connecting = false;
    setStatus({ connected: false, error: String((e && e.message) || e), coreUrl: cfg.coreUrl });
    scheduleRetry();
    return;
  }
  ws = sock;
  let opened = false;
  status.attempts++;

  sock.onopen = () => {
    opened = true;
    connecting = false;
    retryDelay = RETRY_MIN_MS;
    pendingPings = 0;
    setStatus({ connected: true, error: "", coreUrl: cfg.coreUrl, attempts: 0 });
    // Registration is per connection: the core drops our tools when the socket
    // goes, so every (re)connect announces them again.
    send({ type: "browser.hello", agent: "jarvis-extension", version: VERSION, tools: TOOLS });
    pushContext(true);
  };

  sock.onmessage = (ev) => onFrame(ev.data);

  sock.onclose = async (ev) => {
    connecting = false;
    if (ws === sock) ws = null;
    const code = ev && ev.code;
    let error = "";
    if (code === 4401) {
      error = "the core rejected the token (4401) — fix it in the extension settings";
      retryDelay = RETRY_MAX_MS;
    } else if (!opened) {
      // The socket never opened: say WHY, because "reconnecting…" for an hour
      // teaches nobody anything. /api/health separates unreachable / bad token /
      // WS refused.
      error = await diagnose(cfg);
    } else {
      error = "connection lost — reconnecting";
    }
    setStatus({ connected: false, error: error, coreUrl: cfg.coreUrl });
    scheduleRetry();
  };

  sock.onerror = () => {
    try { sock.close(); } catch (e) {}
  };
}

async function diagnose(cfg) {
  try {
    const u = new URL(cfg.coreUrl);
    u.pathname = u.pathname.replace(/\/+$/, "") + "/api/health";
    u.search = "";
    u.hash = "";
    const headers = cfg.token ? { Authorization: "Bearer " + cfg.token } : {};
    const r = await fetch(u.toString(), { headers: headers, cache: "no-store" });
    if (r.status === 401) return "the core rejected the token (401) — fix it in the extension settings";
    if (!r.ok) return "the core answered /api/health with HTTP " + r.status;
    return "the core is up (/api/health ok) but /ws did not open — retrying";
  } catch (e) {
    return "cannot reach " + cfg.coreUrl + " — is the core running?";
  }
}

function scheduleRetry() {
  clearTimeout(retryTimer);
  retryTimer = setTimeout(connect, retryDelay);
  retryDelay = Math.min(retryDelay * 2, RETRY_MAX_MS);
}

function reconnectNow() {
  clearTimeout(retryTimer);
  retryDelay = RETRY_MIN_MS;
  generation++;
  if (ws) {
    // Abandon the socket silently: no onclose → no retry from it, no hello from it.
    const old = ws;
    ws = null;
    try { old.onclose = null; old.onopen = null; old.onmessage = null; old.close(); } catch (e) {}
    connecting = false;
  }
  // If a connect() is still reading the config, the generation bump makes it
  // restart with the new one; otherwise this starts a fresh attempt now.
  connect();
}

function send(obj) {
  if (!ws || ws.readyState !== 1) return false;
  try {
    ws.send(JSON.stringify(obj));
    return true;
  } catch (e) {
    return false;
  }
}

function onFrame(raw) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch (e) {
    return;
  }
  if (!msg || typeof msg.type !== "string") return;
  switch (msg.type) {
    // The core asks the worker to reload itself — after a deploy that shipped
    // new extension files, so nobody has to find brave://extensions. An unpacked
    // extension re-reads its files from disk on chrome.runtime.reload().
    case "browser.reload":
      try { chrome.runtime.reload(); } catch (e) { console.warn("reload refused", e); }
      return;
    case "browser.call":
      handleCall(msg);
      break;
    case "browser.job_done":
      jobDone(String(msg.session || ""));
      break;
    case "ping":
      send({ type: "pong" });
      break;
    case "pong":
      pendingPings = 0;
      break;
    default:
      // The core fans conversation/run events out to every socket; none of them
      // are for us. Ignore rather than parse.
      break;
  }
}

// ---------------------------------------------------------------------------
// Calls: one browser.result per browser.call, no matter what
// ---------------------------------------------------------------------------

function withTimeout(promise, ms, message) {
  let timer;
  const budget = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message())), ms);
  });
  return Promise.race([promise, budget]).finally(() => clearTimeout(timer));
}

async function handleCall(msg) {
  if (msg.call_id === undefined || msg.call_id === null) return; // nothing to answer to
  const name = String(msg.name || "");
  const args = (msg.arguments && typeof msg.arguments === "object") ? msg.arguments : {};
  // Which chat this call belongs to: its own work tab, never another chat's. An older
  // core that does not send one keeps the single shared tab it always had.
  const session = String(msg.session || "default");
  await cancelClose(session); // it is working again; the tab is not going anywhere
  let result;
  try {
    // browser.wait is the one call that is MEANT to take its time.
    const budget = name === "browser.wait" ? waitBudgetMs(args) + 5000 : CALL_TIMEOUT_MS;
    result = await withTimeout(runTool(name, args, session), budget, () =>
      name + " did not complete within " + Math.round(budget / 1000) +
      "s — the tab is probably still loading or busy; wait a moment and retry once");
    if (!result || typeof result !== "object" || !result.kind) {
      result = fail(name + " produced no result");
    }
  } catch (e) {
    result = fail(String((e && e.message) || e));
  }
  send(Object.assign({ type: "browser.result", call_id: msg.call_id }, result));
}

function data(text) { return { kind: "data", text: text }; }
function empty(text) { return { kind: "empty", text: text }; }
function fail(error) { return { kind: "error", text: "Error: " + error, error: error }; }

async function runTool(name, args, session) {
  switch (name) {
    case "browser.tabs": return toolTabs(session);
    case "browser.open": return toolOpen(args, session);
    case "browser.read": return toolRead(args, session);
    case "browser.find": return toolFind(args, session);
    case "browser.click": return toolAct("click", args, session);
    case "browser.type": return toolAct("type", args, session);
    case "browser.scroll": return toolScroll(args, session);
    case "browser.screenshot": return toolScreenshot(session);
    case "browser.wait": return toolWait(args, session);
    case "browser.eval": return toolEval(args, session);
    default:
      return fail("unknown tool " + JSON.stringify(name) + "; this extension provides: " +
                  TOOLS.map((t) => t.name).join(", "));
  }
}

// ---------------------------------------------------------------------------
// Tabs: the user's, the Jarvis app's, and Jarvis's own work tab
// ---------------------------------------------------------------------------

async function activeTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tabs && tabs[0] ? tabs[0] : null;
}

// ONE WORK TAB PER SESSION. A session is a Jarvis chat (the core sends its
// conversation id with every call). It used to be one tab for everything, which
// was right when one chat browsed at a time and wrong the moment two did: the
// second chat drove whatever the first had just opened, and both read each
// other's pages (20 Sep 2026, "they are competing for the same tab").
//
// Still not a tab per TASK — a chat that opens five pages reuses its own one, or
// the browser collects 30 tabs in a day (V1, 21 Aug). When a chat's run ends the
// core says so (browser.job_done) and the tab closes after a short grace, so a
// follow-up turn a few seconds later still finds its page where it left it.
//
// Persisted in chrome.storage.session because MV3 unloads this worker after ~30s
// idle and a bare variable forgets everything.
let workTabs = null; // { [session]: tabId }
const CLOSE_GRACE_MIN = 3;

async function loadWorkTabs() {
  if (workTabs) return workTabs;
  try {
    const stored = await chrome.storage.session.get("workTabs");
    workTabs = (stored && stored.workTabs) || {};
  } catch (e) {
    workTabs = {};
  }
  return workTabs;
}

async function saveWorkTabs() {
  try { await chrome.storage.session.set({ workTabs: workTabs || {} }); } catch (e) {}
}

async function rememberWorkTab(session, tabId) {
  const tabs = await loadWorkTabs();
  tabs[session] = tabId;
  await saveWorkTabs();
}

async function forgetWorkTab(session) {
  const tabs = await loadWorkTabs();
  delete tabs[session];
  await saveWorkTabs();
}

// The session a tab belongs to, or null when it is the user's own.
async function ownerOf(tabId) {
  if (tabId === undefined || tabId === null) return null;
  const tabs = await loadWorkTabs();
  for (const session of Object.keys(tabs)) if (tabs[session] === tabId) return session;
  return null;
}

async function getWorkTab(session) {
  const tabs = await loadWorkTabs();
  const id = tabs[session];
  if (id === undefined) return null;
  try {
    return await chrome.tabs.get(id);
  } catch (e) {
    await forgetWorkTab(session);
    return null;
  }
}

// Tabs running the Jarvis web app itself are PROTECTED: never the fallback
// target, never navigated. Compare host (origin), NOT hostname: with the core on
// localhost:9020 a Grafana on localhost:3040 must stay reachable.
async function isProtectedTab(tab) {
  const url = (tab && (tab.url || tab.pendingUrl)) || "";
  if (!/^https?:/i.test(url)) return false;
  try {
    const cfg = await getConfig();
    if (!cfg.coreUrl) return false;
    return new URL(url).host === new URL(cfg.coreUrl).host;
  } catch (e) {
    return false;
  }
}

// The user's own active tab first — co-browsing depends on it: he opens a course,
// says "done, I opened it for you", and Jarvis must read THAT, not its own stale
// page (13 Sep 2026). But a tab that belongs to ANOTHER session is not the user's
// and is never targeted; that was the competition. So: the active tab if it is
// the user's own or this session's, else this session's work tab.
async function targetTab(session) {
  const tab = await activeTab();
  if (tab && !(await isProtectedTab(tab))) {
    const owner = await ownerOf(tab.id);
    if (!owner || owner === session) return tab;
  }
  const work = await getWorkTab(session);
  if (work) return work;
  if (!tab) throw new Error("no active tab — call browser.open with the URL you need");
  throw new Error(
    "there is no work tab for this chat yet and the active tab is not available to it — call " +
    "browser.open with the URL you need; it opens in this chat's own work tab, never in the " +
    "user's chat window and never in another chat's tab");
}

// An explicit tab id is an instruction. Bring it to the front so the user sees
// what Jarvis is looking at and the following click/type land on the same tab.
async function pickTab(explicit, session) {
  if (explicit !== undefined && explicit !== null && explicit !== "") {
    const id = parseInt(explicit, 10);
    let tab;
    try {
      tab = await chrome.tabs.get(id);
    } catch (e) {
      throw new Error("there is no tab with id " + explicit + " — call browser.tabs for the current list");
    }
    if (await isProtectedTab(tab)) throw new Error("tab " + id + " is the Jarvis app itself — refusing to read it");
    if (!tab.active) {
      try { await chrome.tabs.update(tab.id, { active: true }); } catch (e) {}
    }
    return tab;
  }
  return targetTab(session);
}

function assertScriptable(tab) {
  const url = tab.url || "";
  if (RESTRICTED.test(url) || WEBSTORE.test(url)) {
    throw new Error(
      "this page (" + url + ") is a browser-internal page; extensions are not allowed to read " +
      "or act on it — ask the user to switch to a normal tab, or browser.open a URL");
  }
}

chrome.tabs.onRemoved.addListener((tabId) => {
  ownerOf(tabId).then((session) => { if (session) forgetWorkTab(session); });
  stickyFrame.delete(tabId);
});

// The core says a chat's run has ended. Its tab is given a few minutes in case the
// next turn carries on with the same page, then closed — a job that is over should
// not leave a tab behind (20 Sep 2026).
async function jobDone(session) {
  if (!session) return;
  const tab = await getWorkTab(session);
  if (!tab) return;
  try { await chrome.alarms.create("closeWork:" + session, { delayInMinutes: CLOSE_GRACE_MIN }); } catch (e) {}
}

async function cancelClose(session) {
  try { await chrome.alarms.clear("closeWork:" + session); } catch (e) {}
}

chrome.alarms.onAlarm.addListener((alarm) => {
  const name = (alarm && alarm.name) || "";
  if (!name.startsWith("closeWork:")) return;
  closeWorkTab(name.slice("closeWork:".length));
});

async function closeWorkTab(session) {
  const tab = await getWorkTab(session);
  await forgetWorkTab(session);
  if (!tab) return;
  // Never yank a page out from under the user: a tab he is looking at when the grace
  // runs out is his now, not Jarvis's. And never take the window down with it.
  let focusedWindow = null;
  try { focusedWindow = await chrome.windows.getLastFocused(); } catch (e) {}
  if (tab.active && focusedWindow && focusedWindow.id === tab.windowId) return;
  try {
    const inWindow = await chrome.tabs.query({ windowId: tab.windowId });
    if (inWindow.length <= 1) {
      await chrome.tabs.update(tab.id, { url: "about:blank" });
      return;
    }
    await chrome.tabs.remove(tab.id);
  } catch (e) {}
}

// ---------------------------------------------------------------------------
// Injection and frames
// ---------------------------------------------------------------------------

// chrome.scripting.executeScript can never settle: a tab that keeps committing
// new documents leaves the injection queued with no error and no result. Bound
// it here and say which tab and which op stalled — an unbounded wait is the one
// outcome the model can do nothing with.
function withInjectBudget(promise, tab, args) {
  return withTimeout(promise, INJECT_BUDGET_MS, () =>
    "injecting into tab " + tab.id + " (" + String(tab.url || "").slice(0, 80) + ") did not " +
    "complete within " + Math.round(INJECT_BUDGET_MS / 1000) + "s for '" + (args && args[0]) +
    "' — the tab is still committing a document; wait for it to settle or re-open the URL");
}

async function runInFrame(tab, args, frameId) {
  assertScriptable(tab);
  const target = { tabId: tab.id };
  if (frameId !== undefined && frameId !== null) target.frameIds = [frameId];
  const res = await withInjectBudget(chrome.scripting.executeScript({
    target: target,
    func: pageKernel,
    args: args,
  }), tab, args);
  return res && res[0] ? res[0].result : null;
}

// Run the kernel in every frame; returns [{frameId, result}].
async function runInAllFrames(tab, args) {
  assertScriptable(tab);
  const res = await withInjectBudget(chrome.scripting.executeScript({
    target: { tabId: tab.id, allFrames: true },
    func: pageKernel,
    args: args,
  }), tab, args);
  return (res || [])
    .filter((r) => r && r.result !== null && r.result !== undefined)
    .map((r) => ({ frameId: r.frameId, result: r.result }));
}

// The frame an op last resolved in, per tab. SCORM courses, doc viewers and
// embedded players put the whole UI in an iframe, and refs are DOM attributes
// that only resolve in the frame that minted them. Remember the frame that
// answered and try it before probing every frame.
const stickyFrame = new Map(); // tabId -> frameId

function rememberFrame(tabId, frameId) {
  if (frameId) stickyFrame.set(tabId, frameId);
}

// A top-level navigation tears down every child frame. tabs.onUpdated fires
// for the top frame only, which is exactly the distinction wanted: the course
// moving to its next slide inside the iframe keeps the memory alive.
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info && info.url) stickyFrame.delete(tabId);
});

function emptyForOp(op, res) {
  if (!res || res.ok === false) return true;
  if (op === "find" || op === "locate") return !res.found;
  return false;
}

function scoreOf(op, res) {
  if (!res) return 0;
  if (op === "find") return (res.matches || 0) + (res.elements || []).length;
  if (op === "locate") return res.score || 1;
  return 1;
}

// Same-origin iframes host half the world's chat widgets, editors and embedded
// docs. Rather than firing the action into every frame at once — which would
// double-send anything that matched twice — probe read-only for the frame that
// owns the element, then act in that frame alone.
async function actWithFrameFallback(tab, op, p) {
  const direct = await runInFrame(tab, [op, p]);
  if (direct && direct.ok !== false) return direct;
  const sticky = stickyFrame.get(tab.id);
  if (sticky) {
    const again = await runInFrame(tab, [op, p], sticky);
    if (again && again.ok !== false) {
      again.frame_id = sticky;
      return again;
    }
  }
  let hits = [];
  try {
    hits = await runInAllFrames(tab, ["locate", { selector: p.selector, prefer: op === "type" ? "editable" : "clickable" }]);
  } catch (e) {
    return direct;
  }
  const winner = hits
    .filter((h) => h.frameId !== 0 && h.result && h.result.found)
    .sort((a, b) => (b.result.score || 0) - (a.result.score || 0))[0];
  if (!winner) return withFramesChecked(direct, hits);
  const inFrame = await runInFrame(tab, [op, p], winner.frameId);
  if (inFrame && inFrame.ok !== false) {
    inFrame.frame_id = winner.frameId;
    rememberFrame(tab.id, winner.frameId);
  }
  return inFrame;
}

// The read-only counterpart: "nothing here" is the signal to look in the iframes.
async function readWithFrameFallback(tab, op, p) {
  const direct = await runInFrame(tab, [op, p]);
  if (!emptyForOp(op, direct)) return direct;
  const sticky = stickyFrame.get(tab.id);
  if (sticky) {
    const again = await runInFrame(tab, [op, p], sticky);
    if (!emptyForOp(op, again)) {
      again.frame_id = sticky;
      return again;
    }
  }
  let hits = [];
  try {
    hits = await runInAllFrames(tab, [op, p]);
  } catch (e) {
    return direct;
  }
  const winner = hits
    .filter((h) => h.frameId !== 0 && !emptyForOp(op, h.result))
    .sort((a, b) => scoreOf(op, b.result) - scoreOf(op, a.result))[0];
  if (!winner) return withFramesChecked(direct, hits);
  const out = winner.result;
  out.frame_id = winner.frameId;
  rememberFrame(tab.id, winner.frameId);
  return out;
}

// Nothing matched anywhere. Say how far we looked, so "no longer exists — the
// page re-rendered" is not read as a race the model can win by retrying.
function withFramesChecked(res, hits) {
  const n = (hits || []).length;
  if (!res || n <= 1) return res;
  res.frames_checked = n;
  const note = " Checked all " + n + " frames on this page, not just the top document — it is not in any of them.";
  if (typeof res.error === "string") res.error += note;
  if (typeof res.text === "string") res.text += note;
  return res;
}

// ---------------------------------------------------------------------------
// Page load
// ---------------------------------------------------------------------------

function waitForLoad(tabId) {
  return new Promise((resolve) => {
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      // "complete" means the DOCUMENT finished — on a client-rendered site the
      // content arrives after that. Wait for the text to appear and stop
      // growing instead of guessing a delay.
      waitForContent(tabId).then(resolve, () => resolve());
    };
    const listener = (id, info) => {
      if (id === tabId && info.status === "complete") finish();
    };
    const timer = setTimeout(finish, LOAD_TIMEOUT_MS);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// Poll the page until its visible text stops growing (or the budget runs out).
// Cheap: a length probe, no serialisation of the document.
async function waitForContent(tabId) {
  const deadline = Date.now() + SETTLE_BUDGET_MS;
  let last = -1;
  while (Date.now() < deadline) {
    let len = 0;
    try {
      const r = await chrome.scripting.executeScript({
        target: { tabId: tabId },
        func: () => ((document.body && document.body.innerText) || "").trim().length,
      });
      len = (r && r[0] && r[0].result) || 0;
    } catch (e) {
      return;                       // not scriptable — nothing to wait for
    }
    if (len > 200 && len === last) return;   // settled: two identical readings with real content
    last = len;
    await sleep(250);
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ---------------------------------------------------------------------------
// Tool implementations
// ---------------------------------------------------------------------------

function header(tab) {
  return "[tab " + tab.id + "] " + (tab.title || "(untitled)") + " — " + (tab.url || tab.pendingUrl || "");
}

// Kernel result → browser.result. The kernel's `text` is written for the
// model; this adds which tab (and frame) answered — "I read an empty page" is
// ambiguous between a blank page and the wrong tab without it.
function fromKernel(tab, res, op) {
  if (!res) {
    // Only what is known. The kernel catches its own exceptions and reports
    // them, so a missing result means the injection itself produced nothing:
    // the document was being replaced, or the frame was torn down mid-call.
    return fail("no result came back from the page for " + op + " — tab " + tab.id + " (" +
                String(tab.url || "").slice(0, 80) + "), status " + (tab.status || "unknown") +
                ". The kernel did run and did not throw (it would have said so), so the document " +
                "was most likely replaced or the frame torn down while the call was in flight. " +
                "browser.read to see what is there now, then act again on fresh refs.");
  }
  let head = header(tab);
  if (res.frame_id) head += "\n(inside an iframe of this page)";
  if (res.ok === false) {
    const err = res.error || res.text || "the page refused";
    return { kind: "error", text: "Error: " + (res.text || err) + "\n" + head, error: err };
  }
  if (res.empty) return empty(head + "\n" + res.text);
  return data(head + "\n" + res.text);
}

async function toolTabs(session) {
  const all = await chrome.tabs.query({});
  const mine = await getWorkTab(session);
  const lines = [];
  for (const t of all) {
    const flags = [];
    if (t.active) flags.push("active");
    if (mine && t.id === mine.id) flags.push("this chat's work tab");
    else {
      const owner = await ownerOf(t.id);
      if (owner) flags.push("another chat's work tab — leave it alone");
    }
    if (await isProtectedTab(t)) flags.push("the Jarvis app itself — cannot be read or driven");
    else if (!/^https?:/i.test(t.url || t.pendingUrl || "")) flags.push("browser page — cannot be read");
    lines.push("[tab " + t.id + "] " + (t.title || "(untitled)") + " — " + (t.url || t.pendingUrl || "") +
               (flags.length ? "  (" + flags.join(", ") + ")" : ""));
  }
  if (!lines.length) return empty("No tabs are open.");
  return data(lines.length + " tab" + (lines.length === 1 ? "" : "s") + ":\n" + lines.join("\n"));
}

async function toolOpen(args, session) {
  let url = String(args.url || "").trim();
  if (!url) throw new Error("browser.open needs a url");
  if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) url = "https://" + url;
  if (!/^https?:/i.test(url)) throw new Error("only http(s) URLs can be opened, not " + url.split(":")[0] + ":");
  let tab = await getWorkTab(session);
  let how;
  if (tab) {
    // No `active: true` on a reuse: with several chats browsing at once, each `open`
    // stealing the foreground made the browser flicker between their tabs. The tab is
    // brought to the front when it is CREATED, so the user sees what was started.
    await chrome.tabs.update(tab.id, { url: url });
    how = "in this chat's work tab";
  } else {
    tab = await chrome.tabs.create({ url: url, active: true });
    await rememberWorkTab(session, tab.id);
    how = "in a new work tab for this chat";
  }
  await waitForLoad(tab.id);
  const fresh = await chrome.tabs.get(tab.id);
  let text = "Opened " + (fresh.title || "(untitled)") + " — " + (fresh.url || url) + " " + how +
             " [tab " + fresh.id + "].";
  // Did we land where we asked? A site that bounces to a login wall, consent
  // gate or its own SPA route leaves the model reading a page it did not request.
  try {
    const want = new URL(url), have = new URL(fresh.url || "");
    const trim = (s) => s.replace(/\/+$/, "");
    if (want.origin !== have.origin || trim(want.pathname) !== trim(have.pathname)) {
      text += "\nNote: the browser ended up on a DIFFERENT url than requested (" + url +
              ") — a redirect, login wall, consent gate or SPA route. Read the page before " +
              "assuming the target loaded.";
    }
  } catch (e) {}
  return data(text);
}

async function toolRead(args, session) {
  const tab = await pickTab(args.tab, session);
  const mode = String(args.mode || "text").toLowerCase() === "outline" ? "outline" : "text";
  const p = { mode: mode, offset: Math.max(0, parseInt(args.offset, 10) || 0), max_chars: READ_CAP };
  const top = await runInFrame(tab, ["read", p]);
  if (!top) return fromKernel(tab, null, "read");
  if (top.ok === false) return fromKernel(tab, top, "read");
  let text = top.text;
  let frameText = false;
  // Embedded widgets, comment boxes, players, checkout forms and docs live in
  // iframes, and their content is part of what the user sees. Fold them in
  // whenever the page has any and there is room left.
  if (top.frames_present > 0 && !top.truncated) {
    const used = (mode === "text" ? (top.body || "") : (top.text || "")).length;
    let frames = [];
    try {
      frames = await runInAllFrames(tab, ["read", {
        mode: mode,
        max_chars: Math.max(500, Math.min(4000, READ_CAP - used)),
        max_elements: 40,
      }]);
    } catch (e) {}
    let richest = null;
    for (const f of frames) {
      if (f.frameId === 0 || !f.result || f.result.ok === false || f.result.empty) continue;
      const body = mode === "text" ? (f.result.body || "") : (f.result.text || "");
      if (body.trim().length < 40) continue;
      text += "\n\n--- iframe: " + (f.result.url || "") + " ---\n" + body;
      frameText = true;
      if (!richest || body.length > richest.len) richest = { id: f.frameId, len: body.length };
    }
    // Refs minted in a frame only resolve there; remember where the page's real
    // content lives so the next click lands in it even without a frame hint.
    if (richest && richest.len > (top.body || top.text || "").length) rememberFrame(tab.id, richest.id);
  }
  text = header(tab) + "\n" + text;
  if (top.empty && !frameText) return empty(text);
  return data(text);
}

async function toolFind(args, session) {
  const tab = await targetTab(session);
  const query = String(args.query == null ? "" : args.query);
  const res = await readWithFrameFallback(tab, "find", { text: query });
  return fromKernel(tab, res, "find");
}

async function toolAct(op, args, session) {
  const tab = await targetTab(session);
  const ref = String(args.ref == null ? "" : args.ref).trim();
  if (!ref && op === "click") {
    throw new Error("browser.click needs a ref — @e12 from browser.read mode=outline or browser.find, " +
                    "or the control's visible text");
  }
  const p = op === "click"
    ? { selector: ref }
    : { selector: ref, value: String(args.text == null ? "" : args.text), submit: !!args.submit };
  const res = await actWithFrameFallback(tab, op, p);
  // Let a navigation or re-render triggered by the action settle before
  // reporting back — the model's next read should see the new page.
  if (AFTER_ACTION_MS) await sleep(AFTER_ACTION_MS);
  const out = fromKernel(tab, res, op);
  if (out.kind !== "error") {
    try {
      const fresh = await chrome.tabs.get(tab.id);
      if (fresh && fresh.url !== tab.url) {
        out.text += "\nThe page navigated: now " + (fresh.title || "(untitled)") + " — " + (fresh.url || "");
      }
    } catch (e) {}
  }
  return out;
}

async function toolScroll(args, session) {
  const tab = await targetTab(session);
  const ref = String(args.ref == null ? "" : args.ref).trim();
  if (ref) {
    const res = await actWithFrameFallback(tab, "scroll", { selector: ref, direction: "element" });
    return fromKernel(tab, res, "scroll");
  }
  const want = String(args.direction || "down").toLowerCase();
  const dir = ["up", "down", "top", "bottom"].indexOf(want) >= 0 ? want : "down";
  let res = await runInFrame(tab, ["scroll", { direction: dir }]);
  // The top document does not scroll but the frame we last read does: scroll that.
  if (res && res.ok !== false && res.scrollable === false) {
    const sticky = stickyFrame.get(tab.id);
    if (sticky) {
      const again = await runInFrame(tab, ["scroll", { direction: dir }], sticky);
      if (again && again.ok !== false && again.scrollable !== false) {
        again.frame_id = sticky;
        res = again;
      }
    }
  }
  return fromKernel(tab, res, "scroll");
}

const EVAL_MAX_CHARS = 20000;

function renderValue(v) {
  if (v === undefined) return "undefined";
  if (typeof v === "string") return v;
  try {
    const s = JSON.stringify(v, (k, x) => {
      if (x && typeof x === "object" && typeof x.nodeType === "number" && x.tagName) {
        return "<" + String(x.tagName).toLowerCase() + (x.id ? "#" + x.id : "") + ">";
      }
      return x;
    }, 1);
    return s === undefined ? String(v) : s;
  } catch (e) {
    return String(v);
  }
}

// Unlike the typed ops, eval carries the model's own code, so the frame it
// runs in is stated every time and the page's main world is opt-in: a bank's
// CSP may refuse it, and that refusal is reported as the fact it is.
async function toolEval(args, session) {
  const tab = await targetTab(session);
  assertScriptable(tab);
  const code = String(args.code == null ? "" : args.code);
  if (!code.trim()) throw new Error("browser.eval needs code");
  const sticky = stickyFrame.get(tab.id);
  const target = { tabId: tab.id };
  if (sticky) target.frameIds = [sticky];
  const inject = { target: target, func: pageKernel, args: ["eval", { code: code }] };
  if (args.page_world) inject.world = "MAIN";
  let res;
  try {
    const out = await withInjectBudget(chrome.scripting.executeScript(inject), tab, ["eval"]);
    res = out && out[0] ? out[0].result : null;
  } catch (e) {
    return fail("browser.eval could not run in tab " + tab.id + ": " + String((e && e.message) || e) +
                (args.page_world ? " (page_world=true is subject to the page's CSP; try without it)" : ""));
  }
  const where = header(tab) + (sticky ? "\n(inside the iframe last read)" : "") +
                (args.page_world ? "\n(page main world)" : "");
  if (!res) return fail("browser.eval returned no result (the document was replaced mid-call?)\n" + where);
  if (res.ok === false) {
    const err = res.error || "eval failed";
    return { kind: "error", error: err,
             text: "Error: " + err + (res.stack ? "\n" + res.stack : "") + "\n" + where };
  }
  let text = renderValue(res.value);
  if (text.length > EVAL_MAX_CHARS) text = text.slice(0, EVAL_MAX_CHARS) + "\n… [cut at " + EVAL_MAX_CHARS + " chars — return less]";
  if (AFTER_ACTION_MS) await sleep(AFTER_ACTION_MS);
  return data(where + "\n" + text);
}

function waitBudgetMs(args) {
  const asked = Number(args && args.timeout_s);
  const s = asked > 0 ? Math.min(WAIT_MAX_S, Math.max(1, asked)) : WAIT_DEFAULT_S;
  return s * 1000;
}

// The text the model would get from browser.read, from the frame it last read
// (the chat widget's iframe, when there is one), else the top document.
async function snapshotText(tab) {
  const sticky = stickyFrame.get(tab.id);
  let r = null;
  if (sticky) { try { r = await runInFrame(tab, ["snapshot", {}], sticky); } catch (e) { r = null; } }
  if (!r || !r.text) r = await runInFrame(tab, ["snapshot", {}]);
  return (r && typeof r.text === "string") ? r.text : "";
}

// What is new: chat logs, feeds and result lists append, so the text after the
// longest common prefix IS the new content. When the page re-rendered instead,
// show its tail — that is where a reply lands in a chat.
function newSince(before, after) {
  let i = 0;
  const n = Math.min(before.length, after.length);
  while (i < n && before.charCodeAt(i) === after.charCodeAt(i)) i++;
  const grew = after.length > before.length && i >= before.length * 0.8;
  const piece = grew ? after.slice(i) : after.slice(-900);
  const t = piece.trim().slice(0, 1500);
  return { grew: grew, text: t };
}

async function toolWait(args, session) {
  const tab = await targetTab(session);
  const want = String(args.text == null ? "" : args.text).trim().toLowerCase();
  const total = waitBudgetMs(args);
  const started = Date.now();
  const base = await snapshotText(tab);
  const seconds = () => Math.max(1, Math.round((Date.now() - started) / 1000));
  const report = (cur, why) => {
    const d = newSince(base, cur);
    const head = header(tab) + "\n" + why + " after " + seconds() + "s.";
    if (!d.text) return data(head);
    return data(head + (d.grew ? " New text:\n" : " The page re-rendered; it now ends with:\n") + d.text);
  };
  if (want && base.toLowerCase().includes(want)) return report(base, "\u201C" + args.text + "\u201D is already on the page");
  let last = base;
  let changed = false;
  let lastChange = 0;
  while (Date.now() - started < total) {
    await sleep(Math.min(WAIT_POLL_MS, Math.max(50, total - (Date.now() - started))));
    let cur;
    try { cur = await snapshotText(tab); } catch (e) { continue; }   // mid-navigation: try again
    if (cur !== last) { last = cur; changed = true; lastChange = Date.now(); }
    if (want && cur.toLowerCase().includes(want)) return report(cur, "\u201C" + args.text + "\u201D appeared");
    if (!want && changed && Date.now() - lastChange >= WAIT_SETTLE_MS) return report(cur, "The page changed");
  }
  if (changed) return report(last, "The page kept changing for the whole wait; this is where it stood");
  return empty(header(tab) + "\nNothing changed on the page in " + seconds() + "s" +
               (want ? " and \u201C" + args.text + "\u201D did not appear" : "") +
               ". It may still be working — browser.wait again with a longer timeout_s, or browser.read " +
               "to see the page as it is. Do not re-send what you already sent.");
}

async function toolScreenshot(session) {
  let tab = await targetTab(session);
  // captureVisibleTab shoots whatever is visible in the window, so a non-active
  // target would silently return the wrong page.
  if (!tab.active) {
    await chrome.tabs.update(tab.id, { active: true });
    await sleep(250);
    tab = await chrome.tabs.get(tab.id);
  }
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 70 });
  const m = /^data:([^;,]+);base64,([\s\S]*)$/.exec(dataUrl || "");
  if (!m) throw new Error("the browser returned no image for tab " + tab.id);
  const kb = Math.round(m[2].length * 3 / 4 / 1024);
  return {
    kind: "data",
    text: header(tab) + "\nScreenshot captured (" + m[1] + ", ~" + kb + " KB); attached to this result as " +
          "`image` for the core to display or hand to a vision model.",
    image: { mime: m[1], base64: m[2] },
  };
}

// ---------------------------------------------------------------------------
// Page context: which page the user is looking at (side panel open only)
// ---------------------------------------------------------------------------

let contextTimer = null;
let lastContextKey = null;
let lastSelection = { tabId: null, text: "" };

function panelOpen() {
  return panelPorts.size > 0;
}

function pushContext(force) {
  if (!panelOpen()) return;
  clearTimeout(contextTimer);
  contextTimer = setTimeout(() => { sendContext(force).catch(() => {}); }, CONTEXT_DEBOUNCE_MS);
}

async function sendContext(force) {
  if (!ws || ws.readyState !== 1 || !panelOpen()) return;
  const tab = await activeTab();
  if (!tab) return;
  const selection = lastSelection.tabId === tab.id ? lastSelection.text : "";
  const payload = { type: "browser.context", url: tab.url || "", title: tab.title || "", tab_id: tab.id };
  if (selection) payload.selection = selection;
  const key = payload.url + "|" + payload.title + "|" + selection;
  if (!force && key === lastContextKey) return;   // nothing actually changed
  lastContextKey = key;
  send(payload);
}

chrome.tabs.onActivated.addListener(() => pushContext(false));

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete" && tab && tab.active) pushContext(false);
});

chrome.windows.onFocusChanged.addListener((id) => {
  if (id !== chrome.windows.WINDOW_ID_NONE) pushContext(false);
});

// The side panel keeps a Port open while it is shown; its presence is how we
// know the panel is open, and it is where status updates go.
chrome.runtime.onConnect.addListener((port) => {
  if (!port || port.name !== "sidepanel") return;
  panelPorts.add(port);
  try { port.postMessage({ type: "status", status: Object.assign({}, status) }); } catch (e) {}
  port.onMessage.addListener((m) => {
    if (m && m.type === "get_status") {
      try { port.postMessage({ type: "status", status: Object.assign({}, status) }); } catch (e) {}
    } else if (m && m.type === "reconnect") {
      reconnectNow();
    }
  });
  port.onDisconnect.addListener(() => {
    panelPorts.delete(port);
    lastContextKey = null;           // the next open re-announces the page
  });
  if (!ws || ws.readyState !== 1) connect();
  pushContext(true);
});

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg) return;
  if (msg.type === "selection") {
    // content.js: the user's selection changed in some tab.
    if (sender && sender.tab) {
      lastSelection = { tabId: sender.tab.id, text: String(msg.text || "") };
      if (sender.tab.active) pushContext(false);
    }
  } else if (msg.type === "get_status") {
    reply(Object.assign({}, status));
  } else if (msg.type === "reconnect") {
    reconnectNow();
  }
});

// ---------------------------------------------------------------------------
// Keepalive and lifecycle
// ---------------------------------------------------------------------------

// MV3 workers idle out after ~30s. WebSocket traffic resets that timer, so a
// cheap app-level ping doubles as keepalive and liveness check; the alarm also
// revives the worker if it was evicted while the socket was dead. (0.5 min is
// the documented minimum for packed extensions; unpacked ones accept 0.4.)
chrome.alarms.create("jarvis-keepalive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm && alarm.name !== "jarvis-keepalive") return;
  if (ws && ws.readyState === 1) {
    // Three unanswered pings = a half-open socket (laptop sleep, VPN flap).
    // Close it so onclose reconnects, instead of looking connected forever.
    if (pendingPings >= 3) {
      try { ws.close(); } catch (e) {}
      return;
    }
    pendingPings++;
    send({ type: "ping" });
  } else {
    connect();
  }
});

chrome.runtime.onStartup.addListener(() => connect());
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(DEFAULTS).then((cfg) => chrome.storage.local.set(cfg)).catch(() => {});
  connect();
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && (changes.coreUrl || changes.token)) reconnectNow();
});
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});

connect();
