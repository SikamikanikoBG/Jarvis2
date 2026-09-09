/**
 * Phase 2–7 endpoints for the mock core: boards, knowledge, skills, schedules, triage, STT,
 * meetings, collab keys, pairing, conversation summaries. Wired in from server.mjs.
 */
export function createFeatures(ctx) {
  const { readBody, broadcast, conv, conversations, messages, newId, now, sleep, createRun, runs } = ctx;
  // Every handled route returns true so the main router stops (ctx.json itself returns nothing).
  const json = (res, status, body) => {
    ctx.json(res, status, body);
    return true;
  };
  const hoursAgo = (h) => new Date(Date.now() - h * 3600_000).toISOString();

  // ---- data --------------------------------------------------------------------------------
  const boards = [
    { id: 'brd_work', name: 'Work', position: 0, note_count: 0, created_at: hoursAgo(200), updated_at: hoursAgo(5) },
    { id: 'brd_home', name: 'Personal', position: 1, note_count: 0, created_at: hoursAgo(150), updated_at: hoursAgo(30) },
  ];
  const notes = [
    { id: 'note_1', board_id: 'brd_work', text: 'Q3 deck: numbers from Finance, chart from the dashboard snapshot (slide 7).', color: 'yellow', from_message_id: null, position: 0, created_at: hoursAgo(5), updated_at: hoursAgo(5) },
    { id: 'note_2', board_id: 'brd_work', text: 'Rumen owes the DM-1234 reply. Chase Thursday if silent.', color: 'pink', from_message_id: 'msg_x', position: 1, created_at: hoursAgo(20), updated_at: hoursAgo(20) },
    { id: 'note_3', board_id: 'brd_work', text: 'Steering committee is every second Tuesday 14:00.', color: 'blue', from_message_id: null, position: 2, created_at: hoursAgo(60), updated_at: hoursAgo(60) },
    { id: 'note_4', board_id: 'brd_home', text: 'Renew the car insurance before 20 September.', color: 'green', from_message_id: null, position: 0, created_at: hoursAgo(30), updated_at: hoursAgo(30) },
  ];
  const recount = () => boards.forEach((b) => (b.note_count = notes.filter((n) => n.board_id === b.id).length));
  recount();

  const entities = [
    { id: 'ent_rumen', name: 'Rumen Petrov', type: 'person', summary: 'Product owner for the demand DM-1234; expects the Q3 figures.', aliases: ['Rumen', 'R. Petrov'], mention_count: 14, updated_at: hoursAgo(2) },
    { id: 'ent_q3', name: 'Q3 deck', type: 'project', summary: 'Steering committee presentation; numbers still missing on slides 5 and 7.', aliases: ['Q3 presentation'], mention_count: 9, updated_at: hoursAgo(3) },
    { id: 'ent_finance', name: 'Finance', type: 'org', summary: 'Owns the monthly export the deck depends on.', aliases: [], mention_count: 6, updated_at: hoursAgo(40) },
    { id: 'ent_sc', name: 'Steering committee', type: 'topic', summary: 'Bi-weekly Tuesday 14:00 governance meeting.', aliases: ['SteerCo'], mention_count: 11, updated_at: hoursAgo(26) },
    { id: 'ent_dm', name: 'DM-1234', type: 'project', summary: 'Demand for the card-limit change; waiting on the business reply.', aliases: [], mention_count: 7, updated_at: hoursAgo(1) },
    { id: 'ent_sofia', name: 'Sofia', type: 'place', summary: 'Head office.', aliases: ['София'], mention_count: 3, updated_at: hoursAgo(300) },
    { id: 'ent_vader', name: 'vader', type: 'thing', summary: 'GPU server, 3×3090, serves qwen3.8-27b on vLLM.', aliases: ['the GPU box'], mention_count: 5, updated_at: hoursAgo(12) },
  ];
  const edges = [
    { src: 'ent_rumen', dst: 'ent_dm', relation: 'owns', weight: 2, evidence: 'Rumen is the demand owner for DM-1234' },
    { src: 'ent_q3', dst: 'ent_sc', relation: 'presented at', weight: 1.5, evidence: 'the deck for the steering committee' },
    { src: 'ent_finance', dst: 'ent_q3', relation: 'provides numbers for', weight: 1, evidence: null },
    { src: 'ent_rumen', dst: 'ent_finance', relation: 'liaises with', weight: 0.5, evidence: null },
    { src: 'ent_rumen', dst: 'ent_sofia', relation: 'based in', weight: 0.5, evidence: null },
  ];
  const mentions = (id) => [
    { conversation_id: [...conversations.keys()][0] ?? null, message_id: null, snippet: `…${entities.find((e) => e.id === id)?.name} was mentioned in the morning planning…`, at: hoursAgo(3) },
    { conversation_id: null, message_id: null, snippet: 'Older mention without a conversation.', at: hoursAgo(50) },
  ];

  const skills = new Map(
    [
      ['email-triage', 'Classify and file inbound mail; DM numbers route deterministically.', ['triage', 'inbox', 'mail']],
      ['meeting-prep', 'Assemble the brief before a meeting: agenda, open threads, last decisions.', ['meeting', 'prep', 'agenda']],
      ['revolut-radar', 'Weekly review of card transactions for anomalies.', ['revolut', 'transactions']],
    ].map(([name, description, triggers]) => [
      name,
      {
        meta: { name, description, triggers, enabled: name !== 'revolut-radar', size: 0, updated_at: hoursAgo(72) },
        content: `---\nname: ${name}\ndescription: ${description}\ntriggers:\n${triggers.map((t) => `  - ${t}`).join('\n')}\n---\n\n# ${name}\n\n1. Read the relevant context first.\n2. Act in small verified steps.\n3. Report what was and was not done.\n`,
      },
    ]),
  );
  for (const s of skills.values()) s.meta.size = s.content.length;

  const schedules = [
    { id: 'sch_morning', name: 'Morning brief', prompt: 'Summarise calendar, flagged mail and open demands for today.', cron: '0 7 * * 1-5', at: null, tz: 'Europe/Sofia', enabled: true, catch_up: 'skip', think: null, think_level: null, next_fire: new Date(Date.now() + 14 * 3600_000).toISOString(), last_fired_for: hoursAgo(10), last_run_id: null, last_status: 'done', created_at: hoursAgo(500), updated_at: hoursAgo(10) },
    { id: 'sch_revolut', name: 'Revolut radar', prompt: 'Review the last week of card transactions and flag anything unusual.', cron: '0 8 * * 1', at: null, tz: 'Europe/Sofia', enabled: true, catch_up: 'run_once', think: true, think_level: 'low', next_fire: new Date(Date.now() + 3 * 24 * 3600_000).toISOString(), last_fired_for: hoursAgo(80), last_run_id: null, last_status: 'failed', created_at: hoursAgo(900), updated_at: hoursAgo(80) },
    { id: 'sch_once', name: 'Remind: call the insurer', prompt: 'Tell Arsen: call the insurer about the renewal.', cron: null, at: new Date(Date.now() + 2 * 24 * 3600_000).toISOString(), tz: 'Europe/Sofia', enabled: true, catch_up: 'skip', think: false, think_level: null, next_fire: new Date(Date.now() + 2 * 24 * 3600_000).toISOString(), last_fired_for: null, last_run_id: null, last_status: null, created_at: hoursAgo(1), updated_at: hoursAgo(1) },
  ];
  const fires = new Map([
    ['sch_morning', [{ schedule_id: 'sch_morning', scheduled_for: hoursAgo(10), run_id: null, conversation_id: [...conversations.values()].find((c) => c.kind === 'scheduled')?.id ?? null, status: 'done' }, { schedule_id: 'sch_morning', scheduled_for: hoursAgo(34), run_id: null, conversation_id: null, status: 'done' }]],
    ['sch_revolut', [{ schedule_id: 'sch_revolut', scheduled_for: hoursAgo(80), run_id: null, conversation_id: null, status: 'failed' }]],
  ]);

  const triageState = [{ account: 'aapostolov@postbank.bg', cursor: '2026-09-05T08:40:11Z#00A1', day: '2026-09-05', processed_today: 14, routed_today: 2, last_run_at: hoursAgo(1), last_error: null }];
  const meetings = new Map();
  const meetingDetail = new Map(); // id → {segments, frames}
  // Two finished meetings, so the screen (and its search) can be worked on without a host to
  // record from. Starting one still goes through the live path above.
  for (const [title, host, ago] of [
    ['Q3 steering committee', 'laptop', 26],
    ['Card-limit rollout with Rumen', 'ardi', 74],
  ]) {
    const c = conv({ kind: 'meeting', title, updated_at: hoursAgo(ago) });
    const m = { id: newId('mtg'), conversation_id: c.id, title, host, status: 'done', started_at: hoursAgo(ago), ended_at: hoursAgo(ago - 1), summary_run_id: null };
    meetings.set(m.id, m);
    meetingDetail.set(m.id, {
      segments: [
        { seq: 1, t0: 0, t1: 6.2, text: 'Finance обещаха експорта до четвъртък.' },
        { seq: 2, t0: 6.2, t1: 14.8, text: 'Rumen: the DM-1234 reply goes out today after the numbers land.' },
      ],
      frames: [],
    });
  }
  const keys = [{ id: 'key_cc', name: 'claude-code', created_at: hoursAgo(300), last_used_at: hoursAgo(50) }];

  // ---- helpers -----------------------------------------------------------------------------
  const emit = (ev) => broadcast({ ts: now(), ...ev });
  const boardById = (id) => boards.find((b) => b.id === id);
  const touchBoard = (id) => {
    const b = boardById(id);
    if (b) b.updated_at = now();
    recount();
    emit({ type: 'board.changed', board_id: id });
  };
  const validation = (loc, msg) => ({ loc: ['body', ...loc], msg, type: 'value_error' });

  function startSegments(meeting) {
    const lines = ['Добре, започваме със статуса на Q3 презентацията.', 'Finance обещаха експорта до четвъртък.', 'Rumen: the DM-1234 reply goes out today after the numbers land.', 'Next: risks on the card-limit change and the rollout date.', 'Action items: Arsen drafts the summary, Rumen confirms the figures.'];
    let i = 0;
    const timer = setInterval(() => {
      const m = meetings.get(meeting.id);
      if (!m || m.status !== 'recording' || i >= lines.length) {
        clearInterval(timer);
        return;
      }
      const d = meetingDetail.get(meeting.id);
      const seq = d.segments.length + 1;
      const seg = { seq, t0: (seq - 1) * 6, t1: seq * 6 - 0.5, text: lines[i++] };
      d.segments.push(seg);
      if (seq % 2 === 1) d.frames.push({ seq: d.frames.length + 1, at: seg.t0, url: `/api/mock-frame.svg?n=${d.frames.length + 1}`, ocr: `Slide ${d.frames.length + 1}: ${seg.text.slice(0, 40)}` });
      emit({ type: 'meeting.segment', meeting_id: meeting.id, conversation_id: meeting.conversation_id, seq, t0: seg.t0, t1: seg.t1, text: seg.text });
    }, 2500);
  }

  // ---- router ------------------------------------------------------------------------------
  async function handle(req, res, url, parts) {
    const [, resource, id, sub, sub2] = parts;
    const method = req.method;

    if (resource === 'mock-frame.svg') {
      const n = url.searchParams.get('n') ?? '1';
      res.writeHead(200, { 'Content-Type': 'image/svg+xml' });
      res.end(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 90"><rect width="160" height="90" fill="#1b2440"/><rect x="12" y="14" width="136" height="12" rx="2" fill="#25d8ff" opacity=".8"/><rect x="12" y="34" width="100" height="6" rx="2" fill="#aab4cc"/><rect x="12" y="46" width="120" height="6" rx="2" fill="#aab4cc"/><rect x="12" y="58" width="80" height="6" rx="2" fill="#aab4cc"/><text x="148" y="84" font-size="9" fill="#8893ad" text-anchor="end" font-family="sans-serif">frame ${n}</text></svg>`);
      return true;
    }

    // conversation summary
    if (resource === 'conversations' && id && sub === 'summary') {
      const list = messages.get(id) ?? [];
      const firstAssistant = list.find((m) => m.role === 'assistant');
      if (list.length >= 2 && firstAssistant) return json(res, 200, { up_to_message_id: firstAssistant.id, text: 'Earlier in this conversation Arsen asked for the day’s plan; Jarvis listed the steering committee, the DM-1234 reply owed to Rumen and two Revolut transactions, and offered to draft the reply first.' });
      return json(res, 200, null);
    }

    // boards
    if (resource === 'boards') {
      if (!id) {
        if (method === 'GET') return json(res, 200, [...boards].sort((a, b) => a.position - b.position));
        if (method === 'POST') {
          const body = await readBody(req);
          if (!body.name?.trim()) return json(res, 422, { detail: [validation(['name'], 'String should have at least 1 character')] });
          const b = { id: newId('brd'), name: body.name.trim(), position: boards.length, note_count: 0, created_at: now(), updated_at: now() };
          boards.push(b);
          emit({ type: 'board.changed', board_id: null });
          return json(res, 201, b);
        }
      }
      const b = boardById(id);
      if (!b) return json(res, 404, { detail: 'Board not found' });
      if (sub === 'notes') {
        if (method === 'GET') return json(res, 200, notes.filter((n) => n.board_id === id).sort((x, y) => x.position - y.position));
        if (method === 'POST') {
          const body = await readBody(req);
          if (!body.text?.trim()) return json(res, 422, { detail: [validation(['text'], 'String should have at least 1 character')] });
          const n = { id: newId('note'), board_id: id, text: body.text.trim(), color: body.color ?? 'yellow', from_message_id: body.from_message_id ?? null, position: notes.filter((x) => x.board_id === id).length, created_at: now(), updated_at: now() };
          notes.push(n);
          touchBoard(id);
          return json(res, 201, n);
        }
      }
      if (method === 'PATCH') {
        const body = await readBody(req);
        if (body.name !== undefined) b.name = body.name;
        if (typeof body.position === 'number') {
          const sorted = [...boards].sort((x, y) => x.position - y.position).filter((x) => x.id !== id);
          sorted.splice(Math.max(0, Math.min(body.position, sorted.length)), 0, b);
          sorted.forEach((x, i) => (x.position = i));
        }
        b.updated_at = now();
        emit({ type: 'board.changed', board_id: null });
        return json(res, 200, b);
      }
      if (method === 'DELETE') {
        boards.splice(boards.indexOf(b), 1);
        for (let i = notes.length - 1; i >= 0; i--) if (notes[i].board_id === id) notes.splice(i, 1);
        emit({ type: 'board.changed', board_id: null });
        return json(res, 204);
      }
    }
    if (resource === 'notes' && id) {
      const n = notes.find((x) => x.id === id);
      if (!n) return json(res, 404, { detail: 'Note not found' });
      if (method === 'PATCH') {
        const body = await readBody(req);
        const from = n.board_id;
        Object.assign(n, Object.fromEntries(Object.entries(body).filter(([k]) => ['text', 'color', 'board_id', 'position'].includes(k))));
        n.updated_at = now();
        touchBoard(from);
        if (n.board_id !== from) touchBoard(n.board_id);
        return json(res, 200, n);
      }
      if (method === 'DELETE') {
        notes.splice(notes.indexOf(n), 1);
        touchBoard(n.board_id);
        return json(res, 204);
      }
    }

    // knowledge
    if (resource === 'kg') {
      if (id === 'graph') {
        const center = url.searchParams.get('center');
        const near = new Set([center]);
        for (const e of edges) if (e.src === center || e.dst === center) near.add(e.src === center ? e.dst : e.src);
        return json(res, 200, { nodes: entities.filter((e) => near.has(e.id)), edges: edges.filter((e) => near.has(e.src) && near.has(e.dst)) });
      }
      if (id === 'entities') {
        if (!sub) {
          const q = (url.searchParams.get('q') ?? '').toLowerCase();
          const limit = Number(url.searchParams.get('limit') ?? 50);
          return json(res, 200, entities.filter((e) => !q || e.name.toLowerCase().includes(q) || e.aliases.some((a) => a.toLowerCase().includes(q))).slice(0, limit));
        }
        const e = entities.find((x) => x.id === sub);
        if (!e) return json(res, 404, { detail: 'Entity not found' });
        if (sub2 === 'merge' && method === 'POST') {
          const { into } = await readBody(req);
          const target = entities.find((x) => x.id === into);
          if (!target) return json(res, 422, { detail: [validation(['into'], 'unknown entity')] });
          target.aliases = [...new Set([...target.aliases, e.name, ...e.aliases])];
          target.mention_count += e.mention_count;
          for (const ed of edges) {
            if (ed.src === e.id) ed.src = into;
            if (ed.dst === e.id) ed.dst = into;
          }
          entities.splice(entities.indexOf(e), 1);
          emit({ type: 'kg.changed', entity_ids: [e.id, into] });
          return json(res, 200, target);
        }
        if (method === 'GET') {
          const detailEdges = edges.filter((ed) => ed.src === e.id || ed.dst === e.id).map((ed) => ({ ...ed, other: entities.find((x) => x.id === (ed.src === e.id ? ed.dst : ed.src)) })).filter((ed) => ed.other);
          return json(res, 200, { ...e, edges: detailEdges, mentions: mentions(e.id) });
        }
        if (method === 'PATCH') {
          const body = await readBody(req);
          if (body.name !== undefined && !body.name.trim()) return json(res, 422, { detail: [validation(['name'], 'String should have at least 1 character')] });
          Object.assign(e, Object.fromEntries(Object.entries(body).filter(([k]) => ['name', 'type', 'summary'].includes(k))));
          e.updated_at = now();
          emit({ type: 'kg.changed', entity_ids: [e.id] });
          return json(res, 200, e);
        }
        if (method === 'DELETE') {
          entities.splice(entities.indexOf(e), 1);
          for (let i = edges.length - 1; i >= 0; i--) if (edges[i].src === e.id || edges[i].dst === e.id) edges.splice(i, 1);
          emit({ type: 'kg.changed', entity_ids: [e.id] });
          return json(res, 204);
        }
      }
    }

    // skills
    if (resource === 'skills') {
      if (!id) return json(res, 200, [...skills.values()].map((s) => s.meta));
      const name = decodeURIComponent(id);
      const s = skills.get(name);
      if (method === 'GET') return s ? json(res, 200, { name, content: s.content }) : json(res, 404, { detail: 'Skill not found' });
      if (method === 'PUT') {
        const { content } = await readBody(req);
        const fm = /^---\n([\s\S]*?)\n---/.exec(content ?? '');
        if (!fm) return json(res, 422, { detail: [validation(['content'], 'frontmatter block (--- … ---) is required')] });
        const get = (k) => new RegExp(`^${k}:\\s*(.+)$`, 'm').exec(fm[1])?.[1]?.trim() ?? '';
        const triggers = [...fm[1].matchAll(/^\s+-\s+(.+)$/gm)].map((m) => m[1].trim());
        const meta = { name, description: get('description'), triggers, enabled: s?.meta.enabled ?? true, size: content.length, updated_at: now() };
        skills.set(name, { meta, content });
        emit({ type: 'skills.changed' });
        return json(res, 200, meta);
      }
      if (!s) return json(res, 404, { detail: 'Skill not found' });
      if (method === 'PATCH') {
        const { enabled } = await readBody(req);
        s.meta.enabled = Boolean(enabled);
        s.meta.updated_at = now();
        emit({ type: 'skills.changed' });
        return json(res, 200, s.meta);
      }
      if (method === 'DELETE') {
        skills.delete(name);
        emit({ type: 'skills.changed' });
        return json(res, 204);
      }
    }

    // schedules
    if (resource === 'schedules') {
      if (!id) {
        if (method === 'GET') return json(res, 200, schedules);
        if (method === 'POST') {
          const body = await readBody(req);
          const errors = [];
          if (!body.name?.trim()) errors.push(validation(['name'], 'String should have at least 1 character'));
          if (!body.prompt?.trim()) errors.push(validation(['prompt'], 'String should have at least 1 character'));
          if (Boolean(body.cron) === Boolean(body.at)) errors.push(validation([], 'Value error, exactly one of cron / at is required'));
          if (errors.length) return json(res, 422, { detail: errors });
          const s = { id: newId('sch'), name: body.name.trim(), prompt: body.prompt.trim(), cron: body.cron ?? null, at: body.at ?? null, tz: body.tz ?? 'Europe/Sofia', enabled: body.enabled ?? true, catch_up: body.catch_up ?? 'skip', think: body.think ?? null, think_level: body.think ? (body.think_level ?? null) : null, next_fire: body.at ?? new Date(Date.now() + 3600_000).toISOString(), last_fired_for: null, last_run_id: null, last_status: null, created_at: now(), updated_at: now() };
          schedules.push(s);
          emit({ type: 'schedule.changed', schedule_id: s.id });
          return json(res, 201, s);
        }
      }
      const s = schedules.find((x) => x.id === id);
      if (!s) return json(res, 404, { detail: 'Schedule not found' });
      if (sub === 'fires') return json(res, 200, fires.get(id) ?? []);
      if (sub === 'run' && method === 'POST') {
        const c = conv({ kind: 'scheduled', title: `${s.name} · ${new Date().toISOString().slice(0, 16).replace('T', ' ')}`, folder_key: s.id, folder_label: s.name });
        broadcast({ type: 'conversation.updated', ts: now(), conversation: c });
        const run = createRun(c, s.prompt, 'scheduled', { think: s.think, think_level: s.think_level });
        s.last_fired_for = now();
        s.last_run_id = run.id;
        s.last_status = 'running';
        fires.set(id, [{ schedule_id: id, scheduled_for: now(), run_id: run.id, conversation_id: c.id, status: 'running' }, ...(fires.get(id) ?? [])]);
        emit({ type: 'schedule.changed', schedule_id: id });
        setTimeout(() => {
          const r = runs.get(run.id);
          s.last_status = r?.status ?? 'done';
          const f = fires.get(id)?.find((x) => x.run_id === run.id);
          if (f) f.status = s.last_status;
          emit({ type: 'schedule.changed', schedule_id: id });
        }, 6000);
        return json(res, 200, { run_id: run.id, conversation_id: c.id });
      }
      if (method === 'PATCH') {
        const body = await readBody(req);
        Object.assign(s, body);
        if (!s.think) s.think_level = null;
        if (s.at && !s.cron) s.next_fire = s.at;
        s.updated_at = now();
        emit({ type: 'schedule.changed', schedule_id: id });
        return json(res, 200, s);
      }
      if (method === 'DELETE') {
        schedules.splice(schedules.indexOf(s), 1);
        emit({ type: 'schedule.changed', schedule_id: null });
        return json(res, 204);
      }
    }

    // triage
    if (resource === 'triage') {
      if (id === 'state') return json(res, 200, triageState);
      if (id === 'run' && method === 'POST') {
        const c = [...conversations.values()].find((x) => x.kind === 'triage') ?? conv({ kind: 'triage', title: '2026-09-05', folder_key: 'aapostolov@postbank.bg', folder_label: 'aapostolov@postbank.bg' });
        const run = createRun(c, 'Triage the inbox since the cursor.', 'triage', {});
        triageState[0].last_run_at = now();
        triageState[0].processed_today += 3;
        return json(res, 200, { run_id: run.id });
      }
    }

    // stt
    if (resource === 'stt' && method === 'POST') {
      let size = 0;
      for await (const chunk of req) size += chunk.length;
      await sleep(900);
      if (size < 200) return json(res, 502, { detail: 'whisper@http://ardi:9110: connection refused' });
      return json(res, 200, { text: 'Напомни ми да се обадя на Румен утре в десет.', language: 'bg', backend: 'whisper:large-v3', duration_ms: 2400 });
    }

    // meetings
    if (resource === 'meetings') {
      if (!id) {
        if (method === 'GET') return json(res, 200, [...meetings.values()]);
        if (method === 'POST') {
          const body = await readBody(req);
          if (!body.host) return json(res, 422, { detail: [validation(['host'], 'Field required')] });
          const title = body.title?.trim() || `Meeting ${new Date().toLocaleTimeString()}`;
          const c = conv({ kind: 'meeting', title, folder_key: null, folder_label: null });
          broadcast({ type: 'conversation.updated', ts: now(), conversation: c });
          const m = { id: newId('mtg'), conversation_id: c.id, title, host: body.host, status: 'recording', started_at: now(), ended_at: null, summary_run_id: null };
          meetings.set(m.id, m);
          meetingDetail.set(m.id, { segments: [], frames: [] });
          emit({ type: 'meeting.changed', meeting_id: m.id, conversation_id: c.id, status: m.status });
          startSegments(m);
          return json(res, 201, m);
        }
      }
      const m = meetings.get(id);
      if (!m) return json(res, 404, { detail: 'Meeting not found' });
      if (sub === 'stop' && method === 'POST') {
        m.status = 'summarising';
        m.ended_at = now();
        emit({ type: 'meeting.changed', meeting_id: m.id, conversation_id: m.conversation_id, status: m.status });
        const c = conversations.get(m.conversation_id);
        const run = c ? createRun(c, 'Summarise the meeting transcript with decisions and action items.', 'meeting', {}) : null;
        m.summary_run_id = run?.id ?? null;
        setTimeout(() => {
          m.status = 'done';
          emit({ type: 'meeting.changed', meeting_id: m.id, conversation_id: m.conversation_id, status: m.status });
        }, 5000);
        return json(res, 200, m);
      }
      if (method === 'GET') return json(res, 200, { ...m, ...meetingDetail.get(id) });
    }

    // collab keys
    if (resource === 'collab' && id === 'keys') {
      if (!sub) {
        if (method === 'GET') return json(res, 200, keys);
        if (method === 'POST') {
          const { name } = await readBody(req);
          if (!name?.trim()) return json(res, 422, { detail: [validation(['name'], 'String should have at least 1 character')] });
          const k = { id: newId('key'), name: name.trim(), created_at: now(), last_used_at: null };
          keys.push(k);
          return json(res, 201, { key: `jk_${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`, id: k.id, name: k.name });
        }
      }
      if (sub && method === 'DELETE') {
        const i = keys.findIndex((k) => k.id === sub);
        if (i >= 0) keys.splice(i, 1);
        return json(res, 204);
      }
    }

    if (resource === 'pair') {
      const cells = [];
      for (let y = 0; y < 21; y++) for (let x = 0; x < 21; x++) if ((x * 7 + y * 13 + x * y) % 3 === 0 || (x < 7 && y < 7) || (x > 13 && y < 7) || (x < 7 && y > 13)) cells.push(`<rect x="${x}" y="${y}" width="1" height="1"/>`);
      return json(res, 200, { url: `${url.protocol}//${req.headers.host}/?token=mock-pair-token`, qr_svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 21 21" shape-rendering="crispEdges"><rect width="21" height="21" fill="#fff"/><g fill="#000">${cells.join('')}</g></svg>` });
    }
    if (resource === 'whoami') return json(res, 200, { owner: true });

    return false;
  }

  return { handle };
}
