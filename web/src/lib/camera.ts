/**
 * Which way this device can take a photo.
 *
 * Two mechanisms, and neither works everywhere:
 *
 * * `getUserMedia` gives a live preview we can frame, retake and switch cameras in — but it is
 *   gated behind a secure context, so on plain http (the core is served over http on the
 *   tailnet) `navigator.mediaDevices` is simply undefined;
 * * a file input with `capture` hands the job to the OS camera app. It needs no secure context,
 *   so it is the one that still works on a phone over http — but a desktop browser ignores
 *   `capture` and just opens a file picker, which is not taking a photo.
 *
 * So: prefer the in-app camera, fall back to the OS camera on a touch device, and otherwise say
 * plainly why there is no camera rather than showing a button that opens a file dialog.
 */
export type CameraMode = 'stream' | 'os' | 'unavailable';

export interface CameraSupport {
  mode: CameraMode;
  /** Why it is unavailable, phrased for the person reading it. Empty unless mode is 'unavailable'. */
  reason: string;
}

export interface CameraEnv {
  hasUserMedia: boolean;
  isSecureContext: boolean;
  coarsePointer: boolean;
}

export function readCameraEnv(): CameraEnv {
  return {
    hasUserMedia: typeof navigator !== 'undefined' && typeof navigator.mediaDevices?.getUserMedia === 'function',
    isSecureContext: typeof window !== 'undefined' && window.isSecureContext,
    coarsePointer: typeof window !== 'undefined' && window.matchMedia('(pointer: coarse)').matches,
  };
}

export function cameraSupport(env: CameraEnv): CameraSupport {
  if (env.hasUserMedia) return { mode: 'stream', reason: '' };
  if (env.coarsePointer) return { mode: 'os', reason: '' };
  return {
    mode: 'unavailable',
    reason: env.isSecureContext
      ? 'This browser exposes no camera.'
      : 'The camera needs a secure connection. Open Jarvis over https (or on localhost) and it will work here.',
  };
}

/** What went wrong, said in a sentence a person can act on rather than a DOMException name. */
export function describeCameraError(err: unknown): string {
  const name = err instanceof Error ? err.name : '';
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'Camera access was refused. Allow the camera for this site in your browser, then try again.';
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'No camera was found on this device.';
    case 'NotReadableError':
    case 'AbortError':
      return 'The camera is busy — another app or tab is already using it.';
    default:
      return err instanceof Error && err.message ? err.message : 'The camera could not be opened.';
  }
}

/** A stable, sortable name for a photo taken now: photo-20260906-141530.jpg */
export function photoFilename(at: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0');
  const stamp = `${at.getFullYear()}${p(at.getMonth() + 1)}${p(at.getDate())}-${p(at.getHours())}${p(at.getMinutes())}${p(at.getSeconds())}`;
  return `photo-${stamp}.jpg`;
}

/**
 * The next camera to switch to, given the one in use. Returns null when there is nothing to
 * switch to, so the button can hide itself instead of being a no-op.
 */
export function nextCamera(devices: MediaDeviceInfo[], currentId: string | null): MediaDeviceInfo | null {
  const cams = devices.filter((d) => d.kind === 'videoinput');
  if (cams.length < 2) return null;
  const at = cams.findIndex((d) => d.deviceId === currentId);
  return cams[(at + 1) % cams.length] ?? cams[0] ?? null;
}

/** The constraints for a capture: the back camera on a phone, and enough pixels that the 1568 px
 *  the pipeline keeps is a down-scale rather than an up-scale. */
export function captureConstraints(deviceId: string | null): MediaStreamConstraints {
  const video: MediaTrackConstraints = deviceId
    ? { deviceId: { exact: deviceId } }
    : { facingMode: { ideal: 'environment' } };
  return { video: { ...video, width: { ideal: 1920 }, height: { ideal: 1440 } }, audio: false };
}
