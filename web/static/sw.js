/* Exists so the dashboard can be installed to a phone home screen. It caches
 * nothing on purpose: every byte this page shows is a run's present state, and
 * a cached answer to "is it still alive" is worse than no answer. With no fetch
 * handler, requests go straight to the network.
 *
 * It also evicts whatever came before it. This origin previously served the
 * predecessor-dashboard dashboard, whose worker cached an app shell AND intercepted fetches,
 * so an installed phone would keep serving the old application from disk long
 * after the server behind it changed. Taking the registration over is not
 * enough; the caches have to go too.
 */
self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});
