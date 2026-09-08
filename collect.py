#!/usr/bin/env python3
"""
Collector giornaliero dei prezzi Cardmarket (via Scryfall) per MTG Price Watch.

Cosa fa, in ordine:
  1. scarica da Scryfall tutte le stampe cartacee con prezzo EUR >= soglia;
  2. salva la fotografia del giorno in data/days/AAAA-MM-GG.csv  (id, prezzo);
  3. tiene aggiornato il dizionario data/cards.csv               (id -> nome, set, n.);
  4. ricostruisce public/history.json con gli ultimi KEEP giorni;
  5. calcola le segnalazioni (soglia % e sigma robuste) in public/alerts.json;
  6. manda il riepilogo su Telegram, se le variabili d'ambiente ci sono.

Lo storico vive nel repo come file giornalieri: piccoli, append-only, e git li
comprime bene. history.json e' solo la vista ricostruita che legge l'app.

Nessuna dipendenza esterna: solo libreria standard.
"""

import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DAYS_DIR = ROOT / "data" / "days"
CARDS_CSV = ROOT / "data" / "cards.csv"
PUBLIC = ROOT / "public"

# ---------------------------------------------------------------- config
def cfg(name, default, cast=float):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except ValueError:
        return default

THRESHOLD = cfg("MPW_THRESHOLD", 3.0)        # prezzo minimo in EUR
MAX_CARDS = int(cfg("MPW_MAX_CARDS", 2500))  # tetto di stampe raccolte
KEEP_DAYS = int(cfg("MPW_KEEP_DAYS", 180))   # giorni esposti in history.json
PCT       = cfg("MPW_PCT", 20.0)             # soglia percentuale di segnalazione
SIGMA     = cfg("MPW_SIGMA", 2.0)            # soglia in deviazioni robuste
WINDOW    = int(cfg("MPW_WINDOW", 30))       # giorni della finestra di riferimento
MIN_OBS   = int(cfg("MPW_MIN_OBS", 5))       # rilevamenti minimi per valutare
MAX_MSG   = int(cfg("MPW_MAX_MSG", 15))      # righe massime nel messaggio Telegram

UA = "MTGPriceWatch/1.0 (collector; +https://github.com/)"
API = os.environ.get("MPW_API_BASE", "https://api.scryfall.com")  # sovrascritto solo dai test

# ---------------------------------------------------------------- Scryfall
def get(url, tries=5):
    """GET con le buone maniere che chiede Scryfall: pausa fissa e backoff sul 429."""
    for attempt in range(tries):
        time.sleep(0.12)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
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


def harvest():
    """Scarica le stampe sopra soglia, dalla piu' cara. Ritorna {id: (prezzo, meta)}."""
    q = f"eur>={THRESHOLD:g} -is:digital game:paper"
    url = (API + "/cards/search?"
           + urllib.parse.urlencode({"q": q, "unique": "prints", "order": "eur", "dir": "desc"}))
    out, pages = {}, 0
    while url and len(out) < MAX_CARDS:
        page = get(url)
        if not page or not page.get("data"):
            break
        pages += 1
        for c in page["data"]:
            eur = (c.get("prices") or {}).get("eur")
            if eur is None:
                continue
            # cmc, identita' di colore e tipo servono ai filtri dell'app:
            # senza di loro i filtri funzionerebbero nella ricerca ma non sui movers
            ci = "".join(c.get("color_identity") or c.get("colors") or [])
            cmc = c.get("cmc")
            out[c["id"]] = (
                round(float(eur), 2),
                (c["name"], c["set"].upper(), c["collector_number"], c.get("oracle_id", ""),
                 "" if cmc is None else f"{float(cmc):g}", ci, c.get("type_line", "")),
            )
            if len(out) >= MAX_CARDS:
                break
        print(f"  pagina {pages}: {len(out)} stampe", flush=True)
        url = page["next_page"] if page.get("has_more") and len(out) < MAX_CARDS else None
    return out


# ---------------------------------------------------------------- persistenza
def write_day(day, harvested):
    DAYS_DIR.mkdir(parents=True, exist_ok=True)
    with (DAYS_DIR / f"{day}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "eur"])
        for cid, (price, _) in sorted(harvested.items()):
            w.writerow([cid, f"{price:.2f}"])


FIELDS = ["id", "name", "set", "cn", "oid", "cmc", "ci", "type"]


def update_dictionary(harvested):
    """Nomi, tipi e costi cambiano di rado: si scrivono una volta, non ogni giorno."""
    known = {}
    if CARDS_CSV.exists():
        with CARDS_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                # i file scritti dalle versioni precedenti non hanno le colonne nuove
                known[row["id"]] = {k: (row.get(k) or "") for k in FIELDS}
    added, filled = 0, 0
    for cid, (_, m) in harvested.items():
        fresh = dict(zip(FIELDS, (cid,) + m))
        if cid not in known:
            known[cid] = fresh
            added += 1
        elif not known[cid].get("type"):
            known[cid] = fresh          # completa le righe scritte prima dei filtri
            filled += 1
    CARDS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with CARDS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for cid in sorted(known):
            w.writerow(known[cid])
    return known, added, filled


def read_days():
    if not DAYS_DIR.exists():
        return [], {}
    files = sorted(p for p in DAYS_DIR.glob("*.csv"))[-KEEP_DAYS:]
    days, series = [], {}
    for i, p in enumerate(files):
        days.append(p.stem)
        with p.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                series.setdefault(row["id"], {})[i] = float(row["eur"])
    return days, series


# ---------------------------------------------------------------- statistica
def median(xs):
    return statistics.median(xs) if xs else None


def robust_sigma(xs, med):
    """1.4826 * MAD: la deviazione mediana non si lascia gonfiare da un singolo picco."""
    if len(xs) < 2:
        return 0.0
    return 1.4826 * statistics.median([abs(x - med) for x in xs])


def evaluate(values):
    """values: lista (indice_giorno, prezzo) in ordine cronologico."""
    if len(values) < 2:
        return None
    last = values[-1][1]
    window = [v for _, v in values[-1 - WINDOW:-1]]
    if not window:
        return None
    med = median(window)
    sig = robust_sigma(window, med)
    d_med = (last - med) / med if med else 0.0
    z = (last - med) / sig if sig > 0 else 0.0
    hit_pct = abs(d_med) >= PCT / 100
    hit_sig = len(values) - 1 >= MIN_OBS and sig > 0 and abs(z) >= SIGMA
    if not (hit_pct or hit_sig):
        return None
    return {"last": round(last, 2), "med": round(med, 2), "dMed": round(d_med, 4),
            "z": round(z, 2), "obs": len(values), "pct": hit_pct, "sigma": hit_sig}


# ---------------------------------------------------------------- output
def build(days, series, dictionary):
    cards, alerts = {}, []
    n = len(days)
    for cid, by_index in series.items():
        meta = dictionary.get(cid)
        if not meta:
            continue
        prices = [by_index.get(i) for i in range(n)]
        entry = {"n": meta["name"], "s": meta["set"], "cn": meta["cn"],
                 "oid": meta["oid"], "p": prices}
        if meta.get("type"):
            entry["tl"] = meta["type"]
        if meta.get("ci") is not None:
            entry["ci"] = list(meta["ci"] or "")
        if meta.get("cmc"):
            try:
                entry["cmc"] = float(meta["cmc"])
            except ValueError:
                pass
        cards[cid] = entry
        ordered = [(i, v) for i, v in sorted(by_index.items())]
        verdict = evaluate(ordered)
        if verdict:
            verdict.update({"id": cid, "n": meta["name"], "s": meta["set"], "cn": meta["cn"]})
            alerts.append(verdict)
    alerts.sort(key=lambda a: abs(a["dMed"]), reverse=True)
    history = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "scryfall/cardmarket-eur",
        "config": {"thr": THRESHOLD, "max": MAX_CARDS, "keep": KEEP_DAYS,
                   "pct": PCT, "sigma": SIGMA, "win": WINDOW, "minObs": MIN_OBS},
        "days": days,
        "cards": cards,
    }
    return history, alerts


def telegram(alerts, days):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram non configurato: salto l'invio.")
        return
    if not alerts:
        print("Nessuna segnalazione: nessun messaggio inviato.")
        return

    def esc(s):
        for ch in "_*[]()~`>#+-=|{}.!":
            s = s.replace(ch, "\\" + ch)
        return s

    up = [a for a in alerts if a["dMed"] > 0]
    down = [a for a in alerts if a["dMed"] < 0]
    lines = [f"*{len(alerts)} carte in movimento* · {esc(days[-1])}",
             f"_{len(up)} in rialzo, {len(down)} in ribasso_", ""]
    for a in alerts[:MAX_MSG]:
        arrow = "\U0001F4C8" if a["dMed"] > 0 else "\U0001F4C9"
        pct_s = esc("{:+.1f}".format(a["dMed"] * 100).replace(".", ","))
        last_s = esc("{:.2f}".format(a["last"]).replace(".", ","))
        med_s = esc("{:.2f}".format(a["med"]).replace(".", ","))
        z_s = esc("{:+.1f}".format(a["z"]).replace(".", ","))
        lines.append(
            "{} *{}* · {}\n   {} EUR  \\({}%\\)  mediana {}  σ {}".format(
                arrow, esc(a["n"]), esc(a["s"]), last_s, pct_s, med_s, z_s)
        )
    if len(alerts) > MAX_MSG:
        lines.append(f"\n_e altre {len(alerts) - MAX_MSG}\\._")

    body = urllib.parse.urlencode({
        "chat_id": chat,
        "text": "\n".join(lines)[:4000],
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": "true",
    }).encode()
    tg = os.environ.get("MPW_TELEGRAM_BASE", "https://api.telegram.org")
    req = urllib.request.Request(f"{tg}/bot{token}/sendMessage",
                                 data=body, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        print(f"Telegram: messaggio inviato ({len(alerts)} segnalazioni).")
    except urllib.error.HTTPError as e:
        # un messaggio non partito non deve far fallire la raccolta dati
        print(f"Telegram: invio fallito ({e.code}) {e.read()[:300]!r}", file=sys.stderr)


# ---------------------------------------------------------------- main
def main():
    day = os.environ.get("MPW_DATE") or date.today().isoformat()
    print(f"MTG Price Watch · raccolta del {day}")
    print(f"  soglia {THRESHOLD:g} EUR, tetto {MAX_CARDS} stampe")

    harvested = harvest()
    if not harvested:
        print("Nessun dato ricevuto: esco senza toccare l'archivio.", file=sys.stderr)
        return 1
    print(f"Raccolte {len(harvested)} stampe con prezzo in EUR.")

    write_day(day, harvested)
    dictionary, added, filled = update_dictionary(harvested)
    print(f"Dizionario: {len(dictionary)} carte note ({added} nuove"
          + (f", {filled} completate con tipo e costo)." if filled else ")."))

    days, series = read_days()
    history, alerts = build(days, series, dictionary)

    PUBLIC.mkdir(parents=True, exist_ok=True)
    (PUBLIC / "history.json").write_text(
        json.dumps(history, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    (PUBLIC / "alerts.json").write_text(
        json.dumps({"generated": history["generated"], "day": day, "alerts": alerts},
                   separators=(",", ":"), ensure_ascii=False), encoding="utf-8")

    kb = (PUBLIC / "history.json").stat().st_size // 1024
    print(f"history.json: {len(history['cards'])} carte × {len(days)} giorni ({kb} KB)")
    print(f"Segnalazioni: {len(alerts)}")
    for a in alerts[:10]:
        print(f"  {a['dMed'] * 100:+7.1f}%  {a['n'][:38]:38s} {a['s']:5s} "
              f"{a['last']:8.2f} EUR  sigma {a['z']:+.1f}")

    telegram(alerts, days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
