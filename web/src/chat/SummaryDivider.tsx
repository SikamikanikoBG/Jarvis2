import { useState } from 'react';
import { Icon } from '../components/Icon';

/** Thin marker where older turns were compacted; expands to the summary the model sees. */
export function SummaryDivider({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="summary-divider" role="separator" aria-label="Earlier messages summarised">
      <button type="button" className="summary-toggle" onClick={() => setOpen(!open)} aria-expanded={open}>
        <Icon name="chevronRight" size={12} className={open ? 'chev open' : 'chev'} />
        earlier messages summarised
      </button>
      {open && <div className="summary-text">{text}</div>}
    </div>
  );
}
