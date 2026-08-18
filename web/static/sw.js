/* Caches the shell so the app opens instantly and survives a dead network.
 * Never caches data.
 *
 * That distinction is the whole design. Every byte under /api/ is a run's
 * present state, and a cached answer to "is it still alive" is worse than no
 * answer -- so API requests are network-only and fail loudly. The shell is the
 * opposite: it changes when the app is deployed and never otherwise, so it is
 * served from cache and refreshed in the background.
 *
 * Bump SHELL when the shell files change; the activate handler drops every
 * other cache, which is also how this origin's previous occupant (predecessor-dashboard,
 * which cached an app shell AND intercepted fetches) gets evicted from phones
 * that still have it installed.
 */
const SHELL = "lmloop-shell-v1";
const ASSETS = [
  "/",
  "/static/app.js",
  "/static/style.css",
  "/static/icon-192.png",
  "/manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== SHELL).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== location.origin) return;

  // Data, and anything to do with signing in, must never be answered from disk.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/login")
      || url.pathname.startsWith("/oauth") || url.pathname === "/health") {
    return;
  }

  // Shell: serve from cache, then update it in the background so the next
  // launch has the new build without ever blocking this one.
  event.respondWith(
    caches.match(request).then((hit) => {
      const live = fetch(request)
        .then((response) => {
          if (response.ok) caches.open(SHELL).then((cache) => cache.put(request, response.clone()));
          return response;
        })
        .catch(() => hit);
      return hit || live;
    }),
  );
});
