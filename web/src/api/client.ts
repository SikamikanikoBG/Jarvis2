import type {
  Attachment,
  Board,
  CollabKey,
  CollabKeyCreated,
  Conversation,
  ConversationSummary,
  SearchHit,
  Entity,
  EntityDetail,
  Graph,
  HealthResponse,
  Meeting,
  MeetingDetail,
  Message,
  Note,
  NoteColor,
  PairResponse,
  Run,
  Schedule,
  ScheduleFire,
  ScheduleRunResponse,
  ServerEvent,
  Settings,
  Skill,
  SkillContent,
  StatusResponse,
  SttResponse,
  ToolSpec,
  TriageState,
  Whoami,
} from '../protocol/types';
import { getToken } from '../lib/token';

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const multipart = body instanceof FormData;
  if (body !== undefined && !multipart) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? null : multipart ? body : JSON.stringify(body),
    credentials: 'same-origin',
  });
  if (res.status === 204) return null as T;
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) throw new ApiError(res.status, data, describeError(res.status, data));
  return data as T;
}

interface ValidationItem {
  path: string[];
  msg: string;
}

function asText(v: unknown, fallback: string): string {
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return fallback;
}

/** Normalises a FastAPI/pydantic 422 body (`{detail: [{loc, msg}]}`) into path + message pairs. */
function validationItems(body: unknown): ValidationItem[] | null {
  if (!body || typeof body !== 'object' || !('detail' in body)) return null;
  const detail: unknown = body.detail;
  if (!Array.isArray(detail)) return null;
  return detail.map((d: unknown): ValidationItem => {
    if (!d || typeof d !== 'object') return { path: [], msg: asText(d, 'invalid') };
    const rec = d as Record<string, unknown>;
    const loc: unknown = rec.loc;
    const path = Array.isArray(loc) ? loc.filter((x) => x !== 'body').map((x) => asText(x, '')) : [];
    return { path, msg: asText(rec.msg, 'invalid') };
  });
}

/** Human-readable message from an error body. */
export function describeError(status: number, body: unknown): string {
  if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string') return body.detail;
  const items = validationItems(body);
  if (items) return items.map((it) => (it.path.length ? `${it.path.join('.')}: ${it.msg}` : it.msg)).join('\n');
  if (typeof body === 'string' && body) return body;
  return `Request failed (${status})`;
}

/** Field-level errors from a 422, keyed by top-level settings key ("roles", "budgets", …). */
export function fieldErrors(body: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  for (const it of validationItems(body) ?? []) {
    const key = it.path[0] ?? '_';
    const rest = it.path.slice(1).join('.');
    const line = rest ? `${rest}: ${it.msg}` : it.msg;
    out[key] = key in out ? `${out[key]}\n${line}` : line;
  }
  return out;
}

export const api = {
  health: () => request<HealthResponse>('GET', '/api/health'),
  status: () => request<StatusResponse>('GET', '/api/status'),
  conversations: {
    list: (archived: boolean) => request<Conversation[]>('GET', `/api/conversations?archived=${archived ? 1 : 0}`),
    create: (body: { kind?: string; title?: string }) => request<Conversation>('POST', '/api/conversations', body),
    get: (id: string) => request<Conversation>('GET', `/api/conversations/${encodeURIComponent(id)}`),
    patch: (
      id: string,
      body: { title?: string; archived?: boolean; unread?: boolean; pinned?: boolean; instructions?: string },
    ) =>
      request<Conversation>('PATCH', `/api/conversations/${encodeURIComponent(id)}`, body),
    remove: (id: string) => request<null>('DELETE', `/api/conversations/${encodeURIComponent(id)}`),
    /** New conversation with the transcript BEFORE `upToMessageId` (null = all of it). */
    fork: (id: string, upToMessageId: string | null) =>
      request<Conversation>('POST', `/api/conversations/${encodeURIComponent(id)}/fork`, { up_to_message_id: upToMessageId }),
    search: (q: string, limit = 30) => request<SearchHit[]>('GET', `/api/search?q=${encodeURIComponent(q)}&limit=${limit}`),
    messages: (id: string) => request<Message[]>('GET', `/api/conversations/${encodeURIComponent(id)}/messages`),
    runs: (id: string) => request<Run[]>('GET', `/api/conversations/${encodeURIComponent(id)}/runs`),
    summary: (id: string) => request<ConversationSummary | null>('GET', `/api/conversations/${encodeURIComponent(id)}/summary`),
  },
  boards: {
    list: () => request<Board[]>('GET', '/api/boards'),
    create: (name: string) => request<Board>('POST', '/api/boards', { name }),
    patch: (id: string, body: { name?: string; position?: number }) => request<Board>('PATCH', `/api/boards/${encodeURIComponent(id)}`, body),
    remove: (id: string) => request<null>('DELETE', `/api/boards/${encodeURIComponent(id)}`),
    notes: (id: string) => request<Note[]>('GET', `/api/boards/${encodeURIComponent(id)}/notes`),
    addNote: (id: string, body: { text: string; color?: NoteColor; from_message_id?: string }) =>
      request<Note>('POST', `/api/boards/${encodeURIComponent(id)}/notes`, body),
  },
  notes: {
    patch: (id: string, body: { text?: string; color?: NoteColor; board_id?: string; position?: number }) =>
      request<Note>('PATCH', `/api/notes/${encodeURIComponent(id)}`, body),
    remove: (id: string) => request<null>('DELETE', `/api/notes/${encodeURIComponent(id)}`),
  },
  kg: {
    entities: (q: string, limit = 50) => request<Entity[]>('GET', `/api/kg/entities?q=${encodeURIComponent(q)}&limit=${limit}`),
    entity: (id: string) => request<EntityDetail>('GET', `/api/kg/entities/${encodeURIComponent(id)}`),
    patch: (id: string, body: { name?: string; type?: Entity['type']; summary?: string }) =>
      request<Entity>('PATCH', `/api/kg/entities/${encodeURIComponent(id)}`, body),
    remove: (id: string) => request<null>('DELETE', `/api/kg/entities/${encodeURIComponent(id)}`),
    merge: (id: string, into: string) => request<Entity>('POST', `/api/kg/entities/${encodeURIComponent(id)}/merge`, { into }),
    graph: (center: string, depth = 1, limit = 80) =>
      request<Graph>('GET', `/api/kg/graph?center=${encodeURIComponent(center)}&depth=${depth}&limit=${limit}`),
  },
  skills: {
    list: () => request<Skill[]>('GET', '/api/skills'),
    get: (name: string) => request<SkillContent>('GET', `/api/skills/${encodeURIComponent(name)}`),
    put: (name: string, content: string) => request<Skill>('PUT', `/api/skills/${encodeURIComponent(name)}`, { content }),
    patch: (name: string, enabled: boolean) => request<Skill>('PATCH', `/api/skills/${encodeURIComponent(name)}`, { enabled }),
    remove: (name: string) => request<null>('DELETE', `/api/skills/${encodeURIComponent(name)}`),
  },
  schedules: {
    list: () => request<Schedule[]>('GET', '/api/schedules'),
    create: (body: Partial<Schedule>) => request<Schedule>('POST', '/api/schedules', body),
    patch: (id: string, body: Partial<Schedule>) => request<Schedule>('PATCH', `/api/schedules/${encodeURIComponent(id)}`, body),
    remove: (id: string) => request<null>('DELETE', `/api/schedules/${encodeURIComponent(id)}`),
    run: (id: string) => request<ScheduleRunResponse>('POST', `/api/schedules/${encodeURIComponent(id)}/run`),
    fires: (id: string, limit = 20) => request<ScheduleFire[]>('GET', `/api/schedules/${encodeURIComponent(id)}/fires?limit=${limit}`),
  },
  triage: {
    state: () => request<TriageState[]>('GET', '/api/triage/state'),
    run: () => request<{ run_id: string }>('POST', '/api/triage/run'),
  },
  stt: (audio: Blob, language?: string) => {
    const form = new FormData();
    form.append('audio', audio, 'speech.webm');
    if (language) form.append('language', language);
    return request<SttResponse>('POST', '/api/stt', form);
  },
  meetings: {
    list: () => request<Meeting[]>('GET', '/api/meetings'),
    get: (id: string) => request<MeetingDetail>('GET', `/api/meetings/${encodeURIComponent(id)}`),
    create: (body: { title?: string; host: string }) => request<Meeting>('POST', '/api/meetings', body),
    stop: (id: string) => request<Meeting>('POST', `/api/meetings/${encodeURIComponent(id)}/stop`),
    /** Deletes the recording, its frames and the conversation holding its transcript. */
    remove: (id: string) => request<null>('DELETE', `/api/meetings/${encodeURIComponent(id)}`),
  },
  attachments: {
    upload: (file: File, conversationId: string | null) => {
      const form = new FormData();
      form.append('file', file, file.name);
      if (conversationId) form.append('conversation_id', conversationId);
      return request<Attachment>('POST', '/api/attachments', form);
    },
    uploadText: (text: string, name: string, conversationId: string | null) =>
      request<Attachment>('POST', '/api/attachments/text', { text, name, conversation_id: conversationId }),
    remove: (id: string) => request<null>('DELETE', `/api/attachments/${encodeURIComponent(id)}`),
    /** Direct URL for an <img> or a download; the token rides in the query like the SPA's own. */
    url: (id: string, opts: { thumb?: boolean } = {}) => {
      const token = getToken();
      const params = new URLSearchParams();
      if (opts.thumb) params.set('thumb', '1');
      if (token) params.set('token', token);
      const query = params.toString();
      return `/api/attachments/${encodeURIComponent(id)}${query ? `?${query}` : ''}`;
    },
  },
  collab: {
    keys: () => request<CollabKey[]>('GET', '/api/collab/keys'),
    createKey: (name: string) => request<CollabKeyCreated>('POST', '/api/collab/keys', { name }),
    removeKey: (id: string) => request<null>('DELETE', `/api/collab/keys/${encodeURIComponent(id)}`),
  },
  pair: () => request<PairResponse>('GET', '/api/pair'),
  whoami: () => request<Whoami>('GET', '/api/whoami'),
  runs: {
    get: (id: string) => request<Run>('GET', `/api/runs/${encodeURIComponent(id)}`),
    events: (id: string, after = 0) => request<ServerEvent[]>('GET', `/api/runs/${encodeURIComponent(id)}/events?after=${after}`),
  },
  settings: {
    get: () => request<Settings>('GET', '/api/settings'),
    patch: (body: Partial<Settings>) => request<Settings>('PATCH', '/api/settings', body),
  },
  tools: {
    list: () => request<ToolSpec[]>('GET', '/api/tools'),
    reload: () => request<ToolSpec[]>('POST', '/api/tools/reload'),
  },
};
