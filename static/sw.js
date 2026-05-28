const CACHE_NAME = 'nutrisnap-cache-v5.6.28';
const CACHE_PREFIX = 'nutrisnap-cache-';

self.addEventListener('install', () => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const cacheNames = await caches.keys();
        await Promise.all(
            cacheNames
                .filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
                .map((name) => caches.delete(name))
        );
        await self.clients.claim();
    })());
});
const ASSETS_TO_CACHE = [
  '/',
  '/static/favicon.ico',
  '/static/icon-light.png',
  '/static/icon-dark.png',
  '/static/apple-touch-icon.png',
  '/static/vendor/tailwindcss-forms-container-queries.js',
  '/static/vendor/chart.umd.min.js',
  '/static/vendor/html5-qrcode.min.js'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      console.log('[Service Worker] Pre-caching static assets');
      return cache.addAll(ASSETS_TO_CACHE);
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cache => {
          if (cache !== CACHE_NAME) {
            console.log('[Service Worker] Clearing old cache');
            return caches.delete(cache);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const requestUrl = new URL(event.request.url);

  // Ignore non-GET requests and API requests
  if (event.request.method !== 'GET' || requestUrl.pathname.startsWith('/api/')) {
    return;
  }

  event.respondWith(
    caches.match(event.request).then(cachedResponse => {
      if (cachedResponse) {
        return cachedResponse;
      }

      return fetch(event.request).then(networkResponse => {
        if (!networkResponse || networkResponse.status !== 200) {
          return networkResponse;
        }

        // Dynamically cache local static assets as they are requested.
        if (requestUrl.origin === self.location.origin && requestUrl.pathname.startsWith('/static/')) {
          const responseToCache = networkResponse.clone();
          caches.open(CACHE_NAME).then(cache => {
            cache.put(event.request, responseToCache);
          });
        }

        return networkResponse;
      }).catch(() => {
        // Fallback for HTML navigation requests (load SPA root offline)
        if (event.request.mode === 'navigate') {
          return caches.match('/');
        }
      });
    })
  );
});
