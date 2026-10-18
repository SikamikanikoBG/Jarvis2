import { describe, expect, it } from 'vitest';
import { earpieceInput } from './earpiece';

const input = (deviceId: string, label: string) => ({ kind: 'audioinput', deviceId, label }) as MediaDeviceInfo;
const camera = { kind: 'videoinput', deviceId: 'c', label: 'camera 1, facing front' } as MediaDeviceInfo;

describe('earpieceInput', () => {
  it("picks Chrome's earpiece route on a phone with nothing plugged in (2026-10-18 trace)", () => {
    const devices = [input('default', 'Default'), input('a1', 'Speakerphone'), input('b2', 'Headset earpiece'), camera];
    expect(earpieceInput(devices)).toBe('b2');
  });

  it('leaves the default alone when anything else is there: a headset is where the call belongs', () => {
    const devices = [input('default', 'Default'), input('a1', 'Speakerphone'), input('b2', 'Headset earpiece'), input('c3', 'Bluetooth headset')];
    expect(earpieceInput(devices)).toBeNull();
  });

  it('a browser without the routes (a desktop, an iPhone) or without labels yet gets the default', () => {
    expect(earpieceInput([input('default', 'Default'), input('x', 'Built-in Microphone')])).toBeNull();
    expect(earpieceInput([input('', ''), input('', '')])).toBeNull();
    expect(earpieceInput([])).toBeNull();
  });
});
