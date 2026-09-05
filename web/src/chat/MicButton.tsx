import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { Icon } from '../components/Icon';
import { useTicker } from '../components/useTicker';
import { formatSeconds } from '../lib/format';
import { errorText } from '../lib/useLoader';
import { useStore } from '../store/store';

interface Props {
  disabled: boolean;
  onTranscript: (text: string) => void;
}

type MicState = 'idle' | 'recording' | 'uploading';

const supported = () => typeof MediaRecorder !== 'undefined' && typeof navigator.mediaDevices?.getUserMedia === 'function';

/** Push-to-talk: MediaRecorder (webm/opus) → POST /api/stt → text appended to the composer. */
export function MicButton({ disabled, onTranscript }: Props) {
  const notify = useStore((s) => s.notify);
  const [state, setState] = useState<MicState>('idle');
  const [startedAt, setStartedAt] = useState(0);
  const rec = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const now = useTicker(1000, state === 'recording');

  useEffect(() => () => rec.current?.stream.getTracks().forEach((t) => t.stop()), []);

  if (!supported()) return null;

  const start = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : 'audio/webm';
      const r = new MediaRecorder(stream, { mimeType: mime });
      chunks.current = [];
      r.ondataavailable = (e) => {
        if (e.data.size > 0) chunks.current.push(e.data);
      };
      r.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunks.current, { type: 'audio/webm' });
        if (blob.size === 0) {
          setState('idle');
          return;
        }
        setState('uploading');
        api
          .stt(blob)
          .then((res) => {
            if (res.text.trim()) onTranscript(res.text.trim());
            else notify('Nothing recognised.');
          })
          .catch((e: unknown) => notify(`Transcription failed: ${errorText(e)}`, 'error'))
          .finally(() => setState('idle'));
      };
      rec.current = r;
      r.start();
      setStartedAt(Date.now());
      setState('recording');
    } catch (e) {
      notify(`Microphone unavailable: ${errorText(e)}`, 'error');
    }
  };

  const stop = () => rec.current?.stop();

  if (state === 'recording') {
    return (
      <button type="button" className="mic-btn recording" onClick={stop} aria-label="Stop recording" title="Stop recording">
        <span className="dot dot-danger dot-pulse" />
        <span className="mono">{formatSeconds(now - startedAt)}</span>
      </button>
    );
  }
  return (
    <button type="button" className="mic-btn" onClick={() => void start()} disabled={disabled || state === 'uploading'} aria-label={state === 'uploading' ? 'Transcribing' : 'Record a voice message'} title="Record">
      <Icon name="mic" size={17} className={state === 'uploading' ? 'dot-pulse' : undefined} />
    </button>
  );
}
