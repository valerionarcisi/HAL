"""Censimento Italia, fonti non-DGCA: distributori di corti (Centro del Corto) e fondo Marche (graduatorie PDF).

Distributori: parser deterministico dell'HTML. Marche: le graduatorie sono tabelle PDF con celle su più righe;
pdftotext dà il testo, il modello estrae le righe (tetto census_llm_daily_calls) e ogni riga viene poi
verificata in modo deterministico contro il testo (ID domanda, ragione sociale, importo): quello che il testo
non conferma si scarta e si conta come errore di estrazione.
"""
import html as htmllib
import re
import subprocess

from . import census, census_dgca, db, llm, web

CDC_URL = "https://www.centrodelcorto.it/it/distributori/"
CDC_SOURCE = "Centro del Corto · distributori"
MARCHE_SOURCE = "Fondazione Marche Cultura · bando produzione audiovisiva (PR FESR)"
MARCHE_PAGES = {
    2025: "https://www.fondazionemarchecultura.it/bandi/sostegno-alla-produzione-audiovisiva-bando-anno-2025/",
    2024: "https://www.fondazionemarchecultura.it/bandi/sostegno-alla-produzione-audiovisiva-bando-anno-2024/",
}


# ---------------------------------------------------------------- Centro del Corto

CARD = re.compile(r'<a href="(?P<url>[^"]+?)(?:#new_tab)?" title="(?P<name>[^"]+)" class="vc_gitem-link vc-zone-link"', re.I)
EXCERPT = re.compile(r'post_excerpt"[^>]*>\s*<p[^>]*>\s*<p>(?P<text>.*?)</p>', re.S)


def parse_centro_del_corto(h):
    """HTML della pagina distributori -> [{name, url, city, note}]. Ogni scheda è un blocco `vc_grid-item`:
    nome (h6), testo (città, a volte «(ex …)») e link al sito stanno nello stesso blocco."""
    out, seen = [], set()
    for block in re.split(r'<div class="vc_grid-item ', h)[1:]:
        link = re.search(r'<a href="([^"]+?)(?:#new_tab)?" title="([^"]+)" class="vc_gitem-link', block)
        h6 = re.search(r"<h6[^>]*>(.*?)</h6>", block, re.S)
        if not (link and h6):
            continue
        name = htmllib.unescape(re.sub(r"<[^>]+>", "", h6.group(1))).strip()
        url = link.group(1).strip()
        ex = EXCERPT.search(block)
        text = htmllib.unescape(re.sub(r"<[^>]+>", "", ex.group("text"))).strip() if ex else ""
        note = re.findall(r"\(([^)]*)\)", text)
        city = re.sub(r"\([^)]*\)", "", text).split(",")[0].strip() or None
        if (name, url) not in seen:
            seen.add((name, url))
            out.append({"name": name, "url": url, "city": city, "note": note[0] if note else None})
    return out


def run_distributors(html=None):
    """Lavoro `census:distributori`. `html` serve ai test; altrimenti scarica la pagina (robots.txt, 1 richiesta)."""
    errors = 0
    if html is None:
        final, html, err = web.fetch(CDC_URL)
        if err:
            return 0, 1, f"Centro del Corto: {err}"
    items = parse_centro_del_corto(html)
    if not items:
        return 0, 1, "Centro del Corto: nessun distributore trovato, la pagina è cambiata?"
    idx, new, known = census.CompanyIndex(), 0, 0
    for it in items:
        site = None if re.search(r"instagram\.com|facebook\.com|linktr\.ee", it["url"]) else it["url"]
        cid, created = census.upsert_company(idx, it["name"], CDC_URL, CDC_SOURCE, url=site, country="IT", city=it["city"], kind="short_distribution")
        new, known = new + created, known + (not created)
    db.run("INSERT OR IGNORE INTO radar_sources(name, url, kind, project, company_type, country) VALUES(?,?,?,?,?,?)",
           CDC_SOURCE, CDC_URL, "census", "census", "sales", "IT")
    db.run("UPDATE radar_sources SET last_run=datetime('now') WHERE name=?", CDC_SOURCE)
    return new, errors, f"{len(items)} distributori nell'elenco: {new} nuovi, {known} già noti"


# ---------------------------------------------------------------- Marche

PROMPT = (
    "Sei un estrattore di dati da una graduatoria PDF del bando regionale (Fondazione Marche Cultura). "
    "Il testo arriva da pdftotext con le celle su più righe mescolate. Restituisci SOLO un oggetto JSON "
    '{"rows": [{"id": "ID DOMANDA a 5 cifre", "company": "ragione sociale completa", "title": "titolo del progetto", '
    '"director": "regia o null", "kind": "lungometraggio|serie tv|film tv|documentario|cortometraggio|format|animazione", '
    '"amount": numero in euro del CONTRIBUTO OTTENUTO o null, "funded": true se la nota dice AMMESSA A FINANZIAMENTO, false altrimenti}]}. '
    "Una riga per ogni ID domanda. Non inventare: se un dato non c'è nel testo, usa null. Gli importi sono nel formato italiano 1.234,56."
)

KIND_FORMAT = {"lungometraggio": "feature", "film tv": "feature", "serie tv": "series", "documentario": "documentary",
               "cortometraggio": "short", "format": "series", "animazione": "animation"}


def pdf_to_text(raw):
    r = subprocess.run(["pdftotext", "-layout", "-", "-"], input=raw, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("pdftotext ha fallito: " + r.stderr.decode("utf-8", "replace")[:120])
    return r.stdout.decode("utf-8", "replace")


def _words(s):
    return re.findall(r"[a-z0-9]+", census._ascii(s or "").lower())


def verify_rows(rows, text):
    """Tiene solo le righe che il testo conferma. Le celle vanno a capo e pdftotext le mescola con le colonne vicine,
    quindi non si cerca la stringa intera: si chiede che ID, importo e ogni parola di ragione sociale e titolo compaiano nel testo."""
    vocabulary, ok, rejected = set(_words(text)), [], []
    for r in rows:
        rid, amount = str(r.get("id") or ""), r.get("amount")
        company, title = _words(r.get("company")), _words(r.get("title"))
        good = bool(re.fullmatch(r"\d{5}", rid) and rid in text and company and title
                    and all(w in vocabulary for w in company + title)
                    and (amount is None or (isinstance(amount, (int, float)) and amount >= 0)))
        if good and amount is not None:
            italian = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")  # 300000 -> 300.000,00
            good = italian in text or f"{int(amount):,}".replace(",", ".") in text
        (ok if good else rejected).append(r)
    return ok, rejected


def extract_rows(text, ask=None):
    ask = ask or (lambda system, user: llm.ask_json(system, user, max_tokens=4000, budget="census"))
    body = ask(PROMPT, text[:30000])
    return body.get("rows") or []


def plan_marche(rows, year, source_url):
    companies, credits = {}, []
    for r in rows:
        if not r.get("funded"):
            continue  # la domanda ammessa ma non finanziata non è una prova di produzione
        key = census.normalize_name(r["company"])
        if len(key) < 3:
            continue
        c = companies.setdefault(key, {"name": r["company"].strip(), "kind": "production", "country": "IT", "applicant": True, "sources": set()})
        c["sources"].add(source_url)
        amount = f" (EUR {int(r['amount'])})" if r.get("amount") else ""
        credits.append({"company": key, "role": "producer", "title": re.sub(r"\s+", " ", r["title"]).strip(), "year": year,
                        "format": KIND_FORMAT.get((r.get("kind") or "").lower(), "feature"), "countries": "IT",
                        "funds": f"Marche Film Fund PR FESR {year}{amount}", "source_url": source_url, "director": r.get("director")})
    return {"companies": companies, "credits": credits}


def run_marche(texts=None, ask=None):
    """Lavoro `census:marche`. `texts` = {anno: [(url, testo)]} per i test; altrimenti scarica i PDF delle graduatorie definitive."""
    errors, notes, plans = 0, [], []
    if texts is None:
        texts = {}
        for year, page in MARCHE_PAGES.items():
            final, h, err = web.fetch(page)
            if err:
                errors += 1
                notes.append(f"{year}: {err}")
                continue
            for u, label in web.links(final, h):
                if u.endswith(".pdf") and re.search(r"graduatoria", label, re.I) and re.search(r"definitiv", label, re.I) and not re.search(r"provvisoria|scorrimento|determina", label, re.I):
                    raw, err = web.fetch_bytes(u)
                    if err:
                        errors += 1
                        notes.append(f"{year}: {err}")
                        continue
                    texts.setdefault(year, []).append((u, pdf_to_text(raw)))
    for year, docs in texts.items():
        for url, text in docs:
            try:
                rows = extract_rows(text, ask)
            except llm.BudgetExceeded as e:
                errors += 1
                notes.append(str(e))
                break
            ok, rejected = verify_rows(rows, text)
            errors += len(rejected)
            notes.append(f"{year}: {len(ok)} righe verificate, {len(rejected)} scartate")
            plans.append(plan_marche(ok, year, url))
    if not plans:
        return 0, errors, "; ".join(notes) or "nessuna graduatoria"
    st = census_dgca.load(census_dgca.merge_plans(plans), source_name=MARCHE_SOURCE)
    db.run("INSERT OR IGNORE INTO radar_sources(name, url, kind, project, company_type, country) VALUES(?,?,?,?,?,?)",
           MARCHE_SOURCE, "https://www.fondazionemarchecultura.it/bandi/", "census", "census", "production", "IT")
    db.run("UPDATE radar_sources SET last_run=datetime('now'), found_total=found_total+? WHERE name=?", st["companies_new"], MARCHE_SOURCE)
    return st["companies_new"], errors, (f"{st['companies_new']} case nuove, {st['companies_matched']} già note, {st['credits_new']} crediti; " + "; ".join(notes))[:300]
