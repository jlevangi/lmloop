/* Deliberately does nothing but exist.
 *
 * A service worker is what lets the dashboard be installed to a phone home
 * screen, which is the whole reason it is here. Caching, though, is exactly
 * wrong for this page: every byte it shows is a run's present state, and a
 * cached answer to "is it still alive" is worse than no answer.
 */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
