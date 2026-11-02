"""Censimento da DGCA: «Database aiuti alle opere» (cinema.cultura.gov.it), un CSV per anno.

Fonte pubblica e strutturata: casa richiedente, produttori, distributori, titolo, tipologia, nazionalità, importo.
Nessuna chiamata al modello: solo parser deterministico. Una richiesta HTTP per anno (robots.txt rispettato).
I dati sono autodichiarati dai richiedenti (lo dice la DGCA nel file): sono prove di attività, non un giudizio.
"""
import csv
import io
import os
import re

from . import census, db, web

BASE = "https://cinema.cultura.gov.it/tempcsv/dbaiuti/DGCA-DB-aiuti-{year}.csv"
SOURCE_NAME = "DGCA · database aiuti alle opere"

COUNTRIES = {
    "italia": "IT", "francia": "FR", "belgio": "BE", "svizzera": "CH", "spagna": "ES", "germania": "DE", "slovenia": "SI",
    "stati uniti": "US", "polonia": "PL", "regno unito": "GB", "croazia": "HR", "serbia": "RS", "brasile": "BR", "romania": "RO",
    "messico": "MX", "argentina": "AR", "canada": "CA", "irlanda": "IE", "albania": "AL", "grecia": "GR", "slovacchia": "SK",
    "austria": "AT", "portogallo": "PT", "cile": "CL", "lettonia": "LV", "paesi bassi": "NL", "india": "IN", "bulgaria": "BG",
    "marocco": "MA", "danimarca": "DK", "rep. ceca": "CZ", "repubblica ceca": "CZ", "israele": "IL", "finlandia": "FI",
    "tunisia": "TN", "giappone": "JP", "russia": "RU", "singapore": "SG", "lussemburgo": "LU", "ucraina": "UA", "svezia": "SE",
    "norvegia": "NO", "ungheria": "HU", "estonia": "EE", "lituania": "LT", "islanda": "IS", "turchia": "TR", "malta": "MT",
    "cipro": "CY", "bosnia ed erzegovina": "BA", "macedonia del nord": "MK", "montenegro": "ME", "kosovo": "XK", "georgia": "GE",
    "armenia": "AM", "cina": "CN", "australia": "AU", "egitto": "EG", "libano": "LB", "palestina": "PS", "colombia": "CO",
}

# (parola nel nome dell'aiuto, ruolo del richiedente, kind della casa)
AID_ROLES = [
    ("distribuzione internazionale", "sales", "sales_agent"),
    ("home entertainment", "distributor", "theatrical_distribution"),
    ("distribuzione", "distributor", "theatrical_distribution"),
    ("coproduzioni minoritarie", "coproducer", "production"),
]


def fix_text(s):
    """Il file DGCA mescola byte cp1252 e campi già in UTF-8 («qualitÃ\xa0»). Il testo arriva letto come latin-1
    (nessun byte perso): se i byte di un campo sono UTF-8 valido si leggono così, altrimenti come cp1252."""
    s = s or ""
    if any(ord(c) > 127 for c in s):
        raw = s.encode("latin-1", errors="replace")
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            s = raw.decode("cp1252", errors="replace")
    return re.sub(r"\s+", " ", s).strip()


def parse_csv(raw, year=None):
    """Bytes del CSV -> lista di dict con le colonne normalizzate. Salta intestazione e note."""
    text = raw.decode("latin-1") if isinstance(raw, bytes) else raw
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.upper().startswith("AIUTO;")), None)
    if start is None:
        raise ValueError("intestazione AIUTO; non trovata: formato cambiato")
    out = []
    for r in csv.DictReader(io.StringIO("\n".join(lines[start:])), delimiter=";"):
        r = {fix_text(k): fix_text(v) for k, v in r.items() if k}
        if not r.get("TITOLO") or not r.get("SOGGETTO RICHIEDENTE"):
            continue
        out.append(r)
    return out


def split_names(field):
    """Elenco di società separate da virgola; un pezzo che è solo una forma societaria resta attaccato al precedente."""
    parts = []
    for p in (field or "").split(","):
        p = p.strip()
        if not p:
            continue
        if parts and (len(p) < 3 or re.sub(r"[.\s]", "", p).lower() in census.LEGAL_FORMS):
            parts[-1] += ", " + p
        else:
            parts.append(p)
    return parts


def parse_amount(s):
    """'189.553,08' -> 189553 (euro interi). None se non è un numero."""
    s = (s or "").replace(".", "").replace(",", ".")
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_minutes(s):
    m = re.fullmatch(r"\s*(\d+):(\d{1,2})\s*", s or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def countries_of(field):
    out = []
    for n in re.split(r"\s*,\s*", field or ""):
        if n:
            out.append(COUNTRIES.get(n.lower(), n))
    return ", ".join(out) or None


def credit_format(row):
    settore, tip = row.get("SETTORE", "").lower(), row.get("TIPOLOGIA", "").lower()
    mins = parse_minutes(row.get("DURATA (H:M)"))
    if "videogioc" in tip or "videogioc" in settore:
        return None  # non è cinema
    if "cortometragg" in settore or (mins is not None and 0 < mins < 60 and tip != "documentario"):
        return "short"
    if "seriali" in settore:
        return "series"
    if tip == "documentario" or "documentar" in settore:
        return "documentary"
    if tip == "animazione" or "animazione" in settore:
        return "animation"
    return "feature"


def aid_role(row):
    name = row.get("AIUTO", "").lower()
    for word, role, kind in AID_ROLES:
        if word in name:
            return role, kind
    return "producer", "production"


def fund_label(row):
    """Etichetta del fondo, senza virgole (il campo funds si separa su virgola)."""
    aiuto = row.get("AIUTO", "").replace(",", " ")
    settore = row.get("SETTORE", "")
    extra = ""
    low = settore.lower()
    if "opere prime o seconde" in low:
        extra = " opere prime o seconde"
    elif "giovani autori" in low:
        extra = " giovani autori"
    amount = parse_amount(row.get("IMPORTO DEL CONTRIBUTO ASSEGNATO"))
    return f"MiC {aiuto}{extra} {row.get('ANNO', '')}" + (f" (EUR {amount})" if amount else "")


def plan(rows, source_url):
    """Righe CSV -> case e crediti da scrivere. Funzione pura.
    Ritorna {'companies': {chiave: dict}, 'credits': [dict]}. La chiave è il nome normalizzato.
    Il richiedente è una società italiana che ha ricevuto il contributo; gli altri produttori elencati
    non hanno paese noto (possono essere esteri) e non ricevono i fondi."""
    companies, credits = {}, []

    def company(name, kind, italian):
        key = census.normalize_name(name)
        if len(key) < 3:
            return None
        c = companies.setdefault(key, {"name": name, "kind": kind, "country": None, "applicant": False, "sources": set()})
        c["sources"].add(source_url)
        if italian:
            c["country"], c["applicant"] = "IT", True
        if c["kind"] in (None, "other"):
            c["kind"] = kind
        return key

    for r in rows:
        fmt = credit_format(r)
        if fmt is None:
            continue
        role, kind = aid_role(r)
        year = int(r["ANNO"]) if r.get("ANNO", "").isdigit() else 0
        common = {"title": r["TITOLO"], "year": year, "format": fmt, "countries": countries_of(r.get("NAZIONALITA")),
                  "source_url": source_url, "director": r.get("REGISTA") or None}
        applicant = company(r["SOGGETTO RICHIEDENTE"], kind, True)
        if applicant:  # un nome troppo corto per essere una chiave (es. «X») non produce crediti
            credits.append({**common, "company": applicant, "role": role, "funds": fund_label(r)})
        for name in split_names(r.get("PRODUTTORE")):
            key = company(name, "production", False)
            if key and key != applicant:
                credits.append({**common, "company": key, "role": "producer", "funds": None})
        if role in ("distributor", "sales"):
            for name in split_names(r.get("DISTRIBUTORE")):
                key = company(name, kind, False)
                if key and key != applicant:
                    credits.append({**common, "company": key, "role": role, "funds": None})
    return {"companies": companies, "credits": credits}


def merge_plans(plans):
    """Unisce i piani di più anni: stesse società (chiave), fonti sommate."""
    companies, credits = {}, []
    for p in plans:
        for key, c in p["companies"].items():
            m = companies.setdefault(key, {**c, "sources": set()})
            m["sources"] |= c["sources"]
            if c["applicant"]:
                m["country"], m["applicant"] = "IT", True
        credits += p["credits"]
    return {"companies": companies, "credits": credits}


def load(p, source_name=SOURCE_NAME, dry_run=False):
    """Scrive il piano nel database con scritture raggruppate. Ritorna le statistiche.
    Non crea righe in contact_projects e non riscrive campi già pieni."""
    idx = census.CompanyIndex()
    stats = {"companies_new": 0, "companies_matched": 0, "credits_new": 0, "credits_merged": 0, "matched_samples": []}
    ids, new_keys = {}, []
    for key, c in p["companies"].items():
        cid = idx.find(c["name"], None, c["country"])
        if cid:
            ids[key] = cid
            stats["companies_matched"] += 1
            if len(stats["matched_samples"]) < 400:
                stats["matched_samples"].append((c["name"], cid))
        else:
            new_keys.append(key)
    stats["companies_new"] = len(new_keys)
    if dry_run:
        return stats
    for chunk in _chunks(new_keys, 100):
        res = db.batch([("""INSERT INTO contacts(name, type, country, kind, status, source_system, verification_level)
                            VALUES(?,?,?,?, 'census', 'census:dgca', 'to_verify') RETURNING id""",
                         [p["companies"][k]["name"], p["companies"][k]["kind"] or "other", p["companies"][k]["country"], p["companies"][k]["kind"]])
                        for k in chunk])
        for k, rows in zip(chunk, res):
            ids[k] = rows[0]["id"]
    updates = []
    for key, c in p["companies"].items():
        cid = ids[key]
        if key not in new_keys:
            updates.append(("UPDATE contacts SET kind = coalesce(kind, ?), country = coalesce(nullif(country, ''), ?) WHERE id = ?", [c["kind"], c["country"], cid]))
        for url in sorted(c["sources"]):
            updates.append(("INSERT OR IGNORE INTO contact_sources(contact_id, url, source_name) VALUES(?,?,?)", [cid, url, source_name]))
    for chunk in _chunks(updates, 200):
        db.batch(chunk)

    # crediti: uniti per (casa, titolo normalizzato, ruolo, anno) e confrontati con quelli già nel database
    merged = {}
    for cr in p["credits"]:
        k = (ids[cr["company"]], census.normalize_title(cr["title"]), cr["role"], cr["year"])
        m = merged.setdefault(k, {**cr, "contact_id": ids[cr["company"]]})
        m["funds"] = census._union(m.get("funds"), cr.get("funds"))
        m["countries"] = census._union(m.get("countries"), cr.get("countries"))
    existing = {}
    contact_ids = sorted({k[0] for k in merged})
    for chunk in _chunks(contact_ids, 400):
        for r in db.q(f"SELECT * FROM credits WHERE contact_id IN ({','.join('?' * len(chunk))})", *chunk):
            existing.setdefault((r["contact_id"], census.normalize_title(r["title"]), r["role"]), []).append(r)
    writes = []
    for (cid, tkey, role, year), cr in merged.items():
        same = [r for r in existing.get((cid, tkey, role), []) if r["year"] == year or 0 in (r["year"], year)]
        if not same:
            writes.append(("""INSERT OR IGNORE INTO credits(contact_id, title, year, format, role, countries, funds, source_url)
                              VALUES(?,?,?,?,?,?,?,?)""", [cid, cr["title"], year, cr["format"], role, cr["countries"], cr["funds"], cr["source_url"]]))
            stats["credits_new"] += 1
        else:
            r = same[0]
            funds, countries = census._union(r["funds"], cr["funds"]), census._union(r["countries"], cr["countries"])
            if (funds, countries) != (r["funds"], r["countries"]):
                writes.append(("UPDATE credits SET funds=?, countries=? WHERE id=?", [funds, countries, r["id"]]))
                stats["credits_merged"] += 1
    for chunk in _chunks(writes, 200):
        db.batch(chunk)

    # profilo di ogni casa toccata, calcolato dai crediti ora nel database
    from datetime import date
    info = {r["id"]: r for chunk in _chunks(contact_ids, 400)
            for r in db.q(f"SELECT id, country FROM contacts WHERE id IN ({','.join('?' * len(chunk))})", *chunk)}
    by_contact = {}
    for chunk in _chunks(contact_ids, 400):
        for r in db.q(f"SELECT * FROM credits WHERE contact_id IN ({','.join('?' * len(chunk))})", *chunk):
            by_contact.setdefault(r["contact_id"], []).append(r)
    import json
    profiles = [("UPDATE contacts SET profile_json=? WHERE id=?",
                 [json.dumps(census.build_profile(info[cid], by_contact.get(cid, []), date.today().year), ensure_ascii=False), cid]) for cid in contact_ids]
    for chunk in _chunks(profiles, 200):
        db.batch(chunk)
    stats["profiles"] = len(profiles)
    return stats


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def run(years="", dir=""):
    """Lavoro `census:dgca`: scarica i CSV degli ultimi 3 anni (o li legge da `dir`), carica case e crediti.
    Ritorna (trovati, errori, nota) come gli altri lavori."""
    from datetime import date
    wanted = [int(y) for y in str(years).replace(" ", "").split(",") if y] or [date.today().year - 2 + i for i in range(3)]
    src_url = "https://cinema.cultura.gov.it/database-aiuti-opere/"
    db.run("INSERT OR IGNORE INTO radar_sources(name, url, kind, project, company_type, country) VALUES(?,?,?,?,?,?)",
           SOURCE_NAME, src_url, "census", "census", "production", "IT")
    plans, errors, notes = [], 0, []
    for y in wanted:
        url = BASE.format(year=y)
        if dir:
            with open(os.path.join(dir, f"DGCA-DB-aiuti-{y}.csv"), "rb") as f:
                raw, err = f.read(), None
        else:
            raw, err = web.fetch_bytes(url)
        if err:
            errors += 1
            notes.append(f"{y}: {err}")
            continue
        rows = parse_csv(raw)
        plans.append(plan(rows, url))
        notes.append(f"{y}: {len(rows)} righe")
    if not plans:
        return 0, errors, "nessun CSV letto: " + "; ".join(notes)
    st = load(merge_plans(plans))
    db.run("UPDATE radar_sources SET last_run=datetime('now'), found_total=found_total+?, emails_total=0 WHERE name=?", st["companies_new"], SOURCE_NAME)
    return st["companies_new"], errors, (f"{st['companies_new']} case nuove, {st['companies_matched']} già note, {st['credits_new']} crediti nuovi, "
                                         f"{st['credits_merged']} aggiornati; " + "; ".join(notes))[:300]
