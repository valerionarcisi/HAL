# Radar contatti

Motore sempre acceso sul NAS che trova case di produzione e distribuzione per i film di Valerio
(Non fa ridere → distribuzione festival, Walkie Talkie → produzione/coproduzione), le valuta, scrive le bozze
e spedisce le mail approvate. Python senza librerie esterne, database Turso, modello via OpenRouter.

La dashboard è nel blog (`/admin/contatti`, vedi `docs/radar-contatti-spec.md` del blog). Il blog non spedisce mai email.

## Avvio

    cp .env.example .env   # compilare
    docker compose up -d --build
    docker logs -f radar

## Lavori (`python -m radar run <lavoro>`)

| Lavoro | Quando | Cosa |
| --- | --- | --- |
| `requests` | ogni minuto | esegue le richieste accodate da admin e HAL (`radar_requests`) |
| `send` | ogni 10 minuti | spedisce le bozze approvate (solo se accesi, in fascia, sotto il tetto) |
| `inbox` | ogni 15 minuti | riconosce le risposte senza segnare le mail come lette |
| `enrich_score` | 1:10, 6:10, 13:10, 19:10 | visita i siti, trova email (con MX), dà il punteggio |
| `draft` | 7:00 | scrive fino a `drafts_per_run` bozze |
| `followup` | 7:30 | prepara i follow-up dalle mail già inviate dal motore |
| `report` | lunedì 8:30 | come cresce il database, su Telegram con il bot di HAL |
| `discover` | lunedì 5:00 | nuove case dalle fonti elencate in `radar_sources` |

`send` e `inbox` finiscono nello storico (`radar_runs`) solo se fanno qualcosa o falliscono. Il segno di vita è `hb_<lavoro>` in `radar_settings`.

## Regole

- Gli invii partono **spenti** (`send_enabled = 0`): si accendono dalla pagina Motore.
- Nessuna mail parte con segnaposto tra `[parentesi]`, né a una casa contattata negli ultimi `no_repeat_days`.
- Il punteggio: l'email verificata la decide il codice, il resto il modello con prova dal sito, mai oltre 95; sito illeggibile = punteggio invariato.
- **Tetto di spesa:** `llm_daily_calls` (40 al giorno) limita le chiamate al modello dei lavori automatici; le richieste fatte a mano dall'admin non sono bloccate ma contano. Misurato il 29/10/2026: 0,0055 $ a chiamata (Haiku 4.5), quindi al massimo ~0,22 $ al giorno. Il saldo reale di OpenRouter è nel report del lunedì.
- **Il motore non scarta mai una casa**: se il sito sembra fuori target scrive «Possibile fuori target: …» nel motivo e lascia la fase com'è. Per Non fa ridere il motivo comincia con «Fa corti: sì / no / non so», e senza una frase del sito che lo provi i corti valgono al massimo 6/20.
- Se il modello risponde 402/429/5xx il lavoro si ferma, avvisa su Telegram e non consuma tentativi sui contatti.

## Aggiungere una casa

Da Telegram: `/casa <sito> [produzione|distribuzione]` (comando di HAL). HAL accoda una riga, il motore legge il sito,
salva la casa (in shortlist), le persone trovate su LinkedIn e risponde nella stessa chat con email, Instagram e LinkedIn.

## Test

    python -m unittest discover -s tests -t .

I test eseguono tutte le scritture su SQLite con lo **schema vero** delle tabelle storiche (`tests/real_schema.py`),
con rete, SMTP, IMAP e modello finti. Se cambi una tabella, aggiorna quel file.
