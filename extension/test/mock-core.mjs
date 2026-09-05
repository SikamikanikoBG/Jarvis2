#!/usr/bin/env node
// Mock Jarvis core for the browser extension — the WS leg of docs/API.md
// ("Browser extension") and /api/health, nothing else.
//
//   ext → core  browser.hello   {agent, version, tools}
//   core → ext  browser.call    {call_id, name, arguments}
//   ext → core  browser.result  {call_id, kind, text, error?}
//   ext → core  browser.context {url, title, selection?}
//   both        ping / pong
//
// Auth mirrors the real core: a wrong ?token= is refused at the handshake (the
// core closes before accept, which Starlette turns into an HTTP 403), and
// /api/health wants `Authorization: Bearer <token>` (401 otherwise).
//
// Library use (see transport.test.mjs):
//   const core = await startMockCore({ token: "t0k" });
//   const [hello, client] = await core.waitFor("hello");
//   const result = await core.call(client, "browser.tabs");
//
// Standalone, to smoke-test the real extension against something that talks the
// protocol:   node test/mock-core.mjs [port] [token]
// then point the extension at http://127.0.0.1:<port> with that token.

import http from "node:http";
import { EventEmitter } from "node:events";
import { fileURLToPath } from "node:url";
import { WebSocketServer } from "ws";

export const RESULT_KINDS = new Set(["data", "empty", "error"]);

export async function startMockCore({ port = 0, token = "t0k", host = "127.0.0.1" } = {}) {
  const events = new EventEmitter();
  const clients = new Set();
  const pending = new Map();           // call_id -> {resolve, reject, timer}
  let nextCall = 1;

  const server = http.createServer((req, res) => {
    const u = new URL(req.url, "http://x");
    if (u.pathname === "/api/health") {
      const auth = req.headers.authorization || "";
      if (token && auth !== "Bearer " + token) {
        res.writeHead(401, { "content-type": "application/json" });
        return res.end(JSON.stringify({ detail: "invalid or missing token" }));
      }
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({ ok: true, version: "2.0.0-mock" }));
    }
    res.writeHead(404);
    res.end();
  });

  const wss = new WebSocketServer({ noServer: true });
  server.on("upgrade", (req, socket, head) => {
    const u = new URL(req.url, "http://x");
    const refuse = (reason) => {
      events.emit("rejected", { reason, url: req.url });
      socket.write("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
      socket.destroy();
    };
    if (u.pathname !== "/ws") return refuse("path");
    if (token && u.searchParams.get("token") !== token) return refuse("token");
    if (u.searchParams.get("client") !== "browser") return refuse("client");
    wss.handleUpgrade(req, socket, head, (ws) => wss.emit("connection", ws, req));
  });

  wss.on("connection", (ws) => {
    const client = { ws, hello: null, tools: [], frames: [], contexts: [], pings: 0 };
    clients.add(client);
    events.emit("connection", client);
    ws.on("message", (raw) => {
      let m;
      try { m = JSON.parse(String(raw)); } catch (e) { return; }
      client.frames.push(m);
      events.emit("frame", m, client);
      switch (m.type) {
        case "browser.hello":
          client.hello = m;
          client.tools = Array.isArray(m.tools) ? m.tools : [];
          events.emit("hello", m, client);
          break;
        case "browser.result": {
          const p = pending.get(m.call_id);
          if (p) {
            clearTimeout(p.timer);
            pending.delete(m.call_id);
            p.resolve(m);
          }
          events.emit("result", m, client);
          break;
        }
        case "browser.context":
          client.contexts.push(m);
          events.emit("context", m, client);
          break;
        case "ping":
          client.pings++;
          ws.send(JSON.stringify({ type: "pong", ts: new Date().toISOString() }));
          events.emit("ping", m, client);
          break;
        case "pong":
          events.emit("pong", m, client);
          break;
        default:
          events.emit("unknown", m, client);
      }
    });
    ws.on("close", () => {
      clients.delete(client);
      events.emit("close", client);
    });
  });

  await new Promise((resolve) => server.listen(port, host, resolve));
  const bound = server.address().port;

  const api = {
    port: bound,
    url: `http://${host}:${bound}`,
    wsUrl: `ws://${host}:${bound}/ws`,
    token,
    clients,
    events,

    /** Send browser.call and resolve with the browser.result frame. */
    call(client, name, args = {}, { timeout = 5000 } = {}) {
      const call_id = "call_" + (nextCall++);
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(call_id);
          reject(new Error(`no browser.result for ${name} (${call_id}) within ${timeout}ms`));
        }, timeout);
        pending.set(call_id, { resolve, reject, timer });
        client.ws.send(JSON.stringify({ type: "browser.call", call_id, name, arguments: args }));
      });
    },

    /** Resolve with the emitted arguments of the next matching event. */
    waitFor(event, pred = () => true, timeout = 5000) {
      return new Promise((resolve, reject) => {
        const t = setTimeout(() => {
          events.off(event, handler);
          reject(new Error("timeout waiting for " + event));
        }, timeout);
        const handler = (...a) => {
          if (!pred(...a)) return;
          clearTimeout(t);
          events.off(event, handler);
          resolve(a);
        };
        events.on(event, handler);
      });
    },

    ping(client) { client.ws.send(JSON.stringify({ type: "ping" })); },
    raw(client, frame) { client.ws.send(JSON.stringify(frame)); },
    drop(client) { client.ws.close(1012, "service restart"); },

    close() {
      return new Promise((resolve) => {
        for (const p of pending.values()) { clearTimeout(p.timer); }
        pending.clear();
        for (const c of clients) { try { c.ws.terminate(); } catch (e) {} }
        wss.close();
        server.close(() => resolve());
      });
    },
  };
  return api;
}

// ---- standalone smoke server --------------------------------------------

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];
if (isMain) {
  const port = Number(process.argv[2] || 9021);
  const token = process.argv[3] || process.env.JARVIS_TOKEN || "dev";
  const core = await startMockCore({ port, token });
  console.log(`mock core on ${core.url}  (token: ${token})`);
  console.log(`extension settings → Core URL: ${core.url}   Token: ${token}`);
  core.events.on("rejected", (r) => console.log("refused a connection:", r.reason, r.url));
  core.events.on("hello", async (hello, client) => {
    console.log(`hello from ${hello.agent} ${hello.version}: ${client.tools.map((t) => t.name).join(", ")}`);
    for (const [name, args] of [["browser.tabs", {}], ["browser.read", { mode: "outline" }], ["browser.read", { mode: "text" }]]) {
      try {
        const r = await core.call(client, name, args, { timeout: 40000 });
        const ok = RESULT_KINDS.has(r.kind) ? "ok" : "BAD KIND";
        console.log(`\n== ${name} ${JSON.stringify(args)} → kind=${r.kind} (${ok})\n${String(r.text).slice(0, 1200)}`);
      } catch (e) {
        console.log(`\n== ${name} → ${e.message}`);
      }
    }
  });
  core.events.on("context", (m) => console.log("context:", m.url, "|", m.title, m.selection ? `| sel: ${m.selection.slice(0, 60)}` : ""));
  core.events.on("close", () => console.log("extension disconnected"));
}
