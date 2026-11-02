"""Censimento delle case: normalizzazione, deduplicazione, crediti, profilo, telefoni, do_not_contact.

Il censimento non crea mai righe in contact_projects: sceglie chi contattare una ricerca sul catalogo, più tardi.
Le righe esistenti non si riscrivono: si riempiono solo i campi vuoti e si aggiunge la fonte.
"""
import json
import re
import unicodedata
from urllib.parse import urlparse

from . import db

KINDS = ("production", "short_distribution", "theatrical_distribution", "sales_agent", "festival", "other")
FORMATS = ("short", "feature", "documentary", "series", "animation")
ROLES = ("producer", "coproducer", "distributor", "sales")

# Siti che non identificano una casa: elenchi, social, enciclopedie. Il dominio non vale come chiave di dedup.
AGGREGATOR_DOMAINS = {
    "filmitalia.org", "cineuropa.org", "imdb.com", "wikipedia.org", "wikidata.org", "linkedin.com", "instagram.com",
    "facebook.com", "twitter.com", "x.com", "youtube.com", "vimeo.com", "google.com", "cinema.cultura.gov.it",
    "coe.int", "europa.eu", "centrodelcorto.it", "linktr.ee",
}

LEGAL_FORMS = {
    "srl", "srls", "spa", "sas", "snc", "sapa", "ltd", "limited", "llc", "inc", "gmbh", "ag", "sarl", "sas", "sa", "bv", "nv",
    "ab", "oy", "aps", "as", "kft", "sro", "sl", "lda", "ltda", "ug", "kg", "co", "cie", "the",
}
GENERIC_WORDS = {"film", "films", "filmproduktion", "production", "productions", "produzione", "produzioni", "pictures", "studios", "studio"}


def _ascii(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_name(name):
    """Nome senza etichette tra parentesi, forme societarie, 'film', 'production(s)', accenti e punteggiatura.
    Se togliendo tutto non resta nulla (es. 'Film Production') si torna al nome pulito completo."""
    s = re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", name or "")
    s = _ascii(s).lower().replace("&", " and ")
    s = re.sub(r"\b([a-z])\.(?=[a-z]\.|[a-z]\b)", r"\1", s)  # s.r.l. -> srl
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = s.split()
    core = [w for w in words if w not in LEGAL_FORMS and w not in GENERIC_WORDS]
    return " ".join(core or [w for w in words if w not in LEGAL_FORMS] or words)


def site_domain(url):
    """Dominio registrabile del sito ufficiale, senza www. None per pagine di elenchi e social."""
    if not url:
        return None
    u = url.strip()
    if "//" not in u:
        u = "//" + u
    host = (urlparse(u).hostname or "").lower().removeprefix("www.")
    if not host or "." not in host:
        return None
    if any(host == d or host.endswith("." + d) for d in AGGREGATOR_DOMAINS):
        return None
    return host


def normalize_vat(vat):
    v = re.sub(r"[^A-Za-z0-9]", "", vat or "").upper()
    if re.fullmatch(r"IT\d{11}", v):
        v = v[2:]
    return v if len(v) >= 8 else None


# ---------------------------------------------------------------- telefoni

def normalize_phone(raw, default_country="IT"):
    """Numero in E.164 oppure None. Riconosce +, 00, tel:, '(0)', separatori e i formati italiani (0x…, 3xx…).
    Senza prefisso internazionale si assume l'Italia; per altri paesi serve il prefisso scritto."""
    if not raw:
        return None
    s = re.sub(r"^\s*tel:", "", str(raw).strip(), flags=re.I)
    # "(0)" dopo il prefisso: in Italia lo 0 fa parte del numero (+39 (0)71 -> +39 071), altrove si toglie (+44 (0)20 -> +44 20).
    s = re.sub(r"\(\s*0\s*\)", "0" if re.match(r"\s*(\+|00)\s*39\b", s) else "", s)
    s = re.split(r"\b(?:int|ext|interno|e?xt)\b\.?", s, flags=re.I)[0]
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if s.lstrip().startswith("+"):
        num = digits
    elif digits.startswith("00"):
        num = digits[2:]
    elif default_country == "IT":
        num = "39" + digits
    else:
        return None
    if not (8 <= len(num) <= 15):
        return None
    if num.startswith("39"):
        national = num[2:]
        # Italia: fissi 0x… (6-11 cifre), cellulari 3xx… (9-10 cifre), numeri verdi 80x.
        if not (national[:1] in "03" or national.startswith("8")) or not (6 <= len(national) <= 11):
            return None
    return "+" + num


# ---------------------------------------------------------------- dedup

class CompanyIndex:
    """Indice in memoria di contacts per dominio, nome normalizzato (con paese) e partita IVA."""

    def __init__(self):
        self.by_domain, self.by_name, self.by_vat = {}, {}, {}
        for r in db.q("SELECT id, name, company, official_url, country, vat_id FROM contacts"):
            self.add(r["id"], r["name"], r["official_url"], r["country"], r["vat_id"], r["company"])

    def add(self, cid, name, url, country, vat=None, company=None):
        d = site_domain(url)
        if d:
            self.by_domain.setdefault(d, cid)
        for n in (name, company):
            key = normalize_name(n) if n else ""
            if len(key) >= 3:
                self.by_name.setdefault(key, []).append((cid, (country or "").upper()))
        v = normalize_vat(vat)
        if v:
            self.by_vat.setdefault(v, cid)

    def find(self, name, url=None, country=None, vat=None):
        v = normalize_vat(vat)
        if v and v in self.by_vat:
            return self.by_vat[v]
        d = site_domain(url)
        if d and d in self.by_domain:
            return self.by_domain[d]
        key = normalize_name(name)
        c = (country or "").upper()
        for cid, ccountry in self.by_name.get(key, []) if len(key) >= 3 else []:
            if not c or not ccountry or c == ccountry:  # stesso nome in paesi diversi non è la stessa casa
                return cid
        return None


def add_source(contact_id, url, source_name):
    if url:
        db.run("INSERT OR IGNORE INTO contact_sources(contact_id, url, source_name) VALUES(?,?,?)", contact_id, url, source_name)


def upsert_company(index, name, source_url, source_name, url=None, country=None, city=None, kind=None, vat=None, email=None, phone=None):
    """Trova la casa o la crea. Ritorna (id, creata). Una casa esistente non viene riscritta:
    si riempiono solo i campi vuoti. La fonte si aggiunge sempre. Non tocca contact_projects."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind sconosciuto: {kind}")
    if not source_url:
        raise ValueError("ogni casa ha bisogno di almeno una fonte")
    phone = normalize_phone(phone, (country or "IT").upper()) if phone else None
    vat = normalize_vat(vat)
    cid = index.find(name, url, country, vat)
    if cid:
        db.run("""UPDATE contacts SET kind = coalesce(kind, ?), phone = coalesce(nullif(phone, ''), ?), city = coalesce(nullif(city, ''), ?),
                         country = coalesce(nullif(country, ''), ?), vat_id = coalesce(nullif(vat_id, ''), ?),
                         official_url = coalesce(nullif(official_url, ''), ?), email = coalesce(nullif(email, ''), ?)
                  WHERE id = ?""", kind, phone, city, country, vat, url, email, cid)
        created = False
    else:
        cid = db.batch([("""INSERT INTO contacts(name, type, country, city, email, official_url, kind, phone, vat_id, status, source_system, verification_level)
                            VALUES(?,?,?,?,?,?,?,?,?, 'census', 'census', 'to_verify') RETURNING id""",
                         [name, kind or "other", country, city, email, url, kind, phone, vat])])[0][0]["id"]
        created = True
    add_source(cid, source_url, source_name)
    index.add(cid, name, url, country, vat)
    return cid, created


# ---------------------------------------------------------------- crediti

def _union(*vals):
    seen, out = set(), []
    for v in vals:
        for item in re.split(r"\s*[,;]\s*", v or ""):
            if item and item.lower() not in seen:
                seen.add(item.lower())
                out.append(item)
    return ", ".join(out) or None


def add_credit(contact_id, title, role, source_url, year=0, format=None, genres=None, countries=None, festivals=None, funds=None, person_id=None):
    """Aggiunge un credito o lo completa se esiste già (stesso contatto, titolo, anno e ruolo).
    Se prima l'anno mancava (0) e ora c'è, il credito si aggiorna invece di duplicarsi."""
    if role not in ROLES:
        raise ValueError(f"ruolo sconosciuto: {role}")
    if format is not None and format not in FORMATS:
        raise ValueError(f"formato sconosciuto: {format}")
    if not source_url or not title:
        raise ValueError("un credito ha bisogno di titolo e fonte")
    year = int(year or 0)
    key = normalize_name(title)
    same = [r for r in db.q("SELECT * FROM credits WHERE contact_id=? AND role=?", contact_id, role)
            if normalize_name(r["title"]) == key and (r["year"] == year or 0 in (r["year"], year))]
    if not same:
        db.run("""INSERT INTO credits(contact_id, person_id, title, year, format, genres, role, countries, festivals, funds, source_url)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""", contact_id, person_id, title.strip(), year, format, genres, role, countries, festivals, funds, source_url)
        return True
    r = same[0]
    db.run("""UPDATE credits SET year=?, format=coalesce(format, ?), genres=?, countries=?, festivals=?, funds=?,
                     person_id=coalesce(person_id, ?) WHERE id=?""",
           r["year"] or year, format, _union(r["genres"], genres), _union(r["countries"], countries),
           _union(r["festivals"], festivals), _union(r["funds"], funds), person_id, r["id"])
    return False


# ---------------------------------------------------------------- profilo

FIRST_RE = re.compile(r"opera\s+prima|opere\s+prime|first\s+(feature|film|work)|debut|premier\s+long|primo\s+film", re.I)
SECOND_RE = re.compile(r"opera\s+seconda|opere\s+seconde|second\s+(feature|film|work)|secondo\s+film", re.I)


def build_profile(contact, credits, this_year):
    """Profilo calcolato dai crediti (non da quello che la casa scrive di sé). Funzione pura."""
    own = (contact.get("country") or "").upper()
    prod = [c for c in credits if c["role"] in ("producer", "coproducer")]
    years = sorted({c["year"] for c in credits if c["year"]})
    count = lambda field: _count_items(c[field] for c in credits)  # noqa: E731
    text = lambda c: " ".join(filter(None, (c.get("funds"), c.get("genres"))))  # noqa: E731
    countries = {k: v for k, v in _count_items(c["countries"] for c in prod).items() if k.upper() != own}
    latest = sorted(credits, key=lambda c: (c["year"], c["title"]), reverse=True)[:5]
    return {
        "credits": len(credits),
        "roles": _count_items(c["role"] for c in credits),
        "formats": _count_items(c["format"] for c in credits),
        "genres": count("genres"),
        "first_year": years[0] if years else None,
        "last_year": years[-1] if years else None,
        "active_last_3y": bool(years and years[-1] >= this_year - 2),
        "first_works": sum(1 for c in credits if FIRST_RE.search(text(c))),
        "second_works": sum(1 for c in credits if SECOND_RE.search(text(c))),
        "coproduction_countries": countries,
        "funds": sorted(set(_flat(c["funds"] for c in credits))),
        "latest_titles": [{"title": c["title"], "year": c["year"] or None, "format": c["format"], "role": c["role"]} for c in latest],
    }


def _flat(values):
    for v in values:
        for item in re.split(r"\s*[,;]\s*", v or ""):
            if item:
                yield item


def _count_items(values):
    out = {}
    for item in _flat(values):
        out[item] = out.get(item, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def refresh_profile(contact_id, this_year=None):
    from datetime import date
    contact = db.one("SELECT id, country FROM contacts WHERE id=?", contact_id)
    if not contact:
        return None
    profile = build_profile(contact, db.q("SELECT * FROM credits WHERE contact_id=?", contact_id), this_year or date.today().year)
    db.run("UPDATE contacts SET profile_json=? WHERE id=?", json.dumps(profile, ensure_ascii=False), contact_id)
    return profile


# ---------------------------------------------------------------- persone

def add_person(contact_id, name, role, source_url, email=None, email_confidence=None, phone=None, country="IT"):
    """Una persona entra solo con nome, ruolo, casa e una pagina dove compare. Ritorna l'id o None se esisteva già."""
    if not (name and role and source_url):
        raise ValueError("una persona ha bisogno di nome, ruolo e source_url")
    if email and email_confidence not in ("published", "pattern"):
        raise ValueError("email_confidence deve essere 'published' o 'pattern'")
    existing = db.one("SELECT id FROM people WHERE contact_id=? AND name=?", contact_id, name)
    if existing:
        return None
    return db.batch([("""INSERT INTO people(contact_id, name, role, email, email_confidence, phone, source_url, last_verified_at)
                         VALUES(?,?,?,?,?,?,?, datetime('now')) RETURNING id""",
                      [contact_id, name, role, email, email_confidence if email else None, normalize_phone(phone, country), source_url])])[0][0]["id"]


# ---------------------------------------------------------------- do not contact

def mark_do_not_contact(contact_ids, reason):
    """Segna le case 'da non contattare' e annulla le loro bozze aperte (draft/approved) e le richieste in coda.
    Ritorna il numero di bozze annullate."""
    cancelled = 0
    for cid in contact_ids:
        db.run("UPDATE contacts SET do_not_contact=1, do_not_contact_reason=? WHERE id=?", reason, cid)
        open_drafts = db.q("SELECT id, project FROM radar_outbox WHERE contact_id=? AND status IN ('draft','approved')", cid)
        for o in open_drafts:
            db.run("UPDATE radar_outbox SET status='cancelled', error=? WHERE id=?", "do_not_contact: " + reason, o["id"])
            cancelled += 1
        db.run("UPDATE radar_requests SET status='cancelled', note=?, done_at=datetime('now') WHERE contact_id=? AND status='pending'",
               "do_not_contact: " + reason, cid)
        db.run("INSERT INTO radar_events(contact_id, project, kind, detail) VALUES(?, NULL, 'do_not_contact', ?)", cid, reason)
    return cancelled


def find_by_domain_or_name(domain, name):
    """Tutte le righe che sono la stessa casa: dominio del sito ufficiale o nome normalizzato uguale."""
    key = normalize_name(name)
    return [r["id"] for r in db.q("SELECT id, name, official_url FROM contacts")
            if site_domain(r["official_url"]) == domain or normalize_name(r["name"]) == key]
