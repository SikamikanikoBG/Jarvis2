// Side panel shell: a one-line status bar + the real Jarvis SPA in an iframe.
//
// The chat is NOT reimplemented here — it is the V2 web app served by the core,
// loaded with ?mode=panel so it renders panel-only. The token rides in the URL
// on every load: a chrome-extension:// page framing the core is cross-site, so
// no cookie of the core's ever reaches the frame (V1 lesson).
//
// While this page is open it keeps a Port to the service worker. That Port is
// how the worker knows the panel is open (browser.context is only pushed then)
// and where connection status arrives.

const DEFAULTS = { coreUrl: "", token: "" };

const dot = document.getElementById("dot");
const state = document.getElementById("state");
const urlEl = document.getElementById("url");
const frame = document.getElementById("frame");
const setup = document.getElementById("setup");
const err = document.getElementById("err");

function panelUrl(cfg) {
  const u = new URL(cfg.coreUrl);
  u.pathname = u.pathname.replace(/\/+$/, "") + "/";
  u.search = "";
  u.hash = "";
  u.searchParams.set("mode", "panel");
  if (cfg.token) u.searchParams.set("token", cfg.token);
  return u.toString();
}

function shortHost(coreUrl) {
  try {
    return new URL(coreUrl).host;
  } catch (e) {
    return coreUrl;
  }
}

async function init() {
  const cfg = await chrome.storage.local.get(DEFAULTS);
  if (!cfg.coreUrl) {
    setup.hidden = false;
    frame.hidden = true;
    paint({ connected: false, error: "not configured", coreUrl: "" });
    return;
  }
  setup.hidden = true;
  try {
    const src = panelUrl(cfg);
    if (frame.src !== src) frame.src = src;
    frame.hidden = false;
  } catch (e) {
    err.hidden = false;
    err.textContent = "Bad core URL: " + cfg.coreUrl;
  }
  paint({ connected: false, error: "", coreUrl: cfg.coreUrl });
}

function paint(st) {
  const on = !!(st && st.connected);
  const configured = !!(st && st.coreUrl);
  dot.classList.toggle("on", on);
  dot.classList.toggle("wait", !on && configured);
  state.textContent = on ? "Connected" : (configured ? "Reconnecting…" : "Not configured");
  urlEl.textContent = configured ? shortHost(st.coreUrl) : "";
  const bad = !on && st && st.error && st.error !== "not configured";
  err.hidden = !bad;
  err.textContent = bad ? st.error : "";
  document.getElementById("bar").title = on
    ? "Connected to the Jarvis core at " + st.coreUrl
    : (st && st.error) || "Not connected";
}

// ── the presence/status port ──────────────────────────────────────────────

let port = null;

function connectPort() {
  try {
    port = chrome.runtime.connect({ name: "sidepanel" });
  } catch (e) {
    setTimeout(connectPort, 1000);
    return;
  }
  port.onMessage.addListener((m) => {
    if (m && m.type === "status") paint(m.status);
  });
  port.onDisconnect.addListener(() => {
    // The MV3 worker was restarted (or reloaded): reattach so presence and
    // status keep flowing.
    port = null;
    void chrome.runtime.lastError;
    setTimeout(connectPort, 500);
  });
}

document.getElementById("gear").addEventListener("click", () => chrome.runtime.openOptionsPage());
document.getElementById("openOpts").addEventListener("click", () => chrome.runtime.openOptionsPage());

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && (changes.coreUrl || changes.token)) init();
});

init();
connectPort();
