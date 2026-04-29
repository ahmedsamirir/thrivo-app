/*
 * ═══════════════════════════════════════════════════════════════════════
 *  Thrivo Service Worker
 *  ─────────────────────────────────────────────────────────────────────
 *  Strategies:
 *    - HTML / Streamlit WebSocket:  NETWORK ONLY (never cache live UI)
 *    - Static assets (icons, fonts, manifest):  CACHE FIRST with update
 *    - Everything else:  NETWORK with offline fallback
 *
 *  Why not "offline by default"? Streamlit is a live server-rendered app —
 *  serving a cached version would show stale UI and break WebSockets. We
 *  cache ONLY the shell assets so the app loads faster, not offline.
 *
 *  Bump CACHE_VERSION whenever icons or manifest change, to force refresh.
 * ═══════════════════════════════════════════════════════════════════════
 */

const CACHE_VERSION = "thrivo-v1.0.0";
const STATIC_CACHE  = `${CACHE_VERSION}-static`;

const STATIC_ASSETS = [
  "/static/manifest.json",
  "/static/icon-180.png",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

// ── Install: pre-cache the static shell ──
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => cache.addAll(STATIC_ASSETS).catch(() => null))
      .then(() => self.skipWaiting())
  );
});

// ── Activate: clean up old caches ──
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== STATIC_CACHE)
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch: routing by request type ──
self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Never interfere with non-GET requests (Streamlit uses POST for state)
  if (req.method !== "GET") return;

  // Never cache Streamlit's WebSocket or internal endpoints
  if (url.pathname.startsWith("/_stcore") ||
      url.pathname.startsWith("/stream") ||
      url.pathname.includes("/healthz")) {
    return; // let it pass through normally
  }

  // Static assets: cache-first with background update
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const netFetch = fetch(req).then((netResp) => {
          if (netResp && netResp.status === 200) {
            caches.open(STATIC_CACHE).then((c) => c.put(req, netResp.clone()));
          }
          return netResp;
        }).catch(() => cached);
        return cached || netFetch;
      })
    );
    return;
  }

  // HTML / everything else: network-first with cached fallback
  if (req.mode === "navigate" ||
      (req.headers.get("accept") || "").includes("text/html")) {
    event.respondWith(
      fetch(req).catch(() =>
        caches.match(req).then((c) => c || new Response(
          "<h1 style='font-family:sans-serif;text-align:center;padding:40px;'>" +
          "📡 Thrivo is offline.<br><small>Reconnect to continue.</small></h1>",
          { status: 503, headers: { "Content-Type": "text/html" } }
        ))
      )
    );
    return;
  }

  // Default: pass-through
});
