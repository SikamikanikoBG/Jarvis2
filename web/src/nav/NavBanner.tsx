import { Icon } from '../components/Icon';
import { useStore } from '../store/store';
import { useWakeLock } from '../voice/useWakeLock';

function dist(m: number | null): string {
  if (m === null) return '';
  if (m < 1000) return `${Math.round(m / 10) * 10} m`;
  return `${(m / 1000).toFixed(1)} km`;
}

/**
 * Guidance on the way (core features/navigation.py): the next turn, how far, what is left - over
 * every screen and over a call. The screen stays on while it runs, so the phone keeps sending
 * where he is; each turn is also spoken on a call, or shown and felt without one.
 */
export function NavBanner() {
  const nav = useStore((s) => s.nav);
  const stopNav = useStore((s) => s.stopNav);
  useWakeLock(nav !== null);
  if (!nav) return null;
  const left = [dist(nav.left_m), nav.eta_s !== null ? `${Math.max(1, Math.round(nav.eta_s / 60))} min` : ''].filter(Boolean).join(' · ');
  return (
    <div className="nav-banner" role="status" aria-live="polite">
      <div className="nav-banner-main">
        {nav.to_next_m !== null && <span className="nav-banner-dist">{dist(nav.to_next_m)}</span>}
        <span className="nav-banner-what">
          {nav.next_instruction ?? `To ${nav.destination}`}
          {nav.next_landmark && <span className="nav-banner-mark"> · at {nav.next_landmark}</span>}
        </span>
      </div>
      <div className="nav-banner-meta">
        <span className="truncate">
          To {nav.destination}
          {left && ` · ${left} left`}
        </span>
        <button type="button" className="nav-banner-stop" onClick={() => void stopNav()} aria-label="Stop guidance">
          <Icon name="x" size={14} /> Stop
        </button>
      </div>
    </div>
  );
}
