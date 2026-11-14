// Answers requests from the copies the page loader cached for this build, so Pyodide's own fetches reuse them.
const NAME = "shazzzbot-preview-" + new URL(self.location.href).searchParams.get("v");

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith((async () => {
    const hit = await (await caches.open(NAME)).match(e.request.url);
    return hit || fetch(e.request);
  })());
});
