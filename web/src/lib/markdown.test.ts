import { describe, expect, it } from 'vitest';
import { cellWidthClass } from './markdown';

describe('table cells', () => {
  it('get a wider floor the more text they hold, and a short one none', () => {
    expect(cellWidthClass(1)).toBeNull();
    expect(cellWidthClass(30)).toBe('cell-w1');
    expect(cellWidthClass(100)).toBe('cell-w2');
    expect(cellWidthClass(250)).toBe('cell-w3');
    expect(cellWidthClass(570)).toBe('cell-w4');
  });
});
