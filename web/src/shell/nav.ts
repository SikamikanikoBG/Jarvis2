import type { IconName } from '../components/Icon';
import type { View } from '../lib/router';

export interface NavItem {
  view: View;
  label: string;
  icon: IconName;
}

/** The four that stay in the mobile bottom bar. */
export const NAV_PRIMARY: NavItem[] = [
  { view: 'chat', label: 'Chat', icon: 'chat' },
  { view: 'runs', label: 'Runs', icon: 'activity' },
  { view: 'settings', label: 'Settings', icon: 'sliders' },
  { view: 'status', label: 'Status', icon: 'server' },
];

/** Feature screens: in the desktop header, behind "More" on mobile. */
export const NAV_MORE: NavItem[] = [
  { view: 'boards', label: 'Boards', icon: 'boards' },
  { view: 'knowledge', label: 'Knowledge', icon: 'graph' },
  { view: 'skills', label: 'Skills', icon: 'book' },
  { view: 'schedules', label: 'Schedules', icon: 'calendar' },
  { view: 'meetings', label: 'Meetings', icon: 'headphones' },
  { view: 'triage', label: 'Triage', icon: 'inbox' },
];

/** Desktop header order: Chat, then the feature screens, then the instrument screens. */
export const NAV_DESKTOP: NavItem[] = [...NAV_PRIMARY.slice(0, 1), ...NAV_MORE, ...NAV_PRIMARY.slice(1)];

export const NAV_ALL: NavItem[] = [...NAV_PRIMARY, ...NAV_MORE];
