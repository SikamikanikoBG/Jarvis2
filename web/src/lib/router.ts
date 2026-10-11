export type View = 'chat' | 'mail' | 'runs' | 'settings' | 'status' | 'boards' | 'knowledge' | 'skills' | 'schedules' | 'meetings' | 'triage' | 'office';

export const VIEWS: readonly View[] = ['chat', 'mail', 'runs', 'settings', 'status', 'boards', 'knowledge', 'skills', 'schedules', 'meetings', 'triage', 'office'];

export interface Route {
  view: View;
  conversationId: string | null;
}

const LAST_CONV_KEY = 'jarvis_last_conversation';

function isView(s: string | undefined): s is Exclude<View, 'chat'> {
  return s !== undefined && s !== 'chat' && (VIEWS as readonly string[]).includes(s);
}

export function parseLocation(pathname = window.location.pathname): Route {
  const parts = pathname.split('/').filter(Boolean);
  const head = parts[0];
  if (head === 'c' && parts[1]) return { view: 'chat', conversationId: decodeURIComponent(parts[1]) };
  if (isView(head)) return { view: head, conversationId: rememberedConversation() };
  return { view: 'chat', conversationId: null };
}

/** A ring from Jarvis that was answered from its notification (docs/stories/11_call_me.md). */
export interface IncomingRing {
  conversationId: string | null;
  /** The run that rang ("test" for a test ring). */
  runId: string;
  /** What the call says first; empty when the ring came by a link that does not carry it. */
  say: string;
}

/**
 * The ring in an address: `/c/<chat>?ring=<run>&say=...` (the notification's, or Discord's link).
 * Taken out of the address as it is read, so a reload does not ring again.
 */
export function takeRing(href = window.location.href): IncomingRing | null {
  const url = new URL(href, window.location.origin);
  const runId = url.searchParams.get('ring');
  if (!runId) return null;
  const say = url.searchParams.get('say') ?? '';
  url.searchParams.delete('ring');
  url.searchParams.delete('say');
  if (href === window.location.href) window.history.replaceState(null, '', url.pathname + url.search + url.hash);
  return { conversationId: parseLocation(url.pathname).conversationId, runId, say };
}

export function buildPath(view: View, conversationId: string | null): string {
  if (view === 'chat') return conversationId ? `/c/${encodeURIComponent(conversationId)}` : '/';
  return `/${view}`;
}

/** Keeps `?mode=panel` (and any other query) across navigations. */
export function navigate(view: View, conversationId: string | null, replace = false): void {
  const path = buildPath(view, conversationId) + window.location.search;
  if (path === window.location.pathname + window.location.search) return;
  if (replace) window.history.replaceState(null, '', path);
  else window.history.pushState(null, '', path);
}

export function rememberConversation(id: string | null): void {
  try {
    if (id) sessionStorage.setItem(LAST_CONV_KEY, id);
    else sessionStorage.removeItem(LAST_CONV_KEY);
  } catch {
    /* ignore */
  }
}

export function rememberedConversation(): string | null {
  try {
    return sessionStorage.getItem(LAST_CONV_KEY);
  } catch {
    return null;
  }
}
