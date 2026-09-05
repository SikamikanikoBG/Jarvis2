// A fake `chrome.*` surface for running background.js under Node.
//
// It covers exactly what background.js touches: storage (local/session),
// runtime events and manifest, alarms, tabs, windows, sidePanel and
// scripting.executeScript. The last one is the interesting part: it does what
// Chrome does — serialises the injected function with toString() and re-parses
// it inside a jsdom page for the target tab — so the transport test exercises
// the real kernel end to end, including the "source Chrome cannot re-parse"
// failure class.

import vm from "node:vm";
import { page as makePage } from "./dom.mjs";

export function fakeEvent() {
  const listeners = new Set();
  return {
    addListener: (f) => listeners.add(f),
    removeListener: (f) => listeners.delete(f),
    hasListener: (f) => listeners.has(f),
    hasListeners: () => listeners.size > 0,
    _fire: (...args) => { for (const f of [...listeners]) f(...args); },
  };
}

function storageArea(obj) {
  return {
    async get(keys) {
      if (keys === undefined || keys === null) return { ...obj };
      if (typeof keys === "string") return keys in obj ? { [keys]: obj[keys] } : {};
      if (Array.isArray(keys)) {
        const out = {};
        for (const k of keys) if (k in obj) out[k] = obj[k];
        return out;
      }
      const out = { ...keys };
      for (const k of Object.keys(keys)) if (k in obj) out[k] = obj[k];
      return out;
    },
    async set(values) { Object.assign(obj, values); },
    async remove(keys) { for (const k of [].concat(keys)) delete obj[k]; },
  };
}

function titleFrom(url) {
  try { return new URL(url).pathname.split("/").filter(Boolean).pop() || new URL(url).host; } catch (e) { return url; }
}

/**
 * @param {object} o
 * @param {object} o.storage   initial chrome.storage.local contents ({coreUrl, token})
 * @param {Array}  o.tabs      [{id, url, title, active?, windowId?}]
 * @param {object} o.fixtures  url -> html string | {html, title}
 */
export function makeChrome({ storage = {}, tabs = [], fixtures = {}, manifest = { version: "2.0.0-test" } } = {}) {
  const local = { ...storage };
  const session = {};
  const pages = new Map();                 // tabId -> {url, ctx, dom, win, doc}
  const hang = new Set();                  // tab ids whose injections never settle
  const calls = { executeScript: [], captures: 0, alarms: [], updates: [] };
  const tabList = tabs.map((t) => ({ windowId: 1, active: false, ...t }));
  let nextTabId = Math.max(0, ...tabList.map((t) => t.id)) + 1;

  const fixtureFor = (url) => {
    const fx = fixtures[url];
    if (fx === undefined) return { html: "<p>fixture missing for " + url + "</p>", title: titleFrom(url) };
    return typeof fx === "string" ? { html: fx, title: titleFrom(url) } : fx;
  };

  function pageFor(tab) {
    const cur = pages.get(tab.id);
    if (cur && cur.url === tab.url) return cur;
    if (cur) { try { cur.win.close(); } catch (e) {} }
    const fx = fixtureFor(tab.url);
    const p = makePage(fx.html, { url: tab.url, title: fx.title });
    const entry = { ...p, url: tab.url };
    pages.set(tab.id, entry);
    return entry;
  }

  const setActive = (t) => { for (const o of tabList) if (o.windowId === t.windowId) o.active = false; t.active = true; };
  const find = (id) => {
    const t = tabList.find((x) => x.id === id);
    if (!t) throw new Error("No tab with id: " + id);
    return t;
  };

  const chrome = {
    runtime: {
      getManifest: () => manifest,
      onMessage: fakeEvent(),
      onConnect: fakeEvent(),
      onInstalled: fakeEvent(),
      onStartup: fakeEvent(),
      lastError: undefined,
    },
    storage: { local: storageArea(local), session: storageArea(session), onChanged: fakeEvent() },
    alarms: {
      create: (name, info) => calls.alarms.push({ name, info }),
      onAlarm: fakeEvent(),
    },
    tabs: {
      onActivated: fakeEvent(),
      onUpdated: fakeEvent(),
      onRemoved: fakeEvent(),
      async query(q = {}) {
        return tabList
          .filter((t) => q.active === undefined || t.active === q.active)
          .map((t) => ({ ...t }));
      },
      async get(id) { return { ...find(id) }; },
      async update(id, props) {
        const t = find(id);
        calls.updates.push({ id, props });
        if (props.active) setActive(t);
        if (props.url !== undefined) {
          t.url = props.url;
          t.title = fixtureFor(props.url).title;
          setTimeout(() => chrome.tabs.onUpdated._fire(t.id, { status: "loading", url: t.url }, { ...t }), 1);
          setTimeout(() => chrome.tabs.onUpdated._fire(t.id, { status: "complete" }, { ...t }), 6);
        }
        return { ...t };
      },
      async create(props) {
        const t = { id: nextTabId++, windowId: 1, active: false, url: props.url, title: fixtureFor(props.url).title };
        tabList.push(t);
        if (props.active !== false) setActive(t);
        setTimeout(() => chrome.tabs.onUpdated._fire(t.id, { status: "complete" }, { ...t }), 6);
        return { ...t };
      },
      async remove(id) {
        const i = tabList.findIndex((t) => t.id === id);
        if (i >= 0) tabList.splice(i, 1);
        chrome.tabs.onRemoved._fire(id, {});
      },
      async captureVisibleTab() {
        calls.captures++;
        return "data:image/jpeg;base64," + Buffer.from("not really a jpeg, but long enough to look like one").toString("base64");
      },
    },
    windows: { WINDOW_ID_NONE: -1, onFocusChanged: fakeEvent(), async update() { return {}; } },
    scripting: {
      async executeScript({ target, func, args }) {
        calls.executeScript.push({ target, args });
        const tab = find(target.tabId);
        if (hang.has(tab.id)) return new Promise(() => {});          // never settles
        if (/^(chrome|brave|about|edge):/i.test(tab.url)) throw new Error("Cannot access a chrome:// URL");
        const p = pageFor(tab);
        const fn = vm.runInContext("(" + func.toString() + ")", p.ctx, { filename: "injected.js" });
        const result = await fn(...(args || []));
        const frames = [{ frameId: 0, result: result === undefined ? null : structuredClone(result) }];
        return frames;
      },
    },
    sidePanel: { async setPanelBehavior() {} },

    // test hooks
    _tabs: tabList,
    _pages: pages,
    _pageFor: (id) => pageFor(find(id)),
    _hang: hang,
    _calls: calls,
    _local: local,
    _session: session,
    _closeAll() { for (const p of pages.values()) { try { p.win.close(); } catch (e) {} } pages.clear(); },
  };
  return chrome;
}

/** A fake side-panel Port for chrome.runtime.onConnect. */
export function fakePort(name = "sidepanel") {
  const received = [];
  return {
    name,
    received,
    postMessage: (m) => received.push(m),
    onMessage: fakeEvent(),
    onDisconnect: fakeEvent(),
    disconnect() { this.onDisconnect._fire(); },
  };
}
