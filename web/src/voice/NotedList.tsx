import { useShallow } from 'zustand/react/shallow';
import { useStore } from '../store/store';

/**
 * What Jarvis has noted on this call to do after it (docs/stories/13_delegated_tasks.md), live,
 * in his own words as they were heard: a misheard name or date is seen here, while he can still
 * say "no, the 16th". This list is the confirmation; the red button only hangs up.
 */
export function NotedList() {
  const noted = useStore(
    useShallow((s) => (s.callId ? Object.values(s.tasks).filter((t) => t.call_id === s.callId && t.state === 'noted') : [])),
  );
  if (noted.length === 0) return null;
  const sorted = [...noted].sort((a, b) => a.created_at.localeCompare(b.created_at));
  return (
    <section className="call-noted" aria-label="Noted for after the call">
      <h5>
        Noted for after the call · {sorted.length}
      </h5>
      <ol>
        {sorted.map((t) => (
          <li key={t.id}>
            <span className="call-noted-title">{t.title}</span>
            {t.brief.asked.length > 0 && <span className="call-noted-words">“{t.brief.asked.join(' / ')}”</span>}
          </li>
        ))}
      </ol>
    </section>
  );
}
