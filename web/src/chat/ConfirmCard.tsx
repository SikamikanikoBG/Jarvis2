import { useState } from 'react';
import { Icon } from '../components/Icon';
import { prettyJson } from '../lib/format';
import { useStore } from '../store/store';

interface Props {
  runId: string;
  callId: string;
  name: string;
  arguments: Record<string, unknown>;
  reason: string;
}

/** Approve / reject a tool call the engine paused on. Disappears when `tool.confirm_resolved` arrives. */
export function ConfirmCard({ runId, callId, name, arguments: args, reason }: Props) {
  const confirmTool = useStore((s) => s.confirmTool);
  const [note, setNote] = useState('');
  const [sent, setSent] = useState<boolean | null>(null);

  const decide = (approved: boolean) => {
    setSent(approved);
    confirmTool(runId, callId, approved, note);
  };

  return (
    <div className="confirm" role="group" aria-label={`Approve ${name}?`}>
      <div className="confirm-head">
        <Icon name="alert" size={18} />
        <span>
          Approve <span className="mono">{name}</span>?
        </span>
      </div>
      <div className="confirm-reason">{reason}</div>
      <pre>{prettyJson(args)}</pre>
      <div className="confirm-actions">
        <input
          className="input"
          placeholder="Note for Jarvis (optional)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          disabled={sent !== null}
          aria-label="Note"
        />
        <button type="button" className="btn btn-primary" onClick={() => decide(true)} disabled={sent !== null}>
          <Icon name="check" size={16} />
          {sent === true ? 'Approving…' : 'Approve'}
        </button>
        <button type="button" className="btn btn-secondary" onClick={() => decide(false)} disabled={sent !== null}>
          <Icon name="x" size={16} />
          {sent === false ? 'Rejecting…' : 'Reject'}
        </button>
      </div>
    </div>
  );
}
