import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Icon } from '../components/Icon';
import { Menu } from '../components/primitives';
import { THINK_CHOICES, THINK_DEFAULT, thinkChoiceKey } from '../lib/think';
import { NEW_CONVERSATION_KEY } from '../store/state';
import { useStore } from '../store/store';
import { MicButton } from './MicButton';

interface Props {
  runActive: boolean;
  stopping: boolean;
}

const coarsePointer = () => window.matchMedia('(pointer: coarse)').matches;

/** Grows with its content; Enter sends on desktop (Shift+Enter = newline); Stop replaces Send during a run. */
export function Composer({ runActive, stopping }: Props) {
  const send = useStore((s) => s.send);
  const stop = useStore((s) => s.stop);
  const connection = useStore((s) => s.connection);
  const editing = useStore((s) => (s.editing?.conversationId === s.openConversationId ? s.editing : null));
  const cancelEdit = useStore((s) => s.cancelEdit);
  const sendEdit = useStore((s) => s.sendEdit);
  const [text, setText] = useState('');
  const ref = useRef<HTMLTextAreaElement>(null);

  const resize = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = '0px';
    el.style.height = `${Math.min(el.scrollHeight, window.innerHeight * 0.4)}px`;
  }, []);

  useEffect(resize, [text, resize]);
  useEffect(() => {
    if (!runActive && !coarsePointer()) ref.current?.focus();
  }, [runActive]);
  // "Edit and resend" hands the original text over as an event (store.startEdit); the shortcut
  // Shift+Esc focuses the box the same way.
  useEffect(() => {
    const focus = () => ref.current?.focus();
    const compose = (e: Event) => {
      setText((e as CustomEvent<string>).detail ?? '');
      ref.current?.focus();
    };
    window.addEventListener('jarvis:focus-composer', focus);
    window.addEventListener('jarvis:compose', compose);
    return () => {
      window.removeEventListener('jarvis:focus-composer', focus);
      window.removeEventListener('jarvis:compose', compose);
    };
  }, []);

  const submit = () => {
    if (runActive || !text.trim()) return;
    if (editing) void sendEdit(text);
    else send(text);
    setText('');
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !coarsePointer() && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const canSend = text.trim().length > 0 && connection === 'open';
  return (
    <div className="composer-wrap">
      {editing && (
        <div className="edit-banner" role="status">
          <Icon name="edit" size={13} />
          <span>Editing — sending forks this chat from here and continues in the fork.</span>
          <button
            type="button"
            className="btn btn-sm btn-secondary"
            onClick={() => {
              cancelEdit();
              setText('');
            }}
          >
            Cancel
          </button>
        </div>
      )}
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <textarea
          ref={ref}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          placeholder={runActive ? 'Jarvis is working…' : 'Message Jarvis'}
          aria-label="Message"
          disabled={runActive}
          autoComplete="off"
          enterKeyHint={coarsePointer() ? 'enter' : 'send'}
        />
        <MicButton disabled={runActive} onTranscript={(t) => setText((prev) => (prev.trim() ? `${prev.trimEnd()} ${t}` : t))} />
        {runActive ? (
          <button type="button" className="send-btn stop-btn" onClick={stop} aria-label="Stop" title="Stop" disabled={stopping}>
            <Icon name="stop" size={18} />
          </button>
        ) : (
          <button type="submit" className="send-btn" aria-label="Send" title="Send" disabled={!canSend}>
            <Icon name="send" size={18} />
          </button>
        )}
      </form>
      <div className="composer-hint" aria-live="polite">
        <span className="row">
          <ThinkChip />
          <span>{connection === 'open' ? '' : connection === 'connecting' ? 'Connecting…' : 'Reconnecting…'}</span>
        </span>
        <span className="desktop-only">{runActive ? (stopping ? 'Stopping…' : '') : 'Enter to send · Shift+Enter for a new line'}</span>
      </div>
    </div>
  );
}

/** Per-conversation thinking override for the next messages: role default / off / on / on·level. */
function ThinkChip() {
  const choice = useStore((s) => s.thinkChoice[s.openConversationId ?? NEW_CONVERSATION_KEY] ?? THINK_DEFAULT);
  const setChoice = useStore((s) => s.setThinkChoice);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const current = THINK_CHOICES.find((c) => c.key === thinkChoiceKey(choice)) ?? THINK_CHOICES[0];
  return (
    <>
      <button
        type="button"
        className={`chip think-chip${choice.think === null ? '' : ' chip-accent'}`}
        onClick={(e) => setAnchor(anchor ? null : e.currentTarget)}
        aria-haspopup="menu"
        aria-expanded={Boolean(anchor)}
        title="Thinking for the next message"
      >
        <Icon name="brain" size={12} />
        Thinking: {current?.label}
        <Icon name="chevronDown" size={11} />
      </button>
      {anchor && (
        <Menu
          anchor={anchor}
          onClose={() => setAnchor(null)}
          items={THINK_CHOICES.map((c) => ({
            label: c.label,
            ...(c.key === current?.key ? { icon: 'check' as const } : {}),
            onSelect: () => setChoice(c.choice),
          }))}
        />
      )}
    </>
  );
}
