import { useState } from 'react';
import { Icon } from '../components/Icon';
import type { Plan } from '../protocol/types';

interface Props {
  plan: Plan;
  /** Open by default while the run is active; collapsed once it ends. */
  active: boolean;
}

/** Compact checklist of the run's plan (from `Run.plan` kept current by the plan.* events). */
export function PlanChecklist({ plan, active }: Props) {
  const [manual, setManual] = useState<boolean | null>(null);
  const open = manual ?? active;
  const done = plan.steps.filter((s) => s.status === 'done' || s.status === 'skipped').length;
  return (
    <details className="plan" open={open} onToggle={(e) => setManual(e.currentTarget.open)}>
      <summary>
        <Icon name="chevronRight" size={13} className="chev" />
        <Icon name="list" size={14} />
        <span className="truncate">{plan.goal}</span>
        <span className="plan-count">
          {done}/{plan.steps.length}
        </span>
      </summary>
      <ol className="plan-steps">
        {plan.steps.map((s, i) => (
          <li key={`${i}-${s.title}`} className={`plan-step ${s.status}`}>
            <span className="plan-box" aria-hidden="true">
              {s.status === 'done' && <Icon name="check" size={11} />}
              {s.status === 'in_progress' && <span className="dot dot-accent dot-pulse" />}
              {s.status === 'skipped' && <Icon name="x" size={11} />}
            </span>
            <span className="plan-title">
              {s.title}
              {s.note ? <span className="muted"> — {s.note}</span> : null}
            </span>
          </li>
        ))}
      </ol>
    </details>
  );
}
