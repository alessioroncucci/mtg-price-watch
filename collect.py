#!/usr/bin/env python3
"""
Collector giornaliero dei prezzi Cardmarket (via Scryfall) per MTG Price Watch.

Cosa fa, in ordine:
  1. scarica il file bulk di Scryfall e lo legge in streaming;
  2. salva la fotografia del giorno in data/days/AAAA-MM-GG.csv.gz  (id, prezzo);
  3. tiene aggiornato il dizionario data/cards.csv (nome, set, tipo, trattamento…);
  4. aggrega in settimane i giorni piu' vecchi dello storico pubblicato;
  5. pubblica movers.json (gia' calcolato) e lo storico diviso in frammenti;
  6. manda il riepilogo su Telegram, se le variabili d'ambiente ci sono.

Perche' il bulk e non l'API di ricerca: con decine di migliaia di stampe
servirebbero centinaia di pagine ogni mattina. Scryfall pubblica i file bulk
proprio per questo, aggiornati ogni 12 ore, e chiede di usare quelli.

Perche' i frammenti: un unico history.json con 50.000 carte per 180 giorni
pesa una ventina di megabyte compressi, e l'app lo scaricherebbe a ogni
apertura per guardarne dieci. Con movers.json gia' calcolato piu' i frammenti
presi solo quando servono, l'apertura costa meno di prima pur coprendo venti
volte piu' carte.

Formato del bulk: da settembre 2026 Scryfall pubblica JSON Lines compressi in
gzip, indicati dal campo `jsonl_download_uri`. Prima era un array JSON sotto
`download_uri`. Il collector accetta entrambi, e riconosce il formato dal
contenuto del file invece che dal nome del campo.

Dipendenze: solo libreria standard. `ijson`, se presente, serve unicamente da
ripiego se il formato del file bulk cambia ancora.
"""

import csv
import gzip
import io
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DAYS_DIR = DATA / "days"
WEEKS_DIR = DATA / "weeks"
CARDS_CSV = DATA / "cards.csv"
PUBLIC = ROOT / "public"
SHARD_DIR = PUBLIC / "h"


# ---------------------------------------------------------------- config
def cfg(name, default, cast=float):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except ValueError:
        return default


THRESHOLD = cfg("MPW_THRESHOLD", 0.50)        # prezzo minimo in EUR per entrare
MAX_CARDS = int(cfg("MPW_MAX_CARDS", 0))      # 0 = nessun tetto
KEEP_DAYS = int(cfg("MPW_KEEP_DAYS", 180))    # giorni pubblicati e tenuti al giorno
PCT       = cfg("MPW_PCT", 20.0)              # soglia percentuale di segnalazione
SIGMA     = cfg("MPW_SIGMA", 2.0)             # soglia in deviazioni robuste
WINDOW    = int(cfg("MPW_WINDOW", 30))        # giorni della finestra di riferimento
MIN_OBS   = int(cfg("MPW_MIN_OBS", 5))        # rilevamenti minimi per valutare
MIN_MOVE  = cfg("MPW_MIN_MOVE", 1.00)         # scostamento minimo in EUR per segnalare
MOVERS_N  = int(cfg("MPW_MOVERS", 800))       # righe in movers.json
SPARK_N   = int(cfg("MPW_SPARK", 30))         # punti di storico dentro movers.json
MAX_MSG   = int(cfg("MPW_MAX_MSG", 15))       # righe massime nel messaggio Telegram
SHARD_HEX = int(cfg("MPW_SHARD_HEX", 2))      # caratteri dell'id usati per il frammento

UA = "MTGPriceWatch/2.1 (collector; +https://github.com/)"
API = os.environ.get("MPW_API_BASE", "https://api.scryfall.com")

# ---------------------------------------------------------------- trattamento
# Quattro stampe della stessa carta nello stesso set si distinguono solo per il
# numero di collezione, che non dice nulla. Scryfall descrive il trattamento in
# campi espliciti: qui si riduce a un'etichetta leggibile per l'app.
PROMO_IT = {
    "surgefoil": "Surge foil", "galaxyfoil": "Galaxy foil", "halofoil": "Halo foil",
    "confettifoil": "Confetti foil", "rainbowfoil": "Rainbow foil",
    "doublerainbow": "Double rainbow", "textured": "Textured foil",
    "oilslick": "Oil slick", "neonink": "Neon ink", "gilded": "Gilded",
    "embossed": "Embossed", "ripplefoil": "Ripple foil", "raisedfoil": "Raised foil",
    "invisibleink": "Invisible ink", "stepandcompleat": "Step-and-compleat",
    "serialized": "Serializzata", "glossy": "Glossy", "silverfoil": "Silver foil",
    "golden": "Golden", "magnified": "Magnified", "concept": "Concept art",
    "poster": "Poster", "thick": "Thick display",
}
FRAME_IT = {
    "showcase": "Showcase", "extendedart": "Extended art", "inverted": "Invertita",
    "shatteredglass": "Shattered glass", "etched": "Etched", "nyxtouched": "Nyx",
    "colorshifted": "Colorshifted", "fullart": "Full art", "textless": "Senza testo",
    "legendary": "", "companion": "", "devoid": "", "tombstone": "",
    "miracle": "", "snow": "", "draft": "",
}


def treatment(c):
    out = []
    if c.get("border_color") == "borderless":
        out.append("Borderless")
    for f in c.get("frame_effects") or []:
        if f in FRAME_IT:
            if FRAME_IT[f]:
                out.append(FRAME_IT[f])
        else:
            out.append(f.capitalize())
    if c.get("full_art") and "Full art" not in out:
        out.append("Full art")
    if c.get("textless") and "Senza testo" not in out:
        out.append("Senza testo")
    for pt in c.get("promo_types") or []:
        if pt in ("boosterfun", "promo"):
            continue
        out.append(PROMO_IT.get(pt, pt.capitalize()))
    if c.get("frame") in ("1993", "1997"):
        out.append("Cornice retro")
    if not out and c.get("promo"):
        out.append("Promo")
    fin = c.get("finishes") or []
    if not out and fin and "nonfoil" not in fin:
        out.append("Solo foil")
    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return " · ".join(uniq[:3])


# ---------------------------------------------------------------- rete
def get_json(url, tries=5):
    for attempt in range(tries):
        time.sleep(0.12)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504):
                wait = 2 * (attempt + 1) ** 2
                print(f"  HTTP {e.code}, riprovo tra {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            wait = 3 * (attempt + 1)
            print(f"  rete: {e}, riprovo tra {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Scryfall non raggiungibile dopo {tries} tentativi: {url}")


def download_bulk(dest):
    """Scarica il file bulk 'default_cards'. Ritorna il percorso locale."""
    cat = get_json(API + "/bulk-data")
    if not cat or not cat.get("data"):
        raise RuntimeError("Catalogo bulk non disponibile")
    entry = next((b for b in cat["data"] if b.get("type") == "default_cards"), None)
    if entry is None:
        raise RuntimeError("Nel catalogo bulk manca 'default_cards'")

    # Settembre 2026: `download_uri` e `size` sono diventati `jsonl_download_uri`
    # e `compressed_size`. Si accettano entrambe le generazioni, la nuova prima.
    url = entry.get("jsonl_download_uri") or entry.get("download_uri")
    if not url:
        raise RuntimeError("Nessun indirizzo di download per 'default_cards'. "
                           "Campi presenti nel catalogo: " + ", ".join(sorted(entry)))
    size = entry.get("compressed_size") or entry.get("size") or 0
    print(f"  bulk: {url} ({size / 1048576:.0f} MB dichiarati, "
          f"aggiornato {entry.get('updated_at', '?')})", flush=True)

    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r
        # compressione di trasporto: va tolta qui. Se invece e' il file stesso
        # a essere un .gz, resta compresso su disco e lo apre open_bulk().
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.GzipFile(fileobj=r)
        with open(dest, "wb") as f:
            shutil.copyfileobj(raw, f, length=1 << 20)
    mb = dest.stat().st_size / 1048576
    print(f"  scaricati {mb:.0f} MB su disco", flush=True)
    return dest


def open_bulk(path):
    """Apre il file bulk compresso o no, riconoscendolo dai primi due byte."""
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rb")
    return open(path, "rb")


def first_significant_byte(path):
    with open_bulk(path) as f:
        while True:
            ch = f.read(1)
            if not ch:
                return b""
            if not ch.isspace():
                return ch


def iter_cards(path):
    """
    Itera le carte del file bulk senza caricarlo in memoria.

    Tre formati, riconosciuti dal contenuto e non dal nome del campo:
      - JSON Lines (attuale): un oggetto per riga, il primo byte utile e' '{';
      - array JSON con un oggetto per riga (fino ad agosto 2026): primo byte '[';
      - qualunque altro JSON valido: si ripiega su ijson, parser a flusso.
    Tutti e tre funzionano sia sul file compresso sia su quello in chiaro.
    """
    lead = first_significant_byte(path)

    if lead == b"{":
        with open_bulk(path) as f:
            for line in f:
                s = line.strip()
                if s:
                    yield json.loads(s)
        return

    def array_per_line_ok():
        with open_bulk(path) as f:
            head = f.readline()
            if head.strip() != b"[":
                return False
            for _ in range(3):
                line = f.readline().strip().rstrip(b",")
                if not line or line == b"]":
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    return False
                if not isinstance(obj, dict):
                    return False
            return True

    if lead == b"[" and array_per_line_ok():
        with open_bulk(path) as f:
            f.readline()
            for line in f:
                s = line.strip().rstrip(b",")
                if not s or s == b"]":
                    continue
                yield json.loads(s)
        return

    print("  formato bulk inatteso: passo al parser a flusso (ijson)", flush=True)
    import ijson
    with open_bulk(path) as f:
        for obj in ijson.items(f, "item"):
            yield obj


# ---------------------------------------------------------------- raccolta
FIELDS = ["id", "name", "set", "cn", "oid", "cmc", "ci", "type", "tr", "artist"]


def harvest(path):
    """Ritorna {id: (prezzo, meta)} per le stampe cartacee sopra soglia."""
    out = {}
    seen = 0
    for c in iter_cards(path):
        seen += 1
        if c.get("digital") or c.get("object") != "card":
            continue
        if "paper" not in (c.get("games") or []):
            continue
        eur = (c.get("prices") or {}).get("eur")
        if eur is None:
            continue
        try:
            price = float(eur)
        except (TypeError, ValueError):
            continue
        if price < THRESHOLD:
            continue
        cmc = c.get("cmc")
        out[c["id"]] = (
            round(price, 2),
            (c.get("name", ""), (c.get("set") or "").upper(), c.get("collector_number", ""),
             c.get("oracle_id", ""), "" if cmc is None else f"{float(cmc):g}",
             "".join(c.get("color_identity") or c.get("colors") or []),
             c.get("type_line", ""), treatment(c), c.get("artist", "")),
        )
        if MAX_CARDS and len(out) >= MAX_CARDS:
            break
        if seen % 20000 == 0:
            print(f"  lette {seen} carte, {len(out)} sopra soglia", flush=True)
    print(f"  lette {seen} carte in tutto, {len(out)} sopra {THRESHOLD:g} EUR", flush=True)
    return out


# ---------------------------------------------------------------- persistenza
def write_day(day, harvested):
    DAYS_DIR.mkdir(parents=True, exist_ok=True)
    path = DAYS_DIR / f"{day}.csv.gz"
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "eur"])
    for cid, (price, _) in sorted(harvested.items()):
        w.writerow([cid, f"{price:.2f}"])
    with gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=6) as f:
        f.write(buf.getvalue())
    # una versione precedente scriveva i giorni non compressi
    old = DAYS_DIR / f"{day}.csv"
    if old.exists():
        old.unlink()
    return path


def read_day(path):
    op = gzip.open if path.suffix == ".gz" else open
    with op(path, "rt", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            yield row["id"], float(row["eur"])


def day_files():
    """Tutti i giorni presenti, compressi o no, ordinati per data."""
    if not DAYS_DIR.exists():
        return []
    found = {}
    for p in DAYS_DIR.glob("*.csv*"):
        stem = p.name.split(".csv")[0]
        if len(stem) == 10 and stem[4] == "-":
            found.setdefault(stem, p)
            if p.suffix == ".gz":
                found[stem] = p          # il compresso vince sul vecchio in chiaro
    return [found[k] for k in sorted(found)]


def update_dictionary(harvested):
    """
    Nomi, tipi e trattamenti cambiano di rado. Il file viene riscritto solo se
    e' davvero cambiato: con 50.000 righe, riscriverlo ogni giorno vorrebbe dire
    un blob nuovo da qualche megabyte al giorno nella storia di git.
    """
    known, stale = {}, False
    if CARDS_CSV.exists():
        with CARDS_CSV.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            stale = not set(FIELDS).issubset(set(reader.fieldnames or []))
            for row in reader:
                known[row["id"]] = {k: (row.get(k) or "") for k in FIELDS}
    added, filled = 0, 0
    for cid, (_, m) in harvested.items():
        fresh = dict(zip(FIELDS, (cid,) + m))
        if cid not in known:
            known[cid] = fresh
            added += 1
        elif stale:
            known[cid] = fresh
            filled += 1

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    for cid in sorted(known):
        w.writerow(known[cid])
    new_text = buf.getvalue()
    changed = (not CARDS_CSV.exists()) or CARDS_CSV.read_text(encoding="utf-8") != new_text
    if changed:
        CARDS_CSV.parent.mkdir(parents=True, exist_ok=True)
        CARDS_CSV.write_text(new_text, encoding="utf-8")
    return known, added, filled, changed


# ---------------------------------------------------------------- archivio
def archive_old_days():
    """
    I giorni piu' vecchi dello storico pubblicato diventano un valore settimanale
    per carta (la mediana della settimana) e i file giornalieri spariscono.
    L'andamento di lungo periodo resta, il dettaglio no, e il repo smette di
    crescere in modo lineare.
    """
    files = day_files()
    if len(files) <= KEEP_DAYS:
        return 0, 0
    old = files[:-KEEP_DAYS]
    WEEKS_DIR.mkdir(parents=True, exist_ok=True)
    by_week = defaultdict(lambda: defaultdict(list))
    for p in old:
        stem = p.name.split(".csv")[0]
        y, w, _ = date.fromisoformat(stem).isocalendar()
        key = f"{y}-W{w:02d}"
        for cid, price in read_day(p):
            by_week[key][cid].append(price)

    for key, cards in by_week.items():
        path = WEEKS_DIR / f"{key}.csv.gz"
        merged = {}
        if path.exists():                      # settimana gia' archiviata in parte
            with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    merged[row["id"]] = float(row["eur"])
        for cid, vals in cards.items():
            merged[cid] = round(statistics.median(vals), 2)
        with gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=6) as f:
            w = csv.writer(f)
            w.writerow(["id", "eur"])
            for cid in sorted(merged):
                w.writerow([cid, f"{merged[cid]:.2f}"])

    for p in old:
        p.unlink()
    return len(old), len(by_week)


def read_history():
    """Ritorna (giorni, {id: {indice: prezzo}}) sugli ultimi KEEP_DAYS giorni."""
    files = day_files()[-KEEP_DAYS:]
    days, series = [], defaultdict(dict)
    for i, p in enumerate(files):
        days.append(p.name.split(".csv")[0])
        for cid, price in read_day(p):
            series[cid][i] = price
    return days, series


# ---------------------------------------------------------------- statistica
def median(xs):
    return statistics.median(xs) if xs else None


def robust_sigma(xs, med):
    """1.4826 * MAD: la deviazione mediana non si lascia gonfiare da un picco."""
    if len(xs) < 2:
        return 0.0
    return 1.4826 * statistics.median([abs(x - med) for x in xs])


def evaluate(values):
    """values: [(indice_giorno, prezzo)] in ordine cronologico."""
    if len(values) < 2:
        return None
    last = values[-1][1]
    window = [v for _, v in values[-1 - WINDOW:-1]]
    if not window:
        return None
    med = median(window)
    sig = robust_sigma(window, med)
    if not med:
        return None
    d_med = (last - med) / med
    z = (last - med) / sig if sig > 0 else 0.0
    vol = sig / med
    below = sum(1 for v in window if v < last)
    pctl = below / len(window) if len(window) >= 3 else None

    big_enough = abs(last - med) >= MIN_MOVE
    hit_pct = big_enough and abs(d_med) >= PCT / 100
    hit_sig = (big_enough and len(values) - 1 >= MIN_OBS
               and sig > 0 and abs(z) >= SIGMA)
    return {"last": round(last, 2), "med": round(med, 2), "dMed": round(d_med, 4),
            "z": round(z, 2), "vol": round(vol, 4),
            "pctl": None if pctl is None else round(pctl, 3),
            "obs": len(values), "alert": hit_pct or hit_sig,
            "pct": hit_pct, "sigma": hit_sig}


# ---------------------------------------------------------------- pubblicazione
def shard_of(cid):
    return cid[:SHARD_HEX].lower()


def publish(days, series, dictionary):
    """movers.json con le carte in movimento, piu' lo storico in frammenti."""
    SHARD_DIR.mkdir(parents=True, exist_ok=True)
    n = len(days)
    shards = defaultdict(dict)
    movers, alerts = [], []

    for cid, by_index in series.items():
        meta = dictionary.get(cid)
        if not meta:
            continue
        entry = {"n": meta["name"], "s": meta["set"], "cn": meta["cn"], "oid": meta["oid"],
                 "p": [by_index.get(i) for i in range(n)]}
        if meta.get("type"):
            entry["tl"] = meta["type"]
        if meta.get("ci"):
            entry["ci"] = list(meta["ci"])
        if meta.get("tr"):
            entry["tr"] = meta["tr"]
        if meta.get("artist"):
            entry["ar"] = meta["artist"]
        if meta.get("cmc"):
            try:
                entry["cmc"] = float(meta["cmc"])
            except ValueError:
                pass
        shards[shard_of(cid)][cid] = entry

        verdict = evaluate([(i, v) for i, v in sorted(by_index.items())])
        if not verdict:
            continue
        row = dict(verdict)
        row.update({"id": cid, "n": meta["name"], "s": meta["set"], "cn": meta["cn"],
                    "tr": meta.get("tr", ""), "ci": list(meta.get("ci") or ""),
                    "tl": meta.get("type", "")})
        if meta.get("cmc"):
            try:
                row["cmc"] = float(meta["cmc"])
            except ValueError:
                pass
        row["sp"] = [v for v in entry["p"][-SPARK_N:] if v is not None]
        movers.append(row)
        if verdict["alert"]:
            alerts.append(row)

    movers.sort(key=lambda r: abs(r["dMed"]), reverse=True)
    alerts.sort(key=lambda r: abs(r["dMed"]), reverse=True)
    top = alerts + [m for m in movers if not m["alert"]][:max(0, MOVERS_N - len(alerts))]

    written = 0
    for key, cards in shards.items():
        (SHARD_DIR / f"{key}.json").write_text(
            json.dumps({"days": days, "cards": cards}, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8")
        written += 1

    meta_doc = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "scryfall/cardmarket-eur",
        "days": days,
        "cards": sum(len(v) for v in shards.values()),
        "shards": written,
        "shardHex": SHARD_HEX,
        "floor": THRESHOLD,
        "config": {"thr": THRESHOLD, "keep": KEEP_DAYS, "pct": PCT, "sigma": SIGMA,
                   "win": WINDOW, "minObs": MIN_OBS, "minMove": MIN_MOVE},
    }
    PUBLIC.mkdir(parents=True, exist_ok=True)
    (PUBLIC / "meta.json").write_text(json.dumps(meta_doc, separators=(",", ":"),
                                                 ensure_ascii=False), encoding="utf-8")
    (PUBLIC / "movers.json").write_text(
        json.dumps({"generated": meta_doc["generated"], "days": days[-SPARK_N:],
                    "alerts": len(alerts), "movers": top},
                   separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    (PUBLIC / "alerts.json").write_text(
        json.dumps({"generated": meta_doc["generated"], "day": days[-1] if days else None,
                    "alerts": alerts}, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8")
    return top, alerts, written


# ---------------------------------------------------------------- Telegram
def telegram(alerts, day):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    always = os.environ.get("MPW_ALWAYS_NOTIFY", "").strip() not in ("", "0", "false")
    if not token or not chat:
        print("Telegram non configurato: salto l'invio.")
        return
    if not alerts and not always:
        print("Nessuna segnalazione: nessun messaggio inviato.")
        return

    def esc(s):
        for ch in "_*[]()~`>#+-=|{}.!":
            s = s.replace(ch, "\\" + ch)
        return s

    if not alerts:
        body = f"*Raccolta completata* · {esc(str(day))}\n_Nessuna carta oltre le soglie\\._"
    else:
        up = [a for a in alerts if a["dMed"] > 0]
        down = [a for a in alerts if a["dMed"] < 0]
        lines = [f"*{len(alerts)} carte in movimento* · {esc(str(day))}",
                 f"_{len(up)} in rialzo, {len(down)} in ribasso_", ""]
        for a in alerts[:MAX_MSG]:
            arrow = "\U0001F4C8" if a["dMed"] > 0 else "\U0001F4C9"
            pct_s = esc("{:+.1f}".format(a["dMed"] * 100).replace(".", ","))
            last_s = esc("{:.2f}".format(a["last"]).replace(".", ","))
            med_s = esc("{:.2f}".format(a["med"]).replace(".", ","))
            tr = f" · {esc(a['tr'])}" if a.get("tr") else ""
            lines.append("{} *{}* · {}{}\n   {} EUR  \\({}%\\)  mediana {}".format(
                arrow, esc(a["n"]), esc(a["s"]), tr, last_s, pct_s, med_s))
        if len(alerts) > MAX_MSG:
            lines.append(f"\n_e altre {len(alerts) - MAX_MSG}\\._")
        body = "\n".join(lines)

    payload = urllib.parse.urlencode({
        "chat_id": chat, "text": body[:4000], "parse_mode": "MarkdownV2",
        "disable_web_page_preview": "true"}).encode()
    tg = os.environ.get("MPW_TELEGRAM_BASE", "https://api.telegram.org")
    req = urllib.request.Request(f"{tg}/bot{token}/sendMessage", data=payload,
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        print(f"Telegram: messaggio inviato ({len(alerts)} segnalazioni).")
    except urllib.error.HTTPError as e:
        print(f"Telegram: invio fallito ({e.code}) {e.read()[:300]!r}", file=sys.stderr)


# ---------------------------------------------------------------- main
def main():
    day = os.environ.get("MPW_DATE") or date.today().isoformat()
    print(f"MTG Price Watch · raccolta del {day}")
    print(f"  soglia {THRESHOLD:g} EUR"
          + (f", tetto {MAX_CARDS}" if MAX_CARDS else ", nessun tetto"))

    tmp = Path(tempfile.mkdtemp()) / "bulk.data"
    try:
        download_bulk(tmp)
        harvested = harvest(tmp)
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)

    if not harvested:
        print("Nessun dato ricevuto: esco senza toccare l'archivio.", file=sys.stderr)
        return 1

    cheapest = min(p for p, _ in harvested.values())
    print(f"Raccolte {len(harvested)} stampe · la piu' economica a {cheapest:.2f} EUR")

    write_day(day, harvested)
    dictionary, added, filled, changed = update_dictionary(harvested)
    print(f"Dizionario: {len(dictionary)} carte note ({added} nuove"
          + (f", {filled} completate" if filled else "")
          + (")" if changed else ", invariato)"))

    moved, weeks = archive_old_days()
    if moved:
        print(f"Archivio: {moved} giorni aggregati in {weeks} settimane.")

    days, series = read_history()
    top, alerts, shards = publish(days, series, dictionary)

    def mb(p):
        return p.stat().st_size / 1048576

    shard_mb = sum(f.stat().st_size for f in SHARD_DIR.glob("*.json")) / 1048576
    print(f"Pubblicati: {len(series)} carte × {len(days)} giorni")
    print(f"  movers.json {mb(PUBLIC / 'movers.json') * 1024:.0f} KB "
          f"({len(top)} righe, {len(alerts)} segnalate)")
    print(f"  {shards} frammenti, {shard_mb:.1f} MB in tutto "
          f"({shard_mb * 1024 / max(1, shards):.0f} KB l'uno)")
    for a in alerts[:10]:
        print(f"  {a['dMed'] * 100:+7.1f}%  {a['n'][:34]:34s} {a['s']:5s} "
              f"{a['last']:8.2f} EUR  sigma {a['z']:+.1f}")

    telegram(alerts, days[-1] if days else day)
    return 0


if __name__ == "__main__":
    sys.exit(main())
