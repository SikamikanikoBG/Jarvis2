import { useState } from 'react';
import { Icon } from '../components/Icon';
import { formatDuration, prettyJson, summariseArgs } from '../lib/format';
import type { ToolResultKind } from '../protocol/types';
import type { ToolCardModel } from '../store/transcript';

const KIND_CHIP: Record<ToolResultKind, string> = {
  data: 'chip-ok',
  empty: 'chip',
  partial: 'chip-warn',
  error: 'chip-danger',
};

export function ToolCard({ card }: { card: ToolCardModel }) {
  const [open, setOpen] = useState(false);
  const kind = card.result?.kind ?? null;
  const resultText = card.result?.text ?? card.resultText;
  const rejected = card.confirm?.approved === false;
  const count = card.result?.count ?? null;
  const total = card.result?.total ?? null;
  const dot = card.name.indexOf('.');
  const provider = dot > 0 ? card.name.slice(0, dot) : null;
  const tool = dot > 0 ? card.name.slice(dot + 1) : card.name;
  return (
    <details className={`tool${card.pending && !rejected ? ' pending' : ''}`} open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary aria-label={`Tool ${card.name}`}>
        <Icon name="wrench" size={15} className="muted" />
        {provider && <span className="chip tool-provider">{provider}</span>}
        <span className="tool-name">{tool}</span>
        <span className="tool-args" title={summariseArgs(card.arguments, 400)}>
          {summariseArgs(card.arguments)}
        </span>
        <span className="tool-meta">
          {card.confirm?.approved === null && <span className="chip chip-warn">awaiting approval</span>}
          {card.confirm?.approved === true && <span className="chip chip-outline">approved</span>}
          {rejected && <span className="chip chip-danger">rejected</span>}
          {card.durationMs !== null && <span className="chip chip-outline">{formatDuration(card.durationMs)}</span>}
          {kind && (
            <span className={`chip ${KIND_CHIP[kind]}`}>
              {kind}
              {count !== null && (
                <>
                  {' '}
                  {count}
                  {total !== null ? `/${total}` : ''}
                </>
              )}
            </span>
          )}
          {card.readOnly === false && <span className="chip chip-outline" title="This tool changes something">writes</span>}
        </span>
      </summary>
      {open && (
        <div className="tool-body">
          {card.confirm && (
            <div>
              <div className="label">Confirmation</div>
              <div className="small">
                {card.confirm.reason}
                {card.confirm.note ? ` — note: ${card.confirm.note}` : ''}
              </div>
            </div>
          )}
          <div>
            <div className="label">Arguments</div>
            <pre>{prettyJson(card.arguments)}</pre>
          </div>
          <div>
            <div className="label">Result</div>
            {card.pending && !rejected ? (
              <div className="small muted">Running…</div>
            ) : (
              <pre className={kind === 'error' ? 'error' : ''}>{card.result?.error ?? resultText ?? (rejected ? 'Not executed.' : '')}</pre>
            )}
            {card.result?.cursor && <div className="field-hint">More available (cursor {card.result.cursor})</div>}
          </div>
        </div>
      )}
    </details>
  );
}
