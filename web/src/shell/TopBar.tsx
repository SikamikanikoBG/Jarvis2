import { useEffect } from 'react';
import { Icon } from '../components/Icon';
import { Drawer, IconButton } from '../components/primitives';
import { effectiveTheme } from '../lib/theme';
import { WEB_VERSION, shortWebVersion } from '../lib/version';
import { selectUnreadCount } from '../store/selectors';
import { useStore } from '../store/store';
import { ConversationMenu } from './ConversationMenu';
import { NAV_ALL, NAV_DESKTOP, NAV_MORE, NAV_PRIMARY } from './nav';

export function TopBar({ desktop }: { desktop: boolean }) {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const openId = useStore((s) => s.openConversationId);
  const title = useStore((s) => (s.openConversationId ? s.conversations[s.openConversationId]?.title : undefined));
  const connection = useStore((s) => s.connection);
  const version = useStore((s) => s.version);
  const unread = useStore((s) => selectUnreadCount(s.conversations));
  const themePref = useStore((s) => s.themePref);
  const setTheme = useStore((s) => s.setTheme);
  const setSidebarOpen = useStore((s) => s.setSidebarOpen);
  const activeRun = useStore((s) => (s.openConversationId ? (s.runsByConversation[s.openConversationId] ?? []).some((id) => s.streams[id]) : false));

  const notifyRuns = useStore((s) => s.notifyRuns);
  const setNotifyRuns = useStore((s) => s.setNotifyRuns);

  const theme = effectiveTheme(themePref);
  const heading = view === 'chat' ? (title ?? 'New chat') : NAV_ALL.find((n) => n.view === view)?.label;

  // Tab badge: "(2) Jarvis" while replies wait unread, like every mail client.
  useEffect(() => {
    document.title = unread > 0 ? `(${unread}) Jarvis` : 'Jarvis';
  }, [unread]);

  return (
    <header className="topbar">
      {desktop ? (
        <>
          <div className="brand">
            <Icon name="radio" />
            <span>Jarvis</span>
            <span className="brand-version" title={`core ${version ?? '?'} · web ${WEB_VERSION}`}>
              {version ?? '…'}
              <span className="brand-web">web {shortWebVersion()}</span>
            </span>
          </div>
          <nav className="nav-desktop" aria-label="Primary">
            {NAV_DESKTOP.map((n) => (
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
                <ConversationMenu key={openId ?? 'none'} />
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
          {view === 'chat' && <ConversationMenu key={openId ?? 'none'} />}
        </>
      )}
      <div className="conn" title={`Event stream: ${connection}`}>
        <span className={`dot ${connection === 'open' ? 'dot-ok' : connection === 'closed' ? 'dot-danger' : 'dot-warn dot-pulse'}`} />
        {desktop && <span>{connection === 'open' ? 'live' : connection}</span>}
      </div>
      {'Notification' in window && (
        <IconButton
          icon={notifyRuns ? 'bell' : 'bellOff'}
          label={notifyRuns ? 'Desktop notifications on — click to turn off' : 'Notify me when a run finishes in a background tab'}
          onClick={() => void setNotifyRuns(!notifyRuns)}
        />
      )}
      <IconButton icon={theme === 'dark' ? 'sun' : 'moon'} label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} />
    </header>
  );
}

export function BottomNav() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const unread = useStore((s) => selectUnreadCount(s.conversations));
  const setMoreOpen = useStore((s) => s.setMoreOpen);
  const moreActive = NAV_MORE.some((n) => n.view === view);
  return (
    <nav className="bottomnav" aria-label="Primary">
      {NAV_PRIMARY.map((n) => (
        <button key={n.view} type="button" className={view === n.view ? 'active' : ''} onClick={() => setView(n.view)} aria-current={view === n.view ? 'page' : undefined}>
          <Icon name={n.icon} />
          {n.label}
          {n.view === 'chat' && unread > 0 && view !== 'chat' && <span className="nav-badge" aria-label={`${unread} unread`} />}
        </button>
      ))}
      <button type="button" className={moreActive ? 'active' : ''} onClick={() => setMoreOpen(true)} aria-haspopup="dialog">
        <Icon name="more" />
        More
      </button>
    </nav>
  );
}

/** Mobile sheet with the feature screens that do not fit the bottom bar. */
export function MoreSheet() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const setMoreOpen = useStore((s) => s.setMoreOpen);
  return (
    <Drawer side="sheet" label="More screens" onClose={() => setMoreOpen(false)}>
      <div className="more-grid">
        {NAV_MORE.map((n) => (
          <button key={n.view} type="button" className={`more-item${view === n.view ? ' active' : ''}`} onClick={() => setView(n.view)}>
            <Icon name={n.icon} size={22} />
            <span>{n.label}</span>
          </button>
        ))}
      </div>
    </Drawer>
  );
}
