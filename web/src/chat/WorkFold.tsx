import { useState, type ReactNode } from 'react';
import { Icon } from '../components/Icon';
import { useShowWork } from './showWork';

interface Props {
  steps: number;
  errors: number;
  children: ReactNode;
}

/** A finished run's tools and thinking behind one row; open, they are exactly what ran. */
export function WorkFold({ steps, errors, children }: Props) {
  const showWork = useShowWork((s) => s.showWork);
  // The switch in the chat menu opens or folds them all; a tap opens or folds one, until the
  // switch is flipped again.
  const [tapped, setTapped] = useState<{ under: boolean; open: boolean } | null>(null);
  const open = tapped?.under === showWork ? tapped.open : showWork;
  const setOpen = (next: boolean) => setTapped({ under: showWork, open: next });
  const label = steps === 0 ? 'Thought' : `Worked · ${steps} step${steps === 1 ? '' : 's'}`;
  return (
    <div className={`work${open ? ' open' : ''}`}>
      <button type="button" className="work-row" aria-expanded={open} onClick={() => setOpen(!open)}>
        <Icon name="chevronRight" size={14} className="chev" />
        <Icon name={steps === 0 ? 'brain' : 'wrench'} size={14} />
        <span>{label}</span>
        {errors > 0 && (
          <span className="chip chip-danger">
            {errors} failed
          </span>
        )}
      </button>
      {open && <div className="work-body">{children}</div>}
    </div>
  );
}
