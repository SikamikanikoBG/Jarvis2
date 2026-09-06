import { formatDuration, formatTokens, previewValue, tokPerSec } from '../lib/format';
import type { ModelUsage, RunScopedEvent } from '../protocol/types';

/** "12.4k (94%)" — how much of the prompt the server's prefix cache already had. */
export function cachedLabel(u: ModelUsage): string {
  if (!u.prompt_tokens) return '—';
  const pct = Math.round((u.cached_tokens / u.prompt_tokens) * 100);
  return `${formatTokens(u.cached_tokens)} (${pct}%)`;
}

export type Category = 'model' | 'tool' | 'run' | 'guard' | 'plan' | 'judge';

export interface Metric {
  label: string;
  value: string;
}

export interface TimelineRow {
  key: string;
  ts: string;
  /** Milliseconds since the previous row; null for the first. */
  deltaMs: number | null;
  type: string;
  category: Category;
  preview: string;
  metrics: Metric[];
  payload: unknown;
}

function categoryOf(type: string): Category {
  const head = type.split('.')[0];
  if (head === 'model' || head === 'tool' || head === 'run' || head === 'guard' || head === 'plan' || head === 'judge') return head;
  return 'run';
}

function previewOf(ev: RunScopedEvent): string {
  switch (ev.type) {
    case 'run.queued':
      return `${ev.kind} · ${ev.input_preview}`;
    case 'run.failed':
      return ev.error;
    case 'run.waiting_user':
      return ev.reason;
    case 'run.done':
      return `${ev.steps_used} steps · ${formatTokens(ev.usage.prompt_tokens + ev.usage.completion_tokens)} tok${ev.summary ? ` · ${ev.summary}` : ''}`;
    case 'run.resumed':
      return `from seq ${ev.from_seq}`;
    case 'model.call':
      return `${ev.role} → ${ev.provider}/${ev.model} · ${ev.message_count} msgs · ${ev.tool_count} tools · ${ev.think ? `think${ev.think_level ? `:${ev.think_level}` : ''}` : 'no think'}`;
    case 'tool.call':
    case 'tool.confirm_requested':
      return `${ev.name} ${previewValue(ev.arguments, 70)}`;
    case 'tool.result':
      return `${ev.name} → ${ev.result.kind}${ev.result.count !== null ? ` (${ev.result.count}${ev.result.total !== null ? `/${ev.result.total}` : ''})` : ''}`;
    case 'tool.confirm_resolved':
      return ev.approved ? 'approved' : 'rejected';
    case 'guard.armed':
    case 'guard.consumed':
      return `${ev.guard} · ${ev.detail}`;
    case 'judge.verdict':
      return `${ev.verdict} · ${ev.reason}`;
    case 'plan.created':
      return `${ev.plan.goal} · ${ev.plan.steps.length} steps`;
    case 'plan.step_started':
      return `#${ev.index + 1} ${ev.title}`;
    case 'plan.step_done':
      return `#${ev.index + 1}`;
    default:
      return '';
  }
}

function callThink(payload: unknown): string | null {
  const ev = payload as { type?: string; think?: boolean; think_level?: string | null } | null;
  if (ev?.type !== 'model.call') return null;
  if (!ev.think) return 'off';
  return ev.think_level ? `on · ${ev.think_level}` : 'on';
}

/**
 * Pairs `model.call`→`model.done` and `tool.call`→`tool.result` into single rows with
 * metrics (tokens, TTFT, tok/s, duration); everything else is one row per event.
 */
export function buildTimeline(events: RunScopedEvent[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  const openModel: TimelineRow[] = [];
  const openTools = new Map<string, TimelineRow>();
  let prevTs: number | null = null;

  const push = (ev: RunScopedEvent, type: string, preview: string, payload: unknown): TimelineRow => {
    const t = Date.parse(ev.ts);
    const row: TimelineRow = {
      key: `${ev.seq || ev.ts}:${ev.type}:${rows.length}`,
      ts: ev.ts,
      deltaMs: prevTs === null ? null : Math.max(0, t - prevTs),
      type,
      category: categoryOf(ev.type),
      preview,
      metrics: [],
      payload,
    };
    prevTs = t;
    rows.push(row);
    return row;
  };

  for (const ev of events) {
    switch (ev.type) {
      case 'model.call': {
        const row = push(ev, 'model.call', previewOf(ev), ev);
        openModel.push(row);
        break;
      }
      case 'model.done': {
        const row = openModel.pop();
        if (row) {
          row.type = 'model.call → done';
          const u = ev.usage;
          const tps = tokPerSec(u.completion_tokens, u.duration_ms, u.ttft_ms);
          row.metrics = [
            { label: 'prompt', value: formatTokens(u.prompt_tokens) },
            // What the prefix cache served vs what this call had to read: TTFT is spent on the
            // second number, so a low ratio on a big prompt is the whole explanation for a slow
            // first token (docs/journal_ttft.md).
            { label: 'cached', value: cachedLabel(u) },
            { label: 'completion', value: formatTokens(u.completion_tokens) },
            { label: 'TTFT', value: u.ttft_ms !== null ? formatDuration(u.ttft_ms) : '—' },
            { label: 'tok/s', value: tps !== null ? tps.toFixed(1) : '—' },
            { label: 'duration', value: formatDuration(u.duration_ms) },
            ...(callThink(row.payload) ? [{ label: 'think', value: callThink(row.payload) ?? '' }] : []),
            ...(ev.finish_reason ? [{ label: 'finish', value: ev.finish_reason }] : []),
            ...(ev.tool_call_count ? [{ label: 'tool calls', value: String(ev.tool_call_count) }] : []),
          ];
          row.payload = { call: row.payload, done: ev };
          prevTs = Date.parse(ev.ts);
        } else {
          push(ev, 'model.done', `${formatTokens(ev.usage.completion_tokens)} tok`, ev);
        }
        break;
      }
      case 'tool.call': {
        const row = push(ev, 'tool.call', previewOf(ev), ev);
        openTools.set(ev.call_id, row);
        break;
      }
      case 'tool.result': {
        const row = openTools.get(ev.call_id);
        if (row) {
          openTools.delete(ev.call_id);
          row.type = 'tool.call → result';
          row.preview = `${row.preview} → ${ev.result.kind}`;
          row.metrics = [
            { label: 'duration', value: formatDuration(ev.duration_ms) },
            { label: 'kind', value: ev.result.kind },
            ...(ev.result.count !== null ? [{ label: 'count', value: `${ev.result.count}${ev.result.total !== null ? `/${ev.result.total}` : ''}` }] : []),
          ];
          row.payload = { call: row.payload, result: ev };
          prevTs = Date.parse(ev.ts);
        } else {
          push(ev, 'tool.result', previewOf(ev), ev).metrics = [{ label: 'duration', value: formatDuration(ev.duration_ms) }];
        }
        break;
      }
      default:
        push(ev, ev.type, previewOf(ev), ev);
    }
  }
  return rows;
}
