// Minimal service worker — needed so browsers offer the PWA install prompt.
// No caching: the app always runs locally from FastAPI so offline support
// isn't needed and would only cause stale-file headaches during development.
self.addEventListener('install',  () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
