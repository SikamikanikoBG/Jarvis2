import { navigate } from './router';

const KEY = 'jarvis.notifyRuns';

export function readNotifyPref(): boolean {
  try {
    return localStorage.getItem(KEY) === '1' && 'Notification' in window && Notification.permission === 'granted';
  } catch {
    return false;
  }
}

export function writeNotifyPref(on: boolean): void {
  try {
    localStorage.setItem(KEY, on ? '1' : '0');
  } catch {
    /* private mode: the toggle just does not persist */
  }
}

/** One desktop notification; clicking it brings the tab and the conversation forward. */
export function notifyDesktop(title: string, body: string, conversationId: string): void {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  try {
    const n = new Notification(title, { body: body.slice(0, 160), tag: `jarvis-${conversationId}`, silent: false });
    n.onclick = () => {
      window.focus();
      navigate('chat', conversationId);
      n.close();
    };
  } catch {
    /* some browsers throw in a worker-less context; nothing to do */
  }
}
