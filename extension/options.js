const DEFAULTS = { coreUrl: "", token: "" };

const url = document.getElementById("url");
const token = document.getElementById("token");
const saved = document.getElementById("saved");
const result = document.getElementById("result");

function normaliseUrl(raw) {
  let s = String(raw || "").trim().replace(/\/+$/, "");
  if (s && !/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) s = "http://" + s;
  return s;
}

chrome.storage.local.get(DEFAULTS).then((cfg) => {
  url.value = cfg.coreUrl;
  token.value = cfg.token;
});

document.getElementById("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    coreUrl: normaliseUrl(url.value),
    token: token.value.trim(),
  });
  // The worker also watches storage.onChanged; the message covers the case
  // where it is asleep and has to be woken first.
  try { chrome.runtime.sendMessage({ type: "reconnect" }, () => void chrome.runtime.lastError); } catch (e) {}
  saved.hidden = false;
  setTimeout(() => (saved.hidden = true), 2200);
});

function show(cls, text) {
  result.hidden = false;
  result.className = cls;
  result.textContent = text;
}

document.getElementById("test").addEventListener("click", async () => {
  const base = normaliseUrl(url.value);
  if (!base) return show("bad", "Enter the core URL first.");
  let target;
  try {
    const u = new URL(base);
    u.pathname = u.pathname.replace(/\/+$/, "") + "/api/health";
    u.search = "";
    target = u.toString();
  } catch (e) {
    return show("bad", "That is not a valid URL: " + base);
  }
  show("", "Testing " + target + " …");
  const headers = token.value.trim() ? { Authorization: "Bearer " + token.value.trim() } : {};
  try {
    const r = await fetch(target, { headers: headers, cache: "no-store" });
    if (r.status === 401) return show("bad", "The core is reachable but rejected the token (401).");
    if (!r.ok) return show("bad", "The core answered HTTP " + r.status + " for /api/health.");
    let body = {};
    try { body = await r.json(); } catch (e) {}
    show("ok", "OK — core " + (body.version ? "version " + body.version : "is up") +
         ". Save, then open the side panel; the dot turns green when /ws connects.");
  } catch (e) {
    show("bad", "Cannot reach " + base + " — is the core running, and is this machine allowed to reach it?\n" +
         String((e && e.message) || e));
  }
});
