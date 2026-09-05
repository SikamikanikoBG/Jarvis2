import { Icon, type IconName } from '../components/Icon';
import type { NoteModel } from '../store/transcript';

const ICONS: Record<string, IconName> = {
  guard: 'shield',
  judge: 'gavel',
  plan: 'list',
  waiting: 'pause',
  failed: 'alert',
  stopped: 'square',
  interrupted: 'alert',
  resumed: 'refresh',
  budget: 'clock',
  summary: 'info',
};

/** Compact system note in the transcript: supervisor verdicts, guards, plan steps, outcomes. */
export function Note({ note }: { note: NoteModel }) {
  return (
    <div className={`note note-${note.level}`} role="note">
      <Icon name={ICONS[note.tag] ?? 'info'} size={15} />
      <div className="grow">
        <div className="note-title">{note.title}</div>
        {note.detail && <div className="note-detail">{note.detail}</div>}
        {note.plan && (
          <ol className="note-plan">
            {note.plan.steps.map((s, i) => (
              <li key={`${i}-${s.title}`} className={s.status}>
                {s.title}
                {s.note ? ` — ${s.note}` : ''}
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}
