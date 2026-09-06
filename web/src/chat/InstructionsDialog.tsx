import { useEffect, useRef, useState } from 'react';
import { Icon } from '../components/Icon';
import type { Conversation } from '../protocol/types';

const MAX = 4000;

interface Props {
  conversation: Conversation;
  onSave: (instructions: string) => void;
  onClose: () => void;
}

/**
 * A persona or standing rule for ONE chat.
 *
 * It rides in the system message of every turn in this conversation, which is why it is capped
 * at a paragraph or two: standing guidance, not a document. Anything long belongs in the
 * messages, where it is said once.
 */
export function InstructionsDialog({ conversation, onSave, onClose }: Props) {
  const [text, setText] = useState(conversation.instructions);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const dirty = text.trim() !== conversation.instructions.trim();
  const save = () => {
    onSave(text.trim().slice(0, MAX));
    onClose();
  };

  return (
    <div className="lightbox" role="dialog" aria-modal="true" aria-label="Instructions for this chat" onClick={onClose}>
      <div className="instructions-card" onClick={(e) => e.stopPropagation()}>
        <h3>
          <Icon name="edit" size={14} />
          Instructions for “{conversation.title}”
        </h3>
        <p className="xs muted">
          Applies to this chat only, on every turn. Good for a persona or a standing rule; they add to Jarvis's
          normal rules and never let him claim something he has not checked.
        </p>
        <textarea
          ref={ref}
          className="input"
          rows={7}
          maxLength={MAX}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={'e.g. You are my HR-law sparring partner. Answer in Bulgarian, be blunt, and always\nname the risk before the option.'}
          aria-label="Instructions"
        />
        <div className="instructions-bar">
          <span className="xs muted">
            {text.trim().length}/{MAX}
          </span>
          <span className="row">
            {conversation.instructions.trim() && (
              <button type="button" className="btn btn-sm btn-secondary" onClick={() => setText('')}>
                Clear
              </button>
            )}
            <button type="button" className="btn btn-sm btn-secondary" onClick={onClose}>
              Cancel
            </button>
            <button type="button" className="btn btn-sm" onClick={save} disabled={!dirty}>
              Save
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}
