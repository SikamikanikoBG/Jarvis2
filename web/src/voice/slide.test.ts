import { describe, expect, it } from 'vitest';
import { SLIDE_DONE, slide } from './slide';

describe('slide', () => {
  const start = { x: 10, y: 100 };
  it('counts a sideways drag across the track', () => {
    expect(slide(start, { x: 110, y: 104 }, 200).progress).toBe(0.5);
    expect(slide(start, { x: 230, y: 90 }, 200).progress).toBeGreaterThanOrEqual(SLIDE_DONE);
  });
  it('a contact that moves up or down - a face settling against the glass - spoils it', () => {
    expect(slide(start, { x: 200, y: 160 }, 200).aborted).toBe(true);
  });
  it('a contact that stays put goes nowhere, however long it rests there', () => {
    expect(slide(start, start, 200)).toEqual({ progress: 0, aborted: false });
  });
  it('backwards is not forwards', () => {
    expect(slide(start, { x: -80, y: 100 }, 200).progress).toBe(0);
  });
});
