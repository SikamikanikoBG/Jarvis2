import { useEffect } from 'react';
import { ChatScreen } from '../chat/ChatScreen';
import { Drawer } from '../components/primitives';
import { Toast } from '../components/Toast';
import { RunInspector } from '../runs/RunInspector';
import { RunsScreen } from '../runs/RunsScreen';
import { SettingsScreen } from '../settings/SettingsScreen';
import { Sidebar } from '../sidebar/Sidebar';
import { StatusScreen } from '../status/StatusScreen';
import { useStore } from '../store/store';
import { BottomNav, TopBar } from './TopBar';
import { DESKTOP_QUERY, useMediaQuery } from './useMediaQuery';

export function App() {
  const view = useStore((s) => s.view);
  const panelMode = useStore((s) => s.panelMode);
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const setSidebarOpen = useStore((s) => s.setSidebarOpen);
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

  const screen = view === 'chat' ? <ChatScreen /> : view === 'runs' ? <RunsScreen /> : view === 'settings' ? <SettingsScreen /> : <StatusScreen />;

  return (
    <div className="app">
      {!panelMode && <TopBar desktop={desktop} />}
      <div className="app-body">
        {desktop && !panelMode && <Sidebar />}
        <main className="main">{screen}</main>
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
      {inspectorRunId && !desktop && (
        <Drawer side="sheet" label="Run inspector" onClose={() => openInspector(null)}>
          <RunInspector runId={inspectorRunId} />
        </Drawer>
      )}
      <Toast />
    </div>
  );
}
