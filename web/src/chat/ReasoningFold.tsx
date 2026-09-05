import { useState } from 'react';
import { Icon } from '../components/Icon';
import { useTicker } from '../components/useTicker';
import { formatSeconds } from '../lib/format';

interface Props {
  text: string;
  /** Live: reasoning still arriving. */
  live?: boolean;
  startedAt?: number | null;
  endedAt?: number | null;
}

/** Collapsed by default; shows "Thinking… 12s" while reasoning streams, then the elapsed time. */
export function ReasoningFold({ text, live = false, startedAt = null, endedAt = null }: Props) {
  const [open, setOpen] = useState(false);
  const now = useTicker(1000, live);
  const elapsed = startedAt !== null ? (endedAt ?? now) - startedAt : null;
  const label = live ? 'Thinking' : elapsed !== null ? 'Thought' : 'Reasoning';
  return (
    <details className="reasoning" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>
        <Icon name="chevronRight" size={14} className="chev" />
        <Icon name="brain" size={15} />
        <span>
          {label}
          {live && '…'}
        </span>
        {elapsed !== null && <span className="reasoning-time">{live ? formatSeconds(elapsed) : `for ${formatSeconds(elapsed)}`}</span>}
      </summary>
      {open && <div className="reasoning-body">{text || (live ? '…' : '')}</div>}
    </details>
  );
}
