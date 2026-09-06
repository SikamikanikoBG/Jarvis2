import { describe, expect, it } from 'vitest';
import { cameraSupport, captureConstraints, describeCameraError, nextCamera, photoFilename, type CameraEnv } from './camera';

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
  it('prefers the in-app preview wherever getUserMedia exists — desktop as much as phone', () => {
    expect(cameraSupport(env()).mode).toBe('stream');
    expect(cameraSupport(env({ coarsePointer: true })).mode).toBe('stream');
  });

  it('falls back to the OS camera on a phone with no getUserMedia', () => {
    // The core is served over plain http on the tailnet, so navigator.mediaDevices is undefined.
    // A file input with `capture` still opens the camera app, which is why the phone keeps
    // working where the desktop cannot.
    expect(cameraSupport(env({ hasUserMedia: false, isSecureContext: false, coarsePointer: true })).mode).toBe('os');
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
