"""I lavori del motore. Ognuno è ripetibile: rilanciarlo non crea doppioni né doppi invii."""
import email.utils
import imaplib
import json
import logging
import os
import re
import smtplib
import ssl
import urllib.error
from datetime import datetime, timedelta
from email.message import EmailMessage

from . import db, llm, web
from .notify import telegram
from .projects import PROJECTS, RELATIONS, SENDER, SITE
from .schema import settings

log = logging.getLogger("radar")
SOCIAL = ("facebook.", "twitter.", "x.com", "youtube.", "vimeo.", "linkedin.", "tiktok.", "google.", "apple.", "spotify.", "wa.me", "t.me")
GOOD_LOCAL = ("distrib", "submission", "acquisition", "sales", "festival", "film", "produzione", "production", "office", "contact", "info", "hello")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def llm_down(e):
    """Credito esaurito, limite di richieste, servizio giù o tetto giornaliero: si ferma il lavoro, non si brucia nessun tentativo."""
    return isinstance(e, llm.BudgetExceeded) or (isinstance(e, urllib.error.HTTPError) and (e.code in (402, 429) or e.code >= 500))


def llm_down_note(e):
    if isinstance(e, llm.BudgetExceeded):
        return f"fermato: {e}"
    why = "credito OpenRouter esaurito" if e.code == 402 else "troppe richieste" if e.code == 429 else f"servizio non disponibile ({e.code})"
    telegram(f"Valutazioni e bozze ferme: {why}.")
    return f"fermato: {why}"


def event(contact_id, project, kind, detail):
    db.run("INSERT INTO radar_events(contact_id, project, kind, detail) VALUES(?,?,?,?)", contact_id, project, kind, detail)


# ---------------------------------------------------------------- scoperta

def discover(limit_sources=10):
    """Dalle fonti attive (pagine elenco) ai nuovi contatti, senza doppioni."""
    found = errors = 0
    known = db.q("SELECT id, lower(name) AS name, official_url FROM contacts")
    known_dom = {web.domain(r["official_url"]) for r in known if r["official_url"]}
    known_name = {r["name"] for r in known}
    for src in db.q("SELECT * FROM radar_sources WHERE active=1 AND kind='directory' ORDER BY last_run IS NOT NULL, last_run LIMIT ?", limit_sources):
        final, h, err = web.fetch(src["url"])
        if err:
            errors += 1
            log.warning("fonte %s: %s", src["name"], err)
            continue
        src_dom, new = web.domain(final), 0
        for url, label in web.links(final, h):
            d = web.domain(url)
            if not d or d == src_dom or any(s in d for s in SOCIAL) or len(label) < 3:
                continue
            if d in known_dom or label.lower() in known_name:
                continue
            row = db.batch([
                ("INSERT INTO contacts(name, type, country, official_url, status, project, source_system, verification_level, notes) "
                 "VALUES(?,?,?,?, 'to-contact', ?, ?, 'da_verificare', ?) RETURNING id",
                 [label, src["company_type"], src["country"], url, src["project"], "radar:" + src["name"], f"Trovata in {src['url']}"]),
            ])[0][0]
            db.run("INSERT OR IGNORE INTO contact_projects(contact_id, project, stage) VALUES(?,?,'to-contact')", row["id"], src["project"])
            known_dom.add(d)
            known_name.add(label.lower())
            new += 1
        db.run("UPDATE radar_sources SET last_run=?, found_total=found_total+? WHERE id=?", now(), new, src["id"])
        found += new
    return found, errors, f"{found} case nuove"


# ---------------------------------------------------------------- arricchimento + punteggio

def _best_email(cands):
    ok = [c for c in cands if c["mx"] is not False]
    ok.sort(key=lambda c: next((i for i, k in enumerate(GOOD_LOCAL) if k in c["email"].split("@")[0]), 99))
    return ok[0] if ok else None


SCORE_SYSTEM = (
    "Valuti case di produzione e distribuzione cinematografica per un regista italiano esordiente. "
    "Usa SOLO il testo del sito fornito: se un'informazione non c'è, dai 0 a quella voce. "
    "Sii severo: 100 non si dà quasi mai e la maggior parte delle case deve stare tra 30 e 75. "
    "Una voce supera la metà del suo massimo solo se il testo la documenta in modo esplicito, "
    "e in quel caso riporti in 'prove' la frase o il titolo del sito che la dimostra. "
    "Rispondi solo con un oggetto JSON."
)
MIN_READABLE = 600       # sotto questa soglia il sito non si è letto (JavaScript, errore): non si valuta
UNPROVEN_SHARE = 0.4     # una voce senza prova vale al massimo il 40% del suo massimo
MAX_TOTAL = 95


def rubric_max(desc):
    m = re.search(r"\(0-(\d+)\)", desc)
    return int(m.group(1)) if m else 10


def score_one(contact, project, sig, best):
    p = PROJECTS[project]
    rubric = p["rubric"]
    ask = {k: v for k, v in rubric.items() if k != "contatto"}
    payload = {
        "progetto": p["title"], "cosa_serve": p["need"], "progetto_descrizione": p["brief"], "regole": p["rules"],
        "griglia": ask,
        "casa": {"nome": contact["name"], "sito": contact["official_url"], "paese": contact["country"]},
        "anni_citati_nel_sito": sig["years"][-6:],
        "testo_sito": sig["text"][:9000],
        "formato_risposta": {
            "tipo": "producer|sales|distributor|festival|other",
            "punteggi": {k: "numero" for k in ask},
            "prove": {k: "frase o titolo del sito che giustifica il punteggio, o null" for k in ask},
            "motivo": "1-2 frasi in italiano: perché è adatta o no",
            "aggancio": "un fatto concreto dal sito (un loro film, un festival, una linea editoriale) da citare nella mail, o null",
            "scartare": "null oppure il motivo per cui è fuori target",
        },
    }
    if project == "non-fa-ridere":
        payload["formato_risposta"]["fa_corti"] = "si | no | non_so: la casa distribuisce o vende cortometraggi?"
        payload["formato_risposta"]["prova_corti"] = "frase o titolo del sito che lo dimostra, o null"
    user = json.dumps(payload, ensure_ascii=False)
    out = llm.ask_json(SCORE_SYSTEM, user)
    detail = {}
    for k, desc in ask.items():
        top = rubric_max(desc)
        v = max(0, min(top, int(out.get("punteggi", {}).get(k) or 0)))
        if not (out.get("prove") or {}).get(k):
            v = min(v, int(top * UNPROVEN_SHARE))
        detail[k] = v
    if project == "non-fa-ridere":
        fa = str(out.get("fa_corti") or "non_so").lower()
        fa = fa if fa in ("si", "no", "non_so") else "non_so"
        if fa != "si" or not out.get("prova_corti"):  # senza una prova dal sito i corti non contano come certi
            detail["corti"] = min(detail.get("corti", 0), 6)
        out["motivo"] = f"Fa corti: {'sì' if fa == 'si' else 'no' if fa == 'no' else 'non so'}. " + str(out.get("motivo") or "")
    if "contatto" in rubric:  # lo decide il codice, non il modello
        top = rubric_max(rubric["contatto"])
        detail["contatto"] = top if best and best["mx"] else round(top * 0.4) if sig["emails"] else 0
    return min(MAX_TOTAL, sum(detail.values())), detail, out


def enrich_score(limit=25):
    rows = db.q("""SELECT c.id, c.name, c.official_url, c.country, c.email, cp.project
                   FROM contact_projects cp JOIN contacts c ON c.id = cp.contact_id
                   WHERE cp.scored_at IS NULL AND c.official_url IS NOT NULL AND c.official_url <> ''
                     AND coalesce(c.enrich_attempts, 0) < 3 AND cp.stage IN ('to-contact')
                   ORDER BY cp.shortlist DESC, (cp.project = 'non-fa-ridere') DESC,
                            CASE WHEN CAST(c.priority AS INTEGER) BETWEEN 1 AND 3 THEN CAST(c.priority AS INTEGER) ELSE 9 END, c.id DESC LIMIT ?""", limit)
    done = errors = 0
    for c in rows:
        site = web.crawl_site(c["official_url"])
        if site["error"]:
            db.run("UPDATE contacts SET enrich_attempts=coalesce(enrich_attempts,0)+1, notes=coalesce(notes,'')||char(10)||? WHERE id=?",
                   f"[{now()[:10]} radar] sito non raggiungibile: {site['error']}", c["id"])
            errors += 1
            continue
        sig = web.signals(site)
        best = _best_email(sig["emails"])
        if len(sig["text"]) < MIN_READABLE:
            db.batch([
                ("UPDATE contact_projects SET scored_at=?, updated_at=? WHERE contact_id=? AND project=?", [now(), now(), c["id"], c["project"]]),
                ("UPDATE contacts SET notes=coalesce(notes,'')||char(10)||? WHERE id=?",
                 [f"[{now()[:10]} radar] sito poco leggibile (serve JavaScript?): punteggio non cambiato", c["id"]]),
            ])
            errors += 1
            continue
        try:
            total, detail, out = score_one(c, c["project"], sig, best)
        except Exception as e:
            if llm_down(e):
                return done, errors + 1, f"{done} schede valutate, {llm_down_note(e)}"
            log.warning("punteggio %s: %s", c["name"], e)
            errors += 1
            db.run("UPDATE contacts SET enrich_attempts=coalesce(enrich_attempts,0)+1 WHERE id=?", c["id"])
            continue
        # Il motore segnala "possibile fuori target" ma non scarta mai: la decisione è di Valerio.
        reason = out.get("motivo") or ""
        if out.get("scartare"):
            reason = f"Possibile fuori target: {out['scartare']} " + reason
        db.batch([
            ("UPDATE contacts SET email=coalesce(email, ?), contact_proof=coalesce(?, contact_proof), "
             "verification_level=CASE WHEN ? IS NOT NULL THEN 'verified' ELSE verification_level END, "
             "enriched_at=?, enrich_attempts=coalesce(enrich_attempts,0)+1, hook_title=?, "
             "fit_reason=coalesce(nullif(fit_reason,''), ?) WHERE id=?",
             [best and best["email"], best and f"pubblicata su {best['proof']}", best and best["email"], now(),
              out.get("aggancio"), out.get("motivo"), c["id"]]),
            ("UPDATE contact_projects SET score=?, score_detail=?, reason=?, stage=?, scored_at=?, updated_at=? WHERE contact_id=? AND project=?",
             [total, json.dumps(detail), reason, "to-contact", now(), now(), c["id"], c["project"]]),
        ])
        event(c["id"], c["project"], "scored", f"{total}/100")
        done += 1
    return done, errors, f"{done} schede valutate"


# ---------------------------------------------------------------- bozze

DRAFT_SYSTEM = (
    "Scrivi email brevi e personali da parte di un regista italiano a case di produzione o distribuzione. "
    "Tono diretto, niente adulazione, niente superlativi, niente promesse. Massimo 140 parole. "
    "Cita l'aggancio solo se è un fatto concreto. Non inventare fatti sul film o sulla casa. "
    "Parla di rapporti precedenti SOLO usando il campo 'storia_contatti'; se è vuoto, è un primo contatto. "
    "Non usare 'perche_loro' come fatto da dire al destinatario: serve solo a te per capire il taglio. "
    "Dove manca un dato usa un segnaposto tra parentesi quadre, ad esempio [link screener]. "
    "Rispondi solo con un oggetto JSON {\"oggetto\": ..., \"testo\": ...}."
)


def _busy_domains():
    rows = db.q("""SELECT DISTINCT c.email FROM contacts c WHERE c.contacted_at >= date('now', ?)
                   UNION SELECT to_email FROM radar_outbox WHERE status IN ('draft','approved','sent')""",
                f"-{settings()['no_repeat_days']} days")
    return {(r["email"] or "").split("@")[-1].lower() for r in rows if r["email"]}


def history(c):
    """Fatti sui contatti passati con la stessa casa (stesso sito o stessa email)."""
    facts = []
    dom = web.domain(c["official_url"])
    if dom in RELATIONS:
        facts.append(RELATIONS[dom])
    rows = db.q("""SELECT status, contacted_at, project FROM contacts
                   WHERE id <> ? AND status NOT IN ('to-contact', 'discarded', 'archived-unreachable')
                     AND ((? <> '' AND official_url LIKE ?) OR (email IS NOT NULL AND lower(email) = lower(?)))""",
                c["id"], dom, f"%{dom}%", c["email"])
    for r in rows:
        when = (r["contacted_at"] or "")[:10]
        facts.append(f"Valerio ha già scritto a questa casa{' il ' + when if when else ''} per un altro progetto; stato: {r['status']}. Nessuna collaborazione in corso.")
    return " ".join(dict.fromkeys(facts))


def draft_for_row(c, project, kind="first", budget=True):
    """Scrive una bozza per una casa e la mette in outbox. Solleva se il modello non risponde."""
    p = PROJECTS[project]
    lang = "it" if (c.get("country") or "IT").upper() in ("IT", "ITALY", "CH") else "en"
    user = json.dumps({
        "lingua": "italiano" if lang == "it" else "inglese",
        "mittente": f"{SENDER} ({SITE})", "progetto": p["title"], "cosa_chiedo": p["need"],
        "progetto_descrizione": p["brief"], "regole": p["rules"],
        "destinatario": c["name"], "aggancio": c.get("hook_title"), "perche_loro": c.get("reason") or c.get("fit_reason"),
        "storia_contatti": history(c),
    }, ensure_ascii=False)
    out = llm.ask_json(DRAFT_SYSTEM, user, max_tokens=700, budget=budget)
    new_id = db.batch([("INSERT INTO radar_outbox(contact_id, project, kind, to_email, subject, body, hook, lang) VALUES(?,?,?,?,?,?,?,?) RETURNING id",
                        [c["id"], project, kind, c["email"], out["oggetto"], out["testo"], c.get("hook_title"), lang])])[0][0]["id"]
    db.run("UPDATE contact_projects SET stage='draft', updated_at=? WHERE contact_id=? AND project=?", now(), c["id"], project)
    event(c["id"], project, "draft", out["oggetto"])
    return new_id


def draft(per_run=None):
    st = settings()
    per_run = int(per_run or st["drafts_per_run"])
    busy, made = _busy_domains(), 0
    for project, p in PROJECTS.items():
        cands = db.q("""SELECT c.id, c.name, c.email, c.country, c.official_url, c.hook_title, c.fit_reason, cp.score, cp.reason, cp.shortlist
                        FROM contact_projects cp JOIN contacts c ON c.id = cp.contact_id
                        WHERE cp.project=? AND cp.stage='to-contact' AND c.email LIKE '%@%'
                          AND c.verification_level='verified' AND (cp.shortlist=1 OR cp.score >= 65)
                          AND coalesce(c.do_not_contact, 0) = 0
                        ORDER BY cp.shortlist DESC, cp.score DESC LIMIT 40""", project)
        for c in cands:
            if made >= per_run:
                break
            dom = c["email"].split("@")[1].lower()
            if dom in busy and not dom.endswith(("gmail.com", "outlook.com", "hotmail.com", "yahoo.com")):
                continue
            if c["email"].lower() in busy:
                continue
            try:
                draft_for_row(c, project)
            except Exception as e:
                if llm_down(e):
                    return made, 1, f"{made} bozze, {llm_down_note(e)}"
                log.warning("bozza %s: %s", c["name"], e)
                continue
            busy.add(c["email"].lower())
            busy.add(dom)
            made += 1
    if made:
        telegram(f"{made} bozze pronte da approvare: {SITE}/admin/contatti")
    return made, 0, f"{made} bozze"


FOLLOWUP = {
    "it": ("Re: {subject}", "Ciao,\n\nriprendo la mia mail su {title}, nel caso fosse finita sotto altre.\n"
           "Se non è il momento o non fa per voi, va benissimo anche un no.\n\nGrazie,\n{sender}\n{site}"),
    "en": ("Re: {subject}", "Hi,\n\nfollowing up on my earlier email about {title}, in case it got buried.\n"
           "If it is not a fit or not the right time, a short no is perfectly fine.\n\nThanks,\n{sender}\n{site}"),
}


def followup():
    st = settings()
    rows = db.q("""SELECT o.* FROM radar_outbox o
                   WHERE o.status='sent' AND o.sent_at <= datetime('now', ?)
                     AND NOT EXISTS (SELECT 1 FROM radar_outbox f WHERE f.contact_id=o.contact_id AND f.project=o.project AND f.created_at > o.sent_at)
                     AND (SELECT count(*) FROM radar_outbox f WHERE f.contact_id=o.contact_id AND f.project=o.project AND f.kind='followup') < ?
                     AND (SELECT stage FROM contact_projects WHERE contact_id=o.contact_id AND project=o.project) NOT IN ('replied','discarded')
                     AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.id=o.contact_id AND c.do_not_contact=1)""",
                f"-{st['followup_days']} days", int(st["max_followups"]))
    for o in rows:
        subj, body = FOLLOWUP[o["lang"] if o["lang"] in FOLLOWUP else "it"]
        subject = o["subject"] if o["subject"].startswith("Re:") else subj.format(subject=o["subject"])
        db.run("INSERT INTO radar_outbox(contact_id, project, kind, to_email, subject, body, lang) VALUES(?,?, 'followup', ?,?,?,?)",
               o["contact_id"], o["project"], o["to_email"], subject,
               body.format(title=PROJECTS[o["project"]]["title"], sender=SENDER, site=SITE), o["lang"])
    if rows:
        telegram(f"{len(rows)} follow-up da approvare")
    return len(rows), 0, f"{len(rows)} follow-up preparati"


# ---------------------------------------------------------------- invio

def send():
    st = settings()
    if st["send_enabled"] != "1" or st["paused"] == "1":
        return 0, 0, "invii spenti"
    h = datetime.now().hour
    if not (int(st["window_start"]) <= h < int(st["window_end"])):
        return 0, 0, "fuori fascia oraria"
    sent_today = db.one("SELECT count(*) AS n FROM radar_outbox WHERE status='sent' AND date(sent_at)=date('now','localtime')")["n"]
    room = int(st["daily_cap"]) - sent_today
    if room <= 0:
        return 0, 0, "tetto giornaliero raggiunto"
    # Una casa da non contattare non riceve mai niente, nemmeno una bozza approvata prima.
    rows = db.q("""SELECT * FROM radar_outbox WHERE status='approved'
                   AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.id=radar_outbox.contact_id AND c.do_not_contact=1)
                   ORDER BY approved_at LIMIT ?""", min(room, 3))
    sent = errors = 0
    addr, pwd = os.environ["EMAIL_ADDRESS"], os.environ["EMAIL_PASSWORD"]
    for o in rows:
        if "[" in o["body"] and "]" in o["body"]:
            db.run("UPDATE radar_outbox SET status='draft', error='segnaposto ancora nel testo' WHERE id=?", o["id"])
            errors += 1
            continue
        msg = EmailMessage()
        msg["From"] = email.utils.formataddr((SENDER, addr))
        msg["To"] = o["to_email"]
        msg["Subject"] = o["subject"]
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=addr.split("@")[1])
        msg.set_content(o["body"])
        try:
            with smtplib.SMTP_SSL(os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("EMAIL_SMTP_PORT_SSL", "465")),
                                  context=ssl.create_default_context(), timeout=30) as s:
                s.login(addr, pwd)
                s.send_message(msg)
        except Exception as e:
            db.run("UPDATE radar_outbox SET status='error', error=? WHERE id=?", str(e)[:300], o["id"])
            errors += 1
            continue
        # Stati come quelli di Hermes: 'contacted', poi 'follow-up-1' e 'follow-up-2'.
        done_followups = db.one("SELECT count(*) AS n FROM radar_outbox WHERE contact_id=? AND project=? AND kind='followup' AND status='sent'",
                                o["contact_id"], o["project"])["n"]
        contact_status = f"follow-up-{min(done_followups + 1, 2)}" if o["kind"] == "followup" else "contacted"
        db.batch([
            ("UPDATE radar_outbox SET status='sent', sent_at=?, message_id=? WHERE id=?", [now(), msg["Message-ID"], o["id"]]),
            ("UPDATE contact_projects SET stage=?, updated_at=? WHERE contact_id=? AND project=?",
             ["followup" if o["kind"] == "followup" else "contacted", now(), o["contact_id"], o["project"]]),
            ("UPDATE contacts SET status=?, contacted_at=coalesce(contacted_at, ?) WHERE id=?", [contact_status, now(), o["contact_id"]]),
        ])
        event(o["contact_id"], o["project"], "sent", o["subject"])
        sent += 1
    return sent, errors, f"{sent} inviate"


# ---------------------------------------------------------------- casella

def inbox(days=7):
    """Legge i mittenti degli ultimi giorni (senza segnarli come letti) e riconosce le risposte."""
    addr, pwd = os.environ["EMAIL_ADDRESS"], os.environ["EMAIL_PASSWORD"]
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    m = imaplib.IMAP4_SSL(os.environ.get("EMAIL_IMAP_HOST", "imap.gmail.com"), int(os.environ.get("EMAIL_IMAP_PORT", "993")))
    m.login(addr, pwd)
    m.select("INBOX", readonly=True)
    _, ids = m.search(None, f'(SINCE "{since}")')
    senders = set()
    for i in ids[0].split()[-300:]:
        _, data = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
        frm = email.utils.parseaddr(data[0][1].decode("utf-8", "ignore").replace("From:", "").strip())[1].lower()
        if frm:
            senders.add(frm)
    m.logout()
    waiting = db.q("""SELECT DISTINCT o.contact_id, o.project, o.to_email, c.name FROM radar_outbox o JOIN contacts c ON c.id=o.contact_id
                      JOIN contact_projects cp ON cp.contact_id=o.contact_id AND cp.project=o.project
                      WHERE o.status='sent' AND cp.stage IN ('contacted','followup')""")
    hits = 0
    for w in waiting:
        dom = w["to_email"].split("@")[1].lower()
        generic = dom in ("gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "libero.it")
        if w["to_email"].lower() in senders or (not generic and any(s.endswith("@" + dom) for s in senders)):
            db.batch([
                ("UPDATE contact_projects SET stage='replied', updated_at=? WHERE contact_id=? AND project=?", [now(), w["contact_id"], w["project"]]),
                ("UPDATE contacts SET status='replied', reply=coalesce(reply, 'risposta ricevuta ' || ?) WHERE id=?", [now()[:10], w["contact_id"]]),
                ("UPDATE radar_outbox SET status='cancelled' WHERE contact_id=? AND project=? AND status IN ('draft','approved')", [w["contact_id"], w["project"]]),
            ])
            event(w["contact_id"], w["project"], "replied", w["to_email"])
            telegram(f"Risposta da {w['name']} ({PROJECTS[w['project']]['title']})")
            hits += 1
    return hits, 0, f"{len(senders)} mittenti letti, {hits} risposte"


from .report import weekly_report
from .requests_queue import process_requests

def census_dgca(years="", dir=""):
    from . import census_dgca as dgca
    return dgca.run(years, dir)


def census_distributori():
    from . import census_it
    return census_it.run_distributors()


def census_marche():
    from . import census_it
    return census_it.run_marche()


def census_enrich(limit=20):
    from . import census_enrich as enrich
    return enrich.run(limit)


JOBS = {"census:enrich": census_enrich, "census:dgca": census_dgca, "census:distributori": census_distributori, "census:marche": census_marche, "requests": process_requests, "report": weekly_report, "discover": discover, "enrich_score": enrich_score, "draft": draft, "followup": followup, "send": send, "inbox": inbox}
