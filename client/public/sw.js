const SW_VERSION = 2;
const CACHE_NAME = `pharmakon-shell-v${SW_VERSION}-__PHARMAKON_BUILD_ID__`;
const SHELL = [
  "/",
  "/login.html",
  "/signup.html",
  "/manifest.json",
  "/favicon.svg",
  "/icon-192.png",
  "/icon-512.png",
  "/apple-touch-icon.png",
];
const STATIC_PREFIXES = ["/assets/"];
const STATIC_FILES = new Set([
  "/manifest.json",
  "/favicon.svg",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-maskable-192.png",
  "/icon-maskable-512.png",
  "/apple-touch-icon.png",
]);
const SHELL_PAGES = new Set(["/", "/login.html", "/signup.html"]);

function isStaticAsset(url) {
  return (
    STATIC_FILES.has(url.pathname) ||
    STATIC_PREFIXES.some((prefix) => url.pathname.startsWith(prefix))
  );
}

function isCacheable(response) {
  if (!response || response.status !== 200 || response.type === "opaque") {
    return false;
  }
  const control = (response.headers.get("Cache-Control") || "").toLowerCase();
  return !control.includes("no-store") && !control.includes("private");
}

function clearAllCaches() {
  return caches
    .keys()
    .then((names) => Promise.all(names.map((name) => caches.delete(name))));
}

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name)),
        ),
      ),
  );
  self.clients.claim();
});

self.addEventListener("message", (event) => {
  if (event.data === "clear-caches") {
    event.waitUntil(clearAllCaches());
  }
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }
  if (request.headers.has("Authorization") || request.headers.has("Range")) {
    return;
  }

  if (request.mode === "navigate") {
    if (!SHELL_PAGES.has(url.pathname)) {
      return;
    }
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (isCacheable(response)) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => caches.match(request)),
    );
    return;
  }

  if (!isStaticAsset(url)) {
    return;
  }

  event.respondWith(
    caches.match(request).then(
      (cached) =>
        cached ||
        fetch(request).then((response) => {
          if (isCacheable(response)) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return response;
        }),
    ),
  );
});
