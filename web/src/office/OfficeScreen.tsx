import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { IconButton } from '../components/primitives';
import { getToken } from '../lib/token';
import { errorText, useLoader } from '../lib/useLoader';
import { useStore } from '../store/store';

/**
 * The Office: Jarvis's runs as pixel-art characters (pixel-agents' webview, which the core serves
 * at /pixel-office/ and feeds from the run events — features/office.py). A click on a character
 * opens its conversation.
 */
export function OfficeScreen() {
  const openConversation = useStore((s) => s.openConversation);
  const notify = useStore((s) => s.notify);
  const load = useCallback(() => api.office.agents(), []);
  const { data, error } = useLoader(load, 'office');

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (e.origin !== window.location.origin) return;
      const msg = e.data as { source?: unknown; type?: unknown; id?: unknown } | null;
      if (msg?.source !== 'pixel-office' || msg.type !== 'focusAgent' || typeof msg.id !== 'number') return;
      const id = msg.id;
      api.office
        .agents()
        .then((r) => {
          const conversationId = r.agents.find((a) => a.id === id)?.conversation_id;
          if (conversationId) void openConversation(conversationId);
        })
        .catch((err: unknown) => notify(`Could not open that conversation: ${errorText(err)}`, 'error'));
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [openConversation, notify]);

  // Fullscreen: the button's target is this screen's div, so the button itself stays in it and
  // can turn the office back off; Esc exits the same way the browser always does.
  const screenRef = useRef<HTMLDivElement>(null);
  const [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    const onChange = () => setFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);
  const toggleFullscreen = useCallback(() => {
    const el = screenRef.current;
    if (!el) return;
    if (document.fullscreenElement) void document.exitFullscreen();
    else void el.requestFullscreen().catch(() => notify('The browser refused fullscreen', 'error'));
  }, [notify]);

  if (error) {
    return (
      <div className="screen">
        <div className="screen-inner">
          <h1>Office</h1>
          <p className="muted">{error}</p>
        </div>
      </div>
    );
  }
  if (data && !data.built) {
    return (
      <div className="screen">
        <div className="screen-inner">
          <h1>Office</h1>
          <p className="muted">
            The office is not built on this core. Run <code>sh web/office/build.sh</code> (the Docker image does it
            for you) and restart the core.
          </p>
        </div>
      </div>
    );
  }
  const token = getToken();
  const src = `/pixel-office/${token ? `?token=${encodeURIComponent(token)}` : ''}`;
  return (
    <div className="office-screen" ref={screenRef}>
      {data && <iframe className="office-frame" src={src} title="Office" />}
      <IconButton
        className="office-fs-btn"
        icon={fullscreen ? 'minimize' : 'maximize'}
        active={fullscreen}
        label={fullscreen ? 'Leave fullscreen (Esc)' : 'Fullscreen'}
        onClick={toggleFullscreen}
      />
    </div>
  );
}
