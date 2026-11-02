"""Richieste accodate dall'admin del blog e da HAL (Telegram). Il motore le esegue una alla volta.

kind: add_house | draft | followup | shorten | translate
"""
import json
import logging
import re
import urllib.parse

from . import db, llm, web
from .notify import telegram
from .projects import PROJECTS, SENDER, SITE

log = logging.getLogger("radar")

TLD_COUNTRY = {"it": "IT", "fr": "FR", "de": "DE", "es": "ES", "pt": "PT", "uk": "GB", "be": "BE", "nl": "NL",
               "ch": "CH", "at": "AT", "ie": "IE", "se": "SE", "fi": "FI", "no": "NO", "dk": "DK", "pl": "PL"}
STALE_RUNNING_MIN = 10


class RequestFailed(Exception):
    """Errore atteso: la nota finisce nella richiesta e si vede nell'admin."""


def normalize_url(raw):
    raw = (raw or "").strip()
    if not raw:
        raise RequestFailed("indirizzo del sito mancante")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urllib.parse.urlparse(raw)
    if not parsed.netloc or "." not in parsed.netloc:
        raise RequestFailed(f"indirizzo non valido: {raw[:80]}")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}" or raw


def person_name(profile_url):
    """linkedin.com/in/luca-cococcetta-12867827 -> Luca Cococcetta."""
    slug = urllib.parse.unquote(profile_url.rstrip("/").rsplit("/", 1)[-1])
    parts = []
    for x in slug.split("-"):
        if re.fullmatch(r"[0-9a-f]{6,}", x):  # codice che LinkedIn aggiunge in coda
            continue
        x = re.sub(r"\d+$", "", x)
        if x:
            parts.append(x.capitalize())
    return " ".join(parts)[:60]


def match_email(name, emails, dom):
    """Un'email del dominio che coincide col nome (luca@, luca.cococcetta@): ipotesi, non certezza."""
    toks = [t.lower() for t in name.split()]
    if not toks:
        return None
    for e in emails:
        local, _, d = e["email"].partition("@")
        if dom.split(".")[0] in d and local.lower() in (toks[0], ".".join(toks), toks[0][0] + toks[-1], toks[0] + toks[-1]):
            return e["email"]
    return None


def _best_own_email(emails, dom):
    from .jobs import _best_email
    own = [e for e in emails if dom and dom.split(".")[0] in e["email"].split("@")[1]]
    return _best_email(own or emails)


# ---------------------------------------------------------------- aggiungi una casa

def add_house(req):
    from .jobs import now
    url = normalize_url(req["url"])
    project = req["project"] if req["project"] in PROJECTS else "walkie-talkie"
    label = PROJECTS[project]["title"]
    site = web.crawl_site(url, max_pages=8)
    if site["error"]:
        telegram(f"Non riesco a leggere {url}: {site['error']}.")
        raise RequestFailed(f"sito non raggiungibile: {site['error']}")
    dom = web.domain(site["final"])
    sig = web.signals(site)
    soc = web.socials(site["pages"])
    best = _best_own_email(sig["emails"], dom)
    name = web.site_name(site["pages"]) or dom
    country = TLD_COUNTRY.get(dom.rsplit(".", 1)[-1])
    ig = soc["instagram"][0] if soc["instagram"] else None
    li = soc["linkedin_company"][0] if soc["linkedin_company"] else None
    others = [e["email"] for e in sig["emails"] if not best or e["email"] != best["email"]]
    note = (f"[{now()[:10]} aggiunta da Telegram per {label}] sito: {site['final']}"
            + (f"; altre email: {', '.join(others[:6])}" if others else "")
            + (f"; altri Instagram: {', '.join(soc['instagram'][1:4])}" if len(soc["instagram"]) > 1 else "")
            + (f"; LinkedIn persone: {', '.join(soc['linkedin_people'][:5])}" if soc["linkedin_people"] else ""))
    proof = f"pubblicata su {best['proof']}" if best else None
    kind = "producer" if project == "walkie-talkie" else "sales"

    existing = db.one("SELECT id FROM contacts WHERE official_url LIKE ? ORDER BY id LIMIT 1", f"%{dom}%")
    if existing:
        cid, verb = existing["id"], "aggiornata"
        db.run("""UPDATE contacts SET email = coalesce(nullif(email, ''), ?), contact_proof = coalesce(nullif(contact_proof, ''), ?),
                         verification_level = CASE WHEN ? IS NOT NULL AND coalesce(verification_level, '') <> 'verified' THEN 'verified' ELSE verification_level END,
                         instagram = coalesce(nullif(instagram, ''), ?), linkedin = coalesce(nullif(linkedin, ''), ?),
                         country = coalesce(nullif(country, ''), ?),
                         project = CASE WHEN coalesce(project, 'general') = 'general' THEN ? ELSE project END,
                         notes = coalesce(notes, '') || char(10) || ?, enrich_attempts = 0 WHERE id = ?""",
               best and best["email"], proof, best and best["email"], ig, li, country, project, note, cid)
    else:
        cid, verb = db.batch([("""INSERT INTO contacts(name, type, country, email, official_url, contact_proof, verification_level, status,
                                          project, source_system, instagram, linkedin, notes)
                                  VALUES(?,?,?,?,?,?,?, 'to-contact', ?, 'hal-telegram', ?,?,?) RETURNING id""",
                               [name, kind, country, best and best["email"], site["final"], proof,
                                "verified" if best else "da_verificare", project, ig, li, note])])[0][0]["id"], "aggiunta"
    db.run("""INSERT INTO contact_projects(contact_id, project, shortlist, stage) VALUES(?,?,1,'to-contact')
              ON CONFLICT(contact_id, project) DO UPDATE SET shortlist = 1, scored_at = NULL, updated_at = datetime('now')""", cid, project)
    db.run("INSERT INTO radar_events(contact_id, project, kind, detail) VALUES(?,?,'added','da Telegram')", cid, project)
    people = []
    for prof in soc["linkedin_people"][:8]:
        pname = person_name(prof)
        if len(pname) < 6:
            continue
        mail = match_email(pname, sig["emails"], dom)
        db.run("""INSERT INTO people(contact_id, name, email, email_confidence, linkedin_search, source_url, social_follow_status)
                  VALUES(?,?,?,?,?,?, 'to-follow') ON CONFLICT(contact_id, name) DO NOTHING""",
               cid, pname, mail, "guessed-valid" if mail else None, prof, site["final"])
        people.append((pname, mail, prof))
    guess = None
    if not ig:
        guess = f"https://www.instagram.com/{dom.split('.')[0]}/"

    lines = [f"🏠 {name} {verb} per {label} (id {cid})", f"Sito: {site['final']}"]
    if sig["emails"]:
        for e in sig["emails"][:5]:
            mark = "dominio non riceve posta" if e["mx"] is False else "ok"
            lines.append(f"Email: {e['email']} ({mark})")
    else:
        lines.append("Email: nessuna pubblicata sul sito (potrebbe servire un modulo di contatto)")
    lines.append(f"Instagram: {', '.join(soc['instagram'][:3])}" if soc["instagram"] else
                 f"Instagram: il sito non lo linka. Prova {guess} (da verificare) o cerca su Google: "
                 f"https://www.google.com/search?q=site:instagram.com+%22{urllib.parse.quote(name)}%22")
    lines.append(f"LinkedIn: {', '.join(soc['linkedin_company'][:2])}" if soc["linkedin_company"] else
                 f"LinkedIn: non linkato dal sito, cerca: https://www.linkedin.com/search/results/companies/?keywords={urllib.parse.quote(name)}")
    for pname, mail, prof in people[:5]:
        lines.append(f"Persona: {pname}" + (f" · {mail} (da confermare)" if mail else "") + f" · {prof}")
    lines.append("In shortlist. Il punteggio arriva al prossimo giro di valutazione.")
    lines.append(f"{SITE}/admin/contatti#casa-{cid}")
    telegram("\n".join(lines))
    return f"{name}: {len(sig['emails'])} email, {len(soc['instagram'])} Instagram, {len(soc['linkedin_company'])} LinkedIn"


# ---------------------------------------------------------------- bozze e follow-up

def _contact(contact_id, project):
    c = db.one("""SELECT c.id, c.name, c.email, c.country, c.official_url, c.hook_title, c.fit_reason, cp.reason
                  FROM contacts c LEFT JOIN contact_projects cp ON cp.contact_id = c.id AND cp.project = ? WHERE c.id = ?""", project, contact_id)
    if not c:
        raise RequestFailed("contatto non trovato")
    if "@" not in (c["email"] or ""):
        raise RequestFailed("la casa non ha un'email: aggiungila prima")
    return c


def make_draft(req):
    from .jobs import draft_for_row
    if db.one("SELECT id FROM radar_outbox WHERE contact_id=? AND project=? AND status IN ('draft','approved')", req["contact_id"], req["project"]):
        raise RequestFailed("c'è già una bozza per questa casa e questo film")
    new_id = draft_for_row(_contact(req["contact_id"], req["project"]), req["project"], budget=False)
    return f"bozza {new_id} pronta"


def make_followup(req):
    from .jobs import FOLLOWUP, event, history
    c = _contact(req["contact_id"], req["project"])
    if db.one("SELECT id FROM radar_outbox WHERE contact_id=? AND project=? AND status IN ('draft','approved')", c["id"], req["project"]):
        raise RequestFailed("c'è già una bozza in sospeso per questa casa")
    last = db.one("SELECT subject, lang FROM radar_outbox WHERE contact_id=? AND project=? AND subject IS NOT NULL ORDER BY id DESC LIMIT 1", c["id"], req["project"])
    title = PROJECTS[req["project"]]["title"]
    lang = (last or {}).get("lang") or ("it" if (c["country"] or "IT").upper() in ("IT", "CH") else "en")
    lang = lang if lang in FOLLOWUP else "it"
    subj_t, body_t = FOLLOWUP[lang]
    base = (last or {}).get("subject") or title
    subject = base if base.startswith("Re:") else subj_t.format(subject=base)
    new_id = db.batch([("INSERT INTO radar_outbox(contact_id, project, kind, to_email, subject, body, lang) VALUES(?,?, 'followup', ?,?,?,?) RETURNING id",
                        [c["id"], req["project"], c["email"], subject, body_t.format(title=title, sender=SENDER, site=SITE), lang])])[0][0]["id"]
    db.run("UPDATE contact_projects SET stage='draft', updated_at=datetime('now') WHERE contact_id=? AND project=?", c["id"], req["project"])
    event(c["id"], req["project"], "draft", f"follow-up: {subject}")
    return f"follow-up {new_id} pronto"


REWRITE_SYSTEM = (
    "Riscrivi email brevi di un regista italiano a case di produzione o distribuzione. "
    "Non inventare fatti, non aggiungere promesse, mantieni identici i segnaposto tra parentesi quadre. "
    "Rispondi solo con un oggetto JSON {\"oggetto\": ..., \"testo\": ...}."
)


def _edit_draft(req, instruction, lang=None):
    o = db.one("SELECT * FROM radar_outbox WHERE id = ?", req["outbox_id"])
    if not o:
        raise RequestFailed("bozza non trovata")
    if o["status"] != "draft":
        raise RequestFailed("si riscrivono solo le bozze non ancora in coda")
    out = llm.ask_json(REWRITE_SYSTEM, json.dumps({"istruzione": instruction, "oggetto": o["subject"], "testo": o["body"]}, ensure_ascii=False), max_tokens=700, budget=False)
    db.run("UPDATE radar_outbox SET subject=?, body=?, lang=coalesce(?, lang), error=NULL WHERE id=?", out["oggetto"], out["testo"], lang, o["id"])
    return o


def shorten(req):
    _edit_draft(req, "Riscrivi più breve: al massimo 80 parole, stessa lingua, stesso tono diretto, stessi fatti.")
    return "bozza accorciata"


def translate(req):
    _edit_draft(req, "Traduci in inglese naturale, non letterale, mantenendo tono e fatti. Il film è una commedia in dialetto con sottotitoli inglesi curati.", lang="en")
    return "bozza tradotta in inglese"


HANDLERS = {"add_house": add_house, "draft": make_draft, "followup": make_followup, "shorten": shorten, "translate": translate}


def process_requests(limit=10):
    from .jobs import llm_down, llm_down_note
    db.run("UPDATE radar_requests SET status='pending' WHERE status='running' AND created_at <= datetime('now', ?)", f"-{STALE_RUNNING_MIN} minutes")
    done = errors = 0
    for r in db.q("SELECT * FROM radar_requests WHERE status='pending' ORDER BY id LIMIT ?", limit):
        db.run("UPDATE radar_requests SET status='running' WHERE id=?", r["id"])
        try:
            note, status = HANDLERS[r["kind"]](r), "done"
            done += 1
        except RequestFailed as e:
            note, status = str(e), "failed"
            errors += 1
        except Exception as e:
            note, status = (llm_down_note(e) if llm_down(e) else f"{type(e).__name__}: {e}"[:200]), "failed"
            errors += 1
            log.warning("richiesta %s (%s): %s", r["id"], r["kind"], note)
        db.run("UPDATE radar_requests SET status=?, note=?, done_at=datetime('now') WHERE id=?", status, note, r["id"])
    return done, errors, f"{done} richieste eseguite" + (f", {errors} non riuscite" if errors else "")
