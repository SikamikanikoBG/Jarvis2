import { useEffect, useState } from 'react';

/** Re-renders on an interval while `active`; used for "thinking… 12s" and relative timestamps. */
export function useTicker(intervalMs: number, active = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs, active]);
  return now;
}
