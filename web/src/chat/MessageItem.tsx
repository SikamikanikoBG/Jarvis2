import { Icon } from '../components/Icon';
import { Markdown } from '../components/Markdown';
import type { LocalMessage } from '../store/state';
import { ReasoningFold } from './ReasoningFold';

export function MessageItem({ message }: { message: LocalMessage }) {
  if (message.role === 'user') {
    return (
      <div className={`msg msg-user${message.optimistic ? ' optimistic' : ''}`} aria-label="You">
        {message.content}
      </div>
    );
  }
  if (!message.content && !message.reasoning) return null; // pure tool-call turn: the cards say it all
  if (!message.content && message.reasoning) return <ReasoningFold text={message.reasoning} />; // thought, then called tools
  return (
    <div className="msg msg-bot" aria-label="Jarvis">
      {message.reasoning && <ReasoningFold text={message.reasoning} />}
      <Markdown text={message.content} />
      {message.partial && (
        <div className="msg-meta">
          <span className="partial-mark">
            <Icon name="square" size={11} />
            partial — stopped before the reply finished
          </span>
        </div>
      )}
    </div>
  );
}
