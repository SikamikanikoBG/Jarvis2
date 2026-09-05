import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Icon } from '../components/Icon';
import type { TranscriptItem } from '../store/transcript';
import { ConfirmCard } from './ConfirmCard';
import { MessageItem } from './MessageItem';
import { Note } from './Note';
import { RunChip } from './RunChip';
import { StreamBubble } from './StreamBubble';
import { SummaryDivider } from './SummaryDivider';
import { ToolCard } from './ToolCard';

interface Props {
  items: TranscriptItem[];
}

const BOTTOM_SLACK = 48;
/** A scroll this soon after wheel/touch/keyboard input counts as the user's intent. */
const INTENT_WINDOW_MS = 600;

/**
 * Autoscroll that follows new content until the user scrolls up, then offers "jump to latest".
 * Only user-initiated scrolling can unpin (wheel, touch, keys, scrollbar drag) — layout
 * reflows, browser scroll anchoring and our own pins never do. Mounted with
 * `key={conversation}` so a switch starts pinned to the bottom.
 */
export function Transcript({ items }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const lastIntentAt = useRef(0);
  const dragging = useRef(false);
  const [showJump, setShowJump] = useState(false);

  const pinToBottom = useCallback((smooth: boolean) => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    atBottom.current = true;
  }, []);

  useLayoutEffect(() => {
    pinToBottom(false);
  }, [pinToBottom]);

  // Follow growth (streaming, new items) and container resizes while pinned to the bottom.
  useEffect(() => {
    const inner = innerRef.current;
    const outer = scrollRef.current;
    if (!inner || !outer) return;
    const ro = new ResizeObserver(() => {
      if (atBottom.current) pinToBottom(false);
      else setShowJump(true);
    });
    ro.observe(inner);
    ro.observe(outer);
    const up = () => {
      dragging.current = false;
    };
    document.addEventListener('mouseup', up);
    return () => {
      ro.disconnect();
      document.removeEventListener('mouseup', up);
    };
  }, [pinToBottom]);

  const markIntent = () => {
    lastIntentAt.current = Date.now();
  };

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distance < BOTTOM_SLACK) {
      atBottom.current = true;
      setShowJump(false);
    } else if (dragging.current || Date.now() - lastIntentAt.current < INTENT_WINDOW_MS) {
      atBottom.current = false;
    }
  };

  const jump = () => {
    pinToBottom(true);
    setShowJump(false);
  };

  return (
    <>
      <div
        ref={scrollRef}
        className="transcript"
        onScroll={onScroll}
        onWheel={markIntent}
        onTouchMove={markIntent}
        onKeyDown={markIntent}
        onMouseDown={() => {
          dragging.current = true;
          markIntent();
        }}
        role="log"
        aria-label="Transcript"
        tabIndex={-1}
      >
        <div ref={innerRef} className="transcript-inner">
          {items.map((it) => {
            switch (it.kind) {
              case 'message':
                return <MessageItem key={it.key} message={it.message} />;
              case 'tool':
                return <ToolCard key={it.key} card={it.card} />;
              case 'note':
                return <Note key={it.key} note={it.note} />;
              case 'confirm':
                return <ConfirmCard key={it.key} runId={it.runId} callId={it.callId} name={it.name} arguments={it.arguments} reason={it.reason} />;
              case 'stream':
                return <StreamBubble key={it.key} runId={it.runId} />;
              case 'run':
                return <RunChip key={it.key} runId={it.runId} />;
              case 'summary':
                return <SummaryDivider key={it.key} text={it.text} />;
            }
          })}
        </div>
      </div>
      {showJump && (
        <button type="button" className="jump" onClick={jump}>
          <Icon name="arrowDown" size={15} />
          Jump to latest
        </button>
      )}
    </>
  );
}
