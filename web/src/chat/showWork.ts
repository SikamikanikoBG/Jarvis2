import { create } from 'zustand';

/**
 * "Show work": whether finished runs show their tools and thinking open, as they ran, or folded
 * to one row each. Folded by default; remembered on this device.
 */
const KEY = 'jarvis.showWork';

function read(): boolean {
  try {
    return localStorage.getItem(KEY) === '1';
  } catch {
    return false;
  }
}

interface ShowWork {
  showWork: boolean;
  setShowWork: (on: boolean) => void;
}

export const useShowWork = create<ShowWork>()((set) => ({
  showWork: read(),
  setShowWork: (on) => {
    try {
      localStorage.setItem(KEY, on ? '1' : '0');
    } catch {
      /* private mode: the choice lasts the session */
    }
    set({ showWork: on });
  },
}));
