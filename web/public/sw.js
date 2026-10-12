/* Jarvis service worker — caches the app shell only. Never touches /api or /ws.
   It also takes Jarvis's rings (docs/stories/11_call_me.md): a web push from the core becomes a
   notification, and tapping it opens the app on the incoming call. */
const SHELL = 'jarvis-shell-v5'; // v5: answering on the notification opens the call itself
const SHELL_URLS = ['/', '/index.html', '/manifest.webmanifest', '/icons/icon.svg'];

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
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
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
      data: { path: ring.path || '/', say: ring.say || '' },
    }),
  );
});

/* Answering (the Answer button or the notification itself): the app, straight into the call - the
   tap on the notification was the answer (answer=1). An open window is told (no reload);
   otherwise the app opens at the ring's address. Declining only dismisses it - the result is in
   the chat either way. */
self.addEventListener('notificationclick', (event) => {
  const ring = event.notification.data || {};
  event.notification.close();
  if (event.action === 'decline') return;
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
