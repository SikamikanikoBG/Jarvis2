/**
 * Cutting a streamed answer into sentences as it arrives, so speech can start on the first one.
 *
 * `speechSynthesis` wants whole utterances: hand it a token and it says a token; hand it a whole
 * paragraph and Chrome cuts it off after ~15 s. A sentence is the unit that sounds right AND
 * stays under that. The splitter is fed text as it streams and returns what is safely a whole
 * sentence, keeping the rest until more arrives — "3.5 kg" and "т.н." must not end a sentence.
 */

const ENDERS = /[.!?…]/;
/** Abbreviations a full stop does not end a sentence after (lowercased, no dot). */
const ABBREVIATIONS = new Set(['т', 'н', 'т.н', 'напр', 'г', 'ул', 'бул', 'тел', 'e.g', 'i.e', 'etc', 'vs', 'mr', 'mrs', 'dr', 'no', 'св', 'проф', 'инж', 'стр']);
/** After an ender, one of these opens the next sentence: a space or a newline, then an upper-case letter or a digit or a quote. */
const NEXT_START = /^(\s+)(["“„«(]?[A-ZА-ЯЁ0-9])/u;

export class SentenceSplitter {
  private buffer = '';

  /** Feed streamed text; returns the sentences that are now complete (possibly none). */
  push(chunk: string): string[] {
    this.buffer += chunk;
    const out: string[] = [];
    for (;;) {
      const cut = this.findCut();
      if (cut < 0) break;
      const sentence = this.buffer.slice(0, cut).trim();
      this.buffer = this.buffer.slice(cut);
      if (sentence) out.push(sentence);
    }
    return out;
  }

  /** What is left when the stream ends: the last sentence, ender or not. */
  flush(): string | null {
    const rest = this.buffer.trim();
    this.buffer = '';
    return rest || null;
  }

  get pending(): string {
    return this.buffer;
  }

  private findCut(): number {
    let from = 0;
    for (;;) {
      const at = this.buffer.slice(from).search(ENDERS);
      if (at < 0) return -1;
      const i = from + at;
      // Swallow a run of enders ("?!", "...") as one.
      let end = i + 1;
      while (end < this.buffer.length && ENDERS.test(this.buffer[end] ?? '')) end += 1;
      const rest = this.buffer.slice(end);
      // A paragraph break is always a cut, whatever follows.
      if (/^\s*\n\s*\n/.test(rest)) return end;
      const m = NEXT_START.exec(rest);
      if (m && !this.isAbbreviation(i) && !this.isDecimal(i)) return end;
      from = end;
    }
  }

  private isDecimal(dot: number): boolean {
    return this.buffer[dot] === '.' && /\d/.test(this.buffer[dot - 1] ?? '') && /\d/.test(this.buffer[dot + 1] ?? '');
  }

  private isAbbreviation(dot: number): boolean {
    if (this.buffer[dot] !== '.') return false;
    const before = this.buffer.slice(0, dot);
    const word = /([A-Za-zА-Яа-яЁё.]+)$/u.exec(before)?.[1] ?? '';
    const w = word.toLowerCase().replace(/\.$/, '');
    if (ABBREVIATIONS.has(w)) return true;
    // A single letter before the dot is an initial ("Г. Иванов"), not the end of a sentence.
    return /^[A-Za-zА-Яа-яЁё]$/u.test(w);
  }
}

/** Markdown that survived the style block, stripped for the synthesiser: it must never say "asterisk". */
export function speakable(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, ' (код в чата) ')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*[-*•]\s+/gm, '')
    .replace(/^\s*\d+[.)]\s+/gm, '')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/\*([^*]+)\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/_([^_]+)_/g, '$1')
    .replace(/~~([^~]+)~~/g, '$1')
    .replace(/^\s*[|>].*$/gm, '')
    .replace(/https?:\/\/\S+/g, ' линк в чата ')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/**
 * Any Cyrillic word → Bulgarian; a sentence of Latin words only → English (or the caller's
 * default when that is not Bulgarian). Decided per sentence, so a mixed reply switches voice —
 * and a Bulgarian sentence with "vLLM" and "vader" in it is still Bulgarian: product names ride
 * along, the Bulgarian voice reads them well enough.
 */
export function scriptLanguage(text: string, fallback: string): string {
  const cyrWords = (text.match(/[Ѐ-ӿ]{2,}/g) ?? []).length;
  const latWords = (text.match(/[A-Za-z]{2,}/g) ?? []).length;
  if (cyrWords > 0) return 'bg';
  if (latWords > 0) return fallback === 'bg' ? 'en' : fallback;
  return fallback;
}
