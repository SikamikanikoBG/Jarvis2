import { useCallback, useEffect, useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react';
import { Icon } from '../components/Icon';
import { IconButton, Menu } from '../components/primitives';
import { THINK_CHOICES, THINK_DEFAULT, thinkChoiceKey } from '../lib/think';
import { NEW_CONVERSATION_KEY } from '../store/state';
import { useStore } from '../store/store';
import { PendingAttachments } from './Attachments';
import { MicButton } from './MicButton';

/** Pasted text longer than this becomes an attachment instead of filling the input. */
const PASTE_AS_FILE_CHARS = 2000;

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
  const pending = useStore((s) => s.pendingAttachments);
  const uploading = useStore((s) => s.uploadingAttachments);
  const attachFiles = useStore((s) => s.attachFiles);
  const attachText = useStore((s) => s.attachText);
  const removeAttachment = useStore((s) => s.removeAttachment);
  const fileInput = useRef<HTMLInputElement>(null);
  const cameraInput = useRef<HTMLInputElement>(null);
  const [attachMenu, setAttachMenu] = useState<HTMLElement | null>(null);

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
    if (runActive || (!text.trim() && pending.length === 0)) return;
    if (editing) void sendEdit(text);
    else send(text);
    setText('');
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const items: DataTransferItem[] = Array.from(e.clipboardData?.items ?? []);
    const files = items
      .filter((i) => i.kind === 'file')
      .map((i) => i.getAsFile())
      .filter((f): f is File => f !== null);
    if (files.length > 0) {
      e.preventDefault(); // a screenshot from the clipboard is an attachment, not text
      void attachFiles(files);
      return;
    }
    const pasted = e.clipboardData?.getData('text') ?? '';
    if (pasted.length > PASTE_AS_FILE_CHARS) {
      e.preventDefault();
      void attachText(pasted, `pasted ${Math.round(pasted.length / 1024)} kB`);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !coarsePointer() && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const canSend = (text.trim().length > 0 || pending.length > 0) && connection === 'open';
  const pick = (input: HTMLInputElement | null) => {
    setAttachMenu(null);
    input?.click();
  };
  return (
    <div
      className="composer-wrap"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        const files = [...e.dataTransfer.files];
        if (files.length) {
          e.preventDefault();
          void attachFiles(files);
        }
      }}
    >
      <input
        ref={fileInput}
        type="file"
        multiple
        hidden
        onChange={(e) => {
          void attachFiles([...(e.target.files ?? [])]);
          e.target.value = '';
        }}
      />
      <input
        ref={cameraInput}
        type="file"
        accept="image/*"
        capture="environment"
        hidden
        onChange={(e) => {
          void attachFiles([...(e.target.files ?? [])]);
          e.target.value = '';
        }}
      />
      <PendingAttachments pending={pending} uploading={uploading} onRemove={removeAttachment} />
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
        <IconButton
          icon="paperclip"
          label="Attach a photo or a file"
          className="attach-btn"
          aria-haspopup="menu"
          aria-expanded={Boolean(attachMenu)}
          disabled={runActive}
          onClick={(e) => setAttachMenu(attachMenu ? null : e.currentTarget)}
        />
        {attachMenu && (
          <Menu
            anchor={attachMenu}
            onClose={() => setAttachMenu(null)}
            items={[
              ...(coarsePointer() ? [{ label: 'Take a photo', icon: 'camera' as const, onSelect: () => pick(cameraInput.current) }] : []),
              { label: 'Photo or file', icon: 'image', onSelect: () => pick(fileInput.current) },
            ]}
          />
        )}
        <textarea
          ref={ref}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          onPaste={onPaste}
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
