# MTG Price Watch

Prezzi mediani delle carte Magic e avvisi quando una carta si muove in modo
significativo. Prezzi Cardmarket in EUR, presi da [Scryfall](https://scryfall.com).

Due pezzi che lavorano insieme:

- **il collector** (`collect.py`) gira ogni mattina su GitHub Actions, raccoglie i
  prezzi, aggiorna lo storico nel repo e manda il riepilogo su Telegram;
- **l'app** (`app/index.html`) legge quello storico. All'apertura è già aggiornata:
  non c'è niente da premere.

La differenza sostanziale rispetto a una app che scarica per conto suo è che
**lo storico avanza anche nei giorni in cui non apri niente**. Sono proprio quelli
in cui un prezzo può muoversi senza che nessuno se ne accorga.

---

## Installazione in dieci minuti

### 1. Crea il repository

Carica questa cartella in un repo GitHub (pubblico o privato, funziona uguale;
sui repo pubblici i minuti di Actions sono illimitati, sui privati ne hai 2000 al
mese e questo job ne consuma una sessantina).

```bash
git init
git add .
git commit -m "MTG Price Watch"
git branch -M main
git remote add origin https://github.com/TUO-UTENTE/mtg-price-watch.git
git push -u origin main
```

### 2. Accendi GitHub Pages

**Settings → Pages → Source: GitHub Actions.** Non serve scegliere un branch:
il workflow pubblica direttamente.

### 3. Dai al workflow il permesso di scrivere

**Settings → Actions → General → Workflow permissions → Read and write
permissions.** Serve perché il job ricommitta i prezzi del giorno in `data/`.

### 4. Telegram (opzionale, ma è il pezzo che ti avvisa)

1. Su Telegram scrivi a **@BotFather**, comando `/newbot`, segui le istruzioni.
   Ti restituisce un token tipo `123456789:AAH...`.
2. Scrivi un messaggio qualsiasi al tuo nuovo bot (altrimenti non può risponderti:
   Telegram non permette ai bot di iniziare una conversazione).
3. Apri `https://api.telegram.org/bot<IL-TUO-TOKEN>/getUpdates` e prendi il numero
   in `"chat":{"id": ...}`.
4. Su GitHub: **Settings → Secrets and variables → Actions → New repository
   secret**, e crea `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`.

Senza questi due secret il collector funziona lo stesso, semplicemente non manda
messaggi.

### 5. Prova subito

**Actions → Raccolta prezzi → Run workflow.** Il primo giro dura qualche minuto
(scarica ~2500 stampe). Quando finisce, l'app è su
`https://TUO-UTENTE.github.io/mtg-price-watch/`.

Su Android, dal menu di Chrome, *Installa app*: si comporta come un'app normale e
funziona anche offline sull'ultimo storico scaricato.

---

## Un'avvertenza sui tempi

**Le segnalazioni non arrivano dal primo giorno.** Il collector alla prima
esecuzione registra solo la fotografia di partenza: per dire che un prezzo è
anomalo serve qualcosa con cui confrontarlo. Con le impostazioni di serie i primi
avvisi utili arrivano dopo circa una settimana, e la statistica diventa solida
verso i trenta giorni.

Se lasci il repo fermo, **GitHub disattiva i workflow pianificati dopo 60 giorni di
inattività**, e i commit del bot non sempre bastano a farlo considerare attivo.
Ti manda un'email prima di farlo: basta riattivarlo dalla scheda Actions.

---

## Come vengono decise le segnalazioni

Il prezzo di oggi viene confrontato con la **mediana dei giorni precedenti**
(30 di serie), non con la media. Una carta finisce tra le segnalate se supera
almeno una delle due soglie:

- **variazione percentuale**: si discosta di oltre il ±20% dalla mediana;
- **deviazione robusta**: esce dalla banda di ±2σ, dove σ è calcolata come
  `1,4826 × MAD` (mediana degli scarti assoluti dalla mediana).

La seconda merita una parola. Con media e deviazione standard classiche un picco
grosso gonfia la deviazione, allarga la banda e finisce per nascondere sé stesso:
il valore anomalo contribuisce alla misura di quanto sia normale essere anomali.
Mediana e MAD non hanno questo problema — servirebbe che *metà* dei rilevamenti si
muovessero insieme per spostarle. Il risultato pratico è che una carta ferma per
un mese e poi schizzata del 40% viene segnalata, mentre una carta che ballonzola
sempre del 15% non riempie gli avvisi di rumore.

Le due soglie sono complementari: la percentuale prende i movimenti grossi anche
su carte volatili, le sigma prendono i movimenti piccoli ma anomali su carte
tranquille.

---

## Configurazione

Si regola tutto dalle variabili d'ambiente nel workflow
(`.github/workflows/collect.yml`, sezione `env`):

| Variabile | Serie | Significato |
|---|---|---|
| `MPW_THRESHOLD` | `3` | prezzo minimo in EUR perché una stampa entri nell'universo |
| `MPW_MAX_CARDS` | `2500` | tetto di stampe raccolte (dalla più cara in giù) |
| `MPW_KEEP_DAYS` | `180` | giorni esposti in `history.json` |
| `MPW_PCT` | `20` | soglia percentuale di segnalazione |
| `MPW_SIGMA` | `2` | soglia in deviazioni robuste |
| `MPW_WINDOW` | `30` | giorni della finestra di riferimento |
| `MPW_MIN_OBS` | `5` | rilevamenti minimi prima di valutare una carta |

L'orario si cambia nel `cron` dello stesso file, **in UTC**: `0 5 * * *` sono le
07:00 italiane d'estate e le 06:00 d'inverno.

Alzare `MPW_MAX_CARDS` allunga il giro e ingrossa `history.json` (circa 170 KB per
1000 carte su 180 giorni), ma non ci sono problemi di quota: Scryfall non chiede
chiavi, il collector rispetta la pausa di 120 ms tra le richieste.

---

## Struttura

```
collect.py                    il collector
.github/workflows/collect.yml il job giornaliero + pubblicazione su Pages
app/index.html                l'app (file unico, funziona anche da sola)
app/sw.js                     service worker: uso offline e installazione
app/manifest.webmanifest      metadati PWA
data/days/AAAA-MM-GG.csv      una fotografia al giorno: id, prezzo
data/cards.csv               dizionario id -> nome, set, numero
public/                       generata dal workflow, è ciò che finisce online
```

Lo storico sta nei CSV giornalieri, non in `history.json`: sono file piccoli, mai
riscritti, che git comprime bene. `history.json` è solo la vista ricostruita a ogni
giro, e non viene committata — la pubblica Pages. Così il repo non si gonfia.

Se un giorno vuoi cambiare app, i dati restano tuoi e leggibili: sono CSV.

---

## Modalità dell'app

Nelle impostazioni, alla voce *Sorgente dei dati*:

- **Collector, poi Scryfall** (di serie) — legge il file pubblicato; se non lo
  trova, scarica da sé.
- **Solo collector** — non interroga mai Scryfall per la scansione.
- **Solo Scryfall** — l'app com'era prima del collector, tutto in locale.

Le carte che aggiungi ai preferiti e che non stanno nell'universo del collector
(perché sotto soglia) restano comunque nel tuo archivio locale: la fusione dei due
storici non le perde.

---

## Note

- I prezzi sono la *Trend Price* di Cardmarket ripubblicata da Scryfall, che
  sincronizza **ogni 24 ore**. È già una media mobile: il rumore delle singole
  inserzioni è filtrato a monte, ma uno spike reale appare con un giorno di
  ritardo e smussato. Raccogliere più volte al giorno sarebbe inutile.
- Cardmarket non viene mai contattato direttamente: la loro API al momento non
  accetta nuove richieste di accesso e richiederebbe comunque credenziali
  personali, impossibili da distribuire dentro un'app.
- Sono stime a scopo informativo, non quotazioni di vendita.
- Magic: The Gathering è un marchio di Wizards of the Coast. Questo progetto non è
  affiliato a Wizards of the Coast, Scryfall o Cardmarket.
