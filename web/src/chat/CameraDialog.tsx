import { useEffect, useRef, useState } from 'react';
import { Icon } from '../components/Icon';
import { IconButton } from '../components/primitives';
import { captureConstraints, describeCameraError, nextCamera, photoFilename } from '../lib/camera';

interface Props {
  onCapture: (file: File) => void;
  onClose: () => void;
}

/**
 * Take a photo with this device's camera, in the app.
 *
 * The stream is owned by one effect so the camera light goes out the moment the dialog closes
 * or the device is switched — a forgotten track keeps the webcam on until the tab is closed.
 * The frame is captured at the sensor's own resolution; the upload pipeline resizes it to
 * 1568 px, so there is no point sending more and no point sending less.
 */
/** Which camera the state below describes, so switching does not show the old one's status. */
interface CamState {
  for: string | null;
  status: 'ready' | 'error';
  message?: string;
}

export function CameraDialog({ onCapture, onClose }: Props) {
  const video = useRef<HTMLVideoElement>(null);
  const [deviceId, setDeviceId] = useState<string | null>(null);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [cam, setCam] = useState<CamState | null>(null);
  const [busy, setBusy] = useState(false);
  // Derived rather than reset in the effect: while a switch is in flight this is the new
  // camera's id and `cam` still describes the old one, which is exactly "opening".
  const current = cam?.for === deviceId ? cam : null;
  const ready = current?.status === 'ready';
  const error = current?.status === 'error' ? current.message : null;

  useEffect(() => {
    let stream: MediaStream | null = null;
    let cancelled = false;
    void (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia(captureConstraints(deviceId));
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        if (video.current) {
          video.current.srcObject = stream;
          await video.current.play().catch(() => undefined);
        }
        setCam({ for: deviceId, status: 'ready' });
        // Only after permission is granted do device ids and labels exist, so the "switch
        // camera" button can know whether there is a second one.
        const all = await navigator.mediaDevices.enumerateDevices();
        if (!cancelled) setDevices(all);
      } catch (e) {
        if (!cancelled) setCam({ for: deviceId, status: 'error', message: describeCameraError(e) });
      }
    })();
    return () => {
      cancelled = true;
      stream?.getTracks().forEach((t) => t.stop());
    };
  }, [deviceId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const shoot = () => {
    const v = video.current;
    if (!v || !ready || busy) return;
    const width = v.videoWidth;
    const height = v.videoHeight;
    const fail = (message: string) => setCam({ for: deviceId, status: 'error', message });
    if (!width || !height) {
      fail('The camera has not produced a frame yet.');
      return;
    }
    setBusy(true);
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext('2d');
    if (!ctx) {
      setBusy(false);
      fail('This browser could not read the frame.');
      return;
    }
    ctx.drawImage(v, 0, 0, width, height);
    canvas.toBlob(
      (blob) => {
        setBusy(false);
        if (!blob) {
          fail('The photo could not be encoded.');
          return;
        }
        onCapture(new File([blob], photoFilename(), { type: 'image/jpeg' }));
      },
      'image/jpeg',
      0.92,
    );
  };

  const other = nextCamera(devices, deviceId);
  return (
    <div className="camera" role="dialog" aria-modal="true" aria-label="Take a photo">
      <div className="camera-stage">
        {/* muted + playsInline or iOS refuses to show the preview inline */}
        <video ref={video} autoPlay playsInline muted aria-label="Camera preview" />
        {error && (
          <p className="camera-error" role="alert">
            {error}
          </p>
        )}
        {!ready && !error && <p className="camera-hint">Opening the camera…</p>}
      </div>
      <div className="camera-bar">
        <IconButton icon="x" label="Close the camera" onClick={onClose} />
        <button
          type="button"
          className="camera-shutter"
          onClick={shoot}
          disabled={!ready || busy}
          aria-label="Take the photo"
          title="Take the photo"
        >
          <span className="camera-shutter-dot" />
        </button>
        {other ? (
          <IconButton icon="refresh" label="Switch camera" onClick={() => setDeviceId(other.deviceId)} />
        ) : (
          <span className="camera-bar-spacer" aria-hidden="true">
            <Icon name="camera" size={17} />
          </span>
        )}
      </div>
    </div>
  );
}
