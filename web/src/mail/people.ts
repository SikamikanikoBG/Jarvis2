import type { MailItem } from '../protocol/types';

export interface Person {
  name: string;
  address: string;
}

/** Everyone on the thread - senders and every To / Cc - once each, in order of appearance. */
export function participants(items: MailItem[]): Person[] {
  const seen = new Map<string, Person>();
  const add = (name: string | undefined, address: string | undefined) => {
    const n = (name ?? '').trim();
    const a = (address ?? '').trim();
    const usable = a.includes('@') ? a : '';
    if (!n && !usable) return;
    const key = (usable || n).toLowerCase();
    const known = seen.get(key);
    if (!known) seen.set(key, { name: n && n !== usable ? n : '', address: usable });
    else if (!known.name && n && n !== usable) known.name = n;
  };
  for (const item of items) {
    // Exchange sends "" for a name it does not know: then the sender line is the name.
    add([item.from?.name, item.sender].find((s) => s), item.from?.address);
    if (item.recipients) {
      for (const r of item.recipients) add(r.name, r.address);
    } else {
      // An older host: the To / Cc lines carry names only.
      for (const n of [item.to, item.cc].flatMap((l) => (l ?? '').split(';'))) add(n, '');
    }
  }
  // The same person known by name only once and by address elsewhere: keep the one with both.
  const named = new Set([...seen.values()].filter((p) => p.address && p.name).map((p) => p.name.toLowerCase()));
  return [...seen.values()].filter((p) => p.address || !named.has(p.name.toLowerCase()));
}
