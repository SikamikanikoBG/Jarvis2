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
 * **A phone uses its own camera app, always.** The in-app stream gets whichever lens the browser
 * hands over — on Arsen's phone the wide one, "винаги е като широка лупа" — at whatever
 * resolution it feels like, with no tap-to-focus and no zoom, and the model kept answering that
 * the photo was too blurry to read. The camera app focuses, zooms, uses the right lens and
 * returns the full-size photo; there is nothing the web preview does better on a touch screen.
 * So: OS camera on a phone, the in-app preview on a desktop (where `capture` is ignored and a
 * file dialog is not a camera), and otherwise say plainly why there is none.
 *
 * **Except in an incognito chat.** The OS camera app keeps what it shoots: a clip recorded
 * through `<input capture>` lands in the phone's gallery (Android hands the recorder no output
 * file of ours, so it saves its own), and a photo at least sits in the browser's cache
 * directory. Incognito means nothing of the chat stays on the phone, so there the in-app
 * stream is the only camera — the frame goes canvas → blob → upload and touches no storage —
 * and where there is no in-app stream (plain http), there is no camera, and the menu says why.
 * The blurrier wide lens is the price, and it is the right price.
 */
export type CameraMode = 'stream' | 'os' | 'unavailable';

export const INCOGNITO_NO_CAMERA_REASON =
  'In an incognito chat the phone’s camera app is not used: it would keep a copy on the phone. ' +
  'The in-app camera needs https; over this connection there is none.';

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

export function cameraSupport(env: CameraEnv, opts: { incognito?: boolean } = {}): CameraSupport {
  if (opts.incognito) {
    if (env.hasUserMedia) return { mode: 'stream', reason: '' };
    return { mode: 'unavailable', reason: INCOGNITO_NO_CAMERA_REASON };
  }
  if (env.coarsePointer) return { mode: 'os', reason: '' };
  if (env.hasUserMedia) return { mode: 'stream', reason: '' };
  return {
    mode: 'unavailable',
    reason: env.isSecureContext
      ? 'This browser exposes no camera.'
      : 'The camera needs a secure connection. Open Jarvis over https (or on localhost) and it will work here.',
  };
}

export interface RecorderEnv extends CameraEnv {
  hasMediaRecorder: boolean;
}

export function readRecorderEnv(): RecorderEnv {
  return { ...readCameraEnv(), hasMediaRecorder: typeof MediaRecorder !== 'undefined' };
}

/**
 * Which way this device can record a clip. Normally the OS camera app, and only on a phone (a
 * desktop ignores `capture`, and "Photo or video" is already a file dialog there). In an
 * incognito chat never the OS app — it saves the recording to the gallery — so the in-app
 * recorder (getUserMedia + MediaRecorder, memory only) or nothing, with the reason.
 */
export function recorderSupport(env: RecorderEnv, opts: { incognito?: boolean } = {}): CameraSupport {
  if (opts.incognito) {
    if (env.hasUserMedia && env.hasMediaRecorder) return { mode: 'stream', reason: '' };
    return { mode: 'unavailable', reason: INCOGNITO_NO_CAMERA_REASON };
  }
  if (env.coarsePointer) return { mode: 'os', reason: '' };
  return { mode: 'unavailable', reason: '' };
}

/** The MediaRecorder container this browser can write for a clip with sound, best first. */
export function recorderMime(isSupported: (m: string) => boolean = (m) => MediaRecorder.isTypeSupported(m)): string {
  const wanted = ['video/mp4', 'video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm'];
  return wanted.find((m) => isSupported(m)) ?? 'video/webm';
}

/** clip-20260913-203501.webm — the same shape as a photo's name, the suffix from the container. */
export function clipFilename(mime: string, at: Date = new Date()): string {
  const suffix = mime.startsWith('video/mp4') ? '.mp4' : '.webm';
  return photoFilename(at).replace(/^photo-/, 'clip-').replace(/\.jpg$/, suffix);
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
 *  the pipeline keeps is a down-scale rather than an up-scale. A clip asks for sound too — what
 *  is said in it is what was meant — and fewer pixels, since the core samples it to 768 px. */
export function captureConstraints(deviceId: string | null, opts: { clip?: boolean } = {}): MediaStreamConstraints {
  const video: MediaTrackConstraints = deviceId
    ? { deviceId: { exact: deviceId } }
    : { facingMode: { ideal: 'environment' } };
  if (opts.clip) return { video: { ...video, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: true };
  return { video: { ...video, width: { ideal: 1920 }, height: { ideal: 1440 } }, audio: false };
}
