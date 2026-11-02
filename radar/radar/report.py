"""Report settimanale: se e come cresce il database. Parte dal bot Telegram di HAL (stesso token)."""
import json
from datetime import datetime

from . import db, llm
from .notify import telegram
from .projects import SITE


def metrics():
    m = db.one("""SELECT count(*) AS total,
                         sum(email LIKE '%@%') AS with_email,
                         sum(verification_level = 'verified') AS verified
                  FROM contacts""")
    cp = db.one("""SELECT sum(project = 'non-fa-ridere') AS nfr, sum(project = 'walkie-talkie') AS wt,
                          sum(scored_at IS NOT NULL) AS scored, sum(shortlist = 1) AS shortlist,
                          sum(stage IN ('contacted', 'followup', 'replied', 'warm')) AS contacted,
                          sum(stage IN ('replied', 'warm')) AS replied
                   FROM contact_projects""")
    sources = {r["s"]: r["n"] for r in db.q(
        "SELECT coalesce(source_system, 'altro') AS s, count(*) AS n FROM contacts GROUP BY 1 ORDER BY 2 DESC LIMIT 12")}
    return {**{k: int(v or 0) for k, v in {**m, **cp}.items()}, "sources": sources}


def n(v):
    return f"{v:,}".replace(",", ".")


def delta(now, before, key):
    d = now[key] - (before[key] if before else now[key])
    return f"{n(now[key])} ({'+' if d >= 0 else '−'}{n(abs(d))})" if before else n(now[key])


def build(now, before, since, balance=None):
    lines = [f"Settimana del {datetime.now().strftime('%d/%m')}"]
    lines.append(f"Contatti: {delta(now, before, 'total')}" + (f" dal {since}" if before else " (prima rilevazione, il confronto parte dalla prossima)"))
    lines.append(f"• con email {delta(now, before, 'with_email')} · verificati {delta(now, before, 'verified')}")
    lines.append(f"• Non fa ridere {delta(now, before, 'nfr')} · Walkie Talkie {delta(now, before, 'wt')}")
    if before:
        grew = {s: c - before["sources"].get(s, 0) for s, c in now["sources"].items()}
        top = [f"{s} +{c}" for s, c in sorted(grew.items(), key=lambda x: -x[1]) if c > 0][:4]
        lines.append("Da dove arrivano: " + (", ".join(top) if top else "nessuna fonte nuova"))
    lines.append(f"Pipeline: contattati {delta(now, before, 'contacted')} · risposte {delta(now, before, 'replied')} · in shortlist {n(now['shortlist'])}")
    lines.append(f"Valutati con punteggio: {delta(now, before, 'scored')} su {n(now['nfr'] + now['wt'])}")
    runs = db.one("SELECT count(*) AS tot, sum(status != 'ok') AS bad FROM radar_runs WHERE started_at >= datetime('now', '-7 days')")
    last = db.one("SELECT note FROM radar_runs WHERE job IN ('enrich_score', 'draft') ORDER BY id DESC LIMIT 1")
    lines.append(f"Motore: {runs['tot']} giri registrati, {int(runs['bad'] or 0)} con problemi")
    if balance:
        left = balance[0]
        lines.append(f"Credito OpenRouter: {left:.2f} $" + (" ⚠ sta per finire" if left < 2 else ""))
    if last and "fermato" in (last["note"] or ""):
        lines.append(f"⚠ {last['note']}")
    if before and now["total"] == before["total"] and now["with_email"] == before["with_email"]:
        lines.append("Nessuna crescita questa settimana: guarda le fonti nella pagina Motore.")
    lines.append(f"{SITE}/admin/contatti")
    return "\n".join(lines)


def weekly_report(send=1):
    now = metrics()
    row = db.one("SELECT day, data FROM radar_snapshots WHERE day <= date('now', '-6 days') ORDER BY day DESC LIMIT 1") \
        or db.one("SELECT day, data FROM radar_snapshots WHERE day < date('now') ORDER BY day LIMIT 1")
    before = json.loads(row["data"]) if row else None
    text = build(now, before, row["day"][5:].replace("-", "/") if row else "", llm.balance())
    db.run("INSERT INTO radar_snapshots(day, data) VALUES(date('now'), ?) ON CONFLICT(day) DO UPDATE SET data = excluded.data", json.dumps(now))
    if send:
        telegram(text)
    else:
        print(text)
    return 1, 0, "report inviato" if send else "fotografia salvata, report non inviato"
