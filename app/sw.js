/* Service worker di MTG Price Watch.

   Il guscio dell'app (index.html) si chiede SEMPRE prima al server, con una
   richiesta di riconvalida: se non è cambiato il server risponde 304 e costa
   pochi byte, se è cambiato arriva subito la versione nuova. La copia in cache
   serve solo quando la rete non c'è.

   La versione precedente faceva il contrario — prima la cache, aggiornamento
   in sottofondo — e GitHub Pages dichiara i file validi per dieci minuti: una
   modifica poteva restare invisibile per diverse aperture di fila.

   I dati del collector seguono la stessa logica: prima la rete, perché il
   punto è avere i prezzi di stamattina. Scryfall non passa mai dalla cache.  */

const CACHE = "mpw-v3";
const SHELL = ["./", "./index.html", "./manifest.webmanifest"];

self.addEventListener("install", e => {
  // { cache: "reload" } salta la cache HTTP: si parte dalla versione vera
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(SHELL.map(u => new Request(u, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

/* Prima la rete, con riconvalida verso il server; la cache solo da offline.
   Attenzione: una richiesta di navigazione non si può ricostruire passando
   opzioni (il browser lo vieta), per questo si rifà la richiesta dall'URL. */
function networkFirst(req, { revalidate }){
  const isNav = req.mode === "navigate";
  const net = revalidate
    ? fetch(isNav ? req.url : req, isNav ? { cache: "no-cache", credentials: "same-origin" } : { cache: "no-cache" })
    : fetch(req);
  return net.then(r => {
    if(r && r.ok){
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(isNav ? new Request(req.url) : req, copy));
    }
    return r;
  }).catch(() =>
    caches.match(req, { ignoreSearch: true })
      .then(hit => hit || (isNav ? caches.match("./") : undefined))
      .then(hit => hit || Response.error())
  );
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if(req.method !== "GET") return;
  const url = new URL(req.url);

  // le chiamate a Scryfall non passano mai dalla cache: sono prezzi
  if(url.hostname.endsWith("scryfall.com") || url.hostname.endsWith("scryfall.io")) return;
  // tutto ciò che non è del sito (Cardmarket, font…) va dritto in rete
  if(url.origin !== self.location.origin) return;

  // dati del collector
  if(/\/(meta|movers|alerts|history)\.json$/.test(url.pathname) || /\/h\/[0-9a-f]+\.json$/.test(url.pathname)){
    e.respondWith(networkFirst(req, { revalidate: false }));
    return;
  }

  // guscio dell'app: pagina, manifest, e qualunque altro file del sito
  e.respondWith(networkFirst(req, { revalidate: true }));
});
