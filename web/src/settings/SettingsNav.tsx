import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { Icon } from '../components/Icon';
import { IconButton } from '../components/primitives';
import { SETTINGS_INDEX } from './settingsIndex';

/**
 * A sticky table of contents for the settings screen, which is one long form now: a chip per
 * section — click to jump — and a search box that narrows the chips to whichever mention what
 * was typed, jumping to the best match on Enter. The same search-box shape as every other
 * screen's filter bar, so Ctrl/⌘+K focusing it (see FILTERED_VIEWS in shell/App.tsx) is already
 * a learned key.
 */
export function SettingsNav() {
  const [query, setQuery] = useState('');
  const [activeId, setActiveId] = useState<string>(SETTINGS_INDEX[0]?.id ?? '');
  const inputRef = useRef<HTMLInputElement>(null);
  const navRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const focus = () => {
      inputRef.current?.focus();
      inputRef.current?.select();
    };
    window.addEventListener('jarvis:focus-search', focus);
    return () => window.removeEventListener('jarvis:focus-search', focus);
  }, []);

  // A jumped-to heading must not land underneath this rail, and the rail's height is not a
  // constant: it wraps to two rows on a wide screen and to one scrolling row on a phone. So it
  // measures itself and `.settings-anchor` keeps clear of whatever it currently is.
  useEffect(() => {
    const el = navRef.current;
    if (!el) return;
    const root = document.documentElement;
    const publish = () => root.style.setProperty('--settings-nav-h', `${Math.round(el.getBoundingClientRect().height)}px`);
    publish();
    const ro = new ResizeObserver(publish);
    ro.observe(el);
    return () => {
      ro.disconnect();
      root.style.removeProperty('--settings-nav-h');
    };
  }, []);

  // Scroll-spy: the rail tracks whichever section is actually under the rail, not just the last
  // chip clicked. Measured against the rail's own bottom edge rather than an IntersectionObserver
  // band — a sticky header covers the top of the scrollport, so every band that reads "the top of
  // the page" is really reading the strip hidden behind the rail, and the mark went nowhere.
  useEffect(() => {
    const scroller = document.querySelector('.screen');
    const nav = navRef.current;
    if (!(scroller instanceof HTMLElement) || !nav) return;
    const targets = SETTINGS_INDEX.map((s) => document.getElementById(s.id)).filter((el): el is HTMLElement => el !== null);
    if (targets.length === 0) return;
    let frame = 0;
    const measure = () => {
      frame = 0;
      const line = nav.getBoundingClientRect().bottom + 8;
      // The last section whose head has passed under the rail is the one being read.
      let current = targets[0];
      for (const el of targets) {
        if (el.getBoundingClientRect().top <= line) current = el;
      }
      if (current) setActiveId(current.id);
    };
    const onScroll = () => {
      if (frame === 0) frame = requestAnimationFrame(measure);
    };
    measure();
    scroller.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      scroller.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
    };
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return SETTINGS_INDEX;
    // A section NAMED what was typed comes first, so Enter lands on "Voice" for "call" rather
    // than on the section that merely mentions "repeated call" in a hint.
    const byLabel = SETTINGS_INDEX.filter((s) => s.label.toLowerCase().includes(q));
    const byKeyword = SETTINGS_INDEX.filter((s) => !byLabel.includes(s) && s.keywords.some((k) => k.includes(q)));
    return [...byLabel, ...byKeyword];
  }, [query]);

  const jumpTo = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  const onSearchKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      setQuery('');
    } else if (e.key === 'Enter' && filtered[0]) {
      jumpTo(filtered[0].id);
    }
  };

  return (
    <div className="settings-nav" ref={navRef}>
      <div className="search-box">
        <Icon name="search" size={14} />
        <input
          ref={inputRef}
          className="search-input"
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onSearchKey}
          placeholder="Find a setting…"
          aria-label="Find a setting"
          autoComplete="off"
        />
        {query && <IconButton icon="x" label="Clear search" size="sm" onClick={() => setQuery('')} />}
      </div>
      <div className="settings-nav-rail" role="tablist" aria-label="Settings sections">
        {filtered.length === 0 ? (
          <span className="settings-nav-empty">Nothing here mentions that.</span>
        ) : (
          filtered.map((s) => (
            <button
              key={s.id}
              type="button"
              role="tab"
              aria-selected={activeId === s.id}
              className={`chip chip-toggle${activeId === s.id ? ' active' : ''}${s.group ? ' settings-nav-sub' : ''}`}
              onClick={() => jumpTo(s.id)}
              title={s.group ? `${s.group} › ${s.label}` : s.label}
            >
              {s.label}
            </button>
          ))
        )}
      </div>
    </div>
  );
}
