import type { Conversation } from '../protocol/types';

/**
 * Two ways a chat can be private, one field each on the conversation:
 *
 * - **Incognito** (`incognito`): nothing from it is remembered anywhere else — no knowledge is
 *   learned, the title is never made from its words, search never returns it, the sidebar shows
 *   no preview, and the model has no notes or knowledge tools in it. Decided when the chat is
 *   opened and never switched on later. Always disappears too.
 * - **Disappearing** (`ttl_seconds`): an ordinary chat the core deletes once it has sat idle for
 *   that long. `expires_at` says when; every message pushes it out again.
 */

/** The idle times a disappearing chat may be given — the core refuses anything else. */
export const TTL_CHOICES: readonly { seconds: number; label: string }[] = [
  { seconds: 3_600, label: '1 hour' },
  { seconds: 86_400, label: '1 day' },
  { seconds: 604_800, label: '1 week' },
];

/** What an incognito chat gets when no idle time is picked (mirrors the core). */
export const INCOGNITO_DEFAULT_TTL = 3_600;

/** How the NEXT new chat should open. Session-only; reset once that chat exists. */
export interface DraftPrivacy {
  incognito: boolean;
  ttlSeconds: number | null;
}

export const DRAFT_NORMAL: DraftPrivacy = { incognito: false, ttlSeconds: null };

export type PrivacyKind = 'normal' | 'disappearing' | 'incognito';

export function privacyOf(c: Pick<Conversation, 'incognito' | 'ttl_seconds'>): PrivacyKind {
  if (c.incognito) return 'incognito';
  return c.ttl_seconds !== null ? 'disappearing' : 'normal';
}

/** "1 hour" / "1 day" / "1 week" for the offered values; anything else in plain hours or days. */
export function ttlLabel(seconds: number | null): string {
  if (seconds === null) return 'never';
  const known = TTL_CHOICES.find((c) => c.seconds === seconds);
  if (known) return known.label;
  if (seconds < 3_600) return `${Math.max(1, Math.round(seconds / 60))} min`;
  if (seconds < 86_400) return `${Math.round(seconds / 3_600)} h`;
  return `${Math.round(seconds / 86_400)} d`;
}

/**
 * How long until `expiresAt`, for the sidebar and the composer chip: "58 min", "3 h", "2 d" —
 * or "any moment" once the time has passed and the sweep (once a minute) has not come yet.
 */
export function timeLeft(expiresAt: string, now = Date.now()): string {
  const ms = new Date(expiresAt).getTime() - now;
  if (Number.isNaN(ms)) return '';
  if (ms <= 0) return 'any moment';
  const min = Math.ceil(ms / 60_000);
  if (min < 60) return `${min} min`;
  const h = Math.round(ms / 3_600_000);
  if (h < 48) return `${h} h`;
  return `${Math.round(ms / 86_400_000)} d`;
}
