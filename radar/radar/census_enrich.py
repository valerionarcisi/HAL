"""Fase 3 del censimento: dalle case ai loro recapiti pubblicati e alle persone.

Fonti: la scheda Filmitalia della casa (pubblicata da Cinecittà: responsabili, telefono, email, sito) e il sito
ufficiale (pagine chi siamo / team / contatti). Parser deterministici per prima cosa; il modello (tetto
census_llm_daily_calls) solo su pagine team dove i parser non trovano nessuno, e ogni nome che propone deve
comparire nel testo della pagina con la parola del ruolo vicina.

Regole: una persona entra solo con nome, ruolo, casa e source_url. L'email è 'published' solo se letta su una
pagina ufficiale accanto al nome (o è l'indirizzo pubblicato con quel nome); 'pattern' se ricavata dallo schema
degli indirizzi pubblicati dalla casa, e solo se il dominio ha record MX. Niente verifiche SMTP, niente mail di prova.
Telefoni: solo quelli pubblicati dalla casa o dalla persona su pagine ufficiali.
"""
import html as htmllib
import json
import re
import urllib.parse

from . import census, db, llm, web

FREE_MAIL = ("gmail.com", "googlemail.com", "hotmail.com", "hotmail.it", "outlook.com", "outlook.it", "libero.it", "yahoo.com", "yahoo.it",
             "icloud.com", "live.com", "virgilio.it", "tiscali.it", "alice.it", "pec.it", "legalmail.it", "protonmail.com", "me.com")

# ---------------------------------------------------------------- testo e righe

def html_lines(h):
    """HTML -> righe di testo. I tag a blocco diventano a capo, così «Nome» e «Ruolo» in due elementi restano su due righe."""
    h = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|li|ul|tr|td|th|h[1-6]|section|article|header|footer|address|dd|dt|span)>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    lines = [re.sub(r"\s+", " ", l).strip() for l in htmllib.unescape(h).split("\n")]
    return [l for l in lines if l]


def decode_cfemail(hex_str):
    """Email offuscate da Cloudflare (data-cfemail): sono pubblicate sulla pagina, il browser le mostra con questo XOR."""
    try:
        b = bytes.fromhex(hex_str)
        return "".join(chr(c ^ b[0]) for c in b[1:])
    except (ValueError, IndexError):
        return None


def page_emails(h):
    found = {}
    for hx in re.findall(r'data-cfemail="([0-9a-fA-F]+)"', h):
        e = decode_cfemail(hx)
        if e and "@" in e:
            found[e.lower()] = True
    for e in web.emails(re.sub(r'<a [^>]*data-cfemail[^>]*>.*?</a>', " ", h, flags=re.S)):
        found[e] = True
    return list(found)


def page_phones(h):
    """Numeri pubblicati: link tel: e numeri con etichetta (tel, telefono, phone, T). Normalizzati E.164."""
    out = []
    for raw in re.findall(r'href=["\']tel:([^"\']+)["\']', h, re.I):
        p = census.normalize_phone(urllib.parse.unquote(raw))
        if p:
            out.append(p)
    for line in html_lines(h):
        m = re.search(r"(?i)\b(?:tel(?:efono)?\.?|phone|t\.?|mob(?:ile)?\.?|cell\.?)\s*[:.]?\s*(\+?[\d][\d ./()-]{6,20}\d)", line)
        if m:
            p = census.normalize_phone(m.group(1))
            if p:
                out.append(p)
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- Filmitalia

def parse_filmitalia(h):
    """Scheda produzione/distribuzione di Filmitalia -> {managers, phone, email, website, address}. Tutto come pubblicato."""
    lines = html_lines(h)
    flat = re.sub(r":\s*\|\s*", ": ", " | ".join(lines))  # «etichetta:» e valore stanno in due celle: si riuniscono
    out = {"managers": [], "phone": None, "email": None, "website": None, "address": None}
    m = re.search(r"responsabil[ei]:\s*([^|]+)", flat, re.I)
    if m:
        out["managers"] = [n.strip() for n in re.split(r",| e ", m.group(1)) if census_name_ok(n.strip())]
    m = re.search(r"indirizzo:\s*([^|]+)", flat, re.I)
    out["address"] = m.group(1).strip() if m else None
    m = re.search(r"tel:\s*([+\d][\d ./()-]{6,24}\d)", flat, re.I)
    out["phone"] = census.normalize_phone(m.group(1)) if m else None
    mails = page_emails(h)
    out["email"] = next((e for e in mails if not e.endswith(("filmitalia.org", "cinecitta.com", "cinecittà.com"))), None)
    m = re.search(r"sito web:\s*(\S+)", flat, re.I)
    if m:
        site = m.group(1).strip().rstrip("/|")
        out["website"] = site if site.startswith("http") else "http://" + site
        if census.site_domain(out["website"]) is None:
            out["website"] = None
    return out


# ---------------------------------------------------------------- persone

ROLE_WORDS = [
    "produttore delegato", "produttore esecutivo", "produttrice esecutiva", "executive producer", "line producer", "produttore", "produttrice",
    "producer", "head of development", "development", "sviluppo", "acquisizioni", "acquisitions", "head of acquisitions",
    "festival manager", "festival distribution", "distribuzione festival", "festival", "head of distribution", "distribuzione", "distribution",
    "sales", "vendite", "direttore generale", "direttore", "managing director", "ceo", "founder", "co-founder", "fondatore", "fondatrice",
    "artistic director", "direttore artistico", "amministratore", "presidente", "titolare", "responsabile", "head of", "partner",
    "manager", "coordinat", "assistant", "assistente",
]
ROLE_RE = re.compile("|".join(re.escape(w) for w in sorted(ROLE_WORDS, key=len, reverse=True)), re.I)
PARTICLES = {"de", "di", "del", "della", "dei", "degli", "da", "dal", "dalla", "van", "von", "la", "le", "lo", "d'", "dell'", "del'", "el", "al"}
NAME_TOKEN = re.compile(r"^(?:(?:[A-ZÀ-ÖØ-Þ]['’])?[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+(?:['’\-][A-ZÀ-ÖØ-Þ]?[a-zà-öø-ÿ]+)*|[A-Z]\.)$")
STOP_NAME_WORDS = {
    "film", "films", "production", "productions", "produzioni", "produzione", "cinema", "distribution", "distribuzione", "contatti",
    "contact", "contacts", "chi", "siamo", "team", "staff", "home", "about", "via", "viale", "piazza", "roma", "milano", "torino", "italia",
    "italy", "srl", "spa", "info", "email", "telefono", "tel", "news", "progetti", "projects", "catalogo", "catalog", "festival", "sales",
    "privacy", "cookie", "policy", "copyright", "all", "rights", "reserved", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre", "lunedì", "venerdì", "the", "our", "la", "il", "lo", "un", "una",
    "studio", "studios", "media", "group", "pictures", "entertainment", "company", "founder", "producer", "director", "manager", "head",
    "mappa", "sito", "cart", "your", "carrello", "cerca", "search", "menu", "login", "account", "shop", "newsletter", "termini", "condizioni",
    "sitemap", "map", "my", "view", "read", "more", "leggi", "tutto", "scopri", "vedi", "area", "riservata", "iscriviti", "subscribe",
    "follow", "seguici", "share", "condividi", "download", "scarica", "trailer", "synopsis", "sinossi", "cast", "credits", "crediti",
}


def census_name_ok(s):
    """Sembra un nome di persona: 2-4 parole con la maiuscola (particelle ammesse), nessuna parola da azienda/indirizzo."""
    s = re.sub(r"\s+", " ", s.strip(" .,;:-–—|"))
    if not s:
        return False
    if s.isupper() and len(s) > 5:
        s = s.title()
    words = s.split(" ")
    if not 2 <= len(words) <= 4:
        return False
    cap = 0
    for w in words:
        if w.lower() in STOP_NAME_WORDS or any(c.isdigit() for c in w):
            return False
        if w.lower() in PARTICLES:
            continue
        if not NAME_TOKEN.match(w):
            return False
        cap += 1
    return cap >= 2


def _clean_name(s):
    s = re.sub(r"\s+", " ", s.strip(" .,;:-–—|•*"))
    return s.title() if s.isupper() else s


def _split_pair(line):
    for sep in (" – ", " — ", " - ", " | ", ", ", " / ", ": "):
        if sep in line:
            a, b = line.split(sep, 1)
            yield a.strip(), b.strip()
    m = re.match(r"^(.+?)\s*\((.+)\)$", line)
    if m:
        yield m.group(1).strip(), m.group(2).strip()


def extract_people(lines, house_name=""):
    """Righe di testo -> [{name, role, line}]. Tre forme: «Nome – Ruolo», «Ruolo: Nome», «Nome» su una riga e «Ruolo» sulla successiva."""
    house_words = set(census.normalize_name(house_name).split())
    found, seen = [], set()

    def add(name, role, line):
        name = _clean_name(name)
        key = name.lower()
        if key in seen or not census_name_ok(name) or house_words & {w.lower() for w in name.split()}:
            return
        role = re.sub(r"\s+", " ", role).strip(" .,;:-–—|")[:70]
        # un ruolo è una qualifica breve nella casa: niente frasi, niente titoli tra virgolette
        if not ROLE_RE.search(role) or len(role) < 3 or len(role.split()) > 6 or re.search(r"[\"“”«»]", role):
            return
        seen.add(key)
        found.append({"name": name, "role": role, "line": line})

    for i, line in enumerate(lines):
        if len(line) > 140:
            continue
        for a, b in _split_pair(line):
            if census_name_ok(a) and ROLE_RE.search(b) and not census_name_ok(b):
                add(a, b, line)
            elif census_name_ok(b) and ROLE_RE.search(a) and not census_name_ok(a):
                add(b, a, line)
        if census_name_ok(line) and i + 1 < len(lines) and len(lines[i + 1]) <= 70 and ROLE_RE.search(lines[i + 1]) and not census_name_ok(lines[i + 1]):
            add(line, lines[i + 1], line)
        elif ROLE_RE.fullmatch(line.strip(" :").lower()) is None and ROLE_RE.search(line) and len(line) <= 50 and i + 1 < len(lines) and census_name_ok(lines[i + 1]) and not census_name_ok(line):
            add(lines[i + 1], line, line)
    return found


def jsonld_people(h):
    """Persone dichiarate in JSON-LD (Person, o Organization.employee/founder) con jobTitle."""
    out = []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            t = o.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Person" in types and o.get("name") and o.get("jobTitle"):
                out.append({"name": _clean_name(str(o["name"])), "role": str(o["jobTitle"])[:70], "email": o.get("email"), "phone": o.get("telephone")})
            for v in o.values():
                walk(v)

    for blob in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', h, re.S | re.I):
        try:
            walk(json.loads(blob))
        except ValueError:
            continue
    return [p for p in out if census_name_ok(p["name"])]


def jsonld_org(h):
    """Telefono, email e indirizzo dell'organizzazione da JSON-LD."""
    info = {}

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            t = o.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(x in ("Organization", "LocalBusiness", "MovieProductionCompany", "Corporation") for x in types):
                info.setdefault("phone", o.get("telephone"))
                info.setdefault("email", o.get("email"))
                info.setdefault("vat", o.get("vatID"))
            for v in o.values():
                walk(v)

    for blob in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', h, re.S | re.I):
        try:
            walk(json.loads(blob))
        except ValueError:
            continue
    return info


GENERIC_LOCAL = re.compile(r"^(info|infos|contact|contacts|contatti|contatto|hello|hi|mail|email|office|segreteria|films?|film|distribution|distribuzione|submissions?|production|produzione|"
                           r"press|stampa|sales|festivals?|acquisitions?|admin|amministrazione|booking|general|studio|team|shorts?|cortometraggi|post|posta|support)$", re.I)


def person_email_near(name, lines, emails_in_page):
    """Un'email pubblicata sulla stessa riga del nome, o nelle 2 righe vicine, o il cui indirizzo contiene cognome/nome."""
    parts = [census._ascii(p).lower() for p in re.split(r"[\s'’\-]+", name) if len(p) > 1]
    for i, l in enumerate(lines):
        if name.lower() in l.lower():
            for ll in lines[max(0, i - 1): i + 3]:
                for e in web.emails(ll):
                    if e in emails_in_page and not GENERIC_LOCAL.match(e.split("@")[0]):  # info@, films@… sono della casa, non della persona
                        return e
    for e in emails_in_page:
        local = census._ascii(e.split("@")[0]).lower()
        if len(parts) >= 2 and parts[-1] in local and (parts[0] in local or parts[0][0] in local):
            return e
    return None


def person_phone_near(name, lines):
    for i, l in enumerate(lines):
        if name.lower() in l.lower():
            for ll in lines[i: i + 3]:
                m = re.search(r"(?i)(?:tel(?:efono)?|phone|mob(?:ile)?|cell)\.?\s*[:.]?\s*(\+?[\d][\d ./()-]{6,20}\d)", ll)
                if m:
                    return census.normalize_phone(m.group(1))
    return None


def email_pattern(published):
    """Schema dei soli indirizzi nominativi pubblicati dalla casa: ritorna una funzione (nome, cognome, dominio) -> email, o None.
    Serve almeno un indirizzo pubblicato che segua lo schema, con il nome della persona a cui appartiene."""
    for name, e in published:
        local, dom = e.split("@")
        if dom in FREE_MAIL:
            continue
        parts = [census._ascii(p).lower() for p in re.split(r"[\s'’\-]+", name) if len(p) > 1]
        if len(parts) < 2:
            continue
        first, last = parts[0], parts[-1]
        for tpl in ("{f}.{l}", "{f}{l}", "{f0}.{l}", "{f0}{l}", "{l}.{f}", "{l}"):
            if local.lower() == tpl.format(f=first, l=last, f0=first[0]):
                return tpl, dom
    return None


def apply_pattern(pattern, name):
    tpl, dom = pattern
    parts = [census._ascii(p).lower() for p in re.split(r"[\s'’\-]+", name) if len(p) > 1]
    if len(parts) < 2:
        return None
    return f"{tpl.format(f=parts[0], l=parts[-1], f0=parts[0][0])}@{dom}"


# ---------------------------------------------------------------- invio e tipo

SUBMISSION_RE = re.compile(r"(submission|submit|invia[- ]?(il[- ]?)?(tuo|un|progetto|proposta|cortometraggio)|proponi|proposte|send[- ]us|pitch|candida|call[- ]for|invio)", re.I)
FORM_HOSTS = ("typeform.com", "jotform.com", "forms.gle", "docs.google.com/forms", "airtable.com", "filmfreeway.com", "tally.so", "formstack.com")


def find_submission_url(base, h):
    for url, label in web.links(base, h):
        if SUBMISSION_RE.search(label) or (SUBMISSION_RE.search(url) and web.domain(url) == web.domain(base)) or any(f in url for f in FORM_HOSTS):
            if url.startswith("http") and not url.lower().endswith((".jpg", ".png", ".pdf")):
                return url
    return None


PEOPLE_LINK = re.compile(r"(chi-siamo|about|team|staff|people|company|crew|founders?|squadra|persone|contatt|contact|azienda|studio|la-societa|societa|who-we-are)", re.I)


def crawl_for_people(url, max_pages=8):
    """Home + pagine chi siamo/team/contatti dello stesso dominio. Rispetta robots.txt e 1 richiesta ogni 2 s per dominio."""
    final, h, err = web.fetch(url)
    if err:
        return {"final": final, "error": err, "pages": {}}
    pages = {final: h}
    same = [l for l, _ in web.links(final, h) if web.domain(l) == web.domain(final)]
    cand = sorted({l.split("#")[0] for l in same if PEOPLE_LINK.search(urllib.parse.urlparse(l).path)}, key=len)
    for l in cand[: max_pages - 1]:
        f, hh, e = web.fetch(l)
        if not e:
            pages[f] = hh
    return {"final": final, "error": None, "pages": pages}


# ---------------------------------------------------------------- arricchimento di una casa

LLM_SYSTEM = (
    "Estrai da questa pagina di una casa di produzione o distribuzione cinematografica le persone che lavorano lì, "
    'SOLO se nome e ruolo compaiono nel testo. Rispondi con un oggetto JSON {"people": [{"name": "Nome Cognome", "role": "ruolo come scritto"}]}. '
    "Se non ci sono persone con un ruolo, rispondi {\"people\": []}. Non inventare e non usare conoscenze esterne."
)


def llm_people(text, ask=None):
    ask = ask or (lambda s, u: llm.ask_json(s, u, max_tokens=800, budget="census"))
    rows = ask(LLM_SYSTEM, text[:9000]).get("people") or []
    lt = text.lower()
    out = []
    for r in rows:
        name, role = _clean_name(str(r.get("name") or "")), str(r.get("role") or "").strip()[:70]
        i = lt.find(name.lower())
        if name and role and census_name_ok(name) and i >= 0 and role.lower()[:12] in lt[max(0, i - 200): i + 250]:
            out.append({"name": name, "role": role, "line": ""})
    return out


def enrich_site(contact, crawl=None, ask=None, use_llm=True):
    """Visita il sito della casa e ritorna cosa è stato trovato (senza scrivere). Funzione separata da apply per poter essere provata."""
    crawl = crawl or crawl_for_people(contact["site"])
    res = {"error": crawl["error"], "people": [], "phone": None, "email": None, "submission_url": None, "pages": list(crawl["pages"]), "llm_calls": 0}
    if crawl["error"]:
        return res
    pages = crawl["pages"]
    emails_all, phones_all = {}, []
    for url, h in pages.items():
        for e in page_emails(h):
            emails_all.setdefault(e, url)
        phones_all += page_phones(h)
        res["submission_url"] = res["submission_url"] or find_submission_url(url, h)
        org = jsonld_org(h)
        if org.get("phone") and census.normalize_phone(str(org["phone"])):
            phones_all.append(census.normalize_phone(str(org["phone"])))
    dom = web.domain(crawl["final"])
    own = [e for e in emails_all if dom.split(".")[0] in e.split("@")[1]] or list(emails_all)
    generic = [e for e in own if re.match(r"^(info|contact|contatti|hello|mail|office|segreteria|distribution|submissions?|production|produzione)@", e)]
    res["email"] = (generic or own or [None])[0]
    res["phone"] = phones_all[0] if phones_all else None
    people = {}
    for url, h in pages.items():
        lines = html_lines(h)
        page_mails = set(page_emails(h))
        people_page = bool(PEOPLE_LINK.search(urllib.parse.urlparse(url).path))
        cands = jsonld_people(h) + (extract_people(lines, contact["name"]) if people_page else [])
        if not cands and use_llm and people_page and ROLE_RE.search(" ".join(lines)):
            try:
                cands = llm_people("\n".join(lines), ask)
                res["llm_calls"] += 1
            except llm.BudgetExceeded:
                res["budget_exhausted"] = True
            except Exception as e:  # il modello non risponde: il parser resta quello che è
                res["error_llm"] = str(e)[:80]
        for c in cands:
            if c["name"].lower() in people:
                continue
            email = c.get("email") or person_email_near(c["name"], lines, page_mails)
            phone = census.normalize_phone(str(c["phone"])) if c.get("phone") else person_phone_near(c["name"], lines)
            people[c["name"].lower()] = {"name": c["name"], "role": c["role"], "source_url": url, "email": email.lower() if email else None,
                                         "confidence": "published" if email else None, "phone": phone}
    published = [(p["name"], p["email"]) for p in people.values() if p["email"]]
    pattern = email_pattern(published)
    for p in people.values():
        if not p["email"] and pattern:
            guess = apply_pattern(pattern, p["name"])
            if guess and web.has_mx(guess.split("@")[1]):
                p["email"], p["confidence"] = guess, "pattern"
    res["people"] = list(people.values())
    return res


def apply_result(contact, res, source_url=None):
    """Scrive il risultato: riempie solo i campi vuoti della casa, aggiunge le persone nuove, segna last_verified_at."""
    cid = contact["id"]
    if res["error"]:
        db.run("INSERT INTO radar_events(contact_id, kind, detail) VALUES(?, 'site_unreachable', ?)", cid, (res["error"] or "")[:200])
    else:
        proof = f"pubblicata su {res['pages'][0]}" if res["pages"] else None
        db.run("""UPDATE contacts SET phone = coalesce(nullif(phone, ''), ?), submission_url = coalesce(nullif(submission_url, ''), ?),
                         email = coalesce(nullif(email, ''), ?),
                         contact_proof = CASE WHEN coalesce(email, '') = '' AND ? IS NOT NULL THEN ? ELSE contact_proof END,
                         verification_level = CASE WHEN coalesce(email, '') = '' AND ? IS NOT NULL THEN 'verified' ELSE verification_level END
                  WHERE id = ?""", res["phone"], res["submission_url"], res["email"], res["email"], proof, res["email"], cid)
    for p in res["people"]:
        census.add_person(cid, p["name"], p["role"], p["source_url"], email=p["email"], email_confidence=p["confidence"], phone=p["phone"])
    db.run("UPDATE contacts SET last_verified_at = datetime('now') WHERE id = ?", cid)
    return len(res["people"])


def apply_filmitalia(contact, page_url, f):
    """Scheda Filmitalia: il sito ufficiale entra come fonte ('Sito ufficiale') e, solo se la casa non ha url, come official_url."""
    cid = contact["id"]
    db.run("""UPDATE contacts SET phone = coalesce(nullif(phone, ''), ?), email = coalesce(nullif(email, ''), ?),
                     contact_proof = CASE WHEN coalesce(email, '') = '' AND ? IS NOT NULL THEN ? ELSE contact_proof END,
                     city = coalesce(nullif(city, ''), ?), official_url = coalesce(nullif(official_url, ''), ?)
              WHERE id = ?""", f["phone"], f["email"], f["email"], f"pubblicata su {page_url}", None, f["website"], cid)
    if f["website"]:
        census.add_source(cid, f["website"], "Sito ufficiale")
    for n in f["managers"]:
        census.add_person(cid, _clean_name(n), "responsabile", page_url)
    return len(f["managers"])


# ---------------------------------------------------------------- lavoro

def candidates(limit):
    """Case con crediti, mai verificate, con un sito o una scheda Filmitalia. Prima distributori di corti e distribuzione, poi chi ha corti/opere prime/fondi selettivi."""
    rows = db.q("""SELECT c.id, c.name, c.kind, c.official_url, c.profile_json,
                          (SELECT url FROM contact_sources s WHERE s.contact_id = c.id AND s.source_name = 'Sito ufficiale' LIMIT 1) AS site_src
                   FROM contacts c
                   WHERE c.last_verified_at IS NULL AND coalesce(c.do_not_contact, 0) = 0
                     AND (c.profile_json IS NOT NULL OR c.kind = 'short_distribution')
                     AND (c.official_url LIKE 'http%' OR c.official_url LIKE '%.%')""")
    def score(r):
        prof = json.loads(r["profile_json"]) if r["profile_json"] else {}
        s = 100 if r["kind"] == "short_distribution" else 50 if r["kind"] in ("sales_agent", "theatrical_distribution") else 0
        s += 10 * min(3, (prof.get("formats") or {}).get("short", 0)) + 15 * min(2, prof.get("first_or_second_works", 0) + prof.get("first_works", 0))
        s += 10 * any("selettiv" in f.lower() for f in prof.get("funds", [])) + min(10, prof.get("credits", 0))
        return -s
    return sorted(rows, key=score)[:limit]


def run(limit=20):
    """Lavoro `census:enrich`. Per ogni casa: scheda Filmitalia (una richiesta) oppure sito ufficiale (fino a 8 pagine)."""
    found = errors = people = visited = 0
    for c in candidates(int(limit)):
        url = c["official_url"] if c["official_url"].startswith("http") else "http://" + c["official_url"]
        visited += 1
        try:
            if "filmitalia.org" in url:
                final, h, err = web.fetch(url, timeout=40)
                if err:
                    errors += 1
                    db.run("INSERT INTO radar_events(contact_id, kind, detail) VALUES(?, 'site_unreachable', ?)", c["id"], err[:200])
                    db.run("UPDATE contacts SET last_verified_at = datetime('now') WHERE id = ?", c["id"])
                    continue
                f = parse_filmitalia(h)
                people += apply_filmitalia(c, final, f)
                db.run("UPDATE contacts SET last_verified_at = datetime('now') WHERE id = ?", c["id"])
                found += bool(f["website"] or f["managers"] or f["phone"] or f["email"])
                if f["website"]:  # la scheda dà il sito ufficiale: si visita subito per le persone
                    res = enrich_site({**c, "site": f["website"]})
                    errors += bool(res["error"])
                    people += apply_result(c, res)
                    found += bool(res["people"])
            else:
                res = enrich_site({**c, "site": url})
                errors += bool(res["error"])
                people += apply_result(c, res)
                found += bool(res["people"] or res["phone"] or res["email"])
        except Exception as e:
            errors += 1
            db.run("INSERT INTO radar_events(contact_id, kind, detail) VALUES(?, 'enrich_error', ?)", c["id"], f"{type(e).__name__}: {e}"[:200])
    return found, errors, f"{visited} case visitate, {found} con dati, {people} persone, {errors} errori"
