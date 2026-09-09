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
import type { View } from '../lib/router';
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

/** Screens with a search box of their own, so Ctrl/⌘+K focuses that instead of leaving. */
const FILTERED_VIEWS = new Set<View>(['schedules', 'meetings', 'skills', 'boards', 'knowledge']);

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

  // Keyboard shortcuts, one map: Ctrl/⌘+Shift+O new chat · Ctrl/⌘+K search · Esc stop the run ·
  // Shift+Esc focus the composer. (The same keys ChatGPT / claude.ai use where they overlap.)
  const newChat = useStore((s) => s.newChat);
  const stop = useStore((s) => s.stop);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.shiftKey && e.key.toLowerCase() === 'o') {
        e.preventDefault();
        newChat();
      } else if (mod && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        // The screen already showing a filter bar owns the shortcut: jumping to Chat to focus a
        // different search than the one being asked for is the wrong answer.
        if (!FILTERED_VIEWS.has(useStore.getState().view)) {
          useStore.getState().setView('chat');
          if (!desktop) setSidebarOpen(true);
        }
        setTimeout(() => window.dispatchEvent(new CustomEvent('jarvis:focus-search')), 0);
      } else if (e.key === 'Escape' && e.shiftKey) {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent('jarvis:focus-composer'));
      } else if (e.key === 'Escape' && !(e.target instanceof HTMLInputElement) && !(e.target instanceof HTMLTextAreaElement)) {
        stop();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [newChat, stop, desktop, setSidebarOpen]);

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
