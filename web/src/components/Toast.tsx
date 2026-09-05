import { useEffect } from 'react';
import { useStore } from '../store/store';
import { IconButton } from './primitives';

export function Toast() {
  const notice = useStore((s) => s.notice);
  const dismiss = useStore((s) => s.dismissNotice);
  useEffect(() => {
    if (!notice) return;
    const id = setTimeout(dismiss, notice.level === 'error' ? 8000 : 4000);
    return () => clearTimeout(id);
  }, [notice, dismiss]);
  if (!notice) return null;
  return (
    <div className={`toast ${notice.level === 'error' ? 'error' : ''}`} role="status" aria-live="polite">
      <span className="grow">{notice.text}</span>
      <IconButton icon="x" label="Dismiss" size="sm" onClick={dismiss} />
    </div>
  );
}
