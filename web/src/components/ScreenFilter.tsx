import { useEffect, useRef } from 'react';
import { Icon } from './Icon';
import { IconButton } from './primitives';

export interface FilterToggle {
  label: string;
  active: boolean;
  onChange: (on: boolean) => void;
  /** How many rows the toggle would leave — worth knowing before pressing it. */
  count?: number;
  title?: string;
}

interface Props {
  query: string;
  onQuery: (q: string) => void;
  placeholder: string;
  /** "12 of 40" — only shown while the list is actually narrowed. */
  shown: number;
  total: number;
  /** What one row is called, for the count and the empty state ("schedule", "note"). */
  noun: string;
  toggles?: FilterToggle[];
}

/**
 * One filter bar for every screen that holds a list: schedules, meetings, skills, boards.
 *
 * The same shape and the same keys as the sidebar's search (Escape clears, Ctrl/⌘+K focuses the
 * first one on screen) so it is learned once. The filtering itself is the caller's — this only
 * owns the controls.
 */
export function ScreenFilter({ query, onQuery, placeholder, shown, total, noun, toggles = [] }: Props) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const focus = () => {
      ref.current?.focus();
      ref.current?.select();
    };
    window.addEventListener('jarvis:focus-search', focus);
    return () => window.removeEventListener('jarvis:focus-search', focus);
  }, []);

  const narrowed = shown !== total;
  return (
    <div className="screen-filter">
      <div className="search-box">
        <Icon name="search" size={14} />
        <input
          ref={ref}
          className="search-input"
          type="search"
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') onQuery('');
          }}
          placeholder={placeholder}
          aria-label={placeholder}
          autoComplete="off"
        />
        {query && <IconButton icon="x" label="Clear search" size="sm" onClick={() => onQuery('')} />}
      </div>
      {toggles.map((t) => (
        <button
          key={t.label}
          type="button"
          className={`chip chip-toggle${t.active ? ' active' : ''}`}
          aria-pressed={t.active}
          title={t.title}
          onClick={() => t.onChange(!t.active)}
        >
          {t.active && <Icon name="check" size={12} />}
          {t.label}
          {t.count !== undefined && <span className="chip-count">{t.count}</span>}
        </button>
      ))}
      <span className="screen-filter-count" aria-live="polite">
        {narrowed ? `${shown} of ${total}` : `${total} ${total === 1 ? noun : `${noun}s`}`}
      </span>
    </div>
  );
}
