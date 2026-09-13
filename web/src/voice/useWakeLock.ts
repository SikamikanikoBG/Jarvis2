import { useEffect } from 'react';

/**
 * Keep the screen (and with it the page, the microphone and the voice) awake while `on`.
 *
 * A phone locks its screen after a minute of no touches, and a locked screen suspends the
 * page: the microphone stops, `speechSynthesis` goes quiet mid-sentence. The Screen Wake Lock
 * is released by the browser whenever the tab is hidden, so it is taken again on every return
 * to the foreground. Where the API is missing (older Firefox) the call still works; the screen
 * just has to be touched now and then, and the call screen's guard makes that safe.
 */
export function useWakeLock(on: boolean): void {
  useEffect(() => {
    if (!on || !('wakeLock' in navigator)) return;
    let lock: WakeLockSentinel | null = null;
    let cancelled = false;
    const take = async () => {
      if (cancelled || document.visibilityState !== 'visible') return;
      try {
        lock = await navigator.wakeLock.request('screen');
      } catch {
        lock = null; // low battery, or a browser that refuses: the call goes on without it
      }
    };
    const onVisible = () => void take();
    void take();
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      cancelled = true;
      document.removeEventListener('visibilitychange', onVisible);
      void lock?.release();
    };
  }, [on]);
}
