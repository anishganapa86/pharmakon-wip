// Minimal app-shell service worker. Precaches the shell on install, then
// serves same-origin GETs cache-first, populating the cache as requests come
// in (so the hashed JS/CSS bundles from each build get cached on first use).
//
// __PHARMAKON_BUILD_ID__ is replaced with a per-build id by vite.config.ts
// (build only; a dev server serves this literal placeholder). Baking the id
// into the cache name means a new build produces a byte-different sw.js, so
// the browser installs it as an update and `activate` evicts the previous
// build's cache -- without this, CACHE_NAME staying constant across deploys
// meant `activate` never had anything to evict, so a client's first-ever
// install could keep serving that stale snapshot indefinitely.
const CACHE_NAME = "pharmakon-shell-__PHARMAKON_BUILD_ID__";
const APP_SHELL = [
  "/",
  "/manifest.json",
  "/favicon.svg",
  "/icon-192.png",
  "/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)),
        ),
      ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  // The API runs on a different origin/port; only cache the app shell itself.
  if (
    request.method !== "GET" ||
    new URL(request.url).origin !== self.location.origin
  ) {
    return;
  }

  // The navigation request (index.html) names the hashed JS/CSS bundle for
  // the current build. Serving it cache-first pins a browser to whatever
  // bundle was cached on its first visit -- CACHE_NAME never changes between
  // deploys, so `activate` never evicts it and new features (e.g. the
  // Skincare tab) silently stop reaching returning users. Network-first here
  // means a new deploy is picked up on the next load; cache is only a
  // fallback for when the network is unavailable.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          return response;
        })
        .catch(() => caches.match(request)),
    );
    return;
  }

  // Hashed static assets are safe to serve cache-first: a new build gets a
  // new filename, so a stale cache entry is simply never requested again.
  event.respondWith(
    caches.match(request).then(
      (cached) =>
        cached ||
        fetch(request).then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          return response;
        }),
    ),
  );
});
