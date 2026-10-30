import { useEffect, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { selectOpenTasks } from '../store/selectors';
import { useStore } from '../store/store';
import { TASK_STATE_LABEL } from '../lib/tasks';

/**
 * "Tasks · N" in the header of a chat with tasks still going (docs/stories/13_delegated_tasks.md).
 * Hidden at none. A tap lists them; picking one scrolls to its card.
 */
export function TasksChip() {
  const tasks = useStore(useShallow((s) => selectOpenTasks(s.tasks, s.openConversationId)));
  const [open, setOpen] = useState(false);
  // Where the list opens, from the chip's own place on screen: fixed to the viewport, so neither
  // the desktop nav beside the title nor the phone's left edge can hide it (alpha F-11).
  const [at, setAt] = useState<{ top: number; left: number } | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  if (tasks.length === 0) return null;
  const waiting = tasks.some((t) => t.state === 'waiting' || t.state === 'needs_input');

  const show = (id: string) => {
    setOpen(false);
    const el = document.getElementById(`task-${id}`);
    el?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    el?.classList.add('msg-flash');
    setTimeout(() => el?.classList.remove('msg-flash'), 1800);
  };

  return (
    <div className="tasks-chip-wrap" ref={ref}>
      <button
        type="button"
        className={`tasks-chip${waiting ? ' tasks-chip-wait' : ''}`}
        onClick={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const width = Math.min(320, window.innerWidth - 32);
          setAt({ top: r.bottom + 6, left: Math.max(16, Math.min(r.left, window.innerWidth - width - 16)) });
          setOpen((o) => !o);
        }}
        aria-expanded={open}
        aria-label={`${tasks.length} open task${tasks.length === 1 ? '' : 's'}`}
      >
        Tasks · {tasks.length}
      </button>
      {open && (
        <ul className="tasks-pop" role="menu" style={at ? { top: at.top, left: at.left } : undefined}>
          {tasks.map((t) => (
            <li key={t.id} role="none">
              <button type="button" role="menuitem" onClick={() => show(t.id)}>
                <span className="truncate">{t.title}</span>
                <span className="muted">{t.paused ? 'paused' : TASK_STATE_LABEL[t.state]}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
