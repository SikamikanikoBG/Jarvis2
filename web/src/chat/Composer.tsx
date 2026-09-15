import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react';
import { Icon } from '../components/Icon';
import { IconButton, Menu } from '../components/primitives';
import { useTicker } from '../components/useTicker';
import { callSupported } from '../voice/support';
import { cameraSupport, readRecorderEnv, recorderSupport } from '../lib/camera';
import { nextTtl, timeLeft, ttlLabel } from '../lib/privacy';
import { THINK_CHOICES, THINK_DEFAULT, thinkChoiceKey } from '../lib/think';
import { selectIncognitoNow, selectSendRefusal } from '../store/selectors';
import { NEW_CONVERSATION_KEY } from '../store/state';
import { useStore } from '../store/store';
import { PendingAttachments } from './Attachments';
import { CameraDialog } from './CameraDialog';
import { RecorderDialog } from './RecorderDialog';
import { MicButton } from './MicButton';

/** Pasted text longer than this becomes an attachment instead of filling the input. */
const PASTE_AS_FILE_CHARS = 2000;

/** However long the message is, this much of the conversation stays on screen behind it. */
const TRANSCRIPT_FLOOR = 72;
/** One line: the box never collapses to nothing, even on a screen with no room at all. */
const MIN_COMPOSER_H = 24;

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
  const mediaInput = useRef<HTMLInputElement>(null);
  const cameraInput = useRef<HTMLInputElement>(null);
  const videoInput = useRef<HTMLInputElement>(null);
  const [attachMenu, setAttachMenu] = useState<HTMLElement | null>(null);
  const [cameraOpen, setCameraOpen] = useState(false);
  const [recorderOpen, setRecorderOpen] = useState(false);
  const notify = useStore((s) => s.notify);
  const incognito = useStore(selectIncognitoNow);
  // What this device can do does not change while Arsen is typing; what the chat allows does -
  // in an incognito chat the OS camera app is never used (it keeps what it shoots on the phone).
  const env = useMemo(() => readRecorderEnv(), []);
  const camera = useMemo(() => cameraSupport(env, { incognito }), [env, incognito]);
  const recorder = useMemo(() => recorderSupport(env, { incognito }), [env, incognito]);
  const refusal = useStore((s) =>
    selectSendRefusal(s, {
      connection: s.connection,
      hasText: text.trim().length > 0 || s.pendingAttachments.length > 0,
      hasAttachments: s.pendingAttachments.length > 0,
      conversationId: s.openConversationId,
    }),
  );

  /**
   * Grow the box to the text, but never past the room the chat column actually has.
   *
   * It used to stop at 40% of `window.innerHeight`, which is not the same thing on a phone: with
   * the keyboard up, two thirds of that viewport is keyboard, and 40% of the whole screen plus
   * the composer's own chrome is more than what is left between the top bar and the bottom nav.
   * The overflow was painted over by the nav — "on mobile when i write a new message the footer
   * menu overlaps the chat inbox". Worse, this only ran on a keystroke, so a box measured before
   * the keyboard opened kept a height the screen no longer had.
   */
  const resize = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = '0px';
    const viewport = window.visualViewport?.height ?? window.innerHeight;
    let room = viewport * 0.4;
    const chat = el.closest('.chat');
    const wrap = el.closest('.composer-wrap');
    if (chat instanceof HTMLElement && wrap instanceof HTMLElement) {
      // Measured with the box collapsed: everything the wrap holds besides the text itself —
      // padding, the send/mic row, the hint line under it.
      const chrome = wrap.offsetHeight - el.offsetHeight;
      room = Math.min(room, chat.clientHeight - chrome - TRANSCRIPT_FLOOR);
    }
    el.style.height = `${Math.max(MIN_COMPOSER_H, Math.min(el.scrollHeight, room))}px`;
  }, []);

  useEffect(resize, [text, resize]);
  // The keyboard opening is a viewport resize, not a keystroke, and it is the moment the room
  // changes most.
  useEffect(() => {
    const vv = window.visualViewport;
    window.addEventListener('resize', resize);
    vv?.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      vv?.removeEventListener('resize', resize);
    };
  }, [resize]);
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
  // Taking a photo is offered on every device: a phone always hands the job to its own camera
  // app (focus, zoom, the right lens, full resolution — see lib/camera.ts), a desktop opens the
  // in-app preview, since a desktop browser ignores `capture` and would just show a file dialog.
  const takePhoto = () => {
    setAttachMenu(null);
    if (camera.mode === 'stream') setCameraOpen(true);
    else if (camera.mode === 'os') cameraInput.current?.click();
    else notify(camera.reason, 'error');
  };
  // Recording: the camera app on a phone, we take the file - except in an incognito chat, where
  // the camera app would save the clip to the gallery, so the in-app recorder does it in memory.
  const recordVideo = () => {
    setAttachMenu(null);
    if (recorder.mode === 'stream') setRecorderOpen(true);
    else if (recorder.mode === 'os') videoInput.current?.click();
    else notify(recorder.reason, 'error');
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
        ref={mediaInput}
        type="file"
        accept="image/*,video/*"
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
      {/* The camera app again, in video mode: `capture` on a video input opens the recorder. */}
      <input
        ref={videoInput}
        type="file"
        accept="video/*"
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
      {recorderOpen && (
        <RecorderDialog
          onClose={() => setRecorderOpen(false)}
          onCapture={(file) => {
            setRecorderOpen(false);
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
              // Recording is offered where it means something: the camera app on a phone, the
              // in-app recorder in an incognito chat (with its reason when even that is off), and
              // not on a desktop, where the same input is the file dialog "Photo or video" already is.
              ...(recorder.mode !== 'unavailable' || recorder.reason
                ? [{ label: 'Record a video', icon: 'video' as const, onSelect: recordVideo }]
                : []),
              { label: 'Photo or video', icon: 'image', onSelect: () => pick(mediaInput.current) },
              { label: 'Document or file', icon: 'paperclip', onSelect: () => pick(fileInput.current) },
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
        <CallButton />
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
          <PrivacyToggles />
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
 * Two one-tap toggles, not a menu: Arsen flips these often. The eye marks the chat incognito
 * (nothing new from it is remembered) - chosen before the first message, and read-only once the
 * chat exists, since a chat that has already taught the graph cannot be made private after the
 * fact. The dotted bubble makes the chat disappear, on a draft or a live chat alike: off → 1
 * hour → 1 day → 1 week → 1 hour → …, one idle time per tap; the ⋯ menu is still how it goes
 * back off.
 */
function PrivacyToggles() {
  const openId = useStore((s) => s.openConversationId);
  const conv = useStore((s) => (s.openConversationId ? s.conversations[s.openConversationId] : undefined));
  const draft = useStore((s) => s.draftPrivacy);
  const setDraft = useStore((s) => s.setDraftPrivacy);
  const setTtl = useStore((s) => s.setConversationTtl);
  const notify = useStore((s) => s.notify);
  const now = useTicker(30_000, Boolean(conv?.expires_at));
  if (openId && !conv) return null;

  const incognito = conv ? conv.incognito : draft.incognito;
  const ttl = conv ? conv.ttl_seconds : draft.ttlSeconds;
  const left = conv?.expires_at ? timeLeft(conv.expires_at, now) : null;
  const eyeLabel = conv
    ? incognito
      ? 'Incognito: nothing new from this chat is remembered anywhere'
      : 'Incognito is chosen before the first message - start a new chat for that'
    : incognito
      ? 'Incognito on: nothing from this chat will be remembered. Tap to turn off.'
      : 'Make this chat incognito: nothing from it will be remembered';
  const timerLabel =
    ttl === null
      ? `Make this chat disappear after ${ttlLabel(nextTtl(null))} of quiet`
      : `Disappears ${left ? `in ${left}` : `after ${ttlLabel(ttl)} of quiet`}. Tap for ${ttlLabel(nextTtl(ttl))}; use the menu to keep it.`;
  const toggleEye = () => {
    if (conv) notify(eyeLabel);
    else setDraft({ ...draft, incognito: !draft.incognito });
  };
  const toggleTimer = () => {
    const next = nextTtl(ttl);
    if (conv) void setTtl(conv.id, next);
    else setDraft({ ...draft, ttlSeconds: next });
  };
  return (
    <span className="privacy-toggles">
      <IconButton
        icon="incognito"
        label={eyeLabel}
        size="sm"
        className={`privacy-toggle${incognito ? ' on-incognito' : ''}`}
        aria-pressed={incognito}
        onClick={toggleEye}
      />
      {incognito && <span className="privacy-state">Incognito</span>}
      <IconButton
        icon="chatDots"
        label={timerLabel}
        size="sm"
        className={`privacy-toggle${ttl !== null ? ' on-timer' : ''}`}
        aria-pressed={ttl !== null}
        onClick={toggleTimer}
      />
      {ttl !== null && <span className="privacy-state">{left ? `gone in ${left}` : `after ${ttlLabel(ttl)}`}</span>}
    </span>
  );
}

/**
 * The headset next to the microphone: the microphone dictates one message, the headset holds a
 * call (docs/stories/10_voice.md). Shown only where a call can happen at all — a microphone
 * and a voice — and started from the tap itself, which is the user gesture the phone's audio
 * needs.
 */
function CallButton() {
  const startCall = useStore((s) => s.startCall);
  const inCall = useStore((s) => s.call !== null);
  if (!callSupported()) return null;
  return (
    <button type="button" className="mic-btn call-start" onClick={() => void startCall()} disabled={inCall} aria-label="Talk with Jarvis" title="Talk with Jarvis">
      <Icon name="headphones" size={17} />
    </button>
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
