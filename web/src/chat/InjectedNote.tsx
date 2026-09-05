import { useState } from 'react';
import { Icon, type IconName } from '../components/Icon';
import { injectedLabel } from '../lib/injected';

const ICONS: Readonly<Record<string, IconName>> = {
  context: 'book',
  plan: 'list',
  supervisor: 'gavel',
  summary: 'info',
  transcript: 'headphones',
  frame: 'monitor',
  triage: 'inbox',
};

/** Collapsed system-style note for a message the core injected as user-role input. */
export function InjectedNote({ name, text }: { name: string | null; text: string }) {
  const [open, setOpen] = useState(false);
  const label = injectedLabel(name);
  return (
    <details className="note note-injected" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary aria-label={`${label}, ${text.length} characters`}>
        <Icon name="chevronRight" size={12} className="chev" />
        <Icon name={(name !== null ? ICONS[name] : undefined) ?? 'info'} size={14} />
        <span className="note-title">{label}</span>
        <span className="muted">· {text.length.toLocaleString()} chars</span>
      </summary>
      {open && <pre className="injected-text">{text}</pre>}
    </details>
  );
}
