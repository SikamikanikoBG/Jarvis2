import type { IconName } from '../components/Icon';
import type { View } from '../lib/router';

export const NAV: { view: View; label: string; icon: IconName }[] = [
  { view: 'chat', label: 'Chat', icon: 'chat' },
  { view: 'runs', label: 'Runs', icon: 'activity' },
  { view: 'settings', label: 'Settings', icon: 'sliders' },
  { view: 'status', label: 'Status', icon: 'server' },
];
