/**
 * Hold the screen still for a call on a phone: fullscreen, portrait, and nothing of the
 * browser's own around the page. A phone at an ear turns, and the page turned with it
 * (2026-09-19, "the screen rotates"); the address bar and the tab strip were within a
 * cheek's reach. Orientation can only be locked in fullscreen (or an installed app), so the
 * two go together. Both must be asked for inside the tap that starts the call.
 *
 * Nothing here is fatal: a browser that refuses either leaves the call exactly as it was.
 */

const touch = () => typeof window !== 'undefined' && window.matchMedia('(pointer: coarse)').matches;

export function holdScreen(): void {
  if (!touch()) return;
  const el = document.documentElement;
  const lock = () => {
    const o = screen.orientation as ScreenOrientation & { lock?: (o: string) => Promise<void> };
    o.lock?.('portrait-primary').catch(() => undefined);
  };
  if (document.fullscreenElement) {
    lock();
    return;
  }
  // Locking without fullscreen works in an installed app; try it either way.
  if (el.requestFullscreen) el.requestFullscreen({ navigationUI: 'hide' }).then(lock, lock);
  else lock();
}

export function releaseScreen(): void {
  if (!touch()) return;
  const o = screen.orientation as ScreenOrientation & { unlock?: () => void };
  try {
    o.unlock?.();
  } catch {
    /* not locked */
  }
  if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined);
}
