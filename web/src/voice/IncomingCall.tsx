import { Icon } from '../components/Icon';
import { useStore } from '../store/store';

/**
 * Jarvis ringing (docs/stories/11_call_me.md): the screen a ring opens on when it cannot go
 * straight into the call - a ring by link (Discord), or a phone where the page may not start the
 * microphone or speak without a tap of its own, so answering here is that tap. Answer on the
 * notification skips it wherever the page may sound by itself. Answer opens the call
 * (CallScreen) and it says what the task came to; Decline leaves the result in the chat.
 */
export function IncomingCall() {
  const ring = useStore((s) => s.incoming);
  const chat = useStore((s) => (s.incoming?.conversationId ? s.conversations[s.incoming.conversationId]?.title : undefined));
  const answer = useStore((s) => s.answerRing);
  const decline = useStore((s) => s.declineRing);
  if (!ring) return null;
  return (
    <div className="call call-incoming" role="dialog" aria-label="Jarvis is calling" aria-modal="true">
      <div className="call-head">
        <span className="call-title">{chat ?? 'Jarvis'}</span>
      </div>
      <div className="call-ring-wrap" aria-hidden="true">
        <div className="call-ring call-ring-outer" />
        <div className="call-ring call-ring-inner">
          <Icon name="phone" size={34} />
        </div>
      </div>
      <div className="call-phase">Jarvis is calling</div>
      {ring.say && (
        <div className="call-captions">
          <p className="call-saying">{ring.say}</p>
        </div>
      )}
      <div className="call-controls">
        <button type="button" className="call-btn call-btn-danger" onClick={decline} aria-label="Decline">
          <Icon name="phoneOff" size={24} />
        </button>
        <button type="button" className="call-btn call-btn-answer" onClick={() => void answer()} aria-label="Answer">
          <Icon name="phone" size={24} />
        </button>
      </div>
    </div>
  );
}
