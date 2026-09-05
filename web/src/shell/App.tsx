import { useEffect } from 'react';
import { BoardsScreen } from '../boards/BoardsScreen';
import { ChatScreen } from '../chat/ChatScreen';
import { ErrorBoundary } from '../components/ErrorBoundary';
import { Drawer } from '../components/primitives';
import { Toast } from '../components/Toast';
import { KnowledgeScreen } from '../knowledge/KnowledgeScreen';
import { MeetingsScreen } from '../meetings/MeetingsScreen';
import { RunInspector } from '../runs/RunInspector';
import { RunsScreen } from '../runs/RunsScreen';
import { SchedulesScreen } from '../schedules/SchedulesScreen';
import { SettingsScreen } from '../settings/SettingsScreen';
import { Sidebar } from '../sidebar/Sidebar';
import { SkillsScreen } from '../skills/SkillsScreen';
import { StatusScreen } from '../status/StatusScreen';
import { useStore } from '../store/store';
import { TriageScreen } from '../triage/TriageScreen';
import { NAV_ALL } from './nav';
import { BottomNav, MoreSheet, TopBar } from './TopBar';
import { DESKTOP_QUERY, useMediaQuery } from './useMediaQuery';

const SCREENS = {
  chat: ChatScreen,
  runs: RunsScreen,
  settings: SettingsScreen,
  status: StatusScreen,
  boards: BoardsScreen,
  knowledge: KnowledgeScreen,
  skills: SkillsScreen,
  schedules: SchedulesScreen,
  meetings: MeetingsScreen,
  triage: TriageScreen,
} as const;

export function App() {
  const view = useStore((s) => s.view);
  const panelMode = useStore((s) => s.panelMode);
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const setSidebarOpen = useStore((s) => s.setSidebarOpen);
  const moreOpen = useStore((s) => s.moreOpen);
  const inspectorRunId = useStore((s) => s.inspectorRunId);
  const openInspector = useStore((s) => s.openInspector);
  const desktop = useMediaQuery(DESKTOP_QUERY);

  // Follow the OS theme live while the preference is "system".
  const themePref = useStore((s) => s.themePref);
  const setTheme = useStore((s) => s.setTheme);
  useEffect(() => {
    if (themePref !== 'system') return;
    const mq = window.matchMedia('(prefers-color-scheme: light)');
    const onChange = () => setTheme('system');
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, [themePref, setTheme]);

  const Screen = SCREENS[view];

  return (
    <div className="app">
      {!panelMode && <TopBar desktop={desktop} />}
      <div className="app-body">
        {desktop && !panelMode && view === 'chat' && <Sidebar />}
        <main className="main">
          <ErrorBoundary key={view} label={NAV_ALL.find((n) => n.view === view)?.label ?? view}>
            <Screen />
          </ErrorBoundary>
        </main>
        {inspectorRunId && desktop && (
          <aside className="inspector-col">
            <RunInspector runId={inspectorRunId} />
          </aside>
        )}
      </div>
      {!desktop && !panelMode && <BottomNav />}
      {!desktop && sidebarOpen && !panelMode && (
        <Drawer side="left" label="Conversations" onClose={() => setSidebarOpen(false)}>
          <Sidebar />
        </Drawer>
      )}
      {!desktop && moreOpen && !panelMode && <MoreSheet />}
      {inspectorRunId && !desktop && (
        <Drawer side="sheet" label="Run inspector" onClose={() => openInspector(null)}>
          <RunInspector runId={inspectorRunId} />
        </Drawer>
      )}
      <Toast />
    </div>
  );
}
