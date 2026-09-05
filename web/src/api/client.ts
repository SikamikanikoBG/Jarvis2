import type {
  Conversation,
  HealthResponse,
  Message,
  Run,
  ServerEvent,
  Settings,
  StatusResponse,
  ToolSpec,
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
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? null : JSON.stringify(body),
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
    patch: (id: string, body: { title?: string; archived?: boolean; unread?: boolean }) =>
      request<Conversation>('PATCH', `/api/conversations/${encodeURIComponent(id)}`, body),
    remove: (id: string) => request<null>('DELETE', `/api/conversations/${encodeURIComponent(id)}`),
    messages: (id: string) => request<Message[]>('GET', `/api/conversations/${encodeURIComponent(id)}/messages`),
    runs: (id: string) => request<Run[]>('GET', `/api/conversations/${encodeURIComponent(id)}/runs`),
  },
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
