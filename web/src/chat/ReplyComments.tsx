import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Icon } from '../components/Icon';
import { rangeOffsets, type Annotation } from '../lib/annotations';
import { selectActiveRun } from '../store/selectors';
import { useStore } from '../store/store';

const EMPTY: Annotation[] = [];

/** The rendered markdown of a finished reply that `node` sits in, with the message id. */
function replyOf(node: Node | null): { md: HTMLElement; messageId: string } | null {
  const el = node instanceof Element ? node : (node?.parentElement ?? null);
  const md = el?.closest('.msg-bot .md');
  const msg = md?.closest('.msg-bot');
  if (!(md instanceof HTMLElement) || !msg?.id.startsWith('msg-')) return null;
  return { md, messageId: msg.id.slice('msg-'.length) };
}

interface Draft {
  messageId: string;
  quote: string;
  start: number;
  end: number;
  range: Range;
}

/** Room the popover needs below its top edge: quote, three lines of comment, buttons. */
const POP_HEIGHT = 210;

/**
 * Where to float the button/popover: under the selection, but always on screen — the passage
 * may be scrolled away, and on a phone the keyboard takes the bottom half once the box is focused
 * (hence the visual viewport, not the window).
 */
function placeUnder(range: Range, tall: boolean): { top: number; left: number } {
  const rects = range.getClientRects();
  const last = rects[rects.length - 1] ?? range.getBoundingClientRect();
  const vv = window.visualViewport;
  const viewTop = vv?.offsetTop ?? 0;
  const viewBottom = viewTop + (vv?.height ?? window.innerHeight);
  const width = Math.min(340, window.innerWidth - 16);
  const left = Math.max(8, Math.min(last.left, window.innerWidth - width - 8));
  const top = Math.max(viewTop + 8, Math.min(last.bottom + 8, viewBottom - (tall ? POP_HEIGHT : 44) - 8));
  return { top, left };
}

/**
 * Select part of a Jarvis reply → "Comment" → say what should change. The comments wait above
 * the composer and go out with the next message (Composer → composeWithAnnotations), so
 * reworking a draft email is "mark this, mark that, send" instead of copy-paste.
 *
 * Nothing is painted into the reply itself. Marking the commented passages with the CSS Custom
 * Highlight API (web alpha.45-47) made Brave repaint the transcript as stacked stale copies, each
 * with a scrollbar, while a run streamed; it was reverted. The list above the composer is where
 * the comments show.
 */
export function ReplyComments({ conversationId }: { conversationId: string }) {
  const addAnnotation = useStore((s) => s.addAnnotation);
  const [selection, setSelection] = useState<Draft | null>(null);
  const [editing, setEditing] = useState<Draft | null>(null);
  const [comment, setComment] = useState('');
  const [, setTick] = useState(0);
  const box = useRef<HTMLTextAreaElement>(null);

  // Follow the selection: a non-empty one inside a single finished reply offers the button.
  // While a comment is being written `editing` wins over whatever the selection does meanwhile.
  //
  // Gated to idle: while a run streams the transcript's text mutates on every token, which
  // fires document `selectionchange` (the user's selection is still live). Reading it on every
  // token called setSelection on every render, re-rendering the whole transcript subtree and
  // stranding the old DOM nodes instead of replacing them (10 .transcript divs stacked in one
  // .chat, each with its own scrollbar). Only listen when no run is active; re-listen the
  // moment one finishes.
  const runActive = useStore((s) => selectActiveRun(s, s.openConversationId) !== null);
  // Ref mirror so the selectionchange effect below does not need `selection` in its deps
  // (re-running that effect on every selection change would tear down and re-add the listener).
  // Updated in an effect, not during render, per react-hooks/refs.
  const selectionRef = useRef<Draft | null>(null);
  useEffect(() => {
    selectionRef.current = selection;
  });
  useEffect(() => {
    if (runActive) {
      // A run started: drop any stale selection so the button does not float over a live reply.
      // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time reset on the run transition, not a cascading render
      setSelection(null);
      return;
    }
    let timer = 0;
    const read = () => {
      const sel = document.getSelection();
      if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
        if (selectionRef.current) setSelection(null);
        return;
      }
      const range = sel.getRangeAt(0);
      const reply = replyOf(range.commonAncestorContainer);
      const quote = range.toString();
      const off = reply ? rangeOffsets(reply.md, range) : null;
      if (!reply || !off || !quote.trim()) {
        if (selectionRef.current) setSelection(null);
        return;
      }
      // Equality check: a drag that lands on the same passage must not re-render.
      if (
        selectionRef.current?.messageId === reply.messageId &&
        selectionRef.current?.quote === quote &&
        selectionRef.current?.start === off.start &&
        selectionRef.current?.end === off.end
      )
        return;
      setSelection({ messageId: reply.messageId, quote, ...off, range: range.cloneRange() });
    };
    // Debounced: a drag fires this on every pixel, a phone's handles too.
    const onChange = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(read, 120);
    };
    document.addEventListener('selectionchange', onChange);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener('selectionchange', onChange);
    };
  }, [runActive]);

  const active = editing ?? selection;
  // Placed from the range at render time; a scroll or resize just renders again.
  const pos = active ? placeUnder(active.range, editing !== null) : null;
  useEffect(() => {
    if (!active) return;
    const reposition = () => setTick((t) => t + 1);
    const vv = window.visualViewport;
    window.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', reposition);
    vv?.addEventListener('resize', reposition);
    return () => {
      window.removeEventListener('scroll', reposition, true);
      window.removeEventListener('resize', reposition);
      vv?.removeEventListener('resize', reposition);
    };
  }, [active]);

  const open = () => {
    if (!selection) return;
    setEditing(selection);
    setComment('');
    setSelection(null);
    requestAnimationFrame(() => box.current?.focus());
  };
  const close = () => {
    setEditing(null);
    setComment('');
  };
  const save = () => {
    if (!editing || !comment.trim()) return;
    addAnnotation(conversationId, {
      id: `n${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
      messageId: editing.messageId,
      quote: editing.quote,
      comment: comment.trim(),
      start: editing.start,
      end: editing.end,
    });
    document.getSelection()?.removeAllRanges();
    close();
    window.dispatchEvent(new Event('jarvis:focus-composer'));
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
    } else if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      save();
    }
  };

  if (!active || !pos) return null;
  if (!editing) {
    return (
      <button
        type="button"
        className="reply-comment-btn"
        style={{ top: pos.top, left: pos.left }}
        // pointerdown, not click: on a desktop the click would first collapse the selection.
        onPointerDown={(e) => {
          e.preventDefault();
          open();
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') open();
        }}
      >
        <Icon name="comment" size={14} />
        Comment
      </button>
    );
  }
  return (
    <div className="reply-comment-pop" style={{ top: pos.top, left: pos.left }} role="dialog" aria-label="Comment on this part">
      <blockquote className="reply-comment-quote">{editing.quote}</blockquote>
      <textarea
        ref={box}
        rows={3}
        value={comment}
        placeholder="What should change here?"
        aria-label="Your comment"
        onChange={(e) => setComment(e.target.value)}
        onKeyDown={onKey}
      />
      <div className="reply-comment-row">
        <span className="reply-comment-hint desktop-only">Enter adds · Esc cancels</span>
        <button type="button" className="btn btn-sm btn-secondary" onClick={close}>
          Cancel
        </button>
        <button type="button" className="btn btn-sm btn-primary" onClick={save} disabled={!comment.trim()}>
          Add comment
        </button>
      </div>
    </div>
  );
}

/** The comments waiting above the composer: each one removable, all of them clearable. */
export function PendingComments({ conversationId }: { conversationId: string }) {
  const notes = useStore((s) => s.annotations[conversationId] ?? EMPTY);
  const remove = useStore((s) => s.removeAnnotation);
  const clear = useStore((s) => s.clearAnnotations);
  if (notes.length === 0) return null;
  return (
    <div className="pending-comments" role="status">
      <div className="pending-comments-head">
        <Icon name="comment" size={13} />
        <span>
          {notes.length === 1 ? '1 comment' : `${notes.length} comments`} on the reply — attached to your next message
        </span>
        <button type="button" className="btn btn-sm btn-secondary" onClick={() => clear(conversationId)}>
          Clear
        </button>
      </div>
      <ul>
        {notes.map((n) => (
          <li key={n.id}>
            <span className="pending-comment-quote" title={n.quote}>
              {n.quote}
            </span>
            <span className="pending-comment-text">{n.comment}</span>
            <button type="button" className="icon-btn sm" aria-label="Remove this comment" title="Remove this comment" onClick={() => remove(conversationId, n.id)}>
              <Icon name="x" size={14} />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
