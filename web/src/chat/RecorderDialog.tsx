import { useEffect, useRef, useState } from 'react';
import { Icon } from '../components/Icon';
import { IconButton } from '../components/primitives';
import { captureConstraints, clipFilename, describeCameraError, nextCamera, recorderMime } from '../lib/camera';

interface Props {
  onCapture: (file: File) => void;
  onClose: () => void;
}

/** Which camera the state below describes, so switching does not show the old one's status. */
interface CamState {
  for: string | null;
  status: 'ready' | 'error';
  message?: string;
}

/** The longest clip the recorder takes before stopping on its own: the core shows the model 32
 * frames however long the clip is, and a phone's minute of 720p is already tens of megabytes. */
export const MAX_CLIP_S = 120;

/**
 * Record a clip with this device's camera, in the app, and hand it over as a File.
 *
 * This exists for incognito chats: the OS camera app saves what it records to the gallery, and
 * nothing of an incognito chat may stay on the phone. Here the stream goes into a MediaRecorder,
 * the chunks live in memory, and the File built from them is uploaded and dropped — no storage
 * is touched on the way. The stream is owned by one effect so the camera light goes out the
 * moment the dialog closes.
 */
export function RecorderDialog({ onCapture, onClose }: Props) {
  const video = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const [deviceId, setDeviceId] = useState<string | null>(null);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [cam, setCam] = useState<CamState | null>(null);
  const [recording, setRecording] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const current = cam?.for === deviceId ? cam : null;
  const ready = current?.status === 'ready';
  const error = current?.status === 'error' ? current.message : null;

  useEffect(() => {
    let stream: MediaStream | null = null;
    let cancelled = false;
    void (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia(captureConstraints(deviceId, { clip: true }));
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        streamRef.current = stream;
        if (video.current) {
          video.current.srcObject = stream;
          await video.current.play().catch(() => undefined);
        }
        setCam({ for: deviceId, status: 'ready' });
        const all = await navigator.mediaDevices.enumerateDevices();
        if (!cancelled) setDevices(all);
      } catch (e) {
        if (!cancelled) setCam({ for: deviceId, status: 'error', message: describeCameraError(e) });
      }
    })();
    return () => {
      cancelled = true;
      if (recorder.current?.state === 'recording') recorder.current.stop();
      recorder.current = null;
      chunks.current = [];
      stream?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    };
  }, [deviceId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // The clock, and the cap: a clip stops itself at MAX_CLIP_S.
  useEffect(() => {
    if (!recording) return;
    const started = Date.now();
    const tick = setInterval(() => {
      const s = Math.floor((Date.now() - started) / 1000);
      setSeconds(s);
      if (s >= MAX_CLIP_S) recorder.current?.stop();
    }, 250);
    return () => clearInterval(tick);
  }, [recording]);

  const fail = (message: string) => setCam({ for: deviceId, status: 'error', message });

  const start = () => {
    const stream = streamRef.current;
    if (!stream || !ready || recording) return;
    const mime = recorderMime();
    let rec: MediaRecorder;
    try {
      rec = new MediaRecorder(stream, { mimeType: mime });
    } catch (e) {
      fail(describeCameraError(e));
      return;
    }
    chunks.current = [];
    rec.ondataavailable = (e) => {
      if (e.data.size) chunks.current.push(e.data);
    };
    rec.onstop = () => {
      const type = rec.mimeType || mime;
      const blob = new Blob(chunks.current, { type: type.split(';')[0] });
      chunks.current = [];
      recorder.current = null;
      setRecording(false);
      if (!blob.size) {
        fail('Nothing was recorded.');
        return;
      }
      onCapture(new File([blob], clipFilename(type), { type: blob.type }));
    };
    rec.onerror = () => fail('The recording failed.');
    recorder.current = rec;
    rec.start(1000);
    setSeconds(0);
    setRecording(true);
  };

  const stop = () => {
    if (recorder.current?.state === 'recording') recorder.current.stop();
  };

  const other = nextCamera(devices, deviceId);
  const mm = String(Math.floor(seconds / 60)).padStart(2, '0');
  const ss = String(seconds % 60).padStart(2, '0');
  return (
    <div className="camera" role="dialog" aria-modal="true" aria-label="Record a video">
      <div className="camera-stage">
        {/* muted + playsInline or iOS refuses to show the preview inline (muted also keeps the
            phone from playing its own microphone back at itself) */}
        <video ref={video} autoPlay playsInline muted aria-label="Camera preview" />
        {recording && (
          <p className="camera-hint camera-rec" role="timer" aria-live="off">
            <span className="camera-rec-dot" aria-hidden="true" /> {mm}:{ss}
          </p>
        )}
        {error && (
          <p className="camera-error" role="alert">
            {error}
          </p>
        )}
        {!ready && !error && <p className="camera-hint">Opening the camera…</p>}
        {ready && !recording && (
          <p className="camera-hint camera-hint-low">Recorded in the app — nothing is saved to this phone.</p>
        )}
      </div>
      <div className="camera-bar">
        <IconButton icon="x" label="Close the camera" onClick={onClose} />
        <button
          type="button"
          className={`camera-shutter${recording ? ' recording' : ''}`}
          onClick={recording ? stop : start}
          disabled={!ready}
          aria-label={recording ? 'Stop recording' : 'Start recording'}
          title={recording ? 'Stop recording' : 'Start recording'}
        >
          <span className="camera-shutter-dot" />
        </button>
        {other && !recording ? (
          <IconButton icon="refresh" label="Switch camera" onClick={() => setDeviceId(other.deviceId)} />
        ) : (
          <span className="camera-bar-spacer" aria-hidden="true">
            <Icon name="video" size={17} />
          </span>
        )}
      </div>
    </div>
  );
}
