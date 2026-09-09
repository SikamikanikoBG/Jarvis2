/**
 * Mock core for UI development: implements the REST + WS contract the SPA expects
 * (see README → Backend contract) with a scripted "model" so every screen can be
 * exercised without Ollama or the real core.  `npm run mock` then `npm run dev`.
 *
 * Prompts that change the script: "tools" (tool call + result), "confirm" (approval
 * card), "fail", "judge" (guard + judge stop), "long" (slow, many tokens).
 */
import { createServer } from 'node:http';
import { WebSocketServer } from 'ws';
import { createFeatures } from './features.mjs';

const PORT = Number(process.env.PORT ?? 9021);
const VERSION = '2.0.0-alpha.1+mock';
const now = () => new Date().toISOString();
let idSeq = 100;
const newId = (p) => `${p}_${Date.now().toString(16)}_${(idSeq++).toString(16)}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- state -------------------------------------------------------------------------------

const conversations = new Map();
const messages = new Map(); // conv id → Message[]
const runs = new Map();
const chatFolders = new Map(); // Arsen's own chat folders, id → folder
const runEvents = new Map(); // run id → events
const cancelFlags = new Map();
const confirmWaiters = new Map(); // run id → resolve(approved)

/** A window of `text` around the first match, the way jarvis_core.db.store._snippet does it. */
function snippetAround(text, q, width = 70) {
  const flat = text.split(/\s+/).join(' ');
  const at = flat.toLowerCase().indexOf(q.toLowerCase());
  if (at < 0) return flat.slice(0, width) + (flat.length > width ? '…' : '');
  const start = Math.max(0, at - Math.floor(width / 2));
  const end = Math.min(flat.length, at + q.length + Math.floor(width / 2));
  return (start > 0 ? '…' : '') + flat.slice(start, end) + (end < flat.length ? '…' : '');
}

function folder(name, position) {
  const f = { id: newId('cfld'), name, position, conversation_count: 0, unread_count: 0, created_at: now(), updated_at: now() };
  chatFolders.set(f.id, f);
  return f;
}

/** Counts are derived, never stored: archived chats do not count towards a folder. */
function folderList() {
  return [...chatFolders.values()]
    .sort((a, b) => a.position - b.position || a.created_at.localeCompare(b.created_at))
    .map((f) => {
      const inside = [...conversations.values()].filter((c) => c.folder_id === f.id && !c.archived);
      return { ...f, conversation_count: inside.length, unread_count: inside.filter((c) => c.unread).length };
    });
}

function conv(partial) {
  const c = {
    id: newId('conv'),
    kind: 'chat',
    title: 'New chat',
    folder_key: null,
    folder_label: null,
    folder_id: null,
    archived: false,
    unread: false,
    preview: null,
    message_count: 0,
    activity: 'idle',
    created_at: now(),
    updated_at: now(),
    ...partial,
  };
  conversations.set(c.id, c);
  messages.set(c.id, []);
  return c;
}
function msg(convId, partial) {
  const m = {
    id: newId('msg'),
    conversation_id: convId,
    run_id: null,
    role: 'user',
    content: '',
    reasoning: null,
    tool_calls: [],
    tool_call_id: null,
    name: null,
    partial: false,
    created_at: now(),
    ...partial,
  };
  messages.get(convId).push(m);
  const c = conversations.get(convId);
  c.message_count += 1;
  // Injected user-role messages (context, plan, …) never become the sidebar preview.
  const injected = m.role === 'user' && m.name !== null;
  c.preview = m.role === 'tool' || injected ? c.preview : m.content.slice(0, 80) || c.preview;
  c.updated_at = m.created_at;
  return m;
}

const hoursAgo = (h) => new Date(Date.now() - h * 3600_000).toISOString();

const seed = conv({ title: 'Morning planning', updated_at: hoursAgo(2), created_at: hoursAgo(3) });
msg(seed.id, { role: 'user', content: 'What is on my plate today?', created_at: hoursAgo(3) });
msg(seed.id, {
  role: 'assistant',
  content: 'Three things stand out:\n\n1. **Steering committee** at 14:00 — the deck still lacks the Q3 numbers.\n2. Rumen is waiting on the `DM-1234` reply since yesterday.\n3. The Revolut radar flagged two transactions worth a look.\n\nWant me to draft the reply to Rumen first?',
  reasoning: 'The user asks for a summary of the day. Check the calendar, flagged mail, and the scheduled radar output. Keep it short and offer the next action.',
  created_at: hoursAgo(2.98),
});
const seedRun = {
  id: newId('run'),
  conversation_id: seed.id,
  kind: 'chat',
  status: 'done',
  input_text: 'What is on my plate today?',
  plan: null,
  budget: { max_steps: 25, max_tokens: 200000, max_seconds: 600 },
  priority: 0,
  steps_used: 1,
  usage: { prompt_tokens: 2412, completion_tokens: 188, calls: 1, ttft_ms: 640, duration_ms: 5100 },
  last_seq: 6,
  error: null,
  waiting_reason: null,
  think: null,
  think_level: null,
  created_at: hoursAgo(3),
  started_at: hoursAgo(3),
  finished_at: hoursAgo(2.98),
};
runs.set(seedRun.id, seedRun);
for (const m of messages.get(seed.id)) m.run_id = seedRun.id;
runEvents.set(seedRun.id, [
  { type: 'run.queued', ts: hoursAgo(3), run_id: seedRun.id, conversation_id: seed.id, seq: 1, kind: 'chat', input_preview: seedRun.input_text, user_message_id: messages.get(seed.id)[0].id },
  { type: 'run.started', ts: hoursAgo(3), run_id: seedRun.id, conversation_id: seed.id, seq: 2 },
  { type: 'model.call', ts: hoursAgo(3), run_id: seedRun.id, conversation_id: seed.id, seq: 3, role: 'chat', provider: 'vllm', model: 'qwen3.8-27b', message_count: 4, tool_count: 6, think: true, think_level: 'medium' },
  { type: 'model.done', ts: hoursAgo(2.98), run_id: seedRun.id, conversation_id: seed.id, seq: 4, usage: seedRun.usage, finish_reason: 'stop', tool_call_count: 0 },
  { type: 'run.done', ts: hoursAgo(2.98), run_id: seedRun.id, conversation_id: seed.id, seq: 5, message_id: messages.get(seed.id)[1].id, usage: seedRun.usage, steps_used: 1, summary: null },
]);

const workFolder = folder('Work', 0);
folder('Home', 1);
conv({ title: 'Deck numbers for Q3', folder_id: workFolder.id, updated_at: hoursAgo(26), created_at: hoursAgo(27), preview: 'Pulled the figures from the shared folder.' });
conv({ title: 'DM-1234 with Rumen', folder_id: workFolder.id, updated_at: hoursAgo(52), preview: 'Waiting on Finance.' });
conv({ kind: 'scheduled', title: '2026-09-05 07:00', folder_key: 'sch_morning', folder_label: 'Morning brief', updated_at: hoursAgo(4), preview: 'Two meetings, one flagged mail.' });
conv({ kind: 'scheduled', title: '2026-09-04 07:00', folder_key: 'sch_morning', folder_label: 'Morning brief', updated_at: hoursAgo(28), preview: 'Quiet day.' });
conv({ kind: 'scheduled', title: '2026-09-05 08:00', folder_key: 'sch_revolut', folder_label: 'Revolut radar', updated_at: hoursAgo(3), unread: true, preview: '2 transactions worth a look.' });
conv({ kind: 'triage', title: 'Postbank inbox', folder_key: '2026-09-05', updated_at: hoursAgo(1), preview: '14 mails filed, 2 flagged.' });
conv({ kind: 'collab', title: 'claude-code', folder_key: 'key_cc', folder_label: 'claude-code', updated_at: hoursAgo(50), preview: 'Status probe.' });
conv({ title: 'Old thread', archived: true, updated_at: hoursAgo(300), preview: 'Archived.' });
conv({ kind: 'archive', title: 'V1: Trip planning', folder_key: 'v1', folder_label: 'Imported from V1', updated_at: hoursAgo(900), preview: 'Read-only import.' });

let settings = {
  assistant_name: 'Jarvis',
  user_name: 'Arsen',
  timezone: 'Europe/Sofia',
  language_hint: 'Reply in the language the user wrote in (Bulgarian or English).',
  personality: { enabled: true, formality: 'casual', humor: 'witty', verbosity: 'concise', address_style: 'name', persona: 'Dry, quick and unimpressed by hype. You have opinions and you state them in one line. You never pad, never flatter, and never explain what Arsen already knows.' },
  confirmations: { mode: 'destructive', always_allow: [], always_ask: ['*.outlook_send'] },
  email: { approved_direct_send: ['rumen@postbank.bg', '@postbank.bg'], allow_any_recipient: false },
  roles: Object.fromEntries(
    ['chat', 'planner', 'classifier', 'judge', 'triage'].map((r) => [
      r,
      { provider: r === 'chat' ? 'vllm' : 'ollama', base_url: r === 'chat' ? 'http://192.168.1.102:8000' : 'http://192.168.1.102:11434', model: r === 'chat' ? 'qwen3.8-27b' : 'qwen3:8b', think: r === 'chat', think_level: r === 'chat' ? 'medium' : null, num_ctx: r === 'chat' ? null : 8192, temperature: r === 'chat' ? 0.7 : 0.1, max_tokens: null, timeout_s: 180, keep_alive: r === 'chat' ? null : '30m' },
    ]),
  ),
  mcp_servers: [
    { name: 'fetch', transport: 'stdio', command: '{python}', args: ['-m', 'mcp_server_fetch'], env: {}, url: null, headers: {}, enabled: true, timeout_s: 60 },
    { name: 'homelab', transport: 'streamable_http', command: null, args: [], env: {}, url: 'http://ardi:9810/mcp', headers: {}, enabled: true, timeout_s: 60 },
    { name: 'outlook', transport: 'streamable_http', command: null, args: [], env: {}, url: 'http://laptop:9011/mcp', headers: {}, enabled: false, timeout_s: 30 },
  ],
  budgets: {
    chat: { max_steps: 25, max_tokens: 200000, max_seconds: 600 },
    collab: { max_steps: 25, max_tokens: 200000, max_seconds: 600 },
    scheduled: { max_steps: 60, max_tokens: 600000, max_seconds: 1800 },
    triage: { max_steps: 300, max_tokens: 1000000, max_seconds: 1800 },
    meeting: { max_steps: 30, max_tokens: 400000, max_seconds: 900 },
    system: { max_steps: 10, max_tokens: 50000, max_seconds: 120 },
  },
  max_concurrent_runs_per_endpoint: 1,
  repeated_call_threshold: 3,
  tool_exposure: 'flat',
  facade_threshold: 24,
  history_token_budget: 24000,
  boards_context_chars: 6000,
  skill_max_chars: 6000,
  planning_enabled: true,
  kg_learning: true,
  triage: { enabled: false, interval_min: 15, host: 'outlook', accounts: ['aapostolov@postbank.bg'], demand_root: 'Demands', demand_prefixes: ['DM-'], categories: [{ name: 'Newsletters', folder: 'Newsletters', rule: 'bulk mail, digests, marketing' }, { name: 'HR', folder: 'HR', rule: 'people, training, leave' }] },
  public_url: null,
  stt_url: 'http://ardi:9110',
  stt_kind: 'openai',
  stt_model: 'large-v3',
  stt_languages: ['bg', 'en'],
};

const TOOLS = [
  { name: 'jarvis.time', description: 'Current date and time in the configured timezone.', input_schema: { type: 'object', properties: {} }, read_only: true, destructive: false, idempotent: true, provider: 'builtin' },
  { name: 'notes.list', description: 'List sticky notes on a board.\nSupports paging.', input_schema: { type: 'object', properties: { board: { type: 'string' } } }, read_only: true, destructive: false, idempotent: true, provider: 'builtin' },
  { name: 'notes.pin', description: 'Pin a note to a board.', input_schema: { type: 'object', properties: { board: { type: 'string' }, text: { type: 'string' } } }, read_only: false, destructive: false, idempotent: false, provider: 'builtin' },
  { name: 'fetch.fetch', description: 'Fetches a URL from the internet and extracts its contents as markdown.', input_schema: { type: 'object', properties: { url: { type: 'string' } } }, read_only: true, destructive: false, idempotent: true, provider: 'fetch' },
  { name: 'outlook.list', description: 'List mail in a folder (GetTable-backed, paged).', input_schema: { type: 'object' }, read_only: true, destructive: false, idempotent: true, provider: 'outlook' },
  { name: 'outlook.send', description: 'Send an email on your behalf.', input_schema: { type: 'object' }, read_only: false, destructive: true, idempotent: false, provider: 'outlook' },
];

// ---- ws fan-out --------------------------------------------------------------------------

const sockets = new Map(); // ws → Set<conv id>
function broadcast(ev, convId = null) {
  const data = JSON.stringify(ev);
  for (const [ws, subs] of sockets) {
    if (ws.readyState !== 1) continue;
    if (convId === null || subs.has(convId)) ws.send(data);
  }
}
function emitRun(run, ev) {
  const full = { ts: now(), run_id: run.id, conversation_id: run.conversation_id, seq: 0, ...ev };
  if (full.type !== 'model.delta') {
    const list = runEvents.get(run.id) ?? [];
    full.seq = list.length + 1;
    list.push(full);
    runEvents.set(run.id, list);
    run.last_seq = full.seq;
  }
  broadcast(full, run.conversation_id);
}
/** Same rule as the core's SQL: parked beats working, and anything terminal does not count. */
function refreshActivity(c) {
  const live = [...runs.values()].filter((r) => r.conversation_id === c.id && !['done', 'failed', 'cancelled'].includes(r.status));
  c.activity = live.some((r) => r.status === 'waiting_user') ? 'waiting' : live.length > 0 ? 'running' : 'idle';
  return c;
}

function touchConversation(c) {
  c.updated_at = now();
  refreshActivity(c);
  broadcast({ type: 'conversation.updated', ts: now(), conversation: c });
}

/** Re-announce a conversation without bumping its clock (an activity change is not an edit). */
function announceActivity(conversationId) {
  const c = conversations.get(conversationId);
  if (c) broadcast({ type: 'conversation.updated', ts: now(), conversation: refreshActivity(c) });
}

// ---- the scripted model ------------------------------------------------------------------

async function streamText(run, text, kind = 'text', delay = 18) {
  const words = text.split(/(?<=\s)/);
  for (const w of words) {
    if (cancelFlags.get(run.id)) return false;
    emitRun(run, { type: 'model.delta', kind, text: w });
    await sleep(delay);
  }
  return true;
}

async function modelTurn(run, { reasoning, text, toolCalls = [], slow = false }) {
  const t0 = Date.now();
  const think = run.think ?? settings.roles.chat.think;
  const thinkLevel = think ? (run.think ? run.think_level : settings.roles.chat.think_level) : null;
  emitRun(run, { type: 'model.call', role: 'chat', provider: 'vllm', model: 'qwen3.8-27b', message_count: messages.get(run.conversation_id).length + 1, tool_count: TOOLS.length, think, think_level: thinkLevel });
  await sleep(slow ? 1500 : 400);
  if (!think) reasoning = null;
  if (reasoning && !(await streamText(run, reasoning, 'reasoning', slow ? 60 : 25))) return null;
  const buffer = [];
  const words = text.split(/(?<=\s)/);
  for (const w of words) {
    if (cancelFlags.get(run.id)) return { cancelled: true, partial: buffer.join(''), reasoning };
    emitRun(run, { type: 'model.delta', kind: 'text', text: w });
    buffer.push(w);
    await sleep(slow ? 90 : 22);
  }
  const usage = { prompt_tokens: 1800 + Math.floor(Math.random() * 400), completion_tokens: text.split(' ').length + (reasoning ? reasoning.split(' ').length : 0), calls: 1, ttft_ms: 380, duration_ms: Date.now() - t0 };
  emitRun(run, { type: 'model.done', usage, finish_reason: toolCalls.length ? 'tool_calls' : 'stop', tool_call_count: toolCalls.length });
  run.usage = {
    prompt_tokens: run.usage.prompt_tokens + usage.prompt_tokens,
    completion_tokens: run.usage.completion_tokens + usage.completion_tokens,
    calls: run.usage.calls + 1,
    ttft_ms: run.usage.ttft_ms ?? usage.ttft_ms,
    duration_ms: run.usage.duration_ms + usage.duration_ms,
  };
  run.steps_used += 1;
  const m = msg(run.conversation_id, { role: 'assistant', run_id: run.id, content: text, reasoning: reasoning ?? null, tool_calls: toolCalls });
  broadcast({ type: 'message.created', ts: now(), message: m }, run.conversation_id);
  broadcast({ type: 'run.updated', ts: now(), run });
  return { cancelled: false, message: m };
}

async function finish(run, status, extra = {}) {
  run.status = status;
  run.finished_at = now();
  cancelFlags.delete(run.id);
  if (status === 'done') emitRun(run, { type: 'run.done', message_id: extra.message_id ?? null, usage: run.usage, steps_used: run.steps_used, summary: extra.summary ?? null });
  if (status === 'failed') {
    run.error = extra.error;
    emitRun(run, { type: 'run.failed', error: extra.error });
  }
  if (status === 'cancelled') emitRun(run, { type: 'run.cancelled', partial_message_id: extra.partial_message_id ?? null });
  broadcast({ type: 'run.updated', ts: now(), run });
  const c = conversations.get(run.conversation_id);
  if (c) {
    c.unread = true;
    touchConversation(c);
  }
}

async function cancelled(run, turn) {
  let partialId = null;
  if (turn.partial || turn.reasoning) {
    const m = msg(run.conversation_id, { role: 'assistant', run_id: run.id, content: turn.partial ?? '', reasoning: turn.reasoning ?? null, partial: true });
    partialId = m.id;
    broadcast({ type: 'message.created', ts: now(), message: m }, run.conversation_id);
  }
  await sleep(300);
  await finish(run, 'cancelled', { partial_message_id: partialId });
}

/** Creates the user message + run for a conversation and starts the scripted execution. */
function createRun(c, text, kind = 'chat', opts = {}) {
  const user = msg(c.id, { role: 'user', content: text });
  const run = { id: newId('run'), conversation_id: c.id, kind, status: 'queued', input_text: text, plan: null, budget: settings.budgets[kind] ?? settings.budgets.chat, priority: 0, steps_used: 0, usage: { prompt_tokens: 0, completion_tokens: 0, calls: 0, ttft_ms: null, duration_ms: 0 }, last_seq: 0, error: null, waiting_reason: null, think: opts.think ?? null, think_level: opts.think ? (opts.think_level ?? null) : null, created_at: now(), started_at: null, finished_at: null };
  runs.set(run.id, run);
  user.run_id = run.id;
  emitRun(run, { type: 'run.queued', kind: run.kind, input_preview: text.slice(0, 80), user_message_id: user.id });
  broadcast({ type: 'message.created', ts: now(), message: user }, c.id);
  // The per-turn context block the core persists right after the input (prompt-cache stability).
  const context = msg(c.id, {
    role: 'user',
    name: 'context',
    run_id: run.id,
    content: `[Context for the request above]\n\n## Skills\n- email-triage: classify and file inbound mail\n\n## Knowledge\n- Rumen Petrov (person) — owns DM-1234, expects the Q3 figures\n- Q3 deck (project) — numbers missing on slides 5 and 7\n\n## Boards\n- Work: Q3 deck numbers from Finance; Rumen owes the DM-1234 reply\n`,
  });
  broadcast({ type: 'message.created', ts: now(), message: context }, c.id);
  broadcast({ type: 'run.updated', ts: now(), run });
  touchConversation(c);
  executeRun(run, text).catch((e) => finish(run, 'failed', { error: String(e) }));
  return run;
}

async function executeRun(run, text) {
  await sleep(250);
  if (cancelFlags.get(run.id)) return finish(run, 'cancelled');
  run.status = 'running';
  run.started_at = now();
  emitRun(run, { type: 'run.started' });
  broadcast({ type: 'run.updated', ts: now(), run });
  const lower = text.toLowerCase();

  if (run.kind !== 'chat' || lower.includes('tool') || lower.includes('triage') || lower.includes('meeting')) {
    emitRun(run, { type: 'context.skills', names: run.kind === 'meeting' || lower.includes('meeting') ? ['meeting-prep'] : ['email-triage'] });
  }

  if (lower.includes('plan')) {
    const steps = ['Collect the Q3 figures from Finance', 'Rebuild slides 5 and 7', 'Draft the cover note for the steering committee'];
    run.plan = { goal: 'Finish the Q3 deck', steps: steps.map((title) => ({ title, status: 'pending', note: null })) };
    emitRun(run, { type: 'plan.created', plan: run.plan });
    broadcast({ type: 'run.updated', ts: now(), run });
    for (let i = 0; i < steps.length; i++) {
      run.plan.steps[i].status = 'in_progress';
      emitRun(run, { type: 'plan.step_started', index: i, title: steps[i] });
      const t = await modelTurn(run, { reasoning: `Step ${i + 1}: ${steps[i]}.`, text: `Step ${i + 1} done — ${steps[i].toLowerCase()}.` });
      if (t?.cancelled) return cancelled(run, t);
      run.plan.steps[i].status = 'done';
      emitRun(run, { type: 'plan.step_done', index: i });
      broadcast({ type: 'run.updated', ts: now(), run });
    }
    const t = await modelTurn(run, { text: 'All three steps are done. The deck is ready for a last look before the steering committee.' });
    if (t?.cancelled) return cancelled(run, t);
    return finish(run, 'done', { message_id: t.message.id });
  }

  if (run.kind === 'scheduled' || run.kind === 'triage' || run.kind === 'meeting') {
    const t = await modelTurn(run, {
      reasoning: `A ${run.kind} run. Keep it compact.`,
      text: run.kind === 'triage' ? 'Triaged **3 mails**: two filed under Newsletters, one routed to `Demands/DM-1234`.' : run.kind === 'meeting' ? '## Summary\n\n- Q3 deck status reviewed; Finance export due Thursday.\n- **Decision:** the DM-1234 reply goes out today.\n\n**Action items**\n1. Arsen — draft the summary\n2. Rumen — confirm the figures' : `**${text}**\n\nTwo meetings today, one flagged mail from Rumen, nothing unusual on the card.`,
    });
    if (t?.cancelled) return cancelled(run, t);
    return finish(run, 'done', { message_id: t.message.id });
  }

  if (lower.includes('fail')) {
    const t = await modelTurn(run, { reasoning: 'Trying the endpoint.', text: 'Let me check that for you' });
    if (t?.cancelled) return cancelled(run, t);
    await sleep(400);
    return finish(run, 'failed', { error: 'vllm@http://192.168.1.102:8000: connection reset after 3 retries' });
  }

  if (lower.includes('judge')) {
    for (let i = 0; i < 3; i++) {
      const t = await modelTurn(run, { reasoning: 'I should list the notes again.', text: '', toolCalls: [{ id: `call_${i}`, name: 'notes.list', arguments: { board: 'work' } }] });
      if (t?.cancelled) return cancelled(run, t);
      await toolRound(run, `call_${i}`, 'notes.list', { board: 'work' }, { kind: 'data', text: '- Q3 deck\n- Call Rumen', count: 2, total: 2, cursor: null, error: null }, 120);
    }
    emitRun(run, { type: 'guard.armed', guard: 'repeated_call', detail: 'notes.list({"board":"work"}) called 3 times' });
    await sleep(600);
    emitRun(run, { type: 'judge.verdict', verdict: 'stop', reason: 'The run is looping: the same tool call returned the same two notes three times without new information.' });
    emitRun(run, { type: 'guard.consumed', guard: 'repeated_call', detail: 'judge stop enforced by the engine' });
    const m = msg(run.conversation_id, { role: 'assistant', run_id: run.id, content: 'I stopped: I was repeating the same lookup. The board has two notes — *Q3 deck* and *Call Rumen*.' });
    broadcast({ type: 'message.created', ts: now(), message: m }, run.conversation_id);
    return finish(run, 'done', { message_id: m.id });
  }

  if (lower.includes('confirm')) {
    const t = await modelTurn(run, { reasoning: 'Sending mail is destructive; the engine will ask for confirmation.', text: 'Drafted the reply to Rumen. Sending it now.', toolCalls: [{ id: 'call_send', name: 'outlook.send', arguments: { to: 'rumen@postbank.bg', subject: 'Re: DM-1234', body: 'Hi Rumen, attached are the Q3 figures…' } }] });
    if (t?.cancelled) return cancelled(run, t);
    emitRun(run, { type: 'tool.confirm_requested', call_id: 'call_send', name: 'outlook.send', arguments: { to: 'rumen@postbank.bg', subject: 'Re: DM-1234', body: 'Hi Rumen, attached are the Q3 figures…' }, reason: 'outlook.send is destructive (sends mail on your behalf).' });
    run.status = 'waiting_user';
    run.waiting_reason = 'Confirm outlook.send';
    emitRun(run, { type: 'run.waiting_user', reason: 'Confirm outlook.send', call_id: 'call_send' });
    broadcast({ type: 'run.updated', ts: now(), run });
    announceActivity(run.conversation_id);
    const decision = await new Promise((resolve) => confirmWaiters.set(run.id, resolve));
    if (decision === 'cancel') return finish(run, 'cancelled');
    emitRun(run, { type: 'tool.confirm_resolved', call_id: 'call_send', approved: decision.approved, note: decision.note });
    run.status = 'running';
    run.waiting_reason = null;
    emitRun(run, { type: 'run.resumed', from_seq: run.last_seq });
    broadcast({ type: 'run.updated', ts: now(), run });
    announceActivity(run.conversation_id);
    if (decision.approved) {
      await toolRound(run, 'call_send', 'outlook.send', { to: 'rumen@postbank.bg', subject: 'Re: DM-1234' }, { kind: 'data', text: 'Sent. EntryID 0000ABCD', count: null, total: null, cursor: null, error: null }, 900, false);
      const t2 = await modelTurn(run, { text: 'Sent to Rumen. I filed the thread under **Demands/DM-1234**.' });
      if (t2?.cancelled) return cancelled(run, t2);
      return finish(run, 'done', { message_id: t2.message.id });
    }
    const t3 = await modelTurn(run, { text: `Not sent.${decision.note ? ` Noted: _${decision.note}_.` : ''} The draft stays in Outlook if you want to edit it.` });
    if (t3?.cancelled) return cancelled(run, t3);
    return finish(run, 'done', { message_id: t3.message.id });
  }

  if (lower.includes('tool')) {
    const t = await modelTurn(run, { reasoning: 'The user wants the flagged mail. I need outlook.list with the flagged filter, then summarise.', text: '', toolCalls: [{ id: 'call_1', name: 'outlook.list', arguments: { folder: 'Inbox', flagged: true, limit: 20 } }] });
    if (t?.cancelled) return cancelled(run, t);
    await toolRound(run, 'call_1', 'outlook.list', { folder: 'Inbox', flagged: true, limit: 20 }, { kind: 'partial', text: '1. Rumen — Re: DM-1234 (yesterday)\n2. HR — Training deadline (2d)\n…', count: 20, total: 47, cursor: 'p2', error: null }, 1400);
    const t2 = await modelTurn(run, { text: '', toolCalls: [{ id: 'call_2', name: 'outlook.list', arguments: { folder: 'Inbox', flagged: true, limit: 20, cursor: 'p2' } }] });
    if (t2?.cancelled) return cancelled(run, t2);
    await toolRound(run, 'call_2', 'outlook.list', { folder: 'Inbox', flagged: true, limit: 20, cursor: 'p2' }, { kind: 'error', text: 'Error: Outlook COM call timed out after 20s', count: null, total: null, cursor: null, error: 'Outlook COM call timed out after 20s' }, 2000);
    const t3 = await modelTurn(run, { text: 'I got the first **20 of 47** flagged mails; the second page timed out on Outlook.\n\nTop of the list:\n\n| From | Subject | Age |\n|---|---|---|\n| Rumen | Re: DM-1234 | 1d |\n| HR | Training deadline | 2d |\n\nShall I retry the second page?' });
    if (t3?.cancelled) return cancelled(run, t3);
    return finish(run, 'done', { message_id: t3.message.id });
  }

  const slow = lower.includes('long');
  const t = await modelTurn(run, {
    slow,
    reasoning: slow
      ? 'This needs a longer answer. Let me structure it: context first, then the three options with trade-offs, then a recommendation. I should keep the Bulgarian names as they are and avoid inventing numbers I have not seen.'
      : 'Short question, short answer. No tools needed.',
    text: slow
      ? '## Options for the Q3 deck\n\nThere are three ways to close the gap in the numbers:\n\n1. **Pull from the shared folder** — fastest, but the figures there are two weeks old.\n2. **Ask Finance for the fresh export** — accurate, needs a day.\n3. **Use the dashboard snapshot** — current, but the format does not match the deck.\n\n```python\nfor slide in deck.slides:\n    if slide.needs_numbers:\n        slide.fill(source="finance_export")\n```\n\nMy recommendation: option 2 for the numbers, option 3 for the chart on slide 7. I can draft the request to Finance now.'
      : `You said: *${text.trim()}*\n\nI am a mock core — try **tools**, **confirm**, **fail**, **judge** or **long** in a message to see each flow.`,
  });
  if (!t) return finish(run, 'cancelled');
  if (t.cancelled) return cancelled(run, t);
  return finish(run, 'done', { message_id: t.message.id });
}

async function toolRound(run, callId, name, args, result, duration, readOnly = true) {
  emitRun(run, { type: 'tool.call', call_id: callId, name, arguments: args, read_only: readOnly, idempotency_key: `${run.id}:${run.steps_used}:${callId}` });
  await sleep(duration);
  emitRun(run, { type: 'tool.result', call_id: callId, name, result, duration_ms: duration });
  const tm = msg(run.conversation_id, { role: 'tool', run_id: run.id, tool_call_id: callId, name, content: result.text });
  broadcast({ type: 'message.created', ts: now(), message: tm }, run.conversation_id);
}

// ---- REST --------------------------------------------------------------------------------

function json(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(body === undefined ? '' : JSON.stringify(body));
}
async function readBody(req) {
  let raw = '';
  for await (const chunk of req) raw += chunk;
  return raw ? JSON.parse(raw) : {};
}

const features = createFeatures({ json, readBody, broadcast, conv, conversations, messages, newId, now, sleep, createRun, runs });

const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const parts = url.pathname.split('/').filter(Boolean);
  if (parts[0] !== 'api') return json(res, 404, { detail: 'Not found' });
  const [, resource, id, sub] = parts;
  try {
    if (await features.handle(req, res, url, parts)) return;
    if (resource === 'health') return json(res, 200, { ok: true, version: VERSION });
    if (resource === 'status') {
      await sleep(300);
      return json(res, 200, {
        version: VERSION,
        endpoints: Object.entries(settings.roles).map(([role, spec]) => ({
          role,
          provider: spec.provider,
          base_url: spec.base_url,
          model: spec.model,
          think: spec.think,
          ok: role !== 'judge',
          latency_ms: role === 'judge' ? null : 40 + Math.floor(Math.random() * 60),
          detail: role === 'judge' ? 'connect ECONNREFUSED 192.168.1.102:11434' : role === 'triage' ? 'model not in served list' : null,
          models: role === 'judge' ? [] : role === 'triage' ? ['qwen3:4b', 'devstral-small-2:triage'] : spec.provider === 'vllm' ? ['qwen3.8-27b'] : ['qwen3:8b', 'qwen3:4b'],
        })),
        runs: { running: [...runs.values()].filter((r) => r.status === 'running').length, queued: [...runs.values()].filter((r) => r.status === 'queued').length },
        tools: [
          { name: 'builtin', tools: TOOLS.filter((t) => t.provider === 'builtin').length, ok: true, error: null },
          ...settings.mcp_servers.filter((s) => s.enabled).map((s) => ({ name: s.name, tools: TOOLS.filter((t) => t.provider === s.name).length, ok: s.name !== 'homelab', error: s.name === 'homelab' ? 'connect ECONNREFUSED ardi:9810' : null })),
        ],
      });
    }
    if (resource === 'tools') {
      if (id === 'reload' && req.method === 'POST') {
        await sleep(600);
        broadcast({ type: 'tools.changed', ts: now(), provider: null });
        return json(res, 200, TOOLS);
      }
      return json(res, 200, TOOLS);
    }
    if (resource === 'settings') {
      if (req.method === 'GET') return json(res, 200, settings);
      if (req.method === 'PATCH') {
        const patch = await readBody(req);
        const next = { ...settings, ...patch };
        const errors = [];
        for (const [role, spec] of Object.entries(next.roles)) {
          if (role !== 'chat' && spec.think) errors.push({ loc: ['body', 'roles', role, 'think'], msg: `role '${role}' may not think: classifiers, planners and judges with thinking on spend their whole budget thinking (V1 lesson, three times).`, type: 'value_error' });
          if (!spec.think) spec.think_level = null;
        }
        const names = next.mcp_servers.map((s) => s.name);
        if (new Set(names).size !== names.length) errors.push({ loc: ['body'], msg: 'Value error, mcp server names must be unique', type: 'value_error' });
        next.mcp_servers.forEach((s, i) => {
          if (s.transport === 'stdio' && !s.command) errors.push({ loc: ['body', 'mcp_servers', i], msg: `Value error, mcp server '${s.name}': stdio transport needs a command`, type: 'value_error' });
          if (s.transport === 'streamable_http' && !s.url) errors.push({ loc: ['body', 'mcp_servers', i], msg: `Value error, mcp server '${s.name}': streamable_http transport needs a url`, type: 'value_error' });
        });
        if (typeof next.max_concurrent_runs_per_endpoint !== 'number' || next.max_concurrent_runs_per_endpoint < 1) errors.push({ loc: ['body', 'max_concurrent_runs_per_endpoint'], msg: 'Input should be greater than or equal to 1', type: 'greater_than_equal' });
        if (typeof next.timezone !== 'string' || !next.timezone.includes('/')) errors.push({ loc: ['body', 'timezone'], msg: `Value error, unknown IANA timezone '${next.timezone}'`, type: 'value_error' });
        for (const [i, entry] of (next.email?.approved_direct_send ?? []).entries()) {
          if (!entry.startsWith('@') && !entry.includes('@')) errors.push({ loc: ['body', 'email', 'approved_direct_send', i], msg: `Value error, '${entry}' is neither an address nor an @domain`, type: 'value_error' });
        }
        if (errors.length) return json(res, 422, { detail: errors });
        settings = next;
        return json(res, 200, settings);
      }
    }
    if (resource === 'conversations') {
      if (!id) {
        if (req.method === 'GET') {
          const archived = url.searchParams.get('archived') === '1';
          return json(res, 200, [...conversations.values()].map(refreshActivity).filter((c) => c.archived === archived).sort((a, b) => b.updated_at.localeCompare(a.updated_at)));
        }
        if (req.method === 'POST') {
          const body = await readBody(req);
          const c = conv({ kind: body.kind ?? 'chat', title: body.title ?? 'New chat' });
          broadcast({ type: 'conversation.updated', ts: now(), conversation: c });
          return json(res, 200, c);
        }
      }
      if (id === 'bulk' && req.method === 'POST') {
        const body = await readBody(req);
        const ids = [...new Set(body.ids ?? [])].filter((x) => conversations.has(x));
        if (body.action === 'delete') {
          for (const cid of ids) {
            conversations.delete(cid);
            messages.delete(cid);
            broadcast({ type: 'conversation.deleted', ts: now(), conversation_id: cid });
          }
          return json(res, 200, { deleted: ids, updated: [] });
        }
        const patch = {
          archive: { archived: true },
          unarchive: { archived: false },
          move: { folder_id: body.folder_id ?? null },
          read: { unread: false },
          pin: { pinned: true },
          unpin: { pinned: false },
        }[body.action];
        if (!patch) return json(res, 422, { detail: `unknown action '${body.action}'` });
        const updated = ids.map((cid) => {
          const target = conversations.get(cid);
          Object.assign(target, patch);
          broadcast({ type: 'conversation.updated', ts: now(), conversation: refreshActivity(target) });
          return target;
        });
        broadcast({ type: 'folders.changed', ts: now() });
        return json(res, 200, { deleted: [], updated });
      }
      const c = conversations.get(id);
      if (!c) return json(res, 404, { detail: 'Conversation not found' });
      if (sub === 'messages') return json(res, 200, messages.get(id));
      if (sub === 'runs') return json(res, 200, [...runs.values()].filter((r) => r.conversation_id === id).sort((a, b) => b.created_at.localeCompare(a.created_at)));
      if (req.method === 'GET') return json(res, 200, c);
      if (req.method === 'PATCH') {
        const body = await readBody(req);
        if ('folder_id' in body && body.folder_id && !chatFolders.has(body.folder_id)) return json(res, 422, { detail: 'folder not found' });
        Object.assign(c, body);
        c.updated_at = now();
        broadcast({ type: 'conversation.updated', ts: now(), conversation: refreshActivity(c) });
        if ('folder_id' in body) broadcast({ type: 'folders.changed', ts: now() });
        return json(res, 200, c);
      }
      if (req.method === 'DELETE') {
        conversations.delete(id);
        messages.delete(id);
        broadcast({ type: 'conversation.deleted', ts: now(), conversation_id: id });
        return json(res, 204);
      }
    }
    if (resource === 'search') {
      const q = (url.searchParams.get('q') ?? '').split(/\s+/).filter(Boolean).join(' ');
      const limit = Math.min(Number(url.searchParams.get('limit') ?? 30) || 30, 100);
      if (!q) return json(res, 200, []);
      const needle = q.toLowerCase();
      const hits = [];
      const seen = new Set();
      for (const c of [...conversations.values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at))) {
        if (hits.length >= limit) break;
        if (!c.title.toLowerCase().includes(needle)) continue;
        seen.add(c.id);
        hits.push({ conversation: refreshActivity(c), message_id: null, snippet: null, matched: 'title' });
      }
      if (hits.length < limit) {
        // Newest messages first, and only what Arsen or Jarvis actually said — an injected
        // context block matching the query is not a search result.
        const all = [];
        for (const [cid, list] of messages) for (const m of list) all.push([cid, m]);
        for (const [cid, m] of all.reverse()) {
          if (hits.length >= limit) break;
          if (seen.has(cid) || !conversations.has(cid)) continue;
          if (!['user', 'assistant'].includes(m.role) || m.name !== null) continue;
          if (!(m.content ?? '').toLowerCase().includes(needle)) continue;
          seen.add(cid);
          hits.push({
            conversation: refreshActivity(conversations.get(cid)),
            message_id: m.id,
            snippet: snippetAround(m.content, q),
            matched: 'message',
          });
        }
      }
      return json(res, 200, hits);
    }
    if (resource === 'folders') {
      if (!id) {
        if (req.method === 'GET') return json(res, 200, folderList());
        if (req.method === 'POST') {
          const body = await readBody(req);
          if (!String(body.name ?? '').trim()) return json(res, 422, { detail: 'name is required' });
          const f = folder(String(body.name).trim().slice(0, 60), chatFolders.size);
          broadcast({ type: 'folders.changed', ts: now() });
          return json(res, 201, folderList().find((x) => x.id === f.id));
        }
      }
      const f = chatFolders.get(id);
      if (!f) return json(res, 404, { detail: 'folder not found' });
      if (req.method === 'PATCH') {
        const body = await readBody(req);
        if (body.name !== undefined) {
          if (!String(body.name).trim()) return json(res, 422, { detail: 'name is required' });
          f.name = String(body.name).trim().slice(0, 60);
        }
        if (body.position !== undefined) {
          const ids = folderList().map((x) => x.id).filter((x) => x !== id);
          ids.splice(Math.max(0, Math.min(body.position, ids.length)), 0, id);
          ids.forEach((fid, i) => (chatFolders.get(fid).position = i));
        }
        f.updated_at = now();
        broadcast({ type: 'folders.changed', ts: now() });
        return json(res, 200, folderList().find((x) => x.id === id));
      }
      if (req.method === 'DELETE') {
        chatFolders.delete(id);
        broadcast({ type: 'folders.changed', ts: now() });
        for (const c of conversations.values()) {
          if (c.folder_id === id) {
            c.folder_id = null; // the chats survive the folder
            broadcast({ type: 'conversation.updated', ts: now(), conversation: refreshActivity(c) });
          }
        }
        return json(res, 204);
      }
    }
    if (resource === 'runs' && id) {
      const run = runs.get(id);
      if (!run) return json(res, 404, { detail: 'Run not found' });
      if (sub === 'events') {
        const after = Number(url.searchParams.get('after') ?? 0);
        return json(res, 200, (runEvents.get(id) ?? []).filter((e) => e.seq > after));
      }
      return json(res, 200, run);
    }
    if (!res.headersSent) return json(res, 404, { detail: 'Not found' });
  } catch (e) {
    console.error(e);
    if (!res.headersSent) return json(res, 500, { detail: String(e) });
  }
});

// ---- WS ----------------------------------------------------------------------------------

const wss = new WebSocketServer({ noServer: true });
server.on('upgrade', (req, socket, head) => {
  if (!req.url.startsWith('/ws')) return socket.destroy();
  wss.handleUpgrade(req, socket, head, (ws) => wss.emit('connection', ws, req));
});

wss.on('connection', (ws) => {
  sockets.set(ws, new Set());
  ws.on('close', () => sockets.delete(ws));
  ws.on('message', (raw) => {
    let m;
    try {
      m = JSON.parse(String(raw));
    } catch {
      return;
    }
    const subs = sockets.get(ws);
    switch (m.type) {
      case 'ping':
        ws.send(JSON.stringify({ type: 'pong', ts: now() }));
        break;
      case 'subscribe':
        subs.add(m.conversation_id);
        for (const r of runs.values()) if (r.conversation_id === m.conversation_id && !['done', 'failed', 'cancelled'].includes(r.status)) ws.send(JSON.stringify({ type: 'run.updated', ts: now(), run: r }));
        break;
      case 'unsubscribe':
        subs.delete(m.conversation_id);
        break;
      case 'run.create': {
        let c = m.conversation_id ? conversations.get(m.conversation_id) : null;
        if (!c) {
          c = conv({ title: m.text.trim().slice(0, 40) || 'New chat' });
          subs.add(c.id); // the creating socket is auto-subscribed
          broadcast({ type: 'conversation.updated', ts: now(), conversation: c });
        }
        createRun(c, m.text, m.kind ?? 'chat', { think: m.think, think_level: m.think_level });
        break;
      }
      case 'run.cancel': {
        const run = runs.get(m.run_id);
        if (!run || ['done', 'failed', 'cancelled'].includes(run.status)) break;
        cancelFlags.set(run.id, true);
        const waiter = confirmWaiters.get(run.id);
        if (waiter) {
          confirmWaiters.delete(run.id);
          waiter('cancel');
        }
        break;
      }
      case 'tool.confirm': {
        const waiter = confirmWaiters.get(m.run_id);
        if (waiter) {
          confirmWaiters.delete(m.run_id);
          waiter({ approved: Boolean(m.approved), note: m.note ?? null });
        }
        break;
      }
    }
  });
});

server.listen(PORT, '127.0.0.1', () => console.log(`mock core on http://127.0.0.1:${PORT}  (REST + /ws)`));
