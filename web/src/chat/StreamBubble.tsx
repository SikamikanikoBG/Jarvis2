import { Markdown } from '../components/Markdown';
import { useStore } from '../store/store';
import { ReasoningFold } from './ReasoningFold';

/** The live assistant bubble for an active run. Exists from `run.queued` so nothing shifts. */
export function StreamBubble({ runId }: { runId: string }) {
  const stream = useStore((s) => s.streams[runId]);
  const status = useStore((s) => s.runs[runId]?.status);
  const stopping = useStore((s) => s.cancelRequested[runId] === true);

  const text = stream?.text ?? '';
  const reasoning = stream?.reasoning ?? '';
  const reasoningLive = Boolean(stream && stream.reasoningStartedAt !== null && stream.reasoningEndedAt === null && stream.modelActive);

  let activity: string | null = null;
  if (!text) {
    if (stopping) activity = 'Stopping';
    else if (status === 'queued') activity = 'Queued';
    else if (status === 'waiting_user') activity = 'Waiting for your decision';
    else if (stream?.lastEventType === 'tool.call') activity = stream.lastToolName ? `Running ${stream.lastToolName}` : 'Running a tool';
    else if (stream?.lastEventType === 'tool.result') activity = 'Reading the result';
    else if (reasoningLive) activity = null;
    else if (stream?.modelActive) activity = 'Thinking';
    else activity = 'Working';
  } else if (stopping) {
    activity = 'Stopping';
  }

  return (
    <div className={`msg msg-bot${stream?.modelActive && text ? ' streaming' : ''}`} aria-live="polite" aria-label="Jarvis, in progress">
      {(reasoning || reasoningLive) && (
        <ReasoningFold text={reasoning} live={reasoningLive} startedAt={stream?.reasoningStartedAt ?? null} endedAt={stream?.reasoningEndedAt ?? null} />
      )}
      {text && <Markdown text={text} />}
      {activity && (
        <div className="activity">
          <span>{activity}</span>
          <span className="dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
        </div>
      )}
    </div>
  );
}
