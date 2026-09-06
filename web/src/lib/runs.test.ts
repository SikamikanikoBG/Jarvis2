import { describe, expect, it } from 'vitest';
import type { ModelUsage, Run, RunScopedEvent } from '../protocol/types';
import { formatTokensPerSecond, runTokensPerSecond } from './runs';

const usage = (completion: number, durationMs: number): ModelUsage => ({ prompt_tokens: 1000, completion_tokens: completion, calls: 1, ttft_ms: 300, duration_ms: durationMs, cached_tokens: 0 });

const done = (completion: number, durationMs: number): RunScopedEvent => ({
  type: 'model.done',
  ts: '2026-09-05T10:00:00.000Z',
  run_id: 'run_1',
  conversation_id: 'conv_1',
  seq: 1,
  usage: usage(completion, durationMs),
  finish_reason: 'stop',
  tool_call_count: 0,
});

function run(u: ModelUsage): Run {
  return {
    id: 'run_1',
    conversation_id: 'conv_1',
    kind: 'chat',
    status: 'done',
    input_text: '',
    plan: null,
    budget: { max_steps: 25, max_tokens: 1, max_seconds: 1 },
    priority: 0,
    steps_used: 1,
    usage: u,
    last_seq: 3,
    error: null,
    waiting_reason: null,
    think: null,
    think_level: null,
    created_at: '2026-09-05T10:00:00.000Z',
    started_at: '2026-09-05T10:00:00.000Z',
    finished_at: '2026-09-05T10:00:05.000Z',
  };
}

describe('runTokensPerSecond', () => {
  it('sums completion tokens and durations across the run’s model calls', () => {
    const tps = runTokensPerSecond(run(usage(0, 0)), [done(100, 2000), done(50, 1000)]);
    expect(tps).toBeCloseTo(50, 6); // 150 tokens / 3 s
  });

  it('falls back to the run head when the events are not loaded', () => {
    expect(runTokensPerSecond(run(usage(120, 4000)), undefined)).toBeCloseTo(30, 6);
    expect(runTokensPerSecond(run(usage(120, 4000)), [])).toBeCloseTo(30, 6);
  });

  it('returns null when there is nothing to divide, so the UI can omit the segment', () => {
    expect(runTokensPerSecond(run(usage(0, 0)), [])).toBeNull();
    expect(runTokensPerSecond(run(usage(0, 5000)), [done(0, 5000)])).toBeNull();
    expect(runTokensPerSecond(run(usage(50, 0)), [done(50, 0)])).toBeNull();
    expect(runTokensPerSecond(undefined, undefined)).toBeNull();
  });

  it('ignores non-model events', () => {
    const guard: RunScopedEvent = { type: 'guard.armed', ts: '2026-09-05T10:00:00.000Z', run_id: 'run_1', conversation_id: 'conv_1', seq: 2, guard: 'repeated_call', detail: 'x' };
    expect(runTokensPerSecond(run(usage(0, 0)), [guard, done(60, 1500)])).toBeCloseTo(40, 6);
  });
});

describe('formatTokensPerSecond', () => {
  it('uses one decimal below 10 and none above', () => {
    expect(formatTokensPerSecond(9.44)).toBe('9.4 tok/s');
    expect(formatTokensPerSecond(9.99)).toBe('10.0 tok/s');
    expect(formatTokensPerSecond(50.6)).toBe('51 tok/s');
    expect(formatTokensPerSecond(10)).toBe('10 tok/s');
  });
});
