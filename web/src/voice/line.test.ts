import { describe, expect, it, vi } from 'vitest';

vi.mock('../api/client', () => ({
  api: { tts: vi.fn((text: string) => Promise.resolve(new TextEncoder().encode(`mp3:${text}`).buffer)) },
}));

import { LineCues, LineSpeaker, type VoiceLine } from './line';
import type { Cues } from './tones';

function fakeLine(down = false) {
  const played: string[] = [];
  const music: boolean[] = [];
  const line = {
    down,
    ready: Promise.resolve(),
    play: vi.fn((bytes: ArrayBuffer) => {
      played.push(new TextDecoder().decode(bytes));
      return Promise.resolve(true);
    }),
    stop: vi.fn(),
    music: vi.fn((on: boolean) => music.push(on)),
  };
  return { line: line as unknown as VoiceLine, played, music, raw: line };
}

function fakeCues() {
  return { accepted: vi.fn(), thinking: vi.fn(), offline: vi.fn(), close: vi.fn() } satisfies Cues;
}

describe('LineCues', () => {
  it('plays the hold music on the call, not the page, and stops it at the end', () => {
    const { line, music } = fakeLine();
    const inner = fakeCues();
    const hold: boolean[] = [];
    const cues = new LineCues(line, inner, { waiting: 'music', volume: 0.4, onHold: (on) => hold.push(on) });
    cues.thinking(true);
    cues.thinking(true);
    cues.thinking(false);
    cues.accepted();
    cues.close();
    expect(music).toEqual([true, false]);
    expect(hold).toEqual([true, false]);
    expect(inner.thinking).not.toHaveBeenCalled();
    expect(inner.accepted).toHaveBeenCalledOnce();
  });

  it('leaves the waiting sound to the page when the line is down or the setting is not music', () => {
    const down = new LineCues(fakeLine(true).line, fakeCues(), { waiting: 'music', volume: 0.4 });
    const beeps = fakeCues();
    const pulse = new LineCues(fakeLine().line, beeps, { waiting: 'beeps', volume: 0.4 });
    down.thinking(true);
    pulse.thinking(true);
    expect(beeps.thinking).toHaveBeenCalledWith(true);
  });
});

describe('LineSpeaker', () => {
  it('says each sentence over the line and cuts the line on cancel', async () => {
    const { line, played, raw } = fakeLine();
    const speaker = new LineSpeaker(line, { contextOf: () => null, cache: true });
    speaker.prepare('Hello there.', 'en', true);
    await speaker.speak('Hello there.', 'en');
    await speaker.speak('Second one.', 'en');
    expect(played).toEqual(['mp3:Hello there.', 'mp3:Second one.']);
    expect(speaker.cancellable()).toBe(true);
    speaker.cancel();
    expect(raw.stop).toHaveBeenCalled();
  });

  it('hands the voice back to the page when the line is down', async () => {
    const { line, raw } = fakeLine(true);
    const speaker = new LineSpeaker(line, { contextOf: () => null, cache: true });
    const fallback = (speaker as unknown as { fallback: { speak: (t: string, l: string) => Promise<void> } }).fallback;
    const spoken = vi.spyOn(fallback, 'speak').mockResolvedValue();
    await speaker.speak('Still here.', 'en');
    expect(spoken).toHaveBeenCalledWith('Still here.', 'en');
    expect(raw.play).not.toHaveBeenCalled();
  });
});
