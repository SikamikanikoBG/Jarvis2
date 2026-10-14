import { phoneRecorder } from './meetings/recorder';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './styles/tokens.css';
import './styles/base.css';
import './styles/layout.css';
import './styles/chat.css';
import './styles/panels.css';
import './styles/features.css';
import './styles/mail.css';
import { getToken } from './lib/token';
import { App } from './shell/App';
import { useStore } from './store/store';

useStore.getState().boot();

const rootEl = document.getElementById('root');
if (!rootEl) throw new Error('#root missing');
createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// A meeting this phone was recording when the page was last closed or killed: its pieces go up
// now, whatever screen is open (src/meetings/recorder.ts).
void phoneRecorder.recover();

if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => undefined);
    // The worker answers a ring's Decline by marking the chat read; it has no localStorage, so it
    // is handed the token here (the cookie is only set when the app was opened with ?token=).
    const token = getToken();
    if (token) void navigator.serviceWorker.ready.then((reg) => reg.active?.postMessage({ type: 'jarvis.token', token }));
  });
}
