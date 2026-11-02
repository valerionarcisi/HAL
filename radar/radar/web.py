"""Scarico pagine con cortesia (robots.txt, pausa per dominio) ed estraggo segnali."""
import html as htmllib
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import urllib.robotparser

UA = "RadarValerioNarcisi/1.0 (+https://valerionarcisi.me; contatti per cortometraggi)"
_last_hit = {}
_robots = {}
_lock = threading.Lock()
PER_DOMAIN_DELAY = 2.0

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", "sentry", "wixpress", "example.", "@2x", "domain.com", "yourname", "email.com")
SHORTS_LINK = re.compile(r"(cort|short|catalog|film|titoli|works|lavori|progetti|archivio)", re.I)
PAGE_CHARS = 1600  # tetto di testo per pagina, così ogni pagina contribuisce alla valutazione
CONTACT_LINK = re.compile(r"(contact|contatt|about|chi-siamo|team|submi|send|invia|propon|distribu)", re.I)


def domain(url):
    return urllib.parse.urlparse(url or "").netloc.lower().removeprefix("www.")


def _allowed(url):
    d = urllib.parse.urlparse(url)
    base = f"{d.scheme}://{d.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                rp.parse(r.read(200000).decode("utf-8", "ignore").splitlines())
        except Exception:
            rp = None  # nessun robots.txt leggibile: permesso
        _robots[base] = rp
    rp = _robots[base]
    return rp is None or rp.can_fetch(UA, url)


def fetch(url, timeout=15):
    """Ritorna (url_finale, html, errore). Mai eccezioni."""
    try:
        if not _allowed(url):
            return url, "", "robots.txt vieta"
        d = domain(url)
        with _lock:
            wait = _last_hit.get(d, 0) + PER_DOMAIN_DELAY - time.time()
            _last_hit[d] = time.time() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it,en;q=0.8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.geturl(), r.read(800000).decode("utf-8", "ignore"), None
    except Exception as e:
        return url, "", str(e)[:120]


def text(h):
    h = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", h)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", h))).strip()


def links(base, h):
    out = []
    for m in re.finditer(r'<a\s[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', h, re.I | re.S):
        out.append((urllib.parse.urljoin(base, m.group(1)), text(m.group(2))[:120]))
    return out


def emails(h):
    found = set()
    h = h.replace("[at]", "@").replace("(at)", "@").replace(" [dot] ", ".")
    h = re.sub(r"%20", "", h)
    for e in EMAIL_RE.findall(h):
        e = e.strip(".").lower()
        if not any(b in e for b in BAD_EMAIL):
            found.add(e)
    return sorted(found)


_mx = {}


def has_mx(dom):
    dom = dom.lower().strip(".")
    if dom not in _mx:
        try:
            with urllib.request.urlopen(f"https://dns.google/resolve?name={dom}&type=MX", timeout=8) as r:
                d = json.load(r)
            _mx[dom] = d.get("Status") == 0 and any(a.get("type") == 15 for a in d.get("Answer", []))
        except Exception:
            _mx[dom] = None
    return _mx[dom]


def crawl_site(url, max_pages=7):
    """Home + indici di catalogo + pagine contatti/about/submission dello stesso dominio."""
    final, h, err = fetch(url)
    if err:
        return {"url": url, "final": final, "error": err, "pages": {}}
    pages = {final: h}
    same = {l for l, _ in links(final, h) if domain(l) == domain(final)}
    # prima gli indici di catalogo (corti, film, titoli), poi contatti e invio; gli URL più corti sono le pagine indice
    shorts = sorted((l for l in same if SHORTS_LINK.search(l)), key=len)[:3]
    contact = sorted((l for l in same if CONTACT_LINK.search(l)), key=len)[:3]
    for l in list(dict.fromkeys(shorts + contact))[: max_pages - 1]:
        f, hh, e = fetch(l)
        if not e:
            pages[f] = hh
    return {"url": url, "final": final, "error": None, "pages": pages}


def signals(site):
    """Email con la pagina dove compaiono, anni citati, parole chiave."""
    found, alltext = {}, []
    for page, h in site["pages"].items():
        for e in emails(h):
            found.setdefault(e, page)
        alltext.append(text(h)[:PAGE_CHARS])
    t = " ".join(alltext)
    return {
        "emails": [{"email": e, "proof": p, "mx": has_mx(e.split("@")[1])} for e, p in found.items()],
        "years": sorted(set(re.findall(r"\b(20[12]\d)\b", t))),
        "text": t[:15000],
    }


GENERIC_TITLE = {"home", "homepage", "welcome", "benvenuti", "index", "chi siamo", "contatti", "contact", "about", "about us"}
IG_SKIP = {"p", "reel", "reels", "explore", "accounts", "stories", "tv", "about", "share", "sharer", "direct", "developer", "legal"}
IG_RE = re.compile(r"instagram\.com/([A-Za-z0-9._]{2,30})(?![A-Za-z0-9._])", re.I)
LI_RE = re.compile(r"linkedin\.com/(company|in|school)/([A-Za-z0-9_%.\-]{2,100})", re.I)


def socials(pages):
    """Profili Instagram e LinkedIn citati nelle pagine (link, JSON-LD sameAs, footer)."""
    ig, company, people = {}, {}, {}
    for h in pages.values():
        h = h.replace("\\/", "/")
        for m in IG_RE.finditer(h):
            handle = m.group(1).rstrip(".")
            if handle.lower() not in IG_SKIP:
                ig.setdefault(handle.lower(), f"https://www.instagram.com/{handle}/")
        for m in LI_RE.finditer(h):
            slug = m.group(2).rstrip("/.")
            bucket = people if m.group(1).lower() == "in" else company
            bucket.setdefault(slug.lower(), f"https://www.linkedin.com/{m.group(1).lower()}/{slug}/")
    return {"instagram": list(ig.values()), "linkedin_company": list(company.values()), "linkedin_people": list(people.values())}


def site_name(pages):
    for h in pages.values():
        m = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']{2,80})', h, re.I) \
            or re.search(r'<meta[^>]+content=["\']([^"\']{2,80})["\'][^>]+property=["\']og:site_name', h, re.I)
        if m:
            return htmllib.unescape(m.group(1)).strip()
        m = re.search(r"<title[^>]*>(.*?)</title>", h, re.I | re.S)
        if m:
            parts = [x.strip() for x in re.split(r"\s[|–—\-·»]\s", htmllib.unescape(m.group(1))) if x.strip()]
            parts = [x for x in parts if x.lower() not in GENERIC_TITLE] or parts
            if parts:
                return min(parts, key=len) if len(parts[0]) > 40 else parts[0]
        break
    return ""
