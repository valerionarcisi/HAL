"""Uso:
  python -m radar              scheduler sempre acceso (il container)
  python -m radar migrate      crea le tabelle del motore
  python -m radar run <lavoro> lancia un lavoro una volta (discover, enrich_score, draft, followup, send, inbox)
"""
import logging
import sys
import time
import traceback
from datetime import datetime, timezone

from . import db, schema
from .jobs import JOBS
from .notify import telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("radar")

# (lavoro, condizione sull'orario locale). Controllato una volta al minuto.
SCHEDULE = [
    ("discover", lambda t: t.weekday() == 0 and t.hour == 5 and t.minute == 0),
    ("enrich_score", lambda t: t.hour in (1, 6, 13, 19) and t.minute == 10),
    ("draft", lambda t: t.hour == 7 and t.minute == 0),
    ("followup", lambda t: t.hour == 7 and t.minute == 30),
    ("send", lambda t: t.minute % 10 == 5),
    ("inbox", lambda t: t.minute % 15 == 2),
    ("requests", lambda t: True),
    ("report", lambda t: t.weekday() == 0 and t.hour == 8 and t.minute == 30),
]


# Girano ogni pochi minuti: finiscono nello storico solo se fanno qualcosa o falliscono.
# Altrimenti basta un segno di vita (hb_<lavoro>) che l'admin mostra come "ultimo controllo".
QUIET = {"send", "inbox", "requests"}


def run_job(name, **kw):
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        found, errors, note = JOBS[name](**kw)
        status = "ok" if not errors else "warning"
    except Exception as e:
        found, errors, note, status = 0, 1, f"{type(e).__name__}: {e}"[:300], "error"
        log.error("%s fallito\n%s", name, traceback.format_exc())
        telegram(f"Errore nel lavoro {name}: {note}")
    db.run("INSERT INTO radar_settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", f"hb_{name}", started)
    if not (name in QUIET and status == "ok" and not found):
        db.run("INSERT INTO radar_runs(job, started_at, finished_at, status, found, errors, note) VALUES(?,?,datetime('now'),?,?,?,?)",
               name, started, status, found, errors, note)
    log.info("%s: %s (%s errori) %s", name, status, errors, note)
    return status


def loop():
    schema.migrate()
    telegram("Motore avviato")
    last = None
    while True:
        t = datetime.now().replace(second=0, microsecond=0)
        if t != last:
            last = t
            for name, when in SCHEDULE:
                if when(t):
                    run_job(name)
        time.sleep(15)


def main(argv):
    if len(argv) <= 1:
        loop()
    elif argv[1] == "migrate":
        schema.migrate()
        print("ok")
    elif argv[1] == "run" and len(argv) >= 3 and argv[2] in JOBS:
        kw = dict(a.split("=", 1) for a in argv[3:])
        kw = {k: int(v) if v.isdigit() else v for k, v in kw.items()}
        sys.exit(0 if run_job(argv[2], **kw) != "error" else 1)
    else:
        print(__doc__)
        sys.exit(2)


main(sys.argv)
