import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react';
import { Icon, type IconName } from '../components/Icon';
import { IconButton, Menu, type MenuItem } from '../components/primitives';
import { useTicker } from '../components/useTicker';
import { cameraSupport, readCameraEnv } from '../lib/camera';
import { TTL_CHOICES, privacyOf, timeLeft, ttlLabel, type PrivacyKind } from '../lib/privacy';
import { THINK_CHOICES, THINK_DEFAULT, thinkChoiceKey } from '../lib/think';
import { selectSendRefusal } from '../store/selectors';
import { NEW_CONVERSATION_KEY } from '../store/state';
import { useStore } from '../store/store';
import { PendingAttachments } from './Attachments';
import { CameraDialog } from './CameraDialog';
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
  const [cameraOpen, setCameraOpen] = useState(false);
  const notify = useStore((s) => s.notify);
  // Fixed for the life of the composer: whether this device can show a live preview does not
  // change while Arsen is typing.
  const camera = useMemo(() => cameraSupport(readCameraEnv()), []);
  const refusal = useStore((s) =>
    selectSendRefusal(s, {
      connection: s.connection,
      hasText: text.trim().length > 0 || s.pendingAttachments.length > 0,
      hasAttachments: s.pendingAttachments.length > 0,
      conversationId: s.openConversationId,
    }),
  );

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

  // The box is cleared only once the message is actually on its way. It used to clear
  // unconditionally, so a send refused for being offline (Enter bypasses the disabled button)
  // or for a run still in flight threw away what Arsen had just typed.
  const submit = () => {
    if (!text.trim() && pending.length === 0) return;
    if (editing) void sendEdit(text).then((sent) => sent && setText(''));
    else if (send(text)) setText('');
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

  // The same rule the store enforces, so the button and the Enter key agree about what is sendable.
  const canSend = refusal === null;
  const pick = (input: HTMLInputElement | null) => {
    setAttachMenu(null);
    input?.click();
  };
  // Taking a photo is offered on every device. Where a live preview is possible it opens in the
  // app; on a phone without one (the core is served over plain http on the tailnet, and
  // getUserMedia needs a secure context) the OS camera app still works through the file input.
  const takePhoto = () => {
    setAttachMenu(null);
    if (camera.mode === 'stream') setCameraOpen(true);
    else if (camera.mode === 'os') cameraInput.current?.click();
    else notify(camera.reason, 'error');
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
      {cameraOpen && (
        <CameraDialog
          onClose={() => setCameraOpen(false)}
          onCapture={(file) => {
            setCameraOpen(false);
            void attachFiles([file]);
          }}
        />
      )}
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
          // A run has already assembled its context, so a picture cannot join it mid-flight.
          disabled={runActive}
          onClick={(e) => setAttachMenu(attachMenu ? null : e.currentTarget)}
        />
        {attachMenu && (
          <Menu
            anchor={attachMenu}
            onClose={() => setAttachMenu(null)}
            items={[
              { label: 'Take a photo', icon: 'camera', onSelect: takePhoto },
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
          placeholder={runActive ? 'Jarvis is working — say more and he will read it' : 'Message Jarvis'}
          aria-label="Message"
          autoComplete="off"
          enterKeyHint={coarsePointer() ? 'enter' : 'send'}
        />
        <MicButton disabled={false} onTranscript={(t) => setText((prev) => (prev.trim() ? `${prev.trimEnd()} ${t}` : t))} />
        {/* While a run works BOTH are offered: say something more, or stop it. Only hiding Send
            behind Stop is what made "it is going the wrong way" mean "wait until it finishes". */}
        {runActive && (
          <button type="button" className="send-btn stop-btn" onClick={stop} aria-label="Stop" title="Stop" disabled={stopping}>
            <Icon name="stop" size={18} />
          </button>
        )}
        {(!runActive || text.trim().length > 0) && (
          <button
            type="submit"
            className="send-btn"
            aria-label={runActive ? 'Send this to Jarvis while he works' : 'Send'}
            title={runActive ? 'Send this to Jarvis while he works' : 'Send'}
            disabled={!canSend}
          >
            <Icon name="send" size={18} />
          </button>
        )}
      </form>
      <div className="composer-hint" aria-live="polite">
        <span className="row">
          <ThinkChip />
          <PrivacyChip />
          <span>{connection === 'open' ? '' : connection === 'connecting' ? 'Connecting…' : 'Reconnecting…'}</span>
        </span>
        <span className="desktop-only">
          {runActive
            ? stopping
              ? 'Stopping…'
              : 'Enter sends this to Jarvis while he works'
            : 'Enter to send · Shift+Enter for a new line'}
        </span>
      </div>
    </div>
  );
}

/**
 * What kind of chat this is — or, before the first message, what kind it will be.
 *
 * Two independent things, one chip: incognito (nothing remembered) and a timer (gone after an
 * hour / a day / a week of quiet). On a draft both are picked here and travel with the first
 * message; on an existing chat the chip shows the state and changes the timer. Incognito cannot
 * be switched on later, because what an ordinary chat has already taught the knowledge graph
 * cannot be un-learned.
 */
function PrivacyChip() {
  const openId = useStore((s) => s.openConversationId);
  const conv = useStore((s) => (s.openConversationId ? s.conversations[s.openConversationId] : undefined));
  const draft = useStore((s) => s.draftPrivacy);
  const setDraft = useStore((s) => s.setDraftPrivacy);
  const setTtl = useStore((s) => s.setConversationTtl);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const now = useTicker(30_000, Boolean(conv?.expires_at));
  if (openId && !conv) return null;

  const kind: PrivacyKind = conv ? privacyOf(conv) : draft.incognito ? 'incognito' : draft.ttlSeconds !== null ? 'disappearing' : 'normal';
  const ttl = conv ? conv.ttl_seconds : draft.ttlSeconds;
  const left = conv?.expires_at ? timeLeft(conv.expires_at, now) : null;
  // "gone in 58 min" on a live chat, "after 1 day" on a draft that will have a timer.
  const timer = ttl === null ? null : left ? `gone in ${left}` : `after ${ttlLabel(ttl)}`;
  const label = kind === 'incognito' ? (timer ? `Incognito · ${timer}` : 'Incognito') : timer ? `Disappears ${timer}` : 'Kept';
  const icon: IconName = kind === 'incognito' ? 'incognito' : kind === 'disappearing' ? 'hourglass' : 'clock';
  const title = conv
    ? kind === 'incognito'
      ? 'Nothing from this chat is remembered anywhere else. Click to change how long it stays.'
      : 'How long this chat stays. Click to make it disappear after a quiet period.'
    : 'How the new chat behaves: kept, incognito (remembers nothing), or gone after a quiet period.';
  // `icon` is optional-and-never-undefined (exactOptionalPropertyTypes), so it is spread in.
  const check = (on: boolean, fallback?: IconName): Pick<MenuItem, 'icon'> => (on ? { icon: 'check' } : fallback ? { icon: fallback } : {});

  const items: MenuItem[] = conv
    ? [
        { label: 'Keep this chat', ...check(ttl === null), onSelect: () => void setTtl(conv.id, null) },
        ...TTL_CHOICES.map(
          (t): MenuItem => ({
            label: `Disappear after ${t.label} of quiet`,
            ...check(ttl === t.seconds, 'hourglass'),
            onSelect: () => void setTtl(conv.id, t.seconds),
          }),
        ),
        ...(conv.incognito
          ? []
          : [{ label: 'Incognito is only for a new chat', icon: 'incognito' as const, disabled: true, onSelect: () => undefined }]),
      ]
    : [
        // A toggle, then a timer: the two can be combined.
        {
          label: draft.incognito ? 'Incognito — remembers nothing ✓' : 'Incognito — remembers nothing',
          icon: 'incognito',
          keepOpen: true,
          onSelect: () => setDraft({ ...draft, incognito: !draft.incognito }),
        },
        { label: 'Kept until deleted', ...check(ttl === null), onSelect: () => setDraft({ ...draft, ttlSeconds: null }) },
        ...TTL_CHOICES.map(
          (t): MenuItem => ({
            label: `Disappears after ${t.label} of quiet`,
            ...check(ttl === t.seconds, 'hourglass'),
            onSelect: () => setDraft({ ...draft, ttlSeconds: t.seconds }),
          }),
        ),
      ];
  const tone = kind === 'incognito' ? ' chip-warn' : kind === 'disappearing' ? ' chip-accent' : '';
  return (
    <>
      <button
        type="button"
        className={`chip think-chip privacy-chip${tone}`}
        onClick={(e) => setAnchor(anchor ? null : e.currentTarget)}
        aria-haspopup="menu"
        aria-expanded={Boolean(anchor)}
        title={title}
      >
        <Icon name={icon} size={12} />
        {label}
        <Icon name="chevronDown" size={11} />
      </button>
      {anchor && <Menu anchor={anchor} onClose={() => setAnchor(null)} items={items} />}
    </>
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
