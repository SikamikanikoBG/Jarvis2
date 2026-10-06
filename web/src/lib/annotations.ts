/**
 * Comments on parts of a reply: Arsen selects a passage of what Jarvis wrote (a draft email, a
 * paragraph), types what he wants changed, and the comments go out with his next message — no
 * copying the passage into the composer by hand.
 *
 * A comment remembers WHERE it sits as character offsets into the reply's rendered text, so the
 * comments go out in the order the passages appear in the reply.
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

/**
 * The message that goes with the comments when Arsen writes nothing himself. Explicit about ONE
 * reply: given a list of comments a model tends to answer them one by one instead of redoing the
 * draft.
 */
export const DEFAULT_REVISE =
  'Revise your previous reply using my comments in the attached file: apply all of them and send back the complete revised version in one reply.';

/** The attachment's name the composer and the sent message show. */
export function commentsAttachmentName(count: number): string {
  return count === 1 ? 'Comment on the reply' : `${count} comments on the reply`;
}

/** Quote every line, so a multi-paragraph selection stays one quote. */
function quoteBlock(text: string): string {
  return text
    .trim()
    .split(/\r?\n/)
    .map((line) => (line.trim() ? `> ${line.trimEnd()}` : '>'))
    .join('\n');
}

/**
 * The comments as one document — what the attachment holds. Ordered as the passages appear in
 * the reply, so "1." is the first one down the page whatever order they were written in.
 */
export function commentsDocument(notes: Annotation[]): string {
  const ordered = [...notes].sort((a, b) => (a.messageId === b.messageId ? a.start - b.start : 0));
  const items = ordered.map((n, i) => {
    const comment = n.comment.trim() || '(change this)';
    return `${i + 1}. On this part:\n${quoteBlock(n.quote)}\n\n   My comment: ${comment}`;
  });
  const head = notes.length === 1 ? 'A comment on your reply:' : `${notes.length} comments on your reply:`;
  return `${head}\n\n${items.join('\n\n')}\n\n${ONE_PASS}`;
}

/**
 * Written into the comments themselves, so it reaches the model whatever Arsen types with them.
 * Without it the model took the comments one at a time — rewrite, save, rewrite again, save — and
 * every pass showed up as one more full answer.
 */
export const ONE_PASS =
  'Apply ALL of these comments together in ONE pass: one complete revised version, written out once. ' +
  'Do not handle them one by one and do not show intermediate versions.';

/** What to say with the attachment: Arsen's own words, or the default ask. */
export function revisionAsk(text: string): string {
  return text.trim() || DEFAULT_REVISE;
}

/**
 * Comments and message as ONE text — only where an attachment cannot go: a message handed to a
 * run that is already working (it has assembled its context), or an edit that forks the chat.
 */
export function composeWithAnnotations(notes: Annotation[], text: string): string {
  const own = text.trim();
  if (notes.length === 0) return own;
  return `${commentsDocument(notes)}\n\n${own || DEFAULT_REVISE.replace('in the attached file', 'above')}`;
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
  // Range.toString() is the concatenated text nodes in the range (text nodes only).
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
