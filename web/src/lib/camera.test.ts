import { describe, expect, it } from 'vitest';
import {
  cameraSupport,
  captureConstraints,
  clipFilename,
  describeCameraError,
  INCOGNITO_NO_CAMERA_REASON,
  nextCamera,
  photoFilename,
  recorderMime,
  recorderSupport,
  type CameraEnv,
  type RecorderEnv,
} from './camera';

const env = (over: Partial<CameraEnv> = {}): CameraEnv => ({
  hasUserMedia: true,
  isSecureContext: true,
  coarsePointer: false,
  ...over,
});

const cam = (deviceId: string, kind: MediaDeviceKind = 'videoinput'): MediaDeviceInfo => ({
  deviceId,
  label: '',
  kind,
  groupId: 'g',
  toJSON: () => ({}),
});

describe('cameraSupport', () => {
  it('gives a phone its own camera app even where getUserMedia would work', () => {
    // The in-app stream is one fixed wide-angle frame that cannot be focused or zoomed, and the
    // model kept calling the result blurry. The camera app has the lenses and the autofocus.
    expect(cameraSupport(env({ coarsePointer: true })).mode).toBe('os');
    expect(cameraSupport(env({ hasUserMedia: false, isSecureContext: false, coarsePointer: true })).mode).toBe('os');
  });

  it('uses the in-app preview on a desktop, where `capture` is ignored and a file dialog is not a camera', () => {
    expect(cameraSupport(env()).mode).toBe('stream');
  });

  it('says why rather than opening a file picker and calling it a camera', () => {
    const insecure = cameraSupport(env({ hasUserMedia: false, isSecureContext: false }));
    expect(insecure.mode).toBe('unavailable');
    expect(insecure.reason).toContain('https');
    const secure = cameraSupport(env({ hasUserMedia: false, isSecureContext: true }));
    expect(secure.mode).toBe('unavailable');
    expect(secure.reason).toBe('This browser exposes no camera.');
  });
});

describe('in an incognito chat', () => {
  // 2026-09-13: a clip recorded into an incognito chat through the phone's camera app was in the
  // phone's gallery. The camera app keeps what it shoots; incognito means nothing stays.
  const rec = (over: Partial<RecorderEnv> = {}): RecorderEnv => ({ ...env(), hasMediaRecorder: true, ...over });

  it('never hands a phone to its camera app - the in-app stream, which keeps nothing', () => {
    expect(cameraSupport(env({ coarsePointer: true }), { incognito: true }).mode).toBe('stream');
    expect(recorderSupport(rec({ coarsePointer: true }), { incognito: true }).mode).toBe('stream');
  });

  it('has no camera at all where the in-app stream is missing, and says why', () => {
    const photo = cameraSupport(env({ coarsePointer: true, hasUserMedia: false, isSecureContext: false }), { incognito: true });
    expect(photo.mode).toBe('unavailable');
    expect(photo.reason).toBe(INCOGNITO_NO_CAMERA_REASON);
    const clip = recorderSupport(rec({ coarsePointer: true, hasMediaRecorder: false }), { incognito: true });
    expect(clip.mode).toBe('unavailable');
    expect(clip.reason).toBe(INCOGNITO_NO_CAMERA_REASON);
  });

  it('leaves an ordinary chat exactly as it was', () => {
    expect(cameraSupport(env({ coarsePointer: true })).mode).toBe('os');
    expect(recorderSupport(rec({ coarsePointer: true })).mode).toBe('os');
    // A desktop records nothing through `capture`; "Photo or video" is the file dialog there.
    const desktop = recorderSupport(rec());
    expect(desktop.mode).toBe('unavailable');
    expect(desktop.reason).toBe('');
  });
});

describe('the in-app recorder', () => {
  it('asks for sound with the clip and keeps the pixels modest', () => {
    const c = captureConstraints(null, { clip: true });
    expect(c.audio).toBe(true);
    expect((c.video as MediaTrackConstraints).width).toEqual({ ideal: 1280 });
    expect(captureConstraints(null).audio).toBe(false);
  });

  it('picks the best container the browser can write, and names the clip by it', () => {
    expect(recorderMime((m) => m === 'video/webm;codecs=vp8,opus')).toBe('video/webm;codecs=vp8,opus');
    expect(recorderMime((m) => m.startsWith('video/mp4'))).toBe('video/mp4');
    expect(recorderMime(() => false)).toBe('video/webm');
    const at = new Date(2026, 8, 13, 20, 35, 1);
    expect(clipFilename('video/webm;codecs=vp8,opus', at)).toBe('clip-20260913-203501.webm');
    expect(clipFilename('video/mp4', at)).toBe('clip-20260913-203501.mp4');
  });
});

describe('nextCamera', () => {
  it('is null when there is nothing to switch to, so the button can hide', () => {
    expect(nextCamera([], null)).toBeNull();
    expect(nextCamera([cam('a')], 'a')).toBeNull();
    expect(nextCamera([cam('a'), cam('mic', 'audioinput')], 'a')).toBeNull();
  });

  it('cycles through the video inputs', () => {
    const cams = [cam('back'), cam('front')];
    expect(nextCamera(cams, null)?.deviceId).toBe('back'); // unknown current → start at the first
    expect(nextCamera(cams, 'back')?.deviceId).toBe('front');
    expect(nextCamera(cams, 'front')?.deviceId).toBe('back');
  });
});

describe('captureConstraints', () => {
  it('asks for the back camera by default and a specific one after a switch', () => {
    const first = captureConstraints(null).video as MediaTrackConstraints;
    expect(first.facingMode).toEqual({ ideal: 'environment' });
    expect(first.deviceId).toBeUndefined();
    const picked = captureConstraints('front') as { video: MediaTrackConstraints };
    expect(picked.video.deviceId).toEqual({ exact: 'front' });
    expect(picked.video.facingMode).toBeUndefined(); // an explicit choice must not be overridden
  });

  it('asks for more pixels than the 1568 px the upload keeps, so it down-scales', () => {
    const v = captureConstraints(null).video as MediaTrackConstraints;
    expect((v.width as { ideal: number }).ideal).toBeGreaterThan(1568);
  });

  it('never asks for the microphone', () => {
    expect(captureConstraints(null).audio).toBe(false);
  });
});

describe('describeCameraError', () => {
  it('turns DOMException names into something actionable', () => {
    const err = (name: string) => Object.assign(new Error('raw'), { name });
    expect(describeCameraError(err('NotAllowedError'))).toContain('Allow the camera');
    expect(describeCameraError(err('NotFoundError'))).toContain('No camera');
    expect(describeCameraError(err('NotReadableError'))).toContain('busy');
    expect(describeCameraError(err('WeirdError'))).toBe('raw');
    expect(describeCameraError('not an error')).toBe('The camera could not be opened.');
  });
});

describe('photoFilename', () => {
  it('sorts by when it was taken', () => {
    const a = photoFilename(new Date(2026, 8, 6, 14, 15, 30));
    const b = photoFilename(new Date(2026, 8, 6, 14, 15, 31));
    expect(a).toBe('photo-20260906-141530.jpg');
    expect([b, a].sort()).toEqual([a, b]);
  });
});
