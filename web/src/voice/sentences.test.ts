import { describe, expect, it } from 'vitest';
import { SentenceSplitter, scriptLanguage, speakable } from './sentences';

describe('SentenceSplitter', () => {
  it('hands over a sentence as soon as the next one has visibly begun', () => {
    const s = new SentenceSplitter();
    expect(s.push('Три дни, без отделяне.')).toEqual([]); // could still be "…отделяне. и"
    expect(s.push(' Има ли')).toEqual(['Три дни, без отделяне.']);
    expect(s.push(' бучка?')).toEqual([]);
    expect(s.flush()).toBe('Има ли бучка?');
  });

  it('does not cut on decimals, abbreviations or initials', () => {
    const s = new SentenceSplitter();
    expect(s.push('Тежи 3.5 кг. Това е много.')).toEqual(['Тежи 3.5 кг.']); // not at "3."
    expect(s.push(' Да.')).toEqual(['Това е много.']);
    const t = new SentenceSplitter();
    expect(t.push('Вземи хляб, мляко и т.н. Купи и вода. Готово')).toEqual(['Вземи хляб, мляко и т.н. Купи и вода.']);
    const u = new SentenceSplitter();
    expect(u.push('Пише Г. Иванов. Той е тук. И')).toEqual(['Пише Г. Иванов.', 'Той е тук.']);
  });

  it('treats a paragraph break as a cut whatever follows, and swallows runs of enders', () => {
    const s = new SentenceSplitter();
    expect(s.push('Наистина?!\n\nдобре')).toEqual(['Наистина?!']);
    expect(s.flush()).toBe('добре');
  });

  it('flushes the tail with no ender when the stream ends', () => {
    const s = new SentenceSplitter();
    expect(s.push('Ще видим')).toEqual([]);
    expect(s.flush()).toBe('Ще видим');
    expect(s.flush()).toBeNull();
  });
});

describe('speakable', () => {
  it('strips what a synthesiser would read out loud as symbols', () => {
    expect(speakable('**Важно**: виж [сайта](https://x.y/z) и `код`.')).toBe('Важно: виж сайта и код.');
    expect(speakable('- първо\n- второ')).toBe('първо\nвторо');
    expect(speakable('```py\nprint(1)\n```')).toBe('(код в чата)');
    expect(speakable('Виж https://example.com/a?b=1 сега')).toBe('Виж линк в чата сега');
  });
});

describe('scriptLanguage', () => {
  it('picks Bulgarian for Cyrillic, the fallback otherwise, per sentence', () => {
    expect(scriptLanguage('Здравей, Арсен.', 'en')).toBe('bg');
    expect(scriptLanguage('Hello there.', 'bg')).toBe('en');
    expect(scriptLanguage('123', 'bg')).toBe('bg');
    expect(scriptLanguage('vLLM на vader е ок.', 'en')).toBe('bg');
  });
});
