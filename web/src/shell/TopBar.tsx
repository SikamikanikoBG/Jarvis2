import { Icon } from '../components/Icon';
import { IconButton } from '../components/primitives';
import { effectiveTheme } from '../lib/theme';
import { selectUnreadCount } from '../store/selectors';
import { useStore } from '../store/store';
import { NAV } from './nav';

export function TopBar({ desktop }: { desktop: boolean }) {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const title = useStore((s) => (s.openConversationId ? s.conversations[s.openConversationId]?.title : undefined));
  const connection = useStore((s) => s.connection);
  const version = useStore((s) => s.version);
  const unread = useStore((s) => selectUnreadCount(s.conversations));
  const themePref = useStore((s) => s.themePref);
  const setTheme = useStore((s) => s.setTheme);
  const setSidebarOpen = useStore((s) => s.setSidebarOpen);
  const activeRun = useStore((s) => (s.openConversationId ? (s.runsByConversation[s.openConversationId] ?? []).some((id) => s.streams[id]) : false));

  const theme = effectiveTheme(themePref);
  const heading = view === 'chat' ? (title ?? 'New chat') : NAV.find((n) => n.view === view)?.label;

  return (
    <header className="topbar">
      {desktop ? (
        <>
          <div className="brand">
            <Icon name="radio" />
            <span>Jarvis</span>
            {version && <span className="brand-version">{version}</span>}
          </div>
          <nav className="nav-desktop" aria-label="Primary">
            {NAV.map((n) => (
              <button key={n.view} type="button" className={`nav-link${view === n.view ? ' active' : ''}`} onClick={() => setView(n.view)} aria-current={view === n.view ? 'page' : undefined}>
                <Icon name={n.icon} size={16} />
                {n.label}
                {n.view === 'chat' && unread > 0 && view !== 'chat' && <span className="nav-badge" aria-label={`${unread} unread`} />}
              </button>
            ))}
          </nav>
          <div className="topbar-title truncate" style={{ justifyContent: 'center' }}>
            {view === 'chat' && (
              <>
                <span className="truncate">{heading}</span>
                {activeRun && <span className="dot dot-accent dot-pulse" aria-label="Run in progress" />}
              </>
            )}
          </div>
        </>
      ) : (
        <>
          {view === 'chat' && <IconButton icon="menu" label="Conversations" onClick={() => setSidebarOpen(true)} />}
          <div className="topbar-title">
            <span className="truncate">{heading}</span>
            {view === 'chat' && activeRun && <span className="dot dot-accent dot-pulse" aria-label="Run in progress" />}
          </div>
        </>
      )}
      <div className="conn" title={`Event stream: ${connection}`}>
        <span className={`dot ${connection === 'open' ? 'dot-ok' : connection === 'closed' ? 'dot-danger' : 'dot-warn dot-pulse'}`} />
        {desktop && <span>{connection === 'open' ? 'live' : connection}</span>}
      </div>
      <IconButton icon={theme === 'dark' ? 'sun' : 'moon'} label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} />
    </header>
  );
}

export function BottomNav() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const unread = useStore((s) => selectUnreadCount(s.conversations));
  return (
    <nav className="bottomnav" aria-label="Primary">
      {NAV.map((n) => (
        <button key={n.view} type="button" className={view === n.view ? 'active' : ''} onClick={() => setView(n.view)} aria-current={view === n.view ? 'page' : undefined}>
          <Icon name={n.icon} />
          {n.label}
          {n.view === 'chat' && unread > 0 && view !== 'chat' && <span className="nav-badge" aria-label={`${unread} unread`} />}
        </button>
      ))}
    </nav>
  );
}
