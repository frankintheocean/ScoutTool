// ScoutBot service worker — offline, read-only roster viewing.
//
// Scope: caches the app shell (index.html/app.js/styles.css) plus a
// small set of GET-only, read-oriented API responses (roster listing/
// search, favourites, raid candidates, stats, categories, locations)
// so the roster grid and Dashboard widgets still render something
// useful when Twitch/the network is unreachable, instead of a blank
// screen or a hung fetch. Nothing is ever cached or served for POST/
// PUT/DELETE — every mutation (add/remove/favourite/notes/etc.) always
// hits the network, and if the network is down it fails exactly as it
// did before this file existed (the existing api() error handling in
// app.js is unchanged). This is intentionally not a full PWA/offline-
// first rewrite — just enough to keep the roster browsable.
//
// Bump CACHE_VERSION whenever app shell files change so old clients
// pick up the new build instead of serving a stale cached shell
// forever (the activate handler below drops any cache under an older
// version name).
const CACHE_VERSION = "scoutbot-v4.2";
const SHELL_CACHE = `${CACHE_VERSION}-shell`;
const API_CACHE = `${CACHE_VERSION}-api-read`;

const SHELL_FILES = [
  "/",
  "/index.html",
  "/app.js",
  "/nobody-discovery.js",
  "/styles.css",
];

// Only these GET API paths are cached — deliberately a short allowlist
// (not "cache every GET") so nothing sensitive/settings-related or
// rapidly-changing (SSE, diagnostics) ends up served stale. Matched by
// prefix against the request's pathname.
const CACHEABLE_API_PREFIXES = [
  "/api/streamers",       // roster list + search + single-streamer lookups
  "/api/favourites",
  "/api/raid/candidates",
  "/api/dashboard-summary",
  "/api/stats",
  "/api/categories",
  "/api/locations",
  "/api/archived",
  "/api/inactive",
];

function isCacheableApiRequest(url) {
  if (url.origin !== self.location.origin) return false;
  if (!url.pathname.startsWith("/api/")) return false;
  return CACHEABLE_API_PREFIXES.some(prefix => url.pathname.startsWith(prefix));
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) =>
      // addAll fails the whole install if any single file 404s — fine
      // here since these files are always present in a working build;
      // if one genuinely is missing that's worth failing loudly on
      // rather than silently caching a partial, broken shell.
      cache.addAll(SHELL_FILES)
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key.startsWith("scoutbot-") && !key.startsWith(CACHE_VERSION))
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Never intercept anything but plain GET — every write goes straight
  // to the network untouched, same as if this service worker didn't
  // exist. This also skips SSE (/api/events, a long-lived GET stream)
  // implicitly since it's not in CACHEABLE_API_PREFIXES below.
  if (request.method !== "GET") return;

  const url = new URL(request.url);

  if (isCacheableApiRequest(url)) {
    event.respondWith(networkFirstThenCache(request, API_CACHE));
    return;
  }

  if (url.origin === self.location.origin && (SHELL_FILES.includes(url.pathname) || url.pathname === "/")) {
    event.respondWith(networkFirstThenCache(request, SHELL_CACHE));
  }
  // Everything else (fonts, other static assets) is left to the
  // browser's normal network handling — not part of the offline
  // roster-viewing goal, and not worth the added cache-invalidation
  // surface.
});

// Network-first, falling back to whatever's cached if the network call
// fails or times out. Roster data changes often (viewer counts, live
// status), so a stale-but-present offline copy is only ever used when
// there's truly no connection — a live network response always wins
// when available, rather than serving a possibly-minutes-old cached
// roster while online.
async function networkFirstThenCache(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    // Only cache genuinely successful responses — an error response
    // (4xx/5xx) cached here would otherwise get served back as if it
    // were good data the next time the network is down.
    if (response && response.ok) {
      try {
        await cache.put(request, response.clone());
        const keys = await cache.keys();
        await Promise.all(keys.slice(0, Math.max(0, keys.length - 100)).map(key => cache.delete(key)));
      } catch (err) { /* A full/offline cache must not fail a successful request. */ }
    }
    return response;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw err;
  }
}
