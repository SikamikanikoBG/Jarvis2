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
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-label={`${tasks.length} open task${tasks.length === 1 ? '' : 's'}`}
      >
        Tasks · {tasks.length}
      </button>
      {open && (
        <ul className="tasks-pop" role="menu">
          {tasks.map((t) => (
            <li key={t.id} role="none">
              <button type="button" role="menuitem" onClick={() => show(t.id)}>
                <span className="truncate">{t.title}</span>
                <span className="muted">{t.paused ? 'paused for your call' : TASK_STATE_LABEL[t.state]}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
