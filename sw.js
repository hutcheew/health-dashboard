// Health Dashboard Service Worker — self-cleaning edition
// On activate, deletes stale caches and unregisters itself so the
// site runs without a SW going forward (the _headers file handles
// caching directives now).

const CACHE_NAME = 'health-dashboard-v2';

self.addEventListener('install', e => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    Promise.all([
      // Nuke all caches from previous versions
      caches.keys().then(keys =>
        Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
      ),
      // Unregister this SW — no longer needed
      self.registration.unregister(),
      self.clients.claim(),
    ])
  );
});

self.addEventListener('fetch', e => {
  e.respondWith(fetch(e.request).catch(() => new Response('Offline')));
});

self.addEventListener('push', e => {
  const data = e.data?.json() || { title: 'Health Alert', body: 'Check your dashboard' };
  e.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: '',
    badge: '',
  }));
});
