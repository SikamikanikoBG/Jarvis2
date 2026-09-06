import { Icon } from '../components/Icon';
import { Markdown } from '../components/Markdown';
import { isInjectedUserMessage } from '../lib/injected';
import type { LocalMessage } from '../store/state';
import { InjectedNote } from './InjectedNote';
import { MessageActions } from './MessageActions';
import { ReasoningFold } from './ReasoningFold';

export function MessageItem({ message }: { message: LocalMessage }) {
  if (message.role === 'user') {
    // The transcript builder routes injected user-role messages to InjectedNote; this is a last line of defence.
    if (isInjectedUserMessage(message)) return <InjectedNote name={message.name} text={message.content} />;
    return (
      <div className={`msg msg-user has-actions${message.optimistic ? ' optimistic' : ''}`} aria-label="You">
        {message.content}
        <MessageActions message={message} />
      </div>
    );
  }
  if (!message.content && !message.reasoning) return null; // pure tool-call turn: the cards say it all
  if (!message.content && message.reasoning) return <ReasoningFold text={message.reasoning} />; // thought, then called tools
  return (
    <div className="msg msg-bot has-actions" aria-label="Jarvis">
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
      <MessageActions message={message} />
    </div>
  );
}
