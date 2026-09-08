/* Service worker di MTG Price Watch.
   Due politiche diverse, perché i due file hanno esigenze opposte:
   - il guscio dell'app (index.html) va servito dalla cache e aggiornato dopo,
     così l'app si apre subito anche senza rete;
   - history.json va chiesto sempre alla rete, perché il punto è avere i prezzi
     di stamattina; la copia in cache serve solo se la rete non c'è.          */

const CACHE = "mpw-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if(req.method !== "GET") return;
  const url = new URL(req.url);

  // le chiamate a Scryfall non si mettono mai in cache: sono prezzi
  if(url.hostname.endsWith("scryfall.com") || url.hostname.endsWith("scryfall.io")) return;

  // dati del collector: prima la rete, la cache è solo la rete di sicurezza
  if(url.pathname.endsWith("history.json") || url.pathname.endsWith("alerts.json")){
    e.respondWith(
      fetch(req).then(r => {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
        return r;
      }).catch(() => caches.match(req, { ignoreSearch:true }))
    );
    return;
  }

  // guscio dell'app: prima la cache, aggiornamento in sottofondo
  e.respondWith(
    caches.match(req, { ignoreSearch:true }).then(hit => {
      const net = fetch(req).then(r => {
        if(r && r.ok){ const copy = r.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
        return r;
      }).catch(() => hit);
      return hit || net;
    })
  );
});
