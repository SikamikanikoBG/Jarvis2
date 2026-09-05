import { useMemo } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { selectActiveRun } from '../store/selectors';
import { NEW_CONVERSATION_KEY } from '../store/state';
import { useStore } from '../store/store';
import { buildTranscriptFrom } from '../store/transcript';
import { Composer } from './Composer';
import { Transcript } from './Transcript';

export function ChatScreen() {
  const convKey = useStore((s) => s.openConversationId ?? NEW_CONVERSATION_KEY);
  const assistantName = 'Jarvis';
  const src = useStore(
    useShallow((s) => ({
      messages: s.messages[convKey] ?? EMPTY_MESSAGES,
      runIds: s.runsByConversation[convKey] ?? EMPTY_IDS,
      runs: s.runs,
      runEvents: s.runEvents,
      streamKeys: Object.keys(s.streams)
        .filter((k) => s.streams[k])
        .join(','),
    })),
  );
  const items = useMemo(
    () =>
      buildTranscriptFrom({
        messages: src.messages,
        runIds: src.runIds,
        runs: src.runs,
        runEvents: src.runEvents,
        streamRunIds: new Set(src.streamKeys ? src.streamKeys.split(',') : []),
      }),
    [src],
  );
  const activeRun = useStore((s) => selectActiveRun(s, s.openConversationId));
  const stopping = useStore((s) => (activeRun ? s.cancelRequested[activeRun.id] === true : false));
  const loading = useStore((s) => s.loadingMessagesFor === convKey && !s.messages[convKey]);
  const connection = useStore((s) => s.connection);

  return (
    <section className="chat" aria-label="Chat">
      {items.length === 0 ? (
        <div className="chat-empty">
          <div className="empty">
            {loading ? (
              <span>Loading…</span>
            ) : (
              <>
                <strong>{convKey === NEW_CONVERSATION_KEY ? `Talk to ${assistantName}` : 'Nothing here yet'}</strong>
                <span>{connection === 'open' ? 'Type below. Replies stream in as they are written.' : 'Waiting for the connection to the core.'}</span>
              </>
            )}
          </div>
        </div>
      ) : (
        <Transcript key={convKey} items={items} />
      )}
      <Composer runActive={activeRun !== null} stopping={stopping} />
    </section>
  );
}

const EMPTY_MESSAGES: never[] = [];
const EMPTY_IDS: never[] = [];
