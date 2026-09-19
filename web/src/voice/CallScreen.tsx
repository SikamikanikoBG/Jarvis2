import { useEffect, useRef, useState, type PointerEvent } from 'react';
import { Icon } from '../components/Icon';
import { useTicker } from '../components/useTicker';
import { formatSeconds } from '../lib/format';
import { useStore } from '../store/store';
import { useAtEar } from './earPose';
import type { CallPhase, CallState } from './session';
import { useWakeLock } from './useWakeLock';

/** How long a hold must last to act. Long enough that a cheek never does it, short enough not to feel stuck. */
const HOLD_MS = 1200;
/** A contact wider than this (CSS px) is a cheek or an ear, not a fingertip; it holds nothing. */
const FINGER_MAX_PX = 60;

const PHASE_LABEL: Record<CallPhase, string> = {
  idle: '',
  connecting: 'Connecting…',
  listening: 'Listening',
  transcribing: 'Heard you…',
  thinking: 'Thinking…',
  speaking: 'Speaking',
  ended: 'Call ended',
};

/**
 * The call, full screen (docs/stories/10_voice.md). A pure function of the session's state:
 * the ring says whether he is listening, thinking or speaking; the captions carry what was said,
 * both ways; nothing here starts a run or touches audio — the session does that.
 *
 * On a touch device the screen locks itself the moment the call starts: near-black, and every
 * touch swallowed except a deliberate hold. A web page cannot read the proximity sensor, so the
 * accelerometer stands in for it (earPose.ts): with the phone at an ear the screen goes fully
 * dark and *nothing* on it responds, not even a hold — the controls are not there to hold.
 * Pulled away to be looked at, it comes back to the locked screen. A wake lock keeps the page
 * alive, the black takes the light down, the guard takes the touches.
 */
export function CallScreen() {
  const call = useStore((s) => s.call);
  const endCall = useStore((s) => s.endCall);
  const setMuted = useStore((s) => s.setCallMuted);
  const setRoute = useStore((s) => s.setCallRoute);
  const conv = useStore((s) => (s.openConversationId ? s.conversations[s.openConversationId] : undefined));
  const touch = typeof window !== 'undefined' && window.matchMedia('(pointer: coarse)').matches;
  const [locked, setLocked] = useState(touch);
  const atEar = useAtEar(Boolean(call) && touch);
  const now = useTicker(1000, Boolean(call) && !atEar);
  useWakeLock(Boolean(call));

  // Escape hangs up on a keyboard; on a phone there is no Escape, and that is the point.
  useEffect(() => {
    if (!call) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') endCall();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [call, endCall]);

  if (!call) return null;
  const elapsed = call.startedAt ? formatSeconds(now - call.startedAt) : '0:00';

  // At the ear: black, and a guard over everything. No controls exist to be pressed.
  if (atEar) {
    return (
      <div className={`call call-locked call-dark call-${call.phase}`} role="dialog" aria-label="Call with Jarvis" aria-modal="true">
        <div className="call-guard" aria-hidden="true" />
      </div>
    );
  }

  return (
    <div className={`call${locked ? ' call-locked' : ''} call-${call.phase}`} role="dialog" aria-label="Call with Jarvis" aria-modal="true">
      <div className="call-head">
        <span className="call-title">{conv?.title ?? 'Jarvis'}</span>
        <span className="call-elapsed mono">{elapsed}</span>
      </div>

      <Ring phase={call.phase} level={call.level} muted={call.muted} />
      <div className="call-phase" aria-live="polite">
        {!call.online ? 'No connection — reconnecting…' : call.muted && call.phase === 'listening' ? 'Muted' : PHASE_LABEL[call.phase]}
      </div>

      <Captions call={call} />

      {call.problem && (
        <div className="call-problem" role="alert">
          <Icon name="alert" size={14} />
          <span>{call.problem}</span>
        </div>
      )}

      {locked ? (
        <div className="call-controls">
          <HoldButton label="Hold to unlock" icon="chevronDown" onHeld={() => setLocked(false)} />
          <HoldButton
            label={call.route === 'speaker' ? 'Hold for earpiece' : 'Hold for speaker'}
            icon={call.route === 'speaker' ? 'headphones' : 'speaker'}
            onHeld={() => setRoute(call.route === 'speaker' ? 'earpiece' : 'speaker')}
          />
          <HoldButton label="Hold to hang up" icon="phoneOff" danger onHeld={endCall} />
        </div>
      ) : (
        <div className="call-controls">
          <button type="button" className={`call-btn${call.muted ? ' on' : ''}`} onClick={() => setMuted(!call.muted)} aria-pressed={call.muted} aria-label={call.muted ? 'Unmute' : 'Mute'}>
            <Icon name={call.muted ? 'micOff' : 'mic'} size={22} />
          </button>
          <button
            type="button"
            className={`call-btn${call.route === 'speaker' ? ' on-accent' : ''}`}
            onClick={() => setRoute(call.route === 'speaker' ? 'earpiece' : 'speaker')}
            aria-pressed={call.route === 'speaker'}
            aria-label={call.route === 'speaker' ? 'Speaker on — switch to earpiece' : 'Switch to speaker'}
          >
            <Icon name="speaker" size={22} />
          </button>
          {touch ? (
            <HoldButton label="Hold to hang up" icon="phoneOff" danger onHeld={endCall} />
          ) : (
            <button type="button" className="call-btn call-btn-danger" onClick={endCall} aria-label="Hang up">
              <Icon name="phoneOff" size={22} />
            </button>
          )}
          {touch && (
            <button type="button" className="call-btn" onClick={() => setLocked(true)} aria-label="Lock the screen">
              <Icon name="shield" size={22} />
            </button>
          )}
        </div>
      )}
      {!locked && (
        <div className="call-hint">
          {call.route === 'speaker'
            ? 'Speaker: he cannot hear you while he talks — wait for him to finish.'
            : touch
              ? 'Earpiece (or your headset). Talk over him to cut in.'
              : 'Talk over him to cut in.'}
        </div>
      )}
      {/* The guard: under the controls, over everything else. Every touch on it goes nowhere. */}
      {locked && <div className="call-guard" aria-hidden="true" onTouchMove={(e) => e.preventDefault()} />}
    </div>
  );
}

function Ring({ phase, level, muted }: { phase: CallPhase; level: number; muted: boolean }) {
  // The ring breathes with the microphone while listening and pulses on its own while he speaks.
  const scale = phase === 'listening' && !muted ? 1 + Math.min(0.6, level * 1.2) : 1;
  return (
    <div className="call-ring-wrap" aria-hidden="true">
      <div className="call-ring call-ring-outer" style={{ transform: `scale(${scale})` }} />
      <div className="call-ring call-ring-inner">
        <Icon name={muted ? 'micOff' : phase === 'speaking' ? 'headphones' : phase === 'thinking' || phase === 'transcribing' ? 'brain' : 'mic'} size={34} />
      </div>
    </div>
  );
}

function Captions({ call }: { call: CallState }) {
  const said = call.saying;
  return (
    <div className="call-captions">
      {call.heard && (
        <p className="call-heard">
          <span className="call-who">You</span>
          {call.heard}
        </p>
      )}
      {said.length > 0 && (
        <p className="call-saying">
          <span className="call-who">Jarvis</span>
          {said.map((s, i) => (
            <span key={i} className={i < call.spokenUpTo ? 'said' : i === call.spokenUpTo ? 'saying' : 'unsaid'}>
              {s}{' '}
            </span>
          ))}
        </p>
      )}
    </div>
  );
}

/**
 * A control that acts only after a hold: a tap does nothing, a cheek does nothing. The fill
 * shows the hold progressing so a held finger knows it is being counted.
 */
function HoldButton({ label, icon, danger, onHeld }: { label: string; icon: 'chevronDown' | 'phoneOff' | 'speaker' | 'headphones'; danger?: boolean; onHeld: () => void }) {
  const [progress, setProgress] = useState(0);
  const timer = useRef<number | null>(null);
  const startedAt = useRef(0);
  const fired = useRef(false);

  const stop = () => {
    if (timer.current !== null) window.clearInterval(timer.current);
    timer.current = null;
    setProgress(0);
  };
  const begin = (e: PointerEvent<HTMLButtonElement>) => {
    // A cheek is wide and an ear comes with a cheek: only a single fingertip-sized contact holds.
    if (e.width > FINGER_MAX_PX || e.height > FINGER_MAX_PX) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    fired.current = false;
    startedAt.current = performance.now();
    stop();
    timer.current = window.setInterval(() => {
      const p = Math.min(1, (performance.now() - startedAt.current) / HOLD_MS);
      setProgress(p);
      if (p >= 1 && !fired.current) {
        fired.current = true;
        stop();
        onHeld();
      }
    }, 40);
  };
  useEffect(() => stop, []);
  // A second contact anywhere while a hold runs is a face, not a finger: the hold is dropped.
  useEffect(() => {
    const onAnother = (e: globalThis.PointerEvent) => {
      if (timer.current === null) return;
      if (!e.isPrimary || e.width > FINGER_MAX_PX || e.height > FINGER_MAX_PX) stop();
    };
    window.addEventListener('pointerdown', onAnother, true);
    return () => window.removeEventListener('pointerdown', onAnother, true);
  }, []);

  return (
    <button
      type="button"
      className={`call-hold${danger ? ' call-btn-danger' : ''}`}
      onPointerDown={begin}
      onPointerUp={stop}
      onPointerCancel={stop}
      onPointerLeave={stop}
      onContextMenu={(e) => e.preventDefault()}
      aria-label={label}
      style={{ '--hold': progress } as React.CSSProperties}
    >
      <span className="call-hold-fill" aria-hidden="true" />
      <Icon name={icon} size={22} />
      <span className="call-hold-label">{label}</span>
    </button>
  );
}
