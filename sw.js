// Health Dashboard Service Worker
const CACHE_NAME = 'health-dashboard-v1';

self.addEventListener('install', e => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(self.clients.claim());
});

// Pass through all requests — no offline caching needed
self.addEventListener('fetch', e => {
  e.respondWith(fetch(e.request).catch(() => new Response('Offline')));
});

// Handle push notifications
self.addEventListener('push', e => {
  const data = e.data?.json() || { title: 'Health Alert', body: 'Check your dashboard' };
  e.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: '',
    badge: '',
  }));
});
