/**
 * Comments on parts of a reply: Arsen selects a passage of what Jarvis wrote (a draft email, a
 * paragraph), types what he wants changed, and the comments go out with his next message — no
 * copying the passage into the composer by hand.
 *
 * A comment remembers WHERE it sits as character offsets into the reply's rendered text, so the
 * passage can be highlighted again after a re-render (a Range does not survive React redrawing
 * the markdown).
 */
export interface Annotation {
  id: string;
  messageId: string;
  /** The selected text, as the browser gave it. */
  quote: string;
  comment: string;
  /** Offsets into the concatenated text nodes of the reply's rendered markdown. */
  start: number;
  end: number;
}

/** The instruction used when Arsen sends comments without writing anything himself. */
export const DEFAULT_REVISE = 'Please send a revised version with these changes applied.';

/** Quote every line, so a multi-paragraph selection stays one quote. */
function quoteBlock(text: string): string {
  return text
    .trim()
    .split(/\r?\n/)
    .map((line) => (line.trim() ? `> ${line.trimEnd()}` : '>'))
    .join('\n');
}

/**
 * The message the comments become. Ordered as the passages appear in the reply, so "1." is the
 * first one down the page whatever order they were written in.
 */
export function composeWithAnnotations(notes: Annotation[], text: string): string {
  const own = text.trim();
  if (notes.length === 0) return own;
  const ordered = [...notes].sort((a, b) => (a.messageId === b.messageId ? a.start - b.start : 0));
  const items = ordered.map((n, i) => {
    const comment = n.comment.trim() || '(change this)';
    return `${i + 1}. On this part:\n${quoteBlock(n.quote)}\n\n   My comment: ${comment}`;
  });
  const head = notes.length === 1 ? 'A comment on your reply:' : `${notes.length} comments on your reply:`;
  return `${head}\n\n${items.join('\n\n')}\n\n${own || DEFAULT_REVISE}`;
}

/** The text nodes under `root`, in document order. */
function textNodes(root: Node): Text[] {
  const out: Text[] = [];
  const walker = root.ownerDocument?.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */);
  if (!walker) return out;
  for (let n = walker.nextNode(); n; n = walker.nextNode()) out.push(n as Text);
  return out;
}

/** Where a DOM position (node + offset) falls in the concatenated text of `root`, or null. */
function offsetOf(root: Node, node: Node, offset: number): number | null {
  const doc = root.ownerDocument;
  if (!doc) return null;
  // Element positions (offset = child index) are resolved by measuring a range from the start.
  const r = doc.createRange();
  r.setStart(root, 0);
  try {
    r.setEnd(node, offset);
  } catch {
    return null;
  }
  // Range.toString() is the concatenated text nodes in the range — the same text textNodes() sees.
  return r.toString().length;
}

/** The selection's offsets within `root`, or null when the range is not inside it. */
export function rangeOffsets(root: Node, range: Range): { start: number; end: number } | null {
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null;
  const start = offsetOf(root, range.startContainer, range.startOffset);
  const end = offsetOf(root, range.endContainer, range.endOffset);
  if (start === null || end === null || end <= start) return null;
  return { start, end };
}

/**
 * Which text node (by index) and offset within it `start` and `end` fall in, given the lengths
 * of the text nodes in order; null when the text is shorter than `end` now.
 */
export function locateOffsets(
  lengths: number[],
  start: number,
  end: number,
): { startNode: number; startOffset: number; endNode: number; endOffset: number } | null {
  if (end <= start || start < 0) return null;
  let pos = 0;
  let startNode = -1;
  let startOffset = 0;
  for (let i = 0; i < lengths.length; i++) {
    const len = lengths[i] ?? 0;
    if (startNode < 0 && start < pos + len) {
      startNode = i;
      startOffset = start - pos;
    }
    if (startNode >= 0 && end <= pos + len) return { startNode, startOffset, endNode: i, endOffset: end - pos };
    pos += len;
  }
  return null;
}

/** A Range over [start, end) of `root`'s text, or null when the text is shorter than that now. */
export function rangeFromOffsets(root: Node, start: number, end: number): Range | null {
  const doc = root.ownerDocument;
  const nodes = textNodes(root);
  const at = locateOffsets(
    nodes.map((n) => n.data.length),
    start,
    end,
  );
  const first = at ? nodes[at.startNode] : undefined;
  const last = at ? nodes[at.endNode] : undefined;
  if (!doc || !at || !first || !last) return null;
  const r = doc.createRange();
  r.setStart(first, at.startOffset);
  r.setEnd(last, at.endOffset);
  return r;
}
