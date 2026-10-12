// Project City service worker. Installability needs a registered SW with a fetch
// handler; this one also makes the app usable offline WITHOUT ever caching live
// data. Bump VERSION to roll the cache (old caches are purged on activate).
const VERSION = "pc-v1";
// Precached app shell: only the always-public, static assets. The pages (/ and
// /city) are auth-gated and dynamic, so they are cached at runtime on first
// successful visit instead of precached.
const SHELL = [
  "/offline.html",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches
      .open(VERSION)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((ks) =>
        Promise.all(ks.filter((k) => k !== VERSION).map((k) => caches.delete(k))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return; // writes always hit the network
  const url = new URL(req.url);
  if (url.origin !== location.origin) return; // let the font CDN etc. go to network
  // NEVER cache the API — project data must stay live (the list polls every 4s
  // and does optimistic writes; a stale cache would show the wrong city).
  if (url.pathname.startsWith("/api/")) return;

  // Navigations: network-first so you always get the freshest page when online,
  // falling back to the last-seen page (or the offline shell) when not.
  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res && res.status === 200 && res.type === "basic") {
            const copy = res.clone();
            caches.open(VERSION).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() =>
          caches
            .match(req)
            .then((r) => r || caches.match("/offline.html")),
        ),
    );
    return;
  }

  // Static same-origin assets (icons, manifest): stale-while-revalidate — serve
  // the cache instantly, refresh it in the background.
  e.respondWith(
    caches.match(req).then((cached) => {
      const net = fetch(req)
        .then((res) => {
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(VERSION).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || net;
    }),
  );
});
