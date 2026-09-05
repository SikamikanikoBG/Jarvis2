import type { ThinkLevel } from '../protocol/types';

/** A per-message thinking override; both null = the role's configured setting. */
export interface ThinkChoice {
  think: boolean | null;
  think_level: ThinkLevel | null;
}

export const THINK_DEFAULT: ThinkChoice = { think: null, think_level: null };

export const THINK_CHOICES: { key: string; label: string; choice: ThinkChoice }[] = [
  { key: 'default', label: 'role default', choice: THINK_DEFAULT },
  { key: 'off', label: 'off', choice: { think: false, think_level: null } },
  { key: 'on', label: 'on', choice: { think: true, think_level: null } },
  { key: 'low', label: 'on · low', choice: { think: true, think_level: 'low' } },
  { key: 'medium', label: 'on · medium', choice: { think: true, think_level: 'medium' } },
  { key: 'high', label: 'on · high', choice: { think: true, think_level: 'high' } },
];

export function thinkChoiceKey(c: ThinkChoice): string {
  if (c.think === null) return 'default';
  if (!c.think) return 'off';
  return c.think_level ?? 'on';
}

/** Short label for a resolved (think, level) pair as seen on a run or a model call. */
export function describeThink(think: boolean | null, level: ThinkLevel | null): string | null {
  if (think === null) return null;
  if (!think) return 'think off';
  return level ? `think · ${level}` : 'think on';
}
