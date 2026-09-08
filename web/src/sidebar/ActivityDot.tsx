import type { ConversationActivity } from '../protocol/types';

const LABELS: Record<Exclude<ConversationActivity, 'idle'>, string> = {
  running: 'Working on it',
  waiting: 'Waiting for you',
};

/**
 * The sidebar's "this chat is alive" mark, next to the unread dot it was inspired by.
 *
 * Deliberately not the same shape as unread: unread is a solid cyan dot meaning *something
 * happened*, this is a ring that breathes (green) or a steady amber one (a run parked on a
 * confirmation, which will not move until Arsen answers). Nothing is rendered when a chat is
 * idle, so a quiet list looks exactly as it did before.
 */
export function ActivityDot({ activity }: { activity: ConversationActivity }) {
  if (activity === 'idle') return null;
  return <span className={`activity-dot ${activity}`} role="img" aria-label={LABELS[activity]} title={LABELS[activity]} />;
}

/**
 * The same mark on a folder head, with the count of live chats behind it — so a folder Arsen
 * has collapsed still tells him something inside it is moving. One chat waiting on him colours
 * the whole head amber: that is the state worth walking over for.
 */
export function ActivityCount({ running, waiting }: { running: number; waiting: number }) {
  const total = running + waiting;
  if (total <= 0) return null;
  const label = waiting > 0 ? `${total} needing you or working` : `${total} working`;
  return (
    <span className="activity-count" title={label} aria-label={label}>
      <span className={`activity-dot ${waiting > 0 ? 'waiting' : 'running'}`} aria-hidden="true" />
      {total > 1 && <span className={waiting > 0 ? 'activity-n waiting' : 'activity-n'}>{total}</span>}
    </span>
  );
}
