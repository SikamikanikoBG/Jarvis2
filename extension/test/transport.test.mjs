// background.js against a mock core, end to end.
//
// The real service-worker source is loaded into a vm context with a fake
// `chrome` (test/fake-chrome.mjs) and the `ws` WebSocket, and pointed at
// test/mock-core.mjs. The mock drives browser.call and asserts browser.result;
// scripting.executeScript runs the real kernel through toString() in a jsdom
// page, exactly as Chrome injects it.

import { describe, test, before, after } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { WebSocket } from "ws";
import { startMockCore, RESULT_KINDS } from "./mock-core.mjs";
import { makeChrome, fakePort } from "./fake-chrome.mjs";
import { EXT_DIR } from "./dom.mjs";

const BACKGROUND_SRC = fs.readFileSync(path.join(EXT_DIR, "background.js"), "utf8");
const MANIFEST = JSON.parse(fs.readFileSync(path.join(EXT_DIR, "manifest.json"), "utf8"));

const TIMEOUTS = { call: 400, inject: 250, load: 400, settle: 60, retryMin: 40, retryMax: 160, afterAction: 0, contextDebounce: 5 };

const API_TOOLS = ["browser.tabs", "browser.open", "browser.read", "browser.find",
                   "browser.click", "browser.type", "browser.scroll", "browser.screenshot"];

const ARTICLE = "https://example.test/article";
const SECOND = "https://example.test/second";
const FIXTURES = {
  [ARTICLE]: {
    title: "Monitoring guide",
    html: `<h1>Search results</h1>
      <article><a href="/a/1" id="first">Monitoring with Prometheus</a>
        <p>Setting up a homelab monitoring stack for real, with exporters, dashboards and alerts that
           actually page you. This paragraph exists so the page has more than two hundred characters of
           readable text and the settle probe sees real content.</p></article>
      <article><a href="/a/2">Docker container monitoring</a><p>Comparing eight tools.</p></article>
      <textarea id="box" aria-label="Write a comment"></textarea><button id="post">Comment</button>`,
  },
  [SECOND]: {
    title: "Second page",
    html: `<h1>Second</h1><p>${"Landed here after browser.open. ".repeat(12)}</p>`,
  },
};

const workers = [];

/** Load background.js like a service worker would: globals + importScripts. */
function bootWorker({ chrome, timeouts = TIMEOUTS }) {
  const timers = new Set();
  let dead = false;
  const sandbox = {
    chrome, WebSocket, console, URL, URLSearchParams, TextEncoder, TextDecoder, atob, btoa,
    fetch: globalThis.fetch,
    setTimeout: (fn, ms, ...a) => {
      if (dead) return 0;
      const h = setTimeout(() => { timers.delete(h); fn(...a); }, ms);
      timers.add(h);
      return h;
    },
    clearTimeout: (h) => { timers.delete(h); clearTimeout(h); },
    setInterval, clearInterval,
    __JARVIS_TIMEOUTS: timeouts,
  };
  sandbox.self = sandbox;
  const ctx = vm.createContext(sandbox);
  sandbox.importScripts = (...files) => {
    for (const f of files) vm.runInContext(fs.readFileSync(path.join(EXT_DIR, f), "utf8"), ctx, { filename: f });
  };
  vm.runInContext(BACKGROUND_SRC, ctx, { filename: "background.js" });
  const worker = {
    ctx, chrome,
    status() {
      let out = null;
      chrome.runtime.onMessage._fire({ type: "get_status" }, {}, (s) => { out = s; });
      return out;
    },
    shutdown() { dead = true; for (const h of timers) clearTimeout(h); timers.clear(); chrome._closeAll(); },
  };
  workers.push(worker);
  return worker;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// One suite so the before/after hooks are ordered against the tests (top-level
// hooks are not reliably awaited by Node 20's runner).
describe("background.js against the mock core", () => {

let core, chrome, worker, client, hello;

before(async () => {
  core = await startMockCore({ token: "t0k" });
  chrome = makeChrome({
    storage: { coreUrl: core.url, token: "t0k" },
    tabs: [
      { id: 7, url: ARTICLE, title: "Monitoring guide", active: true },
      { id: 8, url: "chrome://extensions/", title: "Extensions" },
    ],
    fixtures: FIXTURES,
  });
  worker = bootWorker({ chrome });
  [hello, client] = await core.waitFor("hello");
});

after(async () => {
  for (const w of workers) w.shutdown();
  await core.close();
});

// ── registration ─────────────────────────────────────────────────────────

test("browser.hello announces agent, version and exactly the tools in API.md", () => {
  assert.equal(hello.type, "browser.hello");
  assert.equal(hello.agent, "jarvis-extension");
  assert.equal(hello.version, "2.0.0-test");
  assert.deepEqual(hello.tools.map((t) => t.name), API_TOOLS);
  assert.equal(MANIFEST.version, "2.0.0");
});

test("every ToolSpec has a closed JSON Schema, a description and the agreed read_only flags", () => {
  const readOnly = new Set(["browser.tabs", "browser.read", "browser.find", "browser.screenshot"]);
  for (const t of hello.tools) {
    assert.equal(t.input_schema.type, "object", t.name);
    assert.equal(t.input_schema.additionalProperties, false, t.name);
    assert.ok(t.description.length > 40, t.name);
    assert.equal(t.read_only, readOnly.has(t.name), t.name);
    assert.equal(t.destructive, false, t.name);
    assert.equal(typeof t.idempotent, "boolean", t.name);
    for (const req of t.input_schema.required || []) assert.ok(req in t.input_schema.properties, `${t.name}.${req}`);
  }
  const read = hello.tools.find((t) => t.name === "browser.read");
  assert.deepEqual(read.input_schema.properties.mode.enum, ["text", "outline"]);
  assert.deepEqual(hello.tools.find((t) => t.name === "browser.type").input_schema.required, ["ref", "text"]);
  assert.deepEqual(hello.tools.find((t) => t.name === "browser.open").input_schema.required, ["url"]);
});

test("the worker registers a keepalive alarm and answers status queries", () => {
  assert.ok(chrome._calls.alarms.some((a) => a.name === "jarvis-keepalive"));
  const st = worker.status();
  assert.equal(st.connected, true);
  assert.equal(st.coreUrl, core.url);
});

// ── calls ────────────────────────────────────────────────────────────────

test("browser.tabs → data listing every tab, marking the active one and browser pages", async () => {
  const r = await core.call(client, "browser.tabs");
  assert.equal(r.type, "browser.result");
  assert.ok(RESULT_KINDS.has(r.kind), r.kind);
  assert.equal(r.kind, "data");
  assert.match(r.text, /^2 tabs:/);
  assert.match(r.text, /\[tab 7\] Monitoring guide — https:\/\/example\.test\/article {2}\(active\)/);
  assert.match(r.text, /\[tab 8\] Extensions — chrome:\/\/extensions\/ {2}\(browser page — cannot be read\)/);
});

test("browser.read mode=text → the kernel, re-parsed like Chrome does, reads the active tab", async () => {
  const r = await core.call(client, "browser.read", { mode: "text" });
  assert.equal(r.kind, "data", r.text);
  assert.match(r.text, /^\[tab 7\] Monitoring guide — https:\/\/example\.test\/article\n/);
  assert.match(r.text, /Search results\nMonitoring with Prometheus/);
  assert.match(r.text, /\[\d+ interactive elements on this page/);
  assert.equal(chrome._calls.executeScript.at(-1).args[0], "read");
});

test("browser.read mode=outline → refs the model can act on", async () => {
  const r = await core.call(client, "browser.read", { mode: "outline" });
  assert.equal(r.kind, "data", r.text);
  assert.match(r.text, /@e\d+ h1 «Search results»/);
  assert.match(r.text, /@e\d+ link «Monitoring with Prometheus» → https:\/\/example\.test\/a\/1/);
  assert.match(r.text, /@e\d+ textarea «Write a comment» \(empty\)/);
  assert.match(r.text, /@e\d+ button «Comment»/);
});

test("browser.read of a browser-internal tab → error naming the reason", async () => {
  const r = await core.call(client, "browser.read", { tab: 8 });
  assert.equal(r.kind, "error");
  assert.match(r.error, /browser-internal page/);
  // pickTab brought it to the front; put the article back for the next tests
  await chrome.tabs.update(7, { active: true });
});

test("browser.read of an unknown tab id → error pointing at browser.tabs", async () => {
  const r = await core.call(client, "browser.read", { tab: 999 });
  assert.equal(r.kind, "error");
  assert.match(r.error, /no tab with id 999 — call browser\.tabs/);
});

test("browser.find → snippets and the matching element by ref", async () => {
  const r = await core.call(client, "browser.find", { query: "Docker container" });
  assert.equal(r.kind, "data", r.text);
  assert.match(r.text, /1 place in the text mention "Docker container"/);
  assert.match(r.text, /@e\d+ a «Docker container monitoring» → https:\/\/example\.test\/a\/2/);
  const miss = await core.call(client, "browser.find", { query: "no such phrase anywhere" });
  assert.equal(miss.kind, "empty");
});

test("browser.click by ref from the outline → the page's own handler runs", async () => {
  const outline = await core.call(client, "browser.read", { mode: "outline" });
  const ref = outline.text.split("\n").find((l) => l.includes("Monitoring with Prometheus")).split(" ")[0];
  const pg = chrome._pageFor(7);
  pg.win.__clicked = null;
  pg.$("#first").addEventListener("click", (e) => { e.preventDefault(); pg.win.__clicked = "first"; });
  const r = await core.call(client, "browser.click", { ref });
  assert.equal(r.kind, "data", r.text);
  assert.equal(pg.win.__clicked, "first");
  assert.match(r.text, /^\[tab 7\] .*\nClicked @e\d+ a#first «Monitoring with Prometheus»\./);
});

test("browser.click with a bad ref → error with recovery advice, not silence", async () => {
  const r = await core.call(client, "browser.click", { ref: "@e424242" });
  assert.equal(r.kind, "error");
  assert.match(r.text, /^Error: Ref @e424242 no longer exists/);
  assert.match(r.error, /browser\.read mode=outline or browser\.find/);
});

test("browser.type → text lands and the nearby button is reported", async () => {
  const r = await core.call(client, "browser.type", { ref: "Write a comment", text: "Great write-up" });
  assert.equal(r.kind, "data", r.text);
  assert.equal(chrome._pageFor(7).$("#box").value, "Great write-up");
  assert.match(r.text, /Typed 14 characters into @e\d+ textarea#box «Write a comment»\./);
  assert.match(r.text, /Buttons next to the field: @e\d+ «Comment»/);
});

test("browser.scroll → position report; by ref → scrolled into view", async () => {
  const down = await core.call(client, "browser.scroll", { direction: "down" });
  assert.equal(down.kind, "data", down.text);
  assert.match(down.text, /Scrolled down/);
  const outline = await core.call(client, "browser.read", { mode: "outline" });
  const ref = outline.text.split("\n").find((l) => l.includes("«Comment»")).split(" ")[0];
  const to = await core.call(client, "browser.scroll", { ref });
  assert.equal(to.kind, "data", to.text);
  assert.match(to.text, /Scrolled @e\d+ button#post «Comment» into view/);
});

test("browser.screenshot → data with the image attached beside the text", async () => {
  const r = await core.call(client, "browser.screenshot");
  assert.equal(r.kind, "data", r.text);
  assert.match(r.text, /Screenshot captured \(image\/jpeg/);
  assert.equal(r.image.mime, "image/jpeg");
  assert.ok(r.image.base64.length > 10);
  assert.equal(chrome._calls.captures, 1);
});

test("browser.open → navigates Jarvis's own work tab (created once), waits for load, reports landing", async () => {
  const before = chrome._tabs.length;
  const r = await core.call(client, "browser.open", { url: SECOND });
  assert.equal(r.kind, "data", r.text);
  assert.equal(chrome._tabs.length, before + 1, "a work tab was created");
  const work = chrome._tabs.at(-1);
  assert.equal(work.url, SECOND);
  assert.equal(work.active, true);
  assert.match(r.text, new RegExp(`^Opened Second page — ${SECOND} in a new Jarvis work tab \\[tab ${work.id}\\]\\.`));
  assert.equal(chrome._session.workTabId, work.id);

  // A second open reuses the same tab; the user's tab 7 is never touched.
  const again = await core.call(client, "browser.open", { url: ARTICLE });
  assert.equal(again.kind, "data", again.text);
  assert.equal(chrome._tabs.length, before + 1);
  assert.match(again.text, /in the Jarvis work tab/);
  assert.equal(chrome._tabs.find((t) => t.id === 7).url, ARTICLE);

  // Reading now hits the active tab — which is the work tab.
  const read = await core.call(client, "browser.read", { mode: "text" });
  assert.match(read.text, new RegExp(`^\\[tab ${work.id}\\]`));
  await chrome.tabs.update(7, { active: true });
});

test("browser.open without a scheme gets https, and a non-http scheme is refused", async () => {
  const r = await core.call(client, "browser.open", { url: "example.test/second" });
  assert.equal(r.kind, "data", r.text);
  assert.match(r.text, /https:\/\/example\.test\/second/);
  const bad = await core.call(client, "browser.open", { url: "javascript:alert(1)" });
  assert.equal(bad.kind, "error");
  assert.match(bad.error, /only http\(s\) URLs/);
  await chrome.tabs.update(7, { active: true });
});

test("the Jarvis app's own tab is protected: never read, never the target", async () => {
  const appTab = await chrome.tabs.create({ url: core.url + "/?mode=panel", active: true });
  const tabs = await core.call(client, "browser.tabs");
  assert.match(tabs.text, new RegExp(`\\[tab ${appTab.id}\\] .*the Jarvis app itself`));
  // Active tab is the app → falls back to the work tab, which exists from the open test.
  const r = await core.call(client, "browser.read", { mode: "text" });
  assert.equal(r.kind, "data", r.text);
  assert.doesNotMatch(r.text, new RegExp(`^\\[tab ${appTab.id}\\]`));
  const explicit = await core.call(client, "browser.read", { tab: appTab.id });
  assert.equal(explicit.kind, "error");
  assert.match(explicit.error, /Jarvis app itself/);
  await chrome.tabs.remove(appTab.id);
  await chrome.tabs.update(7, { active: true });
});

test("an unknown tool name → error result, and the call is still answered", async () => {
  const r = await core.call(client, "browser.extract", { selector: "tr" });
  assert.equal(r.kind, "error");
  assert.match(r.error, /unknown tool "browser\.extract"; this extension provides: browser\.tabs, /);
});

test("a call whose injection never settles → error within the call budget (never unanswered)", async () => {
  chrome._hang.add(7);
  const t0 = Date.now();
  const r = await core.call(client, "browser.read", { mode: "text" }, { timeout: 3000 });
  chrome._hang.delete(7);
  assert.equal(r.kind, "error");
  assert.match(r.error, /did not complete within/);
  assert.ok(Date.now() - t0 < 2000, "answered by the extension's own timeout, not the mock's");
});

test("a call with no call_id is ignored; malformed frames do not kill the socket", async () => {
  core.raw(client, { type: "browser.call", name: "browser.tabs" });
  client.ws.send("this is not json");
  core.raw(client, { type: "run.queued", run_id: "r1", conversation_id: "c1" });
  await sleep(30);
  const r = await core.call(client, "browser.tabs");
  assert.equal(r.kind, "data");
});

// ── keepalive ────────────────────────────────────────────────────────────

test("a server ping is answered with pong", async () => {
  const p = core.waitFor("pong");
  core.ping(client);
  const [pong] = await p;
  assert.equal(pong.type, "pong");
});

test("the keepalive alarm sends ping and the core's pong is consumed", async () => {
  const p = core.waitFor("ping");
  chrome.alarms.onAlarm._fire({ name: "jarvis-keepalive" });
  const [ping] = await p;
  assert.equal(ping.type, "ping");
});

// ── page context ─────────────────────────────────────────────────────────

test("browser.context is pushed only while the side panel is open, on tab change, deduplicated", async () => {
  const seen = [];
  core.events.on("context", (m) => seen.push(m));

  // Panel closed: a tab change produces nothing.
  chrome.tabs.onActivated._fire({ tabId: 7, windowId: 1 });
  await sleep(40);
  assert.equal(seen.length, 0);

  // Panel opens (a Port connects): the current page is announced at once.
  const port = fakePort();
  chrome.runtime.onConnect._fire(port);
  await core.waitFor("context", (m) => m.url === ARTICLE);
  assert.equal(seen.at(-1).title, "Monitoring guide");
  assert.equal(seen.at(-1).tab_id, 7);
  assert.ok(port.received.some((m) => m.type === "status" && m.status.connected === true));

  // Same page again: deduplicated.
  const n = seen.length;
  chrome.tabs.onActivated._fire({ tabId: 7, windowId: 1 });
  await sleep(40);
  assert.equal(seen.length, n);

  // A selection in the active tab rides along.
  chrome.runtime.onMessage._fire({ type: "selection", text: "exporters, dashboards" }, { tab: { id: 7, active: true } }, () => {});
  await core.waitFor("context", (m) => m.selection === "exporters, dashboards");

  // The user switches tabs.
  await chrome.tabs.update(8, { active: true });
  chrome.tabs.onActivated._fire({ tabId: 8, windowId: 1 });
  await core.waitFor("context", (m) => m.url === "chrome://extensions/");

  // Panel closes: silence again.
  port.disconnect();
  const m = seen.length;
  await chrome.tabs.update(7, { active: true });
  chrome.tabs.onActivated._fire({ tabId: 7, windowId: 1 });
  await sleep(40);
  assert.equal(seen.length, m);
});

// ── reconnect and auth ───────────────────────────────────────────────────

test("a dropped connection is re-established with backoff and the tools re-announced", async () => {
  const p = core.waitFor("hello");
  core.drop(client);
  const [hello2, client2] = await p;
  assert.deepEqual(hello2.tools.map((t) => t.name), API_TOOLS);
  client = client2;
  const r = await core.call(client, "browser.tabs");
  assert.equal(r.kind, "data");
  assert.equal(worker.status().connected, true);
});

test("Save in options (storage change + reconnect message in one tick) yields exactly one new connection", async () => {
  const fresh = [];
  const onHello = (h, c) => fresh.push(c);
  core.events.on("hello", onHello);
  chrome.storage.onChanged._fire({ token: { oldValue: "t0k", newValue: "t0k" } }, "local");
  chrome.runtime.onMessage._fire({ type: "reconnect" }, {}, () => {});
  await sleep(300);
  core.events.off("hello", onHello);
  assert.equal(fresh.length, 1, "exactly one hello after a double reconnect");
  assert.equal(core.clients.size, 1, "exactly one live socket — the old one was closed, not orphaned");
  client = fresh[0];
  assert.equal((await core.call(client, "browser.tabs")).kind, "data");
  assert.equal(worker.status().connected, true);
});

test("a rejected token is diagnosed through /api/health and shown in the status", async () => {
  const bad = makeChrome({ storage: { coreUrl: core.url, token: "wrong" }, tabs: [], fixtures: {} });
  const w = bootWorker({ chrome: bad, timeouts: { ...TIMEOUTS, retryMin: 100, retryMax: 100 } });
  await core.waitFor("rejected", (r) => r.reason === "token");
  let st = null;
  for (let i = 0; i < 40; i++) {
    st = w.status();
    if (st && /rejected the token/.test(st.error)) break;
    await sleep(25);
  }
  assert.equal(st.connected, false);
  assert.match(st.error, /rejected the token \(401\)/);
  w.shutdown();
});

test("an unconfigured extension says so and does not connect", async () => {
  const none = makeChrome({ storage: {}, tabs: [], fixtures: {} });
  const w = bootWorker({ chrome: none });
  await sleep(20);
  const st = w.status();
  assert.equal(st.connected, false);
  assert.match(st.error, /not configured/);
  w.shutdown();
});

}); // describe
