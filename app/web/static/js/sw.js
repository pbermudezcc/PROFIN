/* FAMILYFIN service worker: caches ONLY static files (CSS, JS, icons) so
   the home-screen app opens fast. Pages and API responses (financial data)
   always go to the network and are never stored on the device.
   Static URLs carry a content hash (?v=…), so a changed file is a new URL. */
const CACHE = "familyfin-static-v2";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || !url.pathname.startsWith("/static/")) {
    return; // default network handling, nothing cached
  }
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(event.request);
      if (cached) return cached;
      const response = await fetch(event.request);
      if (response.ok && url.searchParams.has("v")) {
        // Drop older versions of the same file, then keep this one.
        for (const key of await cache.keys()) {
          if (new URL(key.url).pathname === url.pathname) await cache.delete(key);
        }
        await cache.put(event.request, response.clone());
      }
      return response;
    })
  );
});
