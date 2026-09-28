// Service worker minimal : l'app reste installable et s'ouvre même sans réseau (dernière version vue).
// Toujours le réseau d'abord, pour ne jamais afficher de données périmées quand le serveur répond.
const CACHE = "lab-inventory-v1";
const SHELL = ["./", "favicon.svg", "manifest.webmanifest", "fonts/inter-latin.woff2", "fonts/inter-latin-ext.woff2"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok && (SHELL.some(p => url.pathname.endsWith(p.replace("./", "/"))) || url.pathname === "/")) {
      const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy));
    }
    return r;
  }).catch(() => caches.match(e.request).then(r => r || caches.match("./"))));
});
