/* Jarvis service worker — caches the app shell only; never caches /api or /ws.
   It also takes Jarvis's rings (docs/stories/11_call_me.md): a web push from the core becomes a
   notification, and tapping it opens the app on the incoming call. Declining marks the chat read
   (its one /api call). */
const SHELL = 'jarvis-shell-v7'; // v7: a meeting recorded on the phone stops from its notification
const SHELL_URLS = ['/', '/index.html', '/manifest.webmanifest', '/icons/icon.svg'];
// The access token the app hands over (main.tsx), kept apart from the shell so a new shell keeps it.
const AUTH = 'jarvis-auth';

self.addEventListener('message', (event) => {
  if (event.data?.type !== 'jarvis.token' || !event.data.token) return;
  event.waitUntil(caches.open(AUTH).then((c) => c.put('/token', new Response(event.data.token))));
});

/* Declined: he has seen what it was about, so the chat is not left unread / waiting for him. */
async function markRead(conversationId) {
  const hit = await caches.match('/token', { cacheName: AUTH });
  const token = hit ? await hit.text() : '';
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  await fetch(`/api/conversations/${encodeURIComponent(conversationId)}`, {
    method: 'PATCH',
    headers,
    credentials: 'same-origin', // the jarvis_token cookie, when there is no handed-over token
    body: JSON.stringify({ unread: false }),
  }).catch(() => undefined);
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL)
      .then((c) => c.addAll(SHELL_URLS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL && k !== AUTH).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

/* A ring: {type:'call', title, body, path, say}. One notification at a time (the tag), and it
   sounds again for a new ring (renotify). It stays until it is answered or declined. */
self.addEventListener('push', (event) => {
  let ring = null;
  try {
    ring = event.data ? event.data.json() : null;
  } catch {
    ring = null;
  }
  if (!ring || ring.type !== 'call') return;
  event.waitUntil(
    self.registration.showNotification(ring.title || 'Jarvis', {
      body: ring.body || '',
      tag: 'jarvis-call',
      renotify: true,
      requireInteraction: true,
      icon: '/icons/icon-192.png',
      badge: '/icons/icon-192.png',
      vibrate: [400, 200, 400, 200, 400, 1000, 400, 200, 400],
      actions: [
        { action: 'answer', title: 'Answer' },
        { action: 'decline', title: 'Decline' },
      ],
      data: { path: ring.path || '/', say: ring.say || '', conversation_id: ring.conversation_id || '' },
    }),
  );
});

/* Answering (the Answer button or the notification itself): the app, straight into the call - the
   tap on the notification was the answer (answer=1). An open window is told (no reload);
   otherwise the app opens at the ring's address. Declining dismisses it and marks the chat read -
   the result is still in the chat. */
/* A meeting being recorded on this phone (src/meetings/recorder.ts): its notification is the
   screen while the phone is locked. Stop tells the page that records; tapping it opens Meetings.
   With no page left (Android killed it), the app opens with ?stop= and finishes it there. */
async function meetingClick(event, data) {
  const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  const open = windows.find((w) => new URL(w.url).origin === self.location.origin);
  if (event.action === 'meeting-stop') {
    event.notification.close();
    if (open) {
      open.postMessage({ type: 'jarvis.meeting.stop', id: data.id });
      return;
    }
    await self.clients.openWindow(`/meetings?stop=${encodeURIComponent(data.id)}`);
    return;
  }
  if (open) return open.focus();
  return self.clients.openWindow('/meetings');
}

self.addEventListener('notificationclick', (event) => {
  const ring = event.notification.data || {};
  if (ring.type === 'meeting') {
    event.waitUntil(meetingClick(event, ring));
    return;
  }
  event.notification.close();
  if (event.action === 'decline') {
    if (ring.conversation_id) event.waitUntil(markRead(ring.conversation_id));
    return;
  }
  const url = new URL(ring.path || '/', self.location.origin);
  if (ring.say) url.searchParams.set('say', ring.say);
  url.searchParams.set('answer', '1');
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
      const open = windows.find((w) => new URL(w.url).origin === self.location.origin);
      if (open) {
        open.postMessage({ type: 'jarvis.ring', path: ring.path || '/', say: ring.say || '', answered: true });
        return open.focus();
      }
      return self.clients.openWindow(url.href);
    }),
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/ws')) return;

  // Navigations: network first, shell fallback (offline reload still opens the app).
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(SHELL).then((c) => c.put('/index.html', copy));
          return res;
        })
        .catch(() => caches.match('/index.html')),
    );
    return;
  }

  // Hashed assets and icons: cache first.
  if (url.pathname.startsWith('/assets/') || url.pathname.startsWith('/icons/')) {
    event.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ??
          fetch(req).then((res) => {
            if (res.ok) {
              const copy = res.clone();
              caches.open(SHELL).then((c) => c.put(req, copy));
            }
            return res;
          }),
      ),
    );
  }
});
