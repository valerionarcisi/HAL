#!/usr/bin/env python3
"""
sub_fetcher.py — Telegram-interactive Italian subtitle downloader
Scans media folders, finds videos missing Italian subs, asks via Telegram
whether to download them, and saves them with the correct filename.

Features:
  - OpenSubtitles hash + name search
  - Placeholder/VIP-bait detection (rejects fake subs)
  - Fallback: download English subs and translate to Italian via Claude API
  - Telegram bot interaction

Runs as a long-running service (in Docker).
"""

import os
import sys
import struct
import json
import gzip
import base64
import logging
import time
import re
import tempfile
import urllib.request
import urllib.error
import urllib.parse
import zipfile
import io
import html
from xmlrpc.client import ServerProxy
from pathlib import Path
from datetime import datetime, timedelta
from threading import Thread
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor, as_completed

# =============================================================================
# CONFIGURATION
# =============================================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# OpenSubtitles.com REST API v1 (new, replaces legacy XML-RPC)
# Get an API key at https://www.opensubtitles.com/en/consumers
OPENSUBTITLES_API_KEY = os.environ.get("OPENSUBTITLES_API_KEY", "")
OPENSUBTITLES_API_URL = "https://api.opensubtitles.com/api/v1"
OPENSUBTITLES_USER_AGENT = "NarcisiSubs v1.0.0"
# Legacy XML-RPC credentials kept for backwards compatibility only — unused now.
OS_USERNAME = os.environ.get("OS_USERNAME", "")
OS_PASSWORD = os.environ.get("OS_PASSWORD", "")
OS_LANGUAGE = "it"  # REST v1 uses ISO 639-1 (two-letter) codes

# Subdl.com API (primary subtitle provider)
SUBDL_API_KEY = os.environ.get("SUBDL_API_KEY", "")
SUBDL_API_URL = "https://api.subdl.com/api/v1/subtitles"
SUBDL_DOWNLOAD_URL = "https://dl.subdl.com/subtitle"

# TMDb (used to resolve title+year -> IMDb ID when no .nfo is present)
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_API_URL = "https://api.themoviedb.org/3"

# SubSource (third subtitle provider)
SUBSOURCE_API_URL = "https://api.subsource.net/v1"

# Claude API for translation (EN -> IT fallback / polish pass)
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
CLAUDE_POLISH_MODEL = os.environ.get("CLAUDE_POLISH_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"

# DeepL API for primary EN -> IT translation (cue-by-cue, no truncation risk)
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "")
DEEPL_API_URL = (
    "https://api-free.deepl.com/v2/translate"
    if DEEPL_API_KEY.endswith(":fx")
    else "https://api.deepl.com/v2/translate"
)
# Run a Claude polish pass over DeepL output to rewrite unnatural lines.
POLISH_TRANSLATION = os.environ.get("POLISH_TRANSLATION", "true").lower() in ("1", "true", "yes")

# Radarr integration (/scarica command — request new films from Telegram).
RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
RADARR_ROOT_FOLDER = os.environ.get("RADARR_ROOT_FOLDER", "")
RADARR_QUALITY_PROFILE = os.environ.get("RADARR_QUALITY_PROFILE", "")
RADARR_PREFERRED_LANGUAGES = [
    s.strip().upper() for s in os.environ.get("RADARR_PREFERRED_LANGUAGES", "ITA,ENG").split(",")
    if s.strip()
]

# Multi-source film download (/cerca command — archive.org + YouTube fallback).
# YT_COOKIES_FILE points to a Netscape cookies.txt that yt-dlp uses to bypass
# age-gates / consent walls. Kept under /config/secrets so the host bind-mount
# survives container rebuilds and the file never leaks into git.
YT_COOKIES_FILE = os.environ.get("YT_COOKIES_FILE", "/config/secrets/yt_cookies.txt")
CERCA_DOWNLOAD_DIR = os.environ.get("CERCA_DOWNLOAD_DIR", "/media/films")
# Minimum acceptable resolution by default. Lower-res candidates are still
# listed but tagged "bassa qualità" so the user knows what they are picking.
CERCA_MIN_RESOLUTION = int(os.environ.get("CERCA_MIN_RESOLUTION", "720"))
CERCA_FALLBACK_RESOLUTION = int(os.environ.get("CERCA_FALLBACK_RESOLUTION", "480"))

# Media paths (inside the container, mapped via volumes)
SERIES_PATH = "/media/series"
FILMS_PATH = "/media/films"

# Excluded folders (Italian audio — no need for Italian subs)
EXCLUDE_FOLDERS_FILE = "/config/exclude_folders.txt"
DEFAULT_EXCLUDES = ["Boris"]

# State
STATE_FILE = "/config/state.json"
BATCHES_FILE = "/config/batches.json"
REQUESTS_FILE = "/config/requests.json"
AUDIO_CACHE_FILE = "/config/release_audio_cache.json"
LOG_FILE = "/config/sub_fetcher.log"

# Timing
SCAN_INTERVAL = 300  # 5 minutes
DELAY_BETWEEN_API_CALLS = 2  # seconds
RETRY_AFTER_HOURS = 72  # don't re-ask for 3 days after "no"
FAILED_RETRY_HOURS = 24  # retry failed downloads after 24h

# Subtitle sync validation
SYNC_MIN_SCORE = 800  # ffsubsync score threshold — reject subs that don't match the video

# Video extensions
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".wmv", ".ts"}

# =============================================================================
# LOGGING
# =============================================================================

os.makedirs("/config", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("sub_fetcher")

# =============================================================================
# STATE
# =============================================================================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "asked": {},              # path -> {"time": iso, "status": "pending"|"yes"|"no"|"failed"}
        "downloaded": {},         # path -> {"sub": filename, "time": iso}
        "italian_original": {},   # path -> {"time": iso} (cache for TMDb original_language=it)
        "seen": [],               # list of paths already notified as "new"
        "last_offset": 0,         # Telegram update offset
    }


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Failed to save state: {e}")


def load_batches():
    """Load pending download/translate batches from separate file (thread-safe)."""
    if os.path.exists(BATCHES_FILE):
        try:
            with open(BATCHES_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_batches(batches):
    """Persist batches independently of main state to avoid race conditions."""
    try:
        with open(BATCHES_FILE, "w") as f:
            json.dump(batches, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Failed to save batches: {e}")


def load_requests():
    """Pending download requests (Radarr-side from `/scarica`, multi-source from
    `/cerca`). Kept separate from state.json so /reset doesn't drop them."""
    if os.path.exists(REQUESTS_FILE):
        try:
            with open(REQUESTS_FILE, "r") as f:
                data = json.load(f)
                data.setdefault("pending_radarr", {})
                data.setdefault("pending_cerca", {})
                return data
        except Exception:
            pass
    return {"pending_radarr": {}, "pending_cerca": {}}


def save_requests(requests):
    try:
        with open(REQUESTS_FILE, "w") as f:
            json.dump(requests, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Failed to save requests: {e}")


# =============================================================================
# EXCLUDE LIST
# =============================================================================

def load_excludes():
    excludes = set(DEFAULT_EXCLUDES)
    if os.path.exists(EXCLUDE_FOLDERS_FILE):
        try:
            with open(EXCLUDE_FOLDERS_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        excludes.add(line)
        except Exception:
            pass
    return excludes


def save_excludes(excludes):
    try:
        with open(EXCLUDE_FOLDERS_FILE, "w") as f:
            f.write("# Folders to exclude (Italian audio, one per line)\n")
            for e in sorted(excludes):
                f.write(f"{e}\n")
    except Exception as e:
        log.error(f"Failed to save excludes: {e}")


# =============================================================================
# TELEGRAM API
# =============================================================================

def tg_request(method, data=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        if data:
            payload = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.error(f"Telegram API error ({method}): {e}")
        return None


def tg_send(text, reply_markup=None):
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    return tg_request("sendMessage", data)


def tg_answer_callback(callback_id, text=""):
    return tg_request("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text,
    })


def tg_edit_message(message_id, text, reply_markup=None):
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_request("editMessageText", payload)


def tg_get_updates(offset=0):
    result = tg_request("getUpdates", {
        "offset": offset,
        "timeout": 1,
    })
    if result and result.get("ok"):
        return result.get("result", [])
    return []


# =============================================================================
# OPENSUBTITLES HASH
# =============================================================================

def compute_hash(filepath):
    try:
        longlongformat = "<q"
        bytesize = struct.calcsize(longlongformat)
        filesize = os.path.getsize(filepath)
        if filesize < 65536 * 2:
            return None, filesize
        filehash = filesize
        with open(filepath, "rb") as f:
            for _ in range(65536 // bytesize):
                buf = f.read(bytesize)
                (val,) = struct.unpack(longlongformat, buf)
                filehash += val
                filehash &= 0xFFFFFFFFFFFFFFFF
            f.seek(max(0, filesize - 65536), 0)
            for _ in range(65536 // bytesize):
                buf = f.read(bytesize)
                (val,) = struct.unpack(longlongformat, buf)
                filehash += val
                filehash &= 0xFFFFFFFFFFFFFFFF
        return "%016x" % filehash, filesize
    except Exception as e:
        log.error(f"Hash failed for {filepath}: {e}")
        return None, 0


# =============================================================================
# FILENAME PARSING
# =============================================================================

_SCRAPER_PREFIX_RE = re.compile(
    r"^\s*(?:www[.\s_-]*)?[\w-]+\.(?:com|org|net|it|to|me|info|io)\s*[-_.\s]+",
    re.IGNORECASE,
)
_BRACKET_PREFIX_RE = re.compile(r"^\s*[\[\(][^\]\)]+[\]\)]\s*[-_.\s]*")


def _strip_scraper_noise(s):
    prev = None
    while prev != s:
        prev = s
        s = _SCRAPER_PREFIX_RE.sub("", s)
        s = _BRACKET_PREFIX_RE.sub("", s)
    return s


def _clean_title(s):
    s = s.replace(".", " ").replace("_", " ")
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" -")


def parse_video(filepath):
    fname = os.path.basename(filepath)
    name_no_ext = os.path.splitext(fname)[0]
    name_no_ext = _strip_scraper_noise(name_no_ext)

    # Series: S01E01
    m = re.search(r"(.+?)[.\s_-]+[Ss](\d{1,2})[Ee](\d{1,2})", name_no_ext)
    if m:
        return {"type": "episode", "name": _clean_title(m.group(1)), "season": int(m.group(2)), "episode": int(m.group(3))}

    # Movie: Name Year  or  Name (Year)
    m = re.search(r"(.+?)[.\s_\-]*\(?(\d{4})\)?(?:[.\s_\-)\]]|$)", name_no_ext)
    if m:
        return {"type": "movie", "name": _clean_title(m.group(1)), "year": int(m.group(2))}

    # Fallback: use filename without extension, cleaned up
    # Avoid using media root folders like "films" or "series" as name
    clean_name = name_no_ext.replace(".", " ").replace("_", " ").replace("-", " ").strip()
    # Remove common junk tags from filename
    clean_name = re.sub(r"\b(720p|1080p|2160p|4k|bluray|webrip|web dl|brrip|hdtv|x264|x265|hevc|aac|yts|yify|mx)\b", "", clean_name, flags=re.IGNORECASE).strip()
    clean_name = re.sub(r"\s{2,}", " ", clean_name).strip(" -[]().")
    if clean_name:
        return {"type": "unknown", "name": clean_name}

    parent = os.path.basename(os.path.dirname(filepath))
    return {"type": "unknown", "name": parent}


def _find_imdb_id_from_nfo(video_path):
    """Search for IMDB ID in .nfo files (created by Sonarr/Radarr)."""
    video_dir = os.path.dirname(video_path)
    parent_dir = os.path.dirname(video_dir)
    search_dirs = [video_dir, parent_dir]

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        try:
            for fname in os.listdir(d):
                if fname.lower().endswith(".nfo"):
                    nfo_path = os.path.join(d, fname)
                    try:
                        with open(nfo_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                        m = re.search(r"(tt\d{7,})", content)
                        if m:
                            log.info(f"  Found IMDB ID {m.group(1)} from {fname}")
                            return m.group(1)
                    except Exception:
                        continue
        except Exception:
            continue
    return None


def tmdb_find_imdb_id(title, year=None, is_tv=False):
    """Look up IMDb ID from TMDb using title (+ year). Returns 'ttXXXXXXX' or None."""
    if not TMDB_API_KEY or not title:
        return None
    endpoint = "/search/tv" if is_tv else "/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": title, "include_adult": "false"}
    if year and not is_tv:
        params["year"] = str(year)
    elif year and is_tv:
        params["first_air_date_year"] = str(year)
    url = f"{TMDB_API_URL}{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning(f"  TMDb search error: {e}")
        return None
    results = data.get("results") or []
    if not results:
        log.info(f"  TMDb: no match for '{title}' ({year})")
        return None
    tmdb_id = results[0].get("id")
    if not tmdb_id:
        return None
    # Fetch external_ids to get the IMDb ID
    ext_endpoint = f"/tv/{tmdb_id}/external_ids" if is_tv else f"/movie/{tmdb_id}/external_ids"
    ext_url = f"{TMDB_API_URL}{ext_endpoint}?api_key={TMDB_API_KEY}"
    try:
        with urllib.request.urlopen(ext_url, timeout=10) as resp:
            ext = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning(f"  TMDb external_ids error: {e}")
        return None
    imdb_id = ext.get("imdb_id")
    if imdb_id:
        log.info(f"  TMDb: resolved '{title}' ({year}) -> {imdb_id}")
        return imdb_id
    return None


def tmdb_search_movies(query, year=None, limit=5):
    """Search TMDb for films matching `query`. Returns a list of dicts:
      {tmdb_id, title, year, original_language, overview, poster_url, director}
    Used by /scarica to disambiguate which film the user wants."""
    if not TMDB_API_KEY or not query:
        return []
    params = {"api_key": TMDB_API_KEY, "query": query, "include_adult": "false"}
    if year:
        params["year"] = str(year)
    url = f"{TMDB_API_URL}/search/movie?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning(f"  TMDb search error: {e}")
        return []
    out = []
    for r in (data.get("results") or [])[:limit]:
        tmdb_id = r.get("id")
        if not tmdb_id:
            continue
        rel_date = r.get("release_date") or ""
        ryear = int(rel_date[:4]) if rel_date[:4].isdigit() else None
        poster = r.get("poster_path")
        out.append({
            "tmdb_id": tmdb_id,
            "title": r.get("title") or r.get("original_title") or "?",
            "year": ryear,
            "original_language": r.get("original_language"),
            "overview": (r.get("overview") or "").strip(),
            "poster_url": f"https://image.tmdb.org/t/p/w342{poster}" if poster else None,
        })
    return out


def tmdb_get_original_language(title, year=None, is_tv=False):
    """Return the original_language ISO-639-1 code from TMDb (e.g. 'it', 'en'),
    or None if the lookup fails. Uses the first search result, like
    tmdb_find_imdb_id."""
    if not TMDB_API_KEY or not title:
        return None
    endpoint = "/search/tv" if is_tv else "/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": title, "include_adult": "false"}
    if year and not is_tv:
        params["year"] = str(year)
    elif year and is_tv:
        params["first_air_date_year"] = str(year)
    url = f"{TMDB_API_URL}{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning(f"  TMDb language lookup error: {e}")
        return None
    results = data.get("results") or []
    if not results:
        return None
    lang = results[0].get("original_language")
    if lang:
        log.debug(f"  TMDb original_language for '{title}' ({year}): {lang}")
    return lang


def tmdb_get_original_language_for_tmdb_id(tmdb_id):
    """Return the `original_language` ISO 639-1 code (e.g. 'it', 'ja') for a
    known TMDb movie id, or None on failure. Used by /scarica to default the
    `--lang` filter to the film's original language when the user did not pass
    an explicit override."""
    if not TMDB_API_KEY or not tmdb_id:
        return None
    url = f"{TMDB_API_URL}/movie/{tmdb_id}?api_key={TMDB_API_KEY}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning(f"  TMDb movie lookup error for {tmdb_id}: {e}")
        return None
    lang = data.get("original_language")
    if lang:
        log.debug(f"  TMDb original_language for tmdb_id={tmdb_id}: {lang}")
    return lang


def tmdb_iso1_to_code(iso1):
    """Map a TMDb ISO 639-1 code (e.g. 'it') to a registry 3-letter code
    (e.g. 'ITA'). Returns None for languages we don't track."""
    if not iso1:
        return None
    return _TMDB_ISO1_TO_CODE.get(iso1.lower())


def verify_audio_language(video_path, required_code):
    """Return True iff the video file's audio streams contain a track matching
    `required_code` (a 3-letter ISO code from LANGUAGE_REGISTRY). Inspects both
    `language` and `title` ffprobe tags. Always returns True when the caller
    didn't request a specific language (required_code is None) — used as a
    "no-op gate" by post-grab notification code.

    Failures (ffprobe missing, file unreadable, malformed JSON) return False so
    the caller can surface the mismatch to the user instead of silently
    declaring success."""
    if required_code is None:
        return True
    spec = LANGUAGE_REGISTRY.get(required_code)
    if not spec:
        return False
    # Build a single, lowercase set of acceptable substrings: the registry
    # tokens plus a few common long-name variants. Comparison is substring on
    # the lowercased ffprobe tag so "Japanese (5.1 Atmos)" matches "japanese".
    needles = {t.lower() for t in spec["tokens"]}
    iso1 = spec.get("tmdb_iso1")
    if iso1:
        needles.add(iso1)
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "a", video_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        log.debug(f"verify_audio_language ffprobe failed for {os.path.basename(video_path)}: {e}")
        return False
    for stream in data.get("streams", []):
        tags = stream.get("tags", {}) or {}
        lang_tag = (tags.get("language") or "").lower()
        title_tag = (tags.get("title") or "").lower()
        for needle in needles:
            if not needle:
                continue
            # Exact match on the language tag (ffprobe usually returns 3-letter
            # ISO codes like 'jpn' / 'ita'), substring match on the human title.
            if lang_tag == needle or needle in title_tag:
                return True
    return False


def is_italian_original(video_path):
    """Return True if TMDb says this film/series was made in Italian.
    Used to skip the subtitle search for Italian-original content (e.g.
    'La Grande Bellezza') even when the file lacks proper audio language
    tags. Falls back to False if TMDb can't resolve it."""
    parsed = parse_video(video_path)
    if not parsed.get("name"):
        return False
    is_tv = parsed.get("type") == "episode"
    lang = tmdb_get_original_language(parsed["name"], parsed.get("year"), is_tv=is_tv)
    return lang == "it"


def find_imdb_id(video_path):
    """Find IMDb ID from .nfo files first, fall back to TMDb lookup."""
    imdb = _find_imdb_id_from_nfo(video_path)
    if imdb:
        return imdb
    parsed = parse_video(video_path)
    is_tv = parsed.get("type") == "episode"
    return tmdb_find_imdb_id(parsed.get("name"), parsed.get("year"), is_tv=is_tv)


def detect_language_from_srt(srt_path, sample_size=2000):
    """Detect if an SRT file is Italian or English by inspecting common words."""
    try:
        with open(srt_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read(sample_size).lower()
    except Exception:
        return "unknown"

    # Remove timecodes and numbers
    text = re.sub(r"\d{2}:\d{2}:\d{2},\d+\s*-->.*", "", text)
    text = re.sub(r"^\d+\s*$", "", text, flags=re.MULTILINE)

    it_words = ["che", "non", "sono", "una", "per", "con", "questo", "della", "anche", "cosa", "come", "perché", "quello", "ancora", "dove", "quando", "tutti", "stato", "fatto", "sempre"]
    en_words = ["the", "and", "you", "that", "was", "for", "are", "with", "his", "they", "this", "have", "from", "not", "been", "what", "would", "there", "their", "will"]

    it_count = sum(1 for w in it_words if re.search(r"\b" + w + r"\b", text))
    en_count = sum(1 for w in en_words if re.search(r"\b" + w + r"\b", text))

    if it_count > en_count and it_count >= 3:
        return "it"
    if en_count > it_count and en_count >= 3:
        return "en"
    return "unknown"


EN_SUB_SUFFIXES = [".en.srt", ".eng.srt", ".english.srt"]


def find_english_sub(video_path):
    """Return the path of an existing English subtitle for this video, or None.
    Checks common suffixes in order: .en.srt, .eng.srt, .english.srt."""
    base = os.path.splitext(video_path)[0]
    for suffix in EN_SUB_SUFFIXES:
        path = base + suffix
        if os.path.exists(path):
            return path
    return None


def find_existing_srt(video_path):
    """Check for existing subtitle files in the video's directory."""
    base = os.path.splitext(video_path)[0]
    video_dir = os.path.dirname(video_path)
    video_base_name = os.path.splitext(os.path.basename(video_path))[0].lower()

    # Check for English subtitles
    en_path = find_english_sub(video_path)
    if en_path:
        return {"lang": "en", "path": en_path}

    # Check for generic .srt matching the video name
    generic_srt = base + ".srt"
    if os.path.exists(generic_srt):
        lang = detect_language_from_srt(generic_srt)
        return {"lang": lang, "path": generic_srt}

    # Check for any .srt in the directory that roughly matches
    try:
        for fname in os.listdir(video_dir):
            if not fname.lower().endswith(".srt"):
                continue
            fbase = fname.lower()
            # Skip files already tagged as Italian
            if any(fbase.endswith(s) for s in [".it.srt", ".ita.srt", ".italian.srt"]):
                continue
            # Check if filename partially matches the video
            if video_base_name[:10] in fbase:
                srt_path = os.path.join(video_dir, fname)
                lang = detect_language_from_srt(srt_path)
                return {"lang": lang, "path": srt_path}
    except Exception:
        pass

    return None


def get_search_queries(video_path):
    """Return a list of search query strings to try, from most to least specific."""
    queries = []
    parsed = parse_video(video_path)
    filename_name = parsed.get("name", "")
    if filename_name:
        queries.append(filename_name)

    series_folder = get_series_folder(video_path)
    if series_folder:
        folder_clean = series_folder.replace(".", " ").replace("_", " ").replace("-", " ").strip()
        if folder_clean.lower() not in [q.lower() for q in queries]:
            queries.append(folder_clean)
        # Also try removing digits/symbols for names like PLUR1BUS -> PLURIBUS
        alpha_only = re.sub(r"[^a-zA-Z\s]", "", folder_clean).strip()
        if alpha_only and alpha_only.lower() not in [q.lower() for q in queries]:
            queries.append(alpha_only)

    return queries


def friendly_name(filepath):
    """Human-readable name for Telegram messages."""
    parsed = parse_video(filepath)
    if parsed["type"] == "episode":
        return f"{parsed['name']} S{parsed['season']:02d}E{parsed['episode']:02d}"
    elif parsed["type"] == "movie":
        return f"{parsed['name']} ({parsed['year']})"
    else:
        return os.path.basename(filepath)


def get_series_folder(filepath):
    """Get the immediate series/movie folder name."""
    # e.g. /media/series/The Chosen/Season 1/file.mkv -> "The Chosen"
    rel = None
    for base in [SERIES_PATH, FILMS_PATH]:
        if filepath.startswith(base):
            rel = os.path.relpath(filepath, base)
            break
    if rel:
        return rel.split(os.sep)[0]
    return os.path.basename(os.path.dirname(filepath))


# =============================================================================
# SCAN
# =============================================================================

def has_italian_sub(video_path):
    base = os.path.splitext(video_path)[0]
    for suffix in [".it.srt", ".ita.srt", ".italian.srt", ".it.hi.srt", ".it.ass", ".ita.ass"]:
        if os.path.exists(base + suffix):
            return True
    return False


def has_italian_audio(video_path):
    """Check if the video file has an Italian audio track using ffprobe."""
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "a", video_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            tags = stream.get("tags", {})
            lang = tags.get("language", "").lower()
            title = tags.get("title", "").lower()
            if lang in ("ita", "it", "italian"):
                return True
            if "italian" in title or "italiano" in title:
                return True
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        log.debug(f"ffprobe check skipped for {os.path.basename(video_path)}: {e}")
    return False


def is_excluded(filepath, excludes):
    folder = get_series_folder(filepath)
    for exc in excludes:
        if folder.lower() == exc.lower():
            return True
    return False


def _notify_new_media(state, full_path, media_path):
    """Send a Telegram notification the first time a new media file is detected."""
    seen = state.setdefault("seen", [])
    if full_path in seen:
        return
    seen.append(full_path)
    save_state(state)
    name = friendly_name(full_path)
    emoji = "🎬" if media_path == FILMS_PATH else "📺"
    label = "film" if media_path == FILMS_PATH else "serie"
    tg_send(f"{emoji} Nuovo {label} scaricato:\n<b>{name}</b>")


def scan_missing(state, excludes):
    missing = []
    now = datetime.now()

    for media_path in [SERIES_PATH, FILMS_PATH]:
        if not os.path.exists(media_path):
            continue
        for root, dirs, files in os.walk(media_path):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in VIDEO_EXTENSIONS:
                    continue

                full_path = os.path.join(root, fname)

                if has_italian_sub(full_path):
                    continue

                if is_excluded(full_path, excludes):
                    continue

                if has_italian_audio(full_path):
                    log.info(f"  Skipping (Italian audio): {fname}")
                    continue

                # Cached "Italian original via TMDb" — set on first detection.
                if full_path in state.get("italian_original", {}):
                    continue

                # Check state
                info = state["asked"].get(full_path)
                if info:
                    t = datetime.fromisoformat(info["time"])
                    status = info["status"]
                    if status == "pending":
                        continue  # Already asked, waiting for response
                    if status == "no" and now - t < timedelta(hours=RETRY_AFTER_HOURS):
                        continue  # User said no, wait before re-asking
                    if status == "failed" and now - t < timedelta(hours=FAILED_RETRY_HOURS):
                        continue  # Download failed, wait before retrying

                if full_path in state["downloaded"]:
                    continue  # Already downloaded

                # New video — check TMDb for Italian original_language.
                # Cached afterwards so we don't re-query at every scan.
                if is_italian_original(full_path):
                    log.info(f"  Skipping (Italian original via TMDb): {fname}")
                    state.setdefault("italian_original", {})[full_path] = {
                        "time": datetime.now().isoformat(),
                    }
                    save_state(state)
                    continue

                _notify_new_media(state, full_path, media_path)
                missing.append(full_path)

    return missing


# =============================================================================
# OPENSUBTITLES CLIENT
# =============================================================================

_LANG_2LETTER = {"ita": "it", "eng": "en", "it": "it", "en": "en"}


def _to_iso2(lang):
    return _LANG_2LETTER.get((lang or "").lower(), (lang or "en").lower()[:2])


class OSClient:
    """Client for OpenSubtitles.com REST API v1.

    Returns results in a legacy-compatible shape (SubFileName, MovieReleaseName,
    IDSubtitleFile, SubFormat, SubDownloadsCnt, SubRating, MatchedBy) so that
    the existing pick_best / _download_first_valid code keeps working.
    """

    def __init__(self):
        self.api_key = OPENSUBTITLES_API_KEY
        self.available = bool(self.api_key)
        self.token = None  # kept for backwards compat with callers
        self.downloads_remaining = None  # populated after POST /download

    def _headers(self, json_body=False):
        h = {
            "Api-Key": self.api_key,
            "User-Agent": OPENSUBTITLES_USER_AGENT,
            "Accept": "application/json",
        }
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _request(self, method, path, params=None, body=None, timeout=15):
        if not self.available:
            return None
        url = f"{OPENSUBTITLES_API_URL}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers(json_body=bool(body)))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="ignore")[:200]
            except Exception:
                pass
            log.warning(f"  OpenSubtitles {method} {path} HTTP {e.code}: {body_text}")
        except Exception as e:
            log.warning(f"  OpenSubtitles {method} {path} error: {e}")
        return None

    def login(self):
        """No-op: REST v1 uses API key, no session token required."""
        if self.available:
            log.info("OpenSubtitles.com REST v1: ready")
            return True
        log.info("OpenSubtitles: OPENSUBTITLES_API_KEY not set, skipping")
        return False

    def logout(self):
        pass

    @staticmethod
    def _to_legacy(item):
        """Map a REST v1 subtitle item to the legacy XML-RPC field names."""
        attrs = item.get("attributes", {}) or {}
        files = attrs.get("files") or []
        first = files[0] if files else {}
        file_name = first.get("file_name") or attrs.get("release") or ""
        fmt = os.path.splitext(file_name)[1].lstrip(".").lower() or "srt"
        return {
            "SubFileName": file_name,
            "MovieReleaseName": attrs.get("release", ""),
            "IDSubtitleFile": str(first.get("file_id", "")),
            "SubFormat": fmt,
            "SubDownloadsCnt": attrs.get("download_count", 0) or 0,
            "SubRating": attrs.get("ratings", 0) or 0,
            "MatchedBy": "moviehash" if attrs.get("moviehash_match") else "",
            "_osd_attrs": attrs,
        }

    def _search(self, params):
        resp = self._request("GET", "/subtitles", params=params)
        if not resp:
            return []
        return [self._to_legacy(it) for it in (resp.get("data") or [])]

    def search_hash(self, file_hash, file_size):
        return self._search({"moviehash": file_hash, "languages": _to_iso2(OS_LANGUAGE)})

    def search_imdb(self, imdb_id, season=None, episode=None, language=None):
        lang = _to_iso2(language or OS_LANGUAGE)
        imdb_num = str(imdb_id).lower().lstrip("t")
        params = {"imdb_id": imdb_num, "languages": lang}
        if season is not None:
            params["season_number"] = int(season)
        if episode is not None:
            params["episode_number"] = int(episode)
        return self._search(params)

    def search_name(self, query, season=None, episode=None, language=None):
        lang = _to_iso2(language or OS_LANGUAGE)
        params = {"query": query, "languages": lang}
        if season is not None:
            params["season_number"] = int(season)
        if episode is not None:
            params["episode_number"] = int(episode)
        return self._search(params)

    def download(self, sub_id):
        """Request a download link, then fetch the SRT bytes."""
        if not sub_id:
            return None
        try:
            file_id = int(sub_id)
        except (TypeError, ValueError):
            log.warning(f"  OpenSubtitles download: invalid file_id '{sub_id}'")
            return None
        resp = self._request("POST", "/download", body={"file_id": file_id})
        if not resp:
            return None
        # Track download quota from response
        remaining = resp.get("remaining")
        if remaining is not None:
            self.downloads_remaining = int(remaining)
            log.info(f"  OpenSubtitles downloads remaining today: {self.downloads_remaining}")
        link = resp.get("link")
        if not link:
            log.warning(f"  OpenSubtitles download: no link in response ({resp})")
            return None
        try:
            with urllib.request.urlopen(link, timeout=30) as r:
                return r.read()
        except Exception as e:
            log.warning(f"  OpenSubtitles download fetch failed: {e}")
            return None


# =============================================================================
# SUBDL.COM CLIENT
# =============================================================================

class SubdlClient:
    """Client for Subdl.com subtitle API — no VIP placeholders."""

    LANG_MAP = {"ita": "it", "eng": "en", "it": "it", "en": "en"}

    def search(self, query, language="it", imdb_id=None, season=None, episode=None):
        if not SUBDL_API_KEY:
            return []
        try:
            lang = self.LANG_MAP.get(language, language)
            params = {
                "api_key": SUBDL_API_KEY,
                "languages": lang,
                "subs_per_page": "30",
            }
            if imdb_id:
                params["imdb_id"] = imdb_id
            else:
                params["film_name"] = query
            if season is not None:
                params["season_number"] = str(season)
            if episode is not None:
                params["episode_number"] = str(episode)

            url = SUBDL_API_URL + "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={"User-Agent": "SubFetcher/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if not data.get("status"):
                log.warning(f"  Subdl API error: {data}")
                return []

            subs = data.get("subtitles", [])
            log.info(f"  Subdl search ({imdb_id or query}, {lang}): {len(subs)} results")
            if subs:
                log.debug(f"  Subdl first result keys: {list(subs[0].keys())}")
                log.info(f"  Subdl first result url: {subs[0].get('url', 'N/A')}")
            return subs

        except Exception as e:
            log.error(f"  Subdl search error: {e}")
            return []

    def download(self, sub_info):
        """Download subtitle from Subdl. Returns SRT content bytes or None."""
        try:
            url_path = sub_info.get("url", "")
            if not url_path:
                return None

            # URL from API may be relative path or full — normalize it
            if url_path.startswith("http"):
                url = url_path
            elif url_path.startswith("/"):
                url = f"https://dl.subdl.com{url_path}"
            else:
                url = f"https://dl.subdl.com/subtitle/{url_path}"
            log.info(f"  Subdl download URL: {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "SubFetcher/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                zip_data = resp.read()

            # Extract best .srt from ZIP (prefer non-forced)
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                srt_files = [n for n in zf.namelist() if n.lower().endswith(".srt")]
                forced_tags = ["forced", "signs", "songs"]
                non_forced = [n for n in srt_files if not any(t in n.lower() for t in forced_tags)]
                best = non_forced[0] if non_forced else (srt_files[0] if srt_files else None)
                if best:
                    content = zf.read(best)
                    if non_forced and len(srt_files) > len(non_forced):
                        log.info(f"  Subdl: skipped forced subs, using: {best}")
                    log.info(f"  Subdl downloaded: {best} from {sub_info.get('release_name', '?')}")
                    return content

            log.warning(f"  Subdl ZIP contains no .srt files")
            return None

        except Exception as e:
            log.error(f"  Subdl download error: {e}")
            return None

    def search_and_download(self, video_path, language="it", trace=None):
        """Search and download best subtitle from Subdl. Returns content bytes or None.
        If trace is a list, appends per-attempt entries describing what was tried."""
        parsed = parse_video(video_path)
        season = parsed.get("season") if parsed["type"] == "episode" else None
        episode = parsed.get("episode") if parsed["type"] == "episode" else None
        lang_label = language.upper()

        # 1. Search by IMDB ID
        imdb_id = find_imdb_id(video_path)
        if imdb_id:
            time.sleep(DELAY_BETWEEN_API_CALLS)
            results = self.search("", language=language, imdb_id=imdb_id, season=season, episode=episode)
            if trace is not None:
                trace.append({"provider": "Subdl", "lang": lang_label,
                              "method": "imdb", "query": imdb_id, "results": len(results)})
            if results:
                content, reject = self._try_download(results, video_path)
                if content:
                    return content
                if trace is not None and reject:
                    trace[-1]["rejected"] = reject
        elif trace is not None:
            trace.append({"provider": "Subdl", "lang": lang_label,
                          "method": "imdb", "query": "(non risolto)", "results": 0})

        # 2. Search by name cascade
        queries = get_search_queries(video_path)
        for query in queries:
            time.sleep(DELAY_BETWEEN_API_CALLS)
            results = self.search(query, language=language, season=season, episode=episode)
            if trace is not None:
                trace.append({"provider": "Subdl", "lang": lang_label,
                              "method": "nome", "query": query, "results": len(results)})
            if results:
                content, reject = self._try_download(results, video_path)
                if content:
                    return content
                if trace is not None and reject:
                    trace[-1]["rejected"] = reject

        return None

    def _try_download(self, results, video_path, max_tries=3):
        """Try downloading from results, picking best match first.
        Returns (content_bytes_or_None, rejection_reason_or_None)."""
        video_base = os.path.splitext(os.path.basename(video_path))[0].lower()
        parsed = parse_video(video_path)
        video_season = parsed.get("season")
        video_episode = parsed.get("episode")

        # Extract release group (last part after dash, e.g. "FENiX" from "...x264-FENiX")
        release_group = ""
        m = re.search(r"-([a-zA-Z0-9]+)$", os.path.splitext(os.path.basename(video_path))[0])
        if m:
            release_group = m.group(1).lower()

        # Extract all meaningful tokens from video filename for matching
        video_tokens = set(re.findall(r"[a-zA-Z0-9]+", video_base))

        # Score and sort results
        scored = []
        for sub in results:
            score = 0
            release = (sub.get("release_name", "") or "").lower()
            sub_name = (sub.get("name", "") or "").lower()
            sub_tokens = set(re.findall(r"[a-zA-Z0-9]+", release))

            # CRITICAL: episode match — reject subs for wrong episode
            if video_season is not None and video_episode is not None:
                ep_match = re.search(r"s(\d+)e(\d+)", release)
                if ep_match:
                    sub_season = int(ep_match.group(1))
                    sub_episode = int(ep_match.group(2))
                    if sub_season == video_season and sub_episode == video_episode:
                        score += 500
                    else:
                        score -= 1000

            # Penalize forced/signs-only subs heavily
            if any(tag in release or tag in sub_name for tag in ["forced", "signs", "songs", "sdh", "hi-only"]):
                score -= 200

            # Exact filename match
            if video_base in release:
                score += 100

            # Release group match (most important for sync)
            if release_group and release_group in release:
                score += 80

            # Quality/source tag matches
            for tag in ["720p", "1080p", "2160p", "bluray", "webrip", "web-dl", "hdtv", "amzn", "x264", "x265", "hevc"]:
                if tag in video_base and tag in release:
                    score += 10

            # Token overlap (how many words in common)
            common = video_tokens & sub_tokens
            score += len(common) * 2

            scored.append((score, sub))

        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            log.info(f"  Subdl best match: score={scored[0][0]}, release={scored[0][1].get('release_name', '?')}")

        last_reject = None
        for i, (score, sub) in enumerate(scored[:max_tries]):
            time.sleep(DELAY_BETWEEN_API_CALLS)
            content = self.download(sub)
            if not content:
                last_reject = "download vuoto"
                continue
            if is_placeholder_sub(content):
                log.warning(f"  Subdl result {i+1} looks like placeholder, trying next")
                last_reject = "placeholder"
                continue
            # Reject forced/incomplete subs (fewer than 10 dialogue blocks)
            block_count = len(re.findall(rb"\d+\r?\n\d{2}:\d{2}:\d{2}", content))
            if block_count < 10:
                log.warning(f"  Subdl result {i+1} has only {block_count} blocks (likely forced/signs-only), trying next")
                last_reject = f"forced/incompleti ({block_count} blocchi)"
                continue
            return content, None

        if scored and not last_reject:
            last_reject = "nessun candidato valido"
        return None, last_reject


def pick_best(results, video_path):
    if not results:
        return None
    srt = [r for r in results if (r.get("SubFormat", "") or "").lower() == "srt"] or results
    video_base = os.path.splitext(os.path.basename(video_path))[0].lower()

    scored = []
    for sub in srt:
        score = 0
        sf = (sub.get("SubFileName", "") or "").lower()
        mr = (sub.get("MovieReleaseName", "") or "").lower()
        if sub.get("MatchedBy") == "moviehash":
            score += 100
        if video_base in sf or video_base in mr:
            score += 50
        for tag in ["galaxytv", "webrip", "720p", "1080p", "amzn", "bluray", "web-dl"]:
            if tag in video_base and tag in (sf + mr):
                score += 10
        score += min(int(sub.get("SubDownloadsCnt", 0) or 0) // 100, 20)
        score += int(float(sub.get("SubRating", 0) or 0))
        scored.append((score, sub))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1] if scored else None


# =============================================================================
# RADARR CLIENT (/scarica command — request new films from Telegram)
# =============================================================================

class RadarrClient:
    """Thin wrapper around Radarr v3 REST API.
    Used by /scarica to add films + run Interactive Search + grab a chosen release."""

    def __init__(self, base_url=None, api_key=None, timeout=30):
        self.base_url = (base_url or RADARR_URL).rstrip("/")
        self.api_key = api_key or RADARR_API_KEY
        self.timeout = timeout

    def _enabled(self):
        return bool(self.base_url and self.api_key)

    def _request(self, method, path, params=None, body=None, timeout=None):
        if not self._enabled():
            raise RuntimeError("RADARR_URL or RADARR_API_KEY not configured")
        url = f"{self.base_url}/api/v3{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))

    def root_folder(self):
        """Return configured root folder path, or first one from Radarr if env unset."""
        if RADARR_ROOT_FOLDER:
            return RADARR_ROOT_FOLDER
        folders = self._request("GET", "/rootFolder") or []
        if not folders:
            raise RuntimeError("Radarr has no root folder configured")
        return folders[0]["path"]

    def quality_profile_id(self):
        """Resolve quality profile env to an integer id (env may be a name or id)."""
        profiles = self._request("GET", "/qualityprofile") or []
        if not profiles:
            raise RuntimeError("Radarr has no quality profile configured")
        if RADARR_QUALITY_PROFILE:
            for p in profiles:
                if str(p["id"]) == str(RADARR_QUALITY_PROFILE) or p["name"].lower() == RADARR_QUALITY_PROFILE.lower():
                    return p["id"]
        return profiles[0]["id"]

    def lookup(self, tmdb_id):
        """Return Radarr's metadata for a TMDb id (also tells us if it's already added/has a file)."""
        return self._request("GET", "/movie/lookup/tmdb", params={"tmdbId": tmdb_id})

    def find_existing(self, tmdb_id):
        """If the movie is already in Radarr's library, return its full record (incl. id), else None."""
        movies = self._request("GET", "/movie") or []
        for m in movies:
            if m.get("tmdbId") == tmdb_id:
                return m
        return None

    def add(self, tmdb_id):
        """Add a film to Radarr without auto-grabbing. Returns the new movieId.
        If the film already exists, returns the existing movieId."""
        existing = self.find_existing(tmdb_id)
        if existing:
            return existing["id"]
        meta = self.lookup(tmdb_id)
        if not meta:
            raise RuntimeError(f"TMDb id {tmdb_id} not found in Radarr lookup")
        payload = {
            "title": meta.get("title"),
            "tmdbId": tmdb_id,
            "year": meta.get("year"),
            "titleSlug": meta.get("titleSlug"),
            "images": meta.get("images", []),
            "qualityProfileId": self.quality_profile_id(),
            "rootFolderPath": self.root_folder(),
            "monitored": True,
            "minimumAvailability": "released",
            "addOptions": {
                "searchForMovie": False,  # We want the release list, not auto-grab.
            },
        }
        result = self._request("POST", "/movie", body=payload)
        return result["id"]

    def releases(self, movie_id):
        """GET /release?movieId=<id> — triggers Interactive Search across all indexers.
        Returns the list as Radarr provides it (Radarr already deduplicates)."""
        return self._request("GET", "/release", params={"movieId": movie_id}, timeout=120) or []

    def grab(self, guid, indexer_id):
        """Send the chosen release to the configured download client."""
        return self._request("POST", "/release", body={"guid": guid, "indexerId": indexer_id})

    def trigger_downloaded_movies_scan(self, path):
        """Ask Radarr to rescan a path so Emby/Jellyfin/Radarr pick up a file
        dropped into the library by something other than the *arr stack
        (e.g. our /cerca direct download). Best-effort; failures are logged but
        not fatal — the bot's own scan loop will still notice the new MKV."""
        body = {"name": "DownloadedMoviesScan", "path": path}
        return self._request("POST", "/command", body=body)


# =============================================================================
# MULTI-SOURCE FILM DOWNLOAD (/cerca command — archive.org + YouTube)
# =============================================================================

# Video extensions accepted from archive.org file lists. .webm is downloaded
# but remuxed to .mkv before installation.
_CERCA_VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".m4v", ".avi", ".mov")
# yt-dlp output schema for the search command. One field per token, pipe-delim.
_YTDLP_PRINT_FIELDS = ["title", "id", "duration", "uploader"]
# Bounds to filter out clips, trailers and suspicious uploads. Movies typically
# clock 80-180 minutes; we accept 60-240 to leave headroom for shorts and
# director's cuts.
_CERCA_MIN_DURATION_SEC = 60 * 60
_CERCA_MAX_DURATION_SEC = 240 * 60


def _parse_duration_to_seconds(raw):
    """Accept either an int/float (seconds), 'HH:MM:SS', 'MM:SS', or 'NNN'
    (integer seconds as string). Returns None if the input is unusable."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if ":" in s:
        try:
            parts = [int(p) for p in s.split(":")]
        except ValueError:
            return None
        total = 0
        for part in parts:
            total = total * 60 + part
        return total
    try:
        return int(float(s))
    except ValueError:
        return None


def _format_duration(total_seconds):
    """'1h35m' / '47m' style label for inline buttons and confirmation cards.
    Returns '?' on bad input so callers can interpolate unconditionally."""
    if total_seconds is None or total_seconds < 0:
        return "?"
    secs = int(total_seconds)
    hours, rem = divmod(secs, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours}h{minutes:02d}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _safe_film_dirname(title, year):
    """Plex/Emby/Jellyfin idiomatic folder name: 'Title (Year)'. Strip the few
    filesystem-hostile characters; everything else is left as-is so accented
    Italian titles render correctly."""
    base = title.strip() if title else "Unknown"
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", base).strip()
    if not cleaned:
        cleaned = "Unknown"
    if year:
        return f"{cleaned} ({year})"
    return cleaned


def _archive_metadata_url(identifier):
    return f"https://archive.org/metadata/{urllib.parse.quote(identifier)}"


def _archive_search_url(query, limit):
    fields = ["identifier", "title", "year", "runtime", "mediatype", "item_size"]
    params = [
        ("q", query),
        ("rows", str(limit)),
        ("output", "json"),
    ]
    qs = "&".join([f"fl[]={f}" for f in fields] + [f"{k}={urllib.parse.quote(str(v))}" for k, v in params])
    return f"https://archive.org/advancedsearch.php?{qs}"


def _archive_file_url(identifier, file_name):
    return (
        f"https://archive.org/download/{urllib.parse.quote(identifier)}/"
        f"{urllib.parse.quote(file_name)}"
    )


def _pick_best_archive_file(files):
    """Given the file list of an archive.org item, return (name, size, fmt) for
    the best video file we want to download. Prefers MP4, then MKV, then WEBM.
    Returns None if no usable video is present."""
    if not files:
        return None
    candidates = []
    for entry in files:
        name = entry.get("name") or ""
        if not name:
            continue
        lower = name.lower()
        if not lower.endswith(_CERCA_VIDEO_EXTS):
            continue
        # Skip side-cars and trailers exposed in the same item.
        if "trailer" in lower or "sample" in lower:
            continue
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        fmt = entry.get("format") or ""
        rank = (
            0 if lower.endswith(".mp4") else
            1 if lower.endswith(".mkv") else
            2 if lower.endswith(".m4v") else
            3 if lower.endswith(".webm") else
            4
        )
        # Bigger file = better encode in archive.org's world.
        candidates.append((rank, -size, name, size, fmt))
    if not candidates:
        return None
    candidates.sort()
    _, _, name, size, fmt = candidates[0]
    return {"name": name, "size": size, "format": fmt}


def search_archive_org(title, year=None, limit=10, opener=None):
    """Search the Internet Archive for video items matching `title`.
    Returns a list of dicts: {source, identifier, title, year, duration_sec,
    size, file_name, url}. `opener` is the urlopen callable (injectable for
    tests). Network failures and missing fields degrade to an empty result —
    the caller still has YouTube to fall back on."""
    if not title:
        return []
    opener = opener or urllib.request.urlopen
    q = f'title:("{title}") mediatype:movies'
    if year:
        q += f" year:{year}"
    url = _archive_search_url(q, limit)
    try:
        with opener(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning(f"  archive.org search error: {e}")
        return []

    docs = (data.get("response") or {}).get("docs") or []
    results = []
    for d in docs:
        if d.get("mediatype") != "movies":
            continue
        ident = d.get("identifier")
        if not ident:
            continue
        # Hit /metadata to find the real video file. Without this we can't show
        # size or build a download URL.
        try:
            with opener(_archive_metadata_url(ident), timeout=15) as resp:
                meta = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            log.warning(f"  archive.org metadata error for {ident}: {e}")
            continue
        best = _pick_best_archive_file(meta.get("files") or [])
        if not best:
            continue
        runtime = d.get("runtime")
        duration = _parse_duration_to_seconds(runtime)
        ryear = d.get("year")
        try:
            ryear = int(str(ryear)[:4]) if ryear else None
        except (TypeError, ValueError):
            ryear = None
        results.append({
            "source": "archive",
            "identifier": ident,
            "title": d.get("title") or ident,
            "year": ryear,
            "duration_sec": duration,
            "size": best["size"] or None,
            "file_name": best["name"],
            "format": best["format"],
            "url": _archive_file_url(ident, best["name"]),
            "uploader": None,
        })
        if len(results) >= limit:
            break
    return results


def _ytdlp_binary():
    """Allow tests to swap the binary path without touching env."""
    return os.environ.get("YTDLP_BINARY", "yt-dlp")


def _ytdlp_cookies_args():
    """Return ['--cookies', PATH] when the cookies file exists, otherwise [].
    Keeps yt-dlp invocations valid on dev machines that lack cookies — the
    bot just gets fewer YouTube results, no hard error."""
    if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
        return ["--cookies", YT_COOKIES_FILE]
    return []


def search_youtube(query, limit=8, runner=None):
    """Run `yt-dlp ytsearchN:<query>` and return a filtered list of candidates:
    {source, video_id, title, duration_sec, uploader, url}. `runner` is the
    subprocess.run callable (injectable for tests). Videos shorter than
    `_CERCA_MIN_DURATION_SEC` (likely clips) or longer than
    `_CERCA_MAX_DURATION_SEC` (likely livestreams) are dropped. If yt-dlp is
    missing from PATH or fails, returns []."""
    if not query:
        return []
    import subprocess
    runner = runner or subprocess.run

    print_fmt = "|".join(f"%({field})s" for field in _YTDLP_PRINT_FIELDS)
    cmd = [
        _ytdlp_binary(),
        "--no-warnings",
        "--skip-download",
        "--flat-playlist",
        "--print", print_fmt,
        f"ytsearch{int(limit)}:{query}",
    ] + _ytdlp_cookies_args()

    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        log.warning("  yt-dlp not installed — YouTube search disabled")
        return []
    except Exception as e:
        log.warning(f"  yt-dlp search error: {e}")
        return []

    if proc.returncode != 0:
        log.warning(f"  yt-dlp returned {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
        return []

    out = []
    for raw_line in (proc.stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("|", len(_YTDLP_PRINT_FIELDS) - 1)
        if len(parts) < len(_YTDLP_PRINT_FIELDS):
            continue
        title, vid, dur_raw, uploader = parts
        title = title.strip()
        vid = vid.strip()
        if not vid or vid in ("NA", "None"):
            continue
        duration = _parse_duration_to_seconds(dur_raw if dur_raw not in ("NA", "None") else None)
        if duration is None:
            continue
        if duration < _CERCA_MIN_DURATION_SEC or duration > _CERCA_MAX_DURATION_SEC:
            continue
        out.append({
            "source": "youtube",
            "video_id": vid,
            "title": title or vid,
            "duration_sec": duration,
            "uploader": uploader.strip() if uploader and uploader not in ("NA", "None") else None,
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return out


class CercaDownloadError(RuntimeError):
    """Raised when a multi-source download fails in a recoverable way (network,
    encoder issue, disk full). The handler converts it to a Telegram message
    instead of dumping a stack trace."""


def download_archive_org(url, dest_path, progress_cb=None, opener=None, chunk_size=1024 * 256):
    """Stream a single archive.org file to disk. Calls `progress_cb(downloaded,
    total)` periodically when provided. Returns the destination path on
    success, raises `CercaDownloadError` otherwise."""
    opener = opener or urllib.request.urlopen
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    except OSError as e:
        raise CercaDownloadError(f"cannot create dest dir: {e}") from e
    try:
        with opener(url, timeout=60) as resp:
            total_raw = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
            total = int(total_raw) if total_raw and str(total_raw).isdigit() else None
            downloaded = 0
            with open(dest_path, "wb") as out:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        try:
                            progress_cb(downloaded, total)
                        except Exception:
                            pass
    except Exception as e:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise CercaDownloadError(f"archive.org download failed: {e}") from e
    return dest_path


def download_youtube(video_id, dest_dir, min_resolution=None, progress_cb=None, runner=None):
    """Download a YouTube video via yt-dlp into `dest_dir` and return the
    resulting file path. `min_resolution` constrains the format selector; on
    failure we fall back to "best" so the user is not blocked by an exotic
    upload. Raises `CercaDownloadError` on hard failure (no yt-dlp, non-zero
    exit). `progress_cb(downloaded_bytes, total_bytes)` is fired while parsing
    yt-dlp stdout when running interactively; the tests stub the runner so
    this is best-effort only."""
    if not video_id:
        raise CercaDownloadError("missing video_id")
    import subprocess
    runner = runner or subprocess.run

    os.makedirs(dest_dir, exist_ok=True)
    target_height = int(min_resolution if min_resolution else CERCA_MIN_RESOLUTION)
    fmt = (
        f"bv*[height>={target_height}]+ba/b[height>={target_height}]"
        f"/bv*[height>={CERCA_FALLBACK_RESOLUTION}]+ba/b"
    )
    out_template = os.path.join(dest_dir, "%(id)s.%(ext)s")
    cmd = [
        _ytdlp_binary(),
        "--no-warnings",
        "--no-playlist",
        "-f", fmt,
        "--merge-output-format", "mkv",
        "-o", out_template,
        "--print", "after_move:filepath",
        f"https://www.youtube.com/watch?v={video_id}",
    ] + _ytdlp_cookies_args()

    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=60 * 60)
    except FileNotFoundError as e:
        raise CercaDownloadError("yt-dlp not installed") from e
    except Exception as e:
        raise CercaDownloadError(f"yt-dlp invocation failed: {e}") from e

    if proc.returncode != 0:
        raise CercaDownloadError(
            f"yt-dlp exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}"
        )

    final_path = None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        # `after_move:filepath` print yields just the final on-disk path. We
        # iterate from the bottom to ignore any progress noise above.
        if os.path.isabs(line) and os.path.exists(line):
            final_path = line
            break
    if not final_path:
        # Last-resort scan of the dest dir; yt-dlp may print the path through a
        # different channel on older builds.
        for name in os.listdir(dest_dir):
            if name.startswith(video_id):
                cand = os.path.join(dest_dir, name)
                if os.path.isfile(cand):
                    final_path = cand
                    break
    if not final_path or not os.path.exists(final_path):
        raise CercaDownloadError("yt-dlp finished but no output file found")
    return final_path


def remux_to_mkv(src_path, dest_path, runner=None):
    """Run `ffmpeg -i src -c copy dest` to remux the container to MKV without
    re-encoding. ~3 seconds for a typical 90-minute video. On success, removes
    the source. On failure, keeps the source intact and raises
    `CercaDownloadError`."""
    if not os.path.isfile(src_path):
        raise CercaDownloadError(f"source not found: {src_path}")
    if src_path == dest_path:
        return src_path
    import subprocess
    runner = runner or subprocess.run

    cmd = ["ffmpeg", "-y", "-i", src_path, "-c", "copy", dest_path]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=15 * 60)
    except FileNotFoundError as e:
        raise CercaDownloadError("ffmpeg not installed") from e
    except Exception as e:
        raise CercaDownloadError(f"ffmpeg invocation failed: {e}") from e

    if proc.returncode != 0:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise CercaDownloadError(
            f"ffmpeg exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}"
        )

    try:
        os.remove(src_path)
    except OSError as e:
        log.warning(f"  remux ok but could not remove source: {e}")
    return dest_path


def install_to_library(src_mkv, title, year, library_dir=None, radarr_scan=True):
    """Move (or rename) `src_mkv` into '<library_dir>/<Title (Year)>/<Title
    (Year)>.mkv'. Triggers a Radarr rescan so the *arr stack picks the file up
    immediately. Returns the final on-disk path. Raises `CercaDownloadError`
    if the destination is not writable."""
    if not os.path.isfile(src_mkv):
        raise CercaDownloadError(f"source not found: {src_mkv}")
    library_dir = library_dir or CERCA_DOWNLOAD_DIR
    folder_name = _safe_film_dirname(title, year)
    dest_folder = os.path.join(library_dir, folder_name)
    try:
        os.makedirs(dest_folder, exist_ok=True)
    except OSError as e:
        raise CercaDownloadError(f"library not writable ({library_dir}): {e}") from e
    dest_file = os.path.join(dest_folder, f"{folder_name}.mkv")
    try:
        # Same-fs case: cheap rename. Cross-fs case: shutil.move falls back to
        # copy+unlink under the hood, which is what we want for /media bind
        # mounts living on a different volume than /tmp.
        import shutil as _shutil
        _shutil.move(src_mkv, dest_file)
    except OSError as e:
        raise CercaDownloadError(f"failed to install file: {e}") from e

    if radarr_scan:
        try:
            RadarrClient().trigger_downloaded_movies_scan(dest_folder)
        except Exception as e:
            # Radarr may be unreachable or the bot may run without the env
            # vars set; the file is already in place so the bot's own scan
            # loop will still notice it.
            log.warning(f"  Radarr scan trigger failed (file still installed): {e}")
    return dest_file


# Language registry: ISO 639-2/T 3-letter code -> spec used by detection,
# normalization and post-grab verification.
#
#   tokens:     uppercase tokens that appear in release titles for this language.
#               Match is word-boundary by construction (release titles are split
#               on non-alpha chars before lookup), so "IT" never matches WHITE,
#               LIMIT, HOBBIT or ITUNES.
#   tmdb_iso1:  ISO 639-1 code as returned by TMDb's `original_language` field.
#   flag:       Telegram flag/emoji used in user-facing strings.
#   verify:     True iff a release tagged with this code is multi-track
#               ("MULTI", "DUAL"...) and must be verified post-grab via ffprobe
#               against the real target language. Single-language codes have
#               verify=False.
LANGUAGE_REGISTRY = {
    "ITA": {"tokens": ["ITA", "ITALIAN", "IT"], "tmdb_iso1": "it", "flag": "🇮🇹", "verify": False},
    "ENG": {"tokens": ["ENG", "ENGLISH", "EN"], "tmdb_iso1": "en", "flag": "🇬🇧", "verify": False},
    "JPN": {"tokens": ["JPN", "JAP", "JAPANESE", "JP", "JA"], "tmdb_iso1": "ja", "flag": "🇯🇵", "verify": False},
    "KOR": {"tokens": ["KOR", "KOREAN", "KO"], "tmdb_iso1": "ko", "flag": "🇰🇷", "verify": False},
    "FRA": {"tokens": ["FRA", "FRE", "FRENCH", "FR"], "tmdb_iso1": "fr", "flag": "🇫🇷", "verify": False},
    "GER": {"tokens": ["GER", "DEU", "GERMAN", "DE"], "tmdb_iso1": "de", "flag": "🇩🇪", "verify": False},
    "SPA": {"tokens": ["SPA", "ESP", "SPANISH", "CASTELLANO", "ES"], "tmdb_iso1": "es", "flag": "🇪🇸", "verify": False},
    "CHI": {"tokens": ["CHI", "CHN", "CHINESE", "MAN", "MANDARIN", "ZH"], "tmdb_iso1": "zh", "flag": "🇨🇳", "verify": False},
    "RUS": {"tokens": ["RUS", "RUSSIAN", "RU"], "tmdb_iso1": "ru", "flag": "🇷🇺", "verify": False},
    "HIN": {"tokens": ["HIN", "HINDI"], "tmdb_iso1": "hi", "flag": "🇮🇳", "verify": False},
    "POR": {"tokens": ["POR", "PORTUGUESE", "PORTUGUES", "BRA"], "tmdb_iso1": "pt", "flag": "🇵🇹", "verify": False},
    "TUR": {"tokens": ["TUR", "TURKISH"], "tmdb_iso1": "tr", "flag": "🇹🇷", "verify": False},
    "POL": {"tokens": ["POL", "POLISH"], "tmdb_iso1": "pl", "flag": "🇵🇱", "verify": False},
    "ARA": {"tokens": ["ARA", "ARABIC"], "tmdb_iso1": "ar", "flag": "🇸🇦", "verify": False},
    "HEB": {"tokens": ["HEB", "HEBREW"], "tmdb_iso1": "he", "flag": "🇮🇱", "verify": False},
    "THA": {"tokens": ["THA", "THAI"], "tmdb_iso1": "th", "flag": "🇹🇭", "verify": False},
    "NLD": {"tokens": ["NLD", "DUT", "DUTCH"], "tmdb_iso1": "nl", "flag": "🇳🇱", "verify": False},
    "SWE": {"tokens": ["SWE", "SWEDISH"], "tmdb_iso1": "sv", "flag": "🇸🇪", "verify": False},
    "DAN": {"tokens": ["DAN", "DANISH"], "tmdb_iso1": "da", "flag": "🇩🇰", "verify": False},
    "NOR": {"tokens": ["NOR", "NORWEGIAN"], "tmdb_iso1": "no", "flag": "🇳🇴", "verify": False},
    "FIN": {"tokens": ["FIN", "FINNISH"], "tmdb_iso1": "fi", "flag": "🇫🇮", "verify": False},
    "HUN": {"tokens": ["HUN", "HUNGARIAN"], "tmdb_iso1": "hu", "flag": "🇭🇺", "verify": False},
    "CZE": {"tokens": ["CZE", "CES", "CZECH"], "tmdb_iso1": "cs", "flag": "🇨🇿", "verify": False},
    "RON": {"tokens": ["RON", "ROM", "ROMANIAN"], "tmdb_iso1": "ro", "flag": "🇷🇴", "verify": False},
    "GRE": {"tokens": ["GRE", "ELL", "GREEK"], "tmdb_iso1": "el", "flag": "🇬🇷", "verify": False},
    "UKR": {"tokens": ["UKR", "UKRAINIAN"], "tmdb_iso1": "uk", "flag": "🇺🇦", "verify": False},
    "VIE": {"tokens": ["VIE", "VIETNAMESE"], "tmdb_iso1": "vi", "flag": "🇻🇳", "verify": False},
    "IND": {"tokens": ["IND", "INDONESIAN"], "tmdb_iso1": "id", "flag": "🇮🇩", "verify": False},
    "MULTI": {"tokens": ["MULTI", "MULTILANG"], "tmdb_iso1": None, "flag": "🌐", "verify": True},
    "DUAL": {"tokens": ["DUAL", "DUALAUDIO"], "tmdb_iso1": None, "flag": "🎭", "verify": True},
}


# Build reverse-lookups once at import time.
_TOKEN_TO_CODE = {tok: code for code, spec in LANGUAGE_REGISTRY.items() for tok in spec["tokens"]}
_TMDB_ISO1_TO_CODE = {spec["tmdb_iso1"]: code for code, spec in LANGUAGE_REGISTRY.items() if spec["tmdb_iso1"]}


def normalize_lang_input(raw):
    """Normalize a user/CLI language token to a 3-letter ISO code from
    `LANGUAGE_REGISTRY`. Accepts 2-letter codes ('it', 'EN'), 3-letter codes
    ('ita', 'ENG'), or full names ('italian', 'JAPANESE').
    Returns the canonical 3-letter code (e.g. 'ITA') or None if unknown."""
    if not raw:
        return None
    token = re.sub(r"[^A-Za-z]", "", str(raw)).upper()
    if not token:
        return None
    # Direct hit on the token table (covers 2-letter, 3-letter and full names).
    code = _TOKEN_TO_CODE.get(token)
    if code:
        return code
    # The registry keys themselves are valid inputs ("MULTI", "DUAL").
    if token in LANGUAGE_REGISTRY:
        return token
    return None


def detect_release_languages(release):
    """Return a sorted list of language tags (e.g. ['ITA', 'ENG']) for a Radarr release.
    Prefers Radarr's structured `languages` field; falls back to word-boundary
    matches on the release title (releases like 'Movie.2024.iTALiAN.ENG.1080p.x265.mkv')."""
    tags = set()
    for lang in release.get("languages", []) or []:
        name = (lang.get("name") or "").strip()
        code = normalize_lang_input(name)
        if code:
            tags.add(code)
        elif name and name.lower() != "unknown":
            tags.add(name[:3].upper())

    title = release.get("title") or ""
    # Split on non-alpha so "ENG" never matches "ENGAGE" and "IT" never matches
    # "WHITE", "LIMIT", "HOBBIT" or "ITUNES".
    upper_title = re.sub(r"[^A-Z]+", " ", title.upper())
    title_tokens = set(upper_title.split())
    for token in title_tokens:
        code = _TOKEN_TO_CODE.get(token)
        if code:
            tags.add(code)

    if not tags:
        tags.add("?")
    return sorted(tags)


def _flag_for(lang_tags):
    """Pick a single flag emoji that represents the language mix of a release.
    Preference order: ITA, then any single-language registry entry, then MULTI,
    DUAL, unknown."""
    if "ITA" in lang_tags:
        return LANGUAGE_REGISTRY["ITA"]["flag"]
    for code in lang_tags:
        spec = LANGUAGE_REGISTRY.get(code)
        if spec and not spec["verify"]:
            return spec["flag"]
    if "MULTI" in lang_tags:
        return LANGUAGE_REGISTRY["MULTI"]["flag"]
    if "DUAL" in lang_tags:
        return LANGUAGE_REGISTRY["DUAL"]["flag"]
    if lang_tags == ["?"]:
        return "❔"
    return "🏳️"


def extract_lang_flag(text):
    """Parse `--lang CODE` (or `--lang=CODE`) from anywhere in `text`. Returns
    `(remaining_text, normalized_code_or_None, raw_token_or_None)`. The flag is
    removed from the remaining text so the caller can use it as the bare query.
    `raw_token` is what the user actually typed (for error messages)."""
    if not text:
        return text, None, None
    # Match `--lang CODE` (space-separated) or `--lang=CODE`. Be liberal about
    # case in the flag itself; the value is normalized via normalize_lang_input.
    pattern = re.compile(r"\s*--lang(?:=|\s+)([A-Za-z]{2,})\b", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return text.strip(), None, None
    raw = match.group(1)
    code = normalize_lang_input(raw)
    cleaned = (text[:match.start()] + " " + text[match.end():]).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned, code, raw


# -----------------------------------------------------------------------------
# Indexer audio scrapers
# -----------------------------------------------------------------------------
# When the release-name parser cannot disambiguate (MULTi alone, DUAL+X, etc.)
# we may probe the indexer's public metadata for the actual audio-track list.
# All scrapers are best-effort: any network/parse failure returns None and the
# caller falls back to the parser score. A successful scrape returns a SET of
# 3-letter codes from LANGUAGE_REGISTRY plus a special "_UNKNOWN" sentinel when
# at least one audio track was named with a language we cannot map (so we know
# the scrape worked but we lack a mapping).

_SCRAPE_TIMEOUT = 8  # seconds, hard cap


def _normalize_language_word(word):
    """Map a single English/Italian language word to its 3-letter ISO code.

    Returns the registry code (e.g. "ENG") for recognised tokens. Returns None
    for unknown words and for the meta-tokens MULTI / DUAL so callers can use
    this as a one-shot filter while scraping payload text. The token lookup
    is case-insensitive."""
    if not word:
        return None
    code = _TOKEN_TO_CODE.get(word.upper())
    if code and code not in {"MULTI", "DUAL"}:
        return code
    return None


def _scrape_audio_from_mediainfo_text(text):
    """Parse a MediaInfo / NFO-style dump and extract audio-track languages.

    Strict: only inspects lines that look like a structured `Audio:` /
    `Language:` header (key followed by `:` followed by a short payload).
    The payload is parsed as a comma/slash/plus/ampersand-separated list of
    language names — narrative prose around the keyword is ignored.

    Defensive choices:
      - The keyword (`Audio` / `Language`) must be the FIRST alphabetic word
        on the line (after optional decoration punctuation). This prevents
        false matches on lines like `Format - AAC` or `Note: ... Hindi Fan
        Dubbed ...` (the latter would have matched the old loose regex).
      - The payload is capped at ~120 characters so the parser cannot wander
        into the storyline.
      - Words must occur in the LANGUAGE_REGISTRY token set; spurious words
        (`Dubbed`, `Fan`, `Quality`) are silently ignored.
      - MULTI / DUAL markers found in the payload are filtered out so the
        return set always contains concrete language codes.

    Returns a set of 3-letter codes (possibly empty when no structured header
    is present)."""
    if not text:
        return set()
    found = set()
    # Two strict patterns:
    #   `Audio` or `Language` as the first alphabetic word on the line.
    #   Followed by optional space/punctuation chars.
    #   Then a literal `:`.
    #   Then a payload no longer than 120 chars and stopping at end-of-line.
    patterns = [
        re.compile(r"^[\s\W]*Audio\b[^:\n]{0,40}:\s*([^\n]{0,120})", re.IGNORECASE | re.MULTILINE),
        re.compile(r"^[\s\W]*Language\b[^:\n]{0,40}:\s*([^\n]{0,120})", re.IGNORECASE | re.MULTILINE),
    ]
    for pat in patterns:
        for match in pat.finditer(text):
            payload = match.group(1)
            # Treat the payload as a list separated by /, |, ,, +, &.
            for chunk in re.split(r"[/|,+&]", payload):
                # A chunk is one audio track description; the language name is
                # the FIRST recognised token in it (other words are codec / qty
                # / extras like `E-AC-3 5.1` or `[HQ Fan Dubbed]`).
                for w in re.findall(r"[A-Za-z]+", chunk):
                    code = _normalize_language_word(w)
                    if code:
                        found.add(code)
                        break
    return found


def _scrape_audio_tpb(info_url):
    """Pirate Bay description scraper via apibay.org JSON API.

    `info_url` is the Radarr-supplied URL, e.g.
    `https://thepiratebay.org/description.php?id=47222815`. Extracts the torrent
    id and queries `https://apibay.org/t.php?id=<id>` for structured metadata
    (which includes the upload `descr` field that often embeds MediaInfo)."""
    m = re.search(r"[?&]id=(\d+)", info_url or "")
    if not m:
        return None
    tid = m.group(1)
    api_url = f"https://apibay.org/t.php?id={tid}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_SCRAPE_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        descr = payload.get("descr") or ""
        return _scrape_audio_from_mediainfo_text(descr)
    except Exception as e:
        log.debug(f"  tpb scrape failed for id={tid}: {e}")
        return None


def _scrape_audio_1337x(info_url):
    """1337x torrent description scraper.

    1337x publishes torrent pages at `https://1337x.to/torrent/<id>/<slug>/`
    (and variants on mirror domains such as `1337x.unblocked.lc`). Pages are
    plain HTML, no auth or JS required for the metadata section we care
    about. We GET the page, look for a `<strong>Language</strong>` or
    `<strong>Audio</strong>` block in the metadata `<ul class="list">`, and
    fall back to the generic MediaInfo parser on the full body if no block
    is found."""
    if not info_url:
        return None
    try:
        req = urllib.request.Request(info_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_SCRAPE_TIMEOUT) as resp:
            html = resp.read(200_000).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"  1337x scrape failed for {info_url}: {e}")
        return None
    found = set()
    # Pattern: `<strong>Language</strong>\s*<span>English</span>` (or `Audio`).
    # The block is short — cap payload at 120 chars per the parser convention.
    label_re = re.compile(
        r"<strong>\s*(?:Language|Audio)\s*</strong>[^<]{0,40}<span[^>]*>([^<]{0,120})</span>",
        re.IGNORECASE,
    )
    for match in label_re.finditer(html):
        for w in re.findall(r"[A-Za-z]+", match.group(1)):
            code = _normalize_language_word(w)
            if code:
                found.add(code)
    if found:
        return found
    # Fallback: maybe the description embeds a MediaInfo dump.
    mi = _scrape_audio_from_mediainfo_text(html)
    return mi if mi else None


def _scrape_audio_yts(info_url, release=None):
    """YTS movie page scraper.

    YTS uploads are an English-only library by editorial policy, so the
    presence of a live YTS page is a sufficient signal to report `{"ENG"}`.
    We probe the page (or the JSON API when an IMDb id is available on the
    release) just to make sure the listing actually exists — a network or
    HTTP error returns None so the caller falls back to the parser."""
    if not info_url:
        return None
    probe_url = info_url
    if release:
        imdb_id = release.get("imdbId") or release.get("imdb_id")
        if imdb_id:
            probe_url = f"https://yts.mx/api/v2/movie_details.json?imdb_id={imdb_id}"
    try:
        req = urllib.request.Request(probe_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_SCRAPE_TIMEOUT) as resp:
            # Read a small chunk just to confirm the response is real.
            resp.read(1024)
    except Exception as e:
        log.debug(f"  yts scrape failed for {probe_url}: {e}")
        return None
    return {"ENG"}


def _scrape_audio_torrentgalaxy(info_url):
    """TorrentGalaxy / tgx.rs scraper.

    Public HTML torrent pages. The description usually embeds a MediaInfo
    dump verbatim, so we delegate to `_scrape_audio_from_mediainfo_text` on
    the raw response body."""
    if not info_url:
        return None
    try:
        req = urllib.request.Request(info_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_SCRAPE_TIMEOUT) as resp:
            html = resp.read(200_000).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"  torrentgalaxy scrape failed for {info_url}: {e}")
        return None
    found = _scrape_audio_from_mediainfo_text(html)
    return found if found else None


def _scrape_audio_generic(info_url):
    """Generic HTML scraper: GET the info_url and run the MediaInfo regex on the
    raw response. Best-effort, may not work for sites that gate behind JS or
    auth."""
    if not info_url:
        return None
    try:
        req = urllib.request.Request(info_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_SCRAPE_TIMEOUT) as resp:
            html = resp.read(200_000).decode("utf-8", errors="replace")
        found = _scrape_audio_from_mediainfo_text(html)
        return found if found else None
    except Exception as e:
        log.debug(f"  generic scrape failed for {info_url}: {e}")
        return None


def scrape_release_audio_languages(release):
    """Best-effort: probe the release's indexer page for the real audio-track
    list. Returns a set of 3-letter codes, or None when no scraper applies or
    the probe failed."""
    info_url = release.get("infoUrl") or ""
    if not info_url:
        return None
    host_lower = info_url.lower()
    if "thepiratebay" in host_lower or "/piratebay" in host_lower:
        result = _scrape_audio_tpb(info_url)
    elif "1337x" in host_lower:
        result = _scrape_audio_1337x(info_url)
    elif "yts.mx" in host_lower or "yts.ag" in host_lower or "yts.lt" in host_lower:
        result = _scrape_audio_yts(info_url, release)
    elif "torrentgalaxy" in host_lower or "tgx.rs" in host_lower:
        result = _scrape_audio_torrentgalaxy(info_url)
    else:
        result = None
    if result is not None:
        return result
    # Generic fallback for indexers that expose audio in plain HTML.
    return _scrape_audio_generic(info_url)


# -----------------------------------------------------------------------------
# Persistent audio-language cache (JSON on disk, TTL'd).
# -----------------------------------------------------------------------------
# Survives container restarts so repeat scrapes of the same release do not
# re-hit the network. Keyed by infoHash (or guid as a fallback). Entries older
# than _AUDIO_CACHE_TTL_DAYS are dropped at load + pruned at save.

_AUDIO_CACHE_TTL_DAYS = 30
# Counter used to throttle save() to once every N dirtying scrapes. The
# release_audio_cache file is not a hot path; a 10-event coalesce keeps the
# disk write rate sane during a bulk download.
_AUDIO_CACHE_DIRTY_SAVE_EVERY = 10
_audio_cache_dirty_count = 0


def _audio_cache_now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _audio_cache_entry_is_fresh(ts_iso):
    if not ts_iso:
        return False
    try:
        ts = datetime.fromisoformat(ts_iso)
    except (TypeError, ValueError):
        return False
    return datetime.now() - ts <= timedelta(days=_AUDIO_CACHE_TTL_DAYS)


def load_release_audio_cache():
    """Load the on-disk audio-language cache. Stale entries (older than TTL)
    are silently dropped. Returns a dict keyed by infoHash mapping to a set
    of 3-letter codes (or None when the cached scrape failed)."""
    if not os.path.exists(AUDIO_CACHE_FILE):
        return {}
    try:
        with open(AUDIO_CACHE_FILE, "r") as f:
            raw = json.load(f)
    except Exception as e:
        log.debug(f"  failed to load audio cache: {e}")
        return {}
    out = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        if not _audio_cache_entry_is_fresh(entry.get("ts")):
            continue
        audio = entry.get("audio")
        if audio is None:
            out[key] = None
        elif isinstance(audio, list):
            out[key] = set(audio)
        # ignore malformed entries
    return out


def save_release_audio_cache(cache):
    """Serialise the in-memory audio-language cache to disk. Sets are written
    as sorted lists so the JSON round-trip is deterministic; None means
    "scrape ran but produced no usable result" and is preserved verbatim.
    Stale entries are pruned on save."""
    payload = {}
    now = _audio_cache_now_iso()
    for key, value in cache.items():
        if not key:
            continue
        if value is None:
            payload[key] = {"audio": None, "ts": now}
        elif isinstance(value, (set, frozenset)):
            payload[key] = {"audio": sorted(value), "ts": now}
        elif isinstance(value, list):
            payload[key] = {"audio": sorted(set(value)), "ts": now}
        # silently skip unsupported types
    try:
        with open(AUDIO_CACHE_FILE, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        log.debug(f"  failed to save audio cache: {e}")


# In-process cache: maps release infoHash to a set of audio language codes (or
# None when the scrape failed). Keeps the picker snappy when a user paginates
# back and forth or runs `/scarica` twice on the same film.
_AUDIO_SCRAPE_CACHE = {}


def _confidence_description(release, target_code):
    """Italian-language description of how likely `release` contains audio in
    `target_code`. Used in the confirmation card and the release picker
    legend. Returns "" when no target is set."""
    if not target_code:
        return ""
    flag = LANGUAGE_REGISTRY.get(target_code, {}).get("flag", "")
    target_label = f"{flag} {target_code}".strip()
    score = score_release_language_confidence(release, target_code)
    scraped = None
    if score is not None:
        scraped = get_release_audio_languages(release)
    if score is None:
        return f"❌ Audio {target_label}: improbabile (nessun indizio)"
    if score == 1000:
        if scraped and target_code in scraped:
            scraped_str = "+".join(sorted(scraped))
            return f"✅ Audio {target_label}: confermato dallo scrape (tracce: {scraped_str})"
        return f"✅ Audio {target_label}: presente nel nome del rilascio"
    if score == _SCRAPE_REFUTED_SCORE:
        scraped_str = "+".join(sorted(scraped)) if scraped else "?"
        return (
            f"🚫 Audio {target_label}: lo scrape ha trovato solo {scraped_str}, "
            f"probabilmente non presente. Potresti procedere comunque se sai che "
            f"il dato dello scrape è incompleto."
        )
    if score == 600:
        return f"⚠️ Audio {target_label}: rilascio MULTI generico, potrebbe contenerlo"
    if score == 500:
        return f"⚠️ Audio {target_label}: rilascio DUAL generico, potrebbe contenerlo"
    if score == 400:
        return f"❓ Audio {target_label}: MULTI con altre lingue dichiarate, possibile su traccia non disclosed"
    if score == 200:
        return f"❓ Audio {target_label}: DUAL con altra lingua già dichiarata, bassa probabilità"
    return f"❓ Audio {target_label}: probabilità incerta (score {score})"


def _picker_confidence_legend(releases, target_code, include_refuted=None):
    """When at least one release in the picker has an ambiguity marker, prepend
    a one-line legend explaining the emojis. Otherwise return "".

    `include_refuted` lets the caller force-include the refuted marker even
    when no visible release carries it (e.g. when refuted releases are
    collapsed under a toggle and we still want to explain the legend). When
    None (default) the presence of the marker is inferred from `releases`
    only."""
    if not target_code:
        return ""
    has_warn = has_quest = has_block = False
    for rel in releases:
        s = score_release_language_confidence(rel, target_code)
        if s is None or s == 1000:
            continue
        if s <= 100:
            has_block = True
        elif s < 500:
            has_quest = True
        else:
            has_warn = True
    if include_refuted is True:
        has_block = True
    elif include_refuted is False:
        has_block = False
    if not (has_warn or has_quest or has_block):
        return ""
    parts = []
    if has_warn:
        parts.append("⚠️ probabile")
    if has_quest:
        parts.append("❓ incerto")
    if has_block:
        parts.append("🚫 lo scrape dice no")
    return "<i>Legenda: " + " · ".join(parts) + "</i>\n"


def get_release_audio_languages(release):
    """Cached wrapper around `scrape_release_audio_languages`. Keyed by
    infoHash (or guid as a fallback). Layered cache:

      1. in-process dict `_AUDIO_SCRAPE_CACHE` (hot path, sub-microsecond)
      2. JSON file `/config/release_audio_cache.json` (survives container
         restarts; TTL = 30 days)
      3. real network scrape (slow path)

    On a JSON hit we also populate the in-process cache. On a real scrape we
    update both layers and persist after every N dirtying writes."""
    global _audio_cache_dirty_count
    key = release.get("infoHash") or release.get("guid")
    if not key:
        return scrape_release_audio_languages(release)
    if key in _AUDIO_SCRAPE_CACHE:
        return _AUDIO_SCRAPE_CACHE[key]
    persistent = load_release_audio_cache()
    if key in persistent:
        _AUDIO_SCRAPE_CACHE[key] = persistent[key]
        return persistent[key]
    result = scrape_release_audio_languages(release)
    _AUDIO_SCRAPE_CACHE[key] = result
    persistent[key] = result
    _audio_cache_dirty_count += 1
    if _audio_cache_dirty_count >= _AUDIO_CACHE_DIRTY_SAVE_EVERY:
        _audio_cache_dirty_count = 0
        save_release_audio_cache(persistent)
    else:
        # Best-effort write so a crash before the threshold still persists.
        # save_release_audio_cache already swallows IO errors silently.
        save_release_audio_cache(persistent)
    return result


# Cap concurrent indexer scrapes. The work is I/O-bound; 4 is gentle enough not
# to anger the indexer hosts while still cutting wall time on a typical batch
# of 20-30 releases.
_PREFETCH_MAX_WORKERS = 4
# Minimum gap between Telegram edits during the prefetch. Telegram rate-limits
# edits at ~1/sec/chat; the final completion edit bypasses this throttle.
_PREFETCH_EDIT_INTERVAL = 1.0
# Skip the "verifica completata" interlude when the prefetch finished in well
# under this many seconds — otherwise it just flashes and feels like a glitch.
_PREFETCH_INTERLUDE_MIN_SECONDS = 2.0
_PREFETCH_INTERLUDE_HOLD_SECONDS = 0.5


def _prefetch_audio_scrapes(releases, progress_msg_id, target_lang,
                            title_label="", max_workers=_PREFETCH_MAX_WORKERS):
    """Warm `_AUDIO_SCRAPE_CACHE` for releases whose audio language is ambiguous
    relative to `target_lang`, showing a Telegram progress bar as scrapes
    complete.

    Filters out releases that either already declare the target explicitly (no
    scrape needed) or are already cached, then runs the surviving HTTP GETs in
    a small thread pool. The pool is sized to `_PREFETCH_MAX_WORKERS` so we
    stay polite to the indexer hosts; per-task exceptions are swallowed so a
    single bad page never breaks the batch.

    Telegram edits are throttled to `_PREFETCH_EDIT_INTERVAL` seconds with the
    final completion always emitted, regardless of throttle.

    Returns the count of scrapes actually executed. When the target language
    is None / falsy, the release list is empty, or every release is already
    cached, this function is a no-op and returns 0 without any Telegram
    traffic."""
    if not target_lang or not releases:
        return 0
    pending = []
    for rel in releases:
        if target_lang in detect_release_languages(rel):
            continue
        key = rel.get("infoHash") or rel.get("guid")
        if not key:
            continue
        if key in _AUDIO_SCRAPE_CACHE:
            continue
        pending.append(rel)
    if not pending:
        return 0

    total = len(pending)
    completed = 0
    last_edit = 0.0
    header = (
        f"🔎 Verifico audio sugli indexer per <b>{html.escape(title_label)}</b>…"
        if title_label
        else "🔎 Verifico audio sugli indexer…"
    )

    def _safe_scrape(rel):
        try:
            return get_release_audio_languages(rel)
        except Exception:
            logging.warning(
                "prefetch scrape failed for guid=%s", rel.get("guid"),
                exc_info=True,
            )
            return None

    def _render(done):
        return f"{header}\n{_progress_bar(done, total)}\n{done}/{total} rilasci"

    if progress_msg_id is not None:
        tg_edit_message(progress_msg_id, _render(0))
        last_edit = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_safe_scrape, rel) for rel in pending]
        for _ in as_completed(futures):
            completed += 1
            if progress_msg_id is None:
                continue
            now = time.time()
            is_final = completed == total
            if is_final or (now - last_edit) >= _PREFETCH_EDIT_INTERVAL:
                tg_edit_message(progress_msg_id, _render(completed))
                last_edit = now

    return total


def _run_prefetch_with_interlude(releases, progress_msg_id, target_lang,
                                 title_label=""):
    """Run `_prefetch_audio_scrapes` and, when the prefetch actually did real
    work and took non-trivial time, briefly show a "verifica completata"
    interlude before the caller renders the picker. Hides UI flicker when the
    prefetch returns immediately."""
    started = time.time()
    fetched = _prefetch_audio_scrapes(
        releases, progress_msg_id, target_lang, title_label=title_label,
    )
    if not fetched or progress_msg_id is None:
        return
    if (time.time() - started) < _PREFETCH_INTERLUDE_MIN_SECONDS:
        return
    tg_edit_message(
        progress_msg_id,
        "✅ Verifica completata, costruisco il picker…",
    )
    time.sleep(_PREFETCH_INTERLUDE_HOLD_SECONDS)


# Score floor used when the scraper actively refutes the target. The release is
# kept (gate-open policy: false positives are cheaper than missing the right
# release) but pushed to the bottom of the picker.
_SCRAPE_REFUTED_SCORE = 50


def _partition_visible_releases(releases, target_code, show_refuted):
    """Split `releases` into the ones the picker should show by default and a
    count of refuted-score releases that are being hidden.

    With a `target_code` set, the picker collapses MULTI/DUAL releases the
    scrape actively refuted (score == _SCRAPE_REFUTED_SCORE) under a toggle so
    the default view stays focused on plausible matches. When `show_refuted`
    is True every release is visible and `refuted_count` reports how many of
    them carry the refuted score.

    Edge case: when the filter would collapse the entire list (no visible
    release), fall back to showing everything so the user is never left with
    an empty picker.

    Returns (visible_releases, refuted_count). `target_code=None` -> no
    partitioning: the full list is visible and refuted_count is 0."""
    if not target_code:
        return list(releases), 0

    if show_refuted:
        refuted_count = sum(
            1 for rel in releases
            if score_release_language_confidence(rel, target_code) == _SCRAPE_REFUTED_SCORE
        )
        return list(releases), refuted_count

    visible = []
    refuted_count = 0
    for rel in releases:
        score = score_release_language_confidence(rel, target_code)
        if score == _SCRAPE_REFUTED_SCORE:
            refuted_count += 1
        else:
            visible.append(rel)

    if not visible and refuted_count > 0:
        return list(releases), refuted_count

    return visible, refuted_count


def score_release_language_confidence(release, target_code, allow_scrape=True):
    """Return a confidence score 0..1000 that `release` contains an audio track
    in `target_code`. Higher = more likely. Returns None ONLY when the release
    is single-language and clearly non-target (e.g. a release tagged ENG with
    no MULTI/DUAL marker and no scrape confirming target).

    Gate-open policy: ambiguous releases are surfaced with a low score and a
    UI marker, never silently dropped. Post-grab ffprobe is the final gate
    and the audio-mismatch flow lets the user re-pick.

    Score scale:
      1000  target_code explicitly declared OR confirmed by indexer audio scrape
       600  MULTI marker alone, no other concrete language declared
       500  DUAL marker alone, no other concrete language declared
       400  MULTI + one or more other concrete languages declared
       200  DUAL + another concrete language declared
        50  scraper actively refuted the target (audio scrape was non-empty
            and the target was not in it). Kept because scrapes can be
            partial (subs vs audio confusion, missing tracks on the page,
            wrong MediaInfo block).
      None  single-language release with a non-target language AND no scrape
            confirmation (the only case we still hide outright; otherwise a
            user looking for ITA would see every pure-ENG/FRA/HIN release).

    When `allow_scrape` is True (default) and the parser would assign a score
    in the ambiguous 200..600 range OR the release looks single-language
    non-target, the function consults `get_release_audio_languages` to fetch
    the real audio-track list. If the scrape returns:
      target in audio    → bumped to 1000 (confirmed)
      audio non-empty
        and target absent → floored to 50 (refuted but kept)
      anything else      → keep the parser score (scrape inconclusive)."""
    if not target_code:
        return None
    langs = set(detect_release_languages(release))
    if not langs:
        return None
    if target_code in langs:
        return 1000
    markers = {"MULTI", "DUAL"}
    has_marker = bool(langs & markers)
    concrete_others = langs - markers
    # Compute parser baseline first.
    if has_marker:
        if not concrete_others:
            base = 600 if "MULTI" in langs else 500
        elif "MULTI" in langs:
            base = 400
        else:
            base = 200
    else:
        # Single-language, non-target release. We will only keep it if the
        # scrape confirms target.
        base = None
    if allow_scrape:
        scraped = get_release_audio_languages(release)
        if scraped:
            if target_code in scraped:
                return 1000
            # Scrape produced concrete languages but target is not among them.
            # Gate-open: keep with floor score so the user can still pick it
            # if they have ground-truth knowledge the scrape missed.
            return _SCRAPE_REFUTED_SCORE
    return base


def filter_releases_by_language(releases, target_code):
    """Return releases that plausibly contain the target language, sorted by
    descending language-confidence score (see `score_release_language_confidence`).

    `target_code` is a normalized 3-letter ISO code from LANGUAGE_REGISTRY, or
    None (no filtering — returns the input unchanged)."""
    if not target_code:
        return list(releases)
    scored = []
    for rel in releases:
        score = score_release_language_confidence(rel, target_code)
        if score is not None:
            scored.append((score, rel))
    scored.sort(key=lambda pair: -pair[0])
    return [rel for _, rel in scored]


def _quality_rank(release):
    """Map Radarr quality strings to a rank used for sorting (higher = better)."""
    name = ((release.get("quality") or {}).get("quality") or {}).get("name", "").lower()
    if "2160" in name or "uhd" in name or "4k" in name:
        return 4
    if "1080" in name:
        return 3
    if "720" in name:
        return 2
    if "480" in name or "dvd" in name:
        return 1
    return 0


def rank_releases(releases, preferred=None, target_code=None):
    """Sort releases for the picker:
      1) when `target_code` is set: higher language-confidence score first
      2) preferred-language match comes first
      3) higher quality first
      4) more seeders first
    Non-destructive: returns a new list."""
    if preferred is None:
        preferred = RADARR_PREFERRED_LANGUAGES

    def key(r):
        langs = detect_release_languages(r)
        pref_idx = min(
            (preferred.index(l) for l in langs if l in preferred),
            default=len(preferred),
        )
        seeders = r.get("seeders") or 0
        if target_code:
            lang_score = score_release_language_confidence(r, target_code) or 0
            return (-lang_score, pref_idx, -_quality_rank(r), -seeders)
        return (pref_idx, -_quality_rank(r), -seeders)

    return sorted(releases, key=key)


def _human_size(num_bytes):
    n = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def _format_lang_filter_line(lang_code, lang_source):
    """Render the language filter banner shown above the release picker and
    above the grab confirmation card. Returns "" when no filter is applied so
    the caller can interpolate it unconditionally."""
    if not lang_code:
        return ""
    spec = LANGUAGE_REGISTRY.get(lang_code)
    if not spec:
        return ""
    if lang_source == "explicit":
        suffix = "(esplicito)"
    elif lang_source == "vo":
        suffix = "(VO da TMDb)"
    else:
        suffix = ""
    suffix_part = f" {suffix}" if suffix else ""
    return f"🔤 Filtro: {spec['flag']} {lang_code}{suffix_part}\n"


def format_release_button(release, target_code=None):
    """Compact label for the inline button (Telegram limits ~64 chars but in
    practice ~80 is fine on most clients). The full title appears in the
    confirmation card on the next step.

    When `target_code` is provided, the label is prefixed with a confidence
    marker reflecting how likely the release contains the target audio:
      (no prefix)  explicit target match (score 1000)
      ⚠️           ambiguous marker (MULTI/DUAL alone, score 500-600)
      ❓           disclosed marker + other lang (score 200-400)"""
    langs = detect_release_languages(release)
    flag = _flag_for(langs)
    quality = ((release.get("quality") or {}).get("quality") or {}).get("name", "?")
    size = _human_size(release.get("size"))
    seeders = release.get("seeders") or 0
    lang_str = "+".join(langs[:2]) if langs != ["?"] else "?"
    label = f"{flag} {quality} · {size} · {lang_str} · {seeders}↑"
    if target_code:
        score = score_release_language_confidence(release, target_code)
        if score is not None and score < 1000:
            if score <= 100:
                marker = "🚫"   # scraper actively refuted; user override possible
            elif score < 500:
                marker = "❓"
            else:
                marker = "⚠️"
            label = f"{marker} {label}"
    rejected = release.get("rejections") or []
    if rejected:
        label = "🚫 " + label
    return label[:80]


# =============================================================================
# SUBTITLE SYNC (ffsubsync)
# =============================================================================

def sync_subtitle(video_path, srt_path, min_score=0):
    """Sync subtitle to video audio using ffsubsync. Overwrites srt_path in place.
    Returns dict with 'ok', 'score', 'offset', 'fps_scale' or False on failure.
    If min_score > 0, rejects syncs with score below threshold."""
    import subprocess, re as _re, tempfile, shutil
    try:
        tmp_out = tempfile.mktemp(suffix=".srt", prefix="ffsync_")
        tmp_log = tempfile.mktemp(suffix=".log", prefix="ffsync_")
        cmd = f'ffsubsync "{video_path}" -i "{srt_path}" -o "{tmp_out}" > "{tmp_log}" 2>&1'
        exit_code = os.system(cmd)
        log_content = ""
        if os.path.exists(tmp_log):
            with open(tmp_log) as f:
                log_content = f.read()
            os.remove(tmp_log)
        score_match = _re.search(r"score:\s*([\d.-]+)", log_content)
        offset_match = _re.search(r"offset seconds:\s*([\d.-]+)", log_content)
        framerate_match = _re.search(r"framerate scale factor:\s*([\d.]+)", log_content)
        score = float(score_match.group(1)) if score_match else 0.0
        offset = offset_match.group(1) if offset_match else "?"
        fps = framerate_match.group(1) if framerate_match else "1.000"
        if os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
            if min_score > 0 and score < min_score:
                log.warning(f"  ffsubsync score too low ({score:.1f} < {min_score}), rejecting sync")
                os.remove(tmp_out)
                return {"ok": False, "score": score, "offset": offset, "fps_scale": fps}
            shutil.move(tmp_out, srt_path)
            log.info(f"  🔄 Synced: {os.path.basename(srt_path)} (score: {score:.0f}, offset: {offset}s, fps_scale: {fps})")
            return {"ok": True, "score": score, "offset": offset, "fps_scale": fps}
        else:
            log.warning(f"  ffsubsync no output (exit {exit_code}): {log_content[:500]}")
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
    except Exception as e:
        log.warning(f"  ffsubsync error: {e}")
        for p in [tmp_out, tmp_log]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except:
                pass
    return False


def validate_sync(video_path, content_bytes, dest_path):
    """Write subtitle content to dest_path, sync it to the video audio,
    and validate the sync score against SYNC_MIN_SCORE.

    Returns:
      {"ok": True, "score": float}  — sync passed, dest_path contains synced sub
      {"ok": False, "score": float} — sync ran but score too low, dest_path removed
      False                         — sync completely failed, dest_path removed
    """
    try:
        with open(dest_path, "wb") as f:
            f.write(content_bytes if isinstance(content_bytes, bytes)
                    else content_bytes.encode("utf-8"))
    except Exception as e:
        log.error(f"  validate_sync: failed to write {dest_path}: {e}")
        return False

    result = sync_subtitle(video_path, dest_path, min_score=SYNC_MIN_SCORE)

    if result and result.get("ok"):
        return result

    # Sync failed or score too low — clean up the file
    try:
        if os.path.exists(dest_path):
            os.remove(dest_path)
    except Exception:
        pass

    if result and not result.get("ok"):
        log.warning(f"  validate_sync: score {result.get('score', '?')} < {SYNC_MIN_SCORE}, discarding")
        return result

    return False


# =============================================================================
# SUBTITLE VALIDATION (detect VIP placeholders)
# =============================================================================

# Patterns that appear in VIP placeholder ads but (almost) never in real subs.
# "opensubtitles" alone is too broad — real subs often credit opensubtitles.org
# in their footer. We require a stronger signal: phrases specific to the VIP
# upsell ads, OR a high density of placeholder keywords in a short sub.
PLACEHOLDER_STRONG_PATTERNS = [
    "osdb.link",
    "become a vip",
    "vip member",
    "advertise your product",
    "api.opensubtitles.org",
    "support us and become vip",
]
PLACEHOLDER_WEAK_PATTERNS = [
    "opensubtitles",
    "advertis",
    "get subtitles",
]


def is_placeholder_sub(content):
    """Check if downloaded subtitle content is a VIP placeholder/ad."""
    try:
        text = content.decode("utf-8", errors="ignore").lower()
    except Exception:
        text = str(content).lower()

    # Count actual SRT blocks first — real subs have 300-2000 blocks.
    blocks = re.findall(r"\d+\s*\n\d{2}:\d{2}:\d{2}", text)
    n_blocks = len(blocks)

    if n_blocks < 3:
        return True  # Too short to be real

    # Strong patterns = almost certainly a placeholder, reject immediately.
    for pattern in PLACEHOLDER_STRONG_PATTERNS:
        if pattern in text:
            return True

    # Weak patterns only count if the sub is also suspiciously short.
    # A real 90-min movie sub has >=200 blocks; anything shorter with
    # placeholder-ish keywords is almost always a fake.
    if n_blocks < 50:
        for pattern in PLACEHOLDER_WEAK_PATTERNS:
            if pattern in text:
                return True

    # Check if a single block spans an absurd time range (e.g., 00:00:00 --> 04:00:00)
    long_spans = re.findall(
        r"(\d{2}):(\d{2}):(\d{2}),\d+ --> (\d{2}):(\d{2}):(\d{2}),\d+", text
    )
    for h1, m1, s1, h2, m2, s2 in long_spans:
        start_sec = int(h1) * 3600 + int(m1) * 60 + int(s1)
        end_sec = int(h2) * 3600 + int(m2) * 60 + int(s2)
        if end_sec - start_sec > 600:  # Single block > 10 minutes = fake
            return True

    return False


# =============================================================================
# CLAUDE TRANSLATION (EN -> IT)
# =============================================================================

def parse_srt(content):
    """Parse SRT content into list of (index, timecode, text) tuples."""
    blocks = []
    # Normalize line endings
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    # Split by double newlines (block separator)
    raw_blocks = re.split(r"\n\n+", text.strip())

    for block in raw_blocks:
        lines = block.strip().split("\n")
        if len(lines) >= 3:
            idx = lines[0].strip()
            timecode = lines[1].strip()
            subtitle_text = "\n".join(lines[2:])
            blocks.append((idx, timecode, subtitle_text))
        elif len(lines) == 2:
            # Sometimes index + timecode with empty text
            idx = lines[0].strip()
            timecode = lines[1].strip()
            blocks.append((idx, timecode, ""))

    return blocks


def blocks_to_srt(blocks):
    """Convert list of (index, timecode, text) back to SRT string."""
    parts = []
    for idx, timecode, text in blocks:
        parts.append(f"{idx}\n{timecode}\n{text}")
    return "\n\n".join(parts) + "\n"


# Claude API pricing (USD per million tokens) — Sonnet
CLAUDE_INPUT_PRICE = 3.0   # $3/M input tokens
CLAUDE_OUTPUT_PRICE = 15.0  # $15/M output tokens


def estimate_translation_cost(srt_content):
    """Estimate the Claude API cost for translating an SRT file.
    Returns (estimated_cost_usd, num_blocks).

    Calibrated on real usage data: output tokens ~ input tokens * 1.09,
    but output costs 5x more than input so the output dominates the total.
    A 15% safety margin is added to avoid systematic underestimation."""
    blocks = parse_srt(srt_content)
    if not blocks:
        return 0.0, 0
    total_text = " ".join(text for _, _, text in blocks if text.strip())
    word_count = len(total_text.split())
    num_batches = len(blocks) // 100 + 1
    input_tokens = int(word_count * 1.3) + num_batches * 150
    output_tokens = int(input_tokens * 1.15)  # Italian tends to be slightly longer
    raw_cost = (input_tokens * CLAUDE_INPUT_PRICE + output_tokens * CLAUDE_OUTPUT_PRICE) / 1_000_000
    cost = raw_cost * 1.15  # 15% safety margin based on observed real vs estimated ratio
    return cost, len(blocks)


def _claude_translate_call(indexed_texts, video_name):
    """One Claude API call to translate a list of (local_idx, en_text) pairs.

    Returns a dict with:
      translations: {local_idx: italian_text}  (only for indices we asked for)
      input_tokens, output_tokens: usage counters
      truncated: True if Claude stopped due to max_tokens (the last parsed
                 line is dropped because it may be a half-sentence)
      ok: True if the API call succeeded; False on transport/HTTP error
    """
    if not indexed_texts:
        return {"translations": {}, "input_tokens": 0, "output_tokens": 0, "truncated": False, "ok": True}

    asked = {i for i, _ in indexed_texts}
    text_block = "\n".join(f"[{i}] {t}" for i, t in indexed_texts)
    prompt = (
        f"Translate these movie subtitles from English to Italian. "
        f"Movie: {video_name}. "
        f"Keep the [N] numbering prefix on each line. "
        f"Translate naturally — use colloquial Italian as it would appear in a real Italian dub. "
        f"Keep it concise as subtitles should be. "
        f"Do NOT add any explanation, just output the translated lines.\n\n"
        f"{text_block}"
    )
    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        CLAUDE_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.error(f"  Claude call failed: {e}")
        return {"translations": {}, "input_tokens": 0, "output_tokens": 0, "truncated": False, "ok": False}

    usage = result.get("usage", {})
    in_tokens = usage.get("input_tokens", 0)
    out_tokens = usage.get("output_tokens", 0)

    raw = ""
    for chunk in result.get("content", []):
        if chunk.get("type") == "text":
            raw += chunk["text"]

    parsed = {}
    for line in raw.strip().split("\n"):
        m = re.match(r"\[(\d+)\]\s*(.+)", line.strip())
        if m:
            idx = int(m.group(1))
            # Reject hallucinated indices — only keep cues we actually asked for.
            if idx in asked:
                parsed[idx] = m.group(2)

    truncated = result.get("stop_reason") == "max_tokens"
    # When Claude was cut off, the *last* parsed line is the dangerous one
    # (may be half a sentence). Drop it so the bisect retry will re-translate it.
    if truncated and parsed:
        last_idx = max(parsed)
        del parsed[last_idx]

    return {
        "translations": parsed,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "truncated": truncated,
        "ok": True,
    }


def _claude_translate_bisect(indexed_texts, video_name, depth=0):
    """Translate `indexed_texts` (list of (local_idx, en_text)) — if Claude
    truncates or skips any cue, split the batch in half and recurse so every
    cue eventually gets a real Italian translation.

    Base case: single cue that *still* doesn't translate → caller keeps EN.

    Returns {"translations": {idx: text}, "input_tokens": int, "output_tokens": int}.
    """
    if not indexed_texts:
        return {"translations": {}, "input_tokens": 0, "output_tokens": 0}

    if depth > 8:
        # Hard safety net — shouldn't fire in practice since the input shrinks
        # to ~1 cue long before this.
        log.error(f"  Claude bisect: depth limit hit, giving up on {len(indexed_texts)} cues")
        return {"translations": {}, "input_tokens": 0, "output_tokens": 0}

    res = _claude_translate_call(indexed_texts, video_name)
    in_t = res["input_tokens"]
    out_t = res["output_tokens"]

    if not res["ok"]:
        # Transport failure — recurse on halves only if we still have something
        # to split, otherwise give up on this cue and let caller keep EN.
        if len(indexed_texts) <= 1:
            return {"translations": {}, "input_tokens": in_t, "output_tokens": out_t}
        mid = len(indexed_texts) // 2
        a = _claude_translate_bisect(indexed_texts[:mid], video_name, depth + 1)
        b = _claude_translate_bisect(indexed_texts[mid:], video_name, depth + 1)
        merged = dict(a["translations"]); merged.update(b["translations"])
        return {
            "translations": merged,
            "input_tokens": in_t + a["input_tokens"] + b["input_tokens"],
            "output_tokens": out_t + a["output_tokens"] + b["output_tokens"],
        }

    translations = res["translations"]
    asked = {i for i, _ in indexed_texts}
    missing = asked - set(translations)

    if not missing and not res["truncated"]:
        return {"translations": translations, "input_tokens": in_t, "output_tokens": out_t}

    if len(indexed_texts) <= 1:
        # Single-cue batch still failed — give up on it; caller keeps EN.
        if missing:
            log.warning(f"  Claude bisect: cue index {indexed_texts[0][0]} unresolvable after retry, keeping EN")
        return {"translations": translations, "input_tokens": in_t, "output_tokens": out_t}

    # Retry the missing ones (and the dropped-last-on-truncation if any) by
    # splitting the batch in half. Translations we already got are kept.
    to_retry = [(i, t) for i, t in indexed_texts if i not in translations]
    log.info(f"  Claude bisect: retrying {len(to_retry)}/{len(indexed_texts)} cues with smaller batch")
    mid = len(to_retry) // 2
    a = _claude_translate_bisect(to_retry[:mid] if mid else to_retry[:1], video_name, depth + 1) if to_retry else {"translations": {}, "input_tokens": 0, "output_tokens": 0}
    b = _claude_translate_bisect(to_retry[mid:] if mid else [], video_name, depth + 1)

    merged = dict(translations)
    merged.update(a["translations"])
    merged.update(b["translations"])
    return {
        "translations": merged,
        "input_tokens": in_t + a["input_tokens"] + b["input_tokens"],
        "output_tokens": out_t + a["output_tokens"] + b["output_tokens"],
    }


def translate_srt_with_claude(srt_content, video_name):
    """Translate SRT content from English to Italian using Claude API.
    Batches of 40 cues with automatic bisect-on-failure: if Claude truncates
    or skips any cue, the batch is recursively split until every cue is
    translated (or — in the worst case — a single cue persistently fails and
    is left in English, which is the only way a missing translation can ever
    survive). Returns the translated SRT string."""

    if not CLAUDE_API_KEY:
        log.error("CLAUDE_API_KEY not set, cannot translate")
        return None

    blocks = parse_srt(srt_content)
    if not blocks:
        log.error("No SRT blocks found to translate")
        return None

    log.info(f"  Translating {len(blocks)} subtitle blocks EN->IT via Claude...")

    BATCH_SIZE = 40
    translated_blocks = []
    total_in = 0
    total_out = 0

    for batch_start in range(0, len(blocks), BATCH_SIZE):
        batch = blocks[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(blocks) + BATCH_SIZE - 1) // BATCH_SIZE

        indexed = [(i, text) for i, (_, _, text) in enumerate(batch) if text.strip()]
        if not indexed:
            translated_blocks.extend(batch)
            continue

        res = _claude_translate_bisect(indexed, video_name)
        translations = res["translations"]
        total_in += res["input_tokens"]
        total_out += res["output_tokens"]

        for i, (idx, timecode, text) in enumerate(batch):
            translated_blocks.append((idx, timecode, translations.get(i, text)))

        kept_en = sum(1 for i, (_, _, text) in enumerate(batch) if text.strip() and i not in translations)
        if kept_en:
            log.warning(f"  Batch {batch_num}/{total_batches}: {len(translations)} translated, {kept_en} kept as EN (unresolvable)")
        else:
            log.info(f"  Batch {batch_num}/{total_batches}: {len(translations)} cues translated")
        time.sleep(1)

    cost = (total_in * CLAUDE_INPUT_PRICE + total_out * CLAUDE_OUTPUT_PRICE) / 1_000_000
    log.info(f"  Translation cost: {total_in} in + {total_out} out = ${cost:.4f}")

    try:
        state = load_state()
        costs = state.setdefault("claude_costs", {"total_input_tokens": 0, "total_output_tokens": 0, "total_cost_usd": 0.0, "translations": 0})
        costs["total_input_tokens"] += total_in
        costs["total_output_tokens"] += total_out
        costs["total_cost_usd"] += cost
        costs["translations"] += 1
        save_state(state)
    except Exception:
        pass

    return blocks_to_srt(translated_blocks)


# =============================================================================
# DEEPL TRANSLATION (EN -> IT primary path)
# =============================================================================

# DeepL pricing: Free tier (500k chars/month) + Pro €20/M chars pay-as-you-go.
DEEPL_PRICE_PER_MILLION_CHARS = 20.0


def translate_srt_with_deepl(srt_content):
    """Translate SRT content from English to Italian using DeepL API.
    Translates one cue at a time (batched 50/request — DeepL's max), so the
    output cannot be truncated mid-cue the way an LLM can.
    Returns (parsed_blocks_with_it_text, total_chars) or None on failure."""

    if not DEEPL_API_KEY:
        return None

    blocks = parse_srt(srt_content)
    if not blocks:
        return None

    # Indices of blocks with non-empty text — we translate only those.
    payload_indices = [i for i, (_, _, text) in enumerate(blocks) if text.strip()]
    if not payload_indices:
        return list(blocks), 0

    log.info(f"  Translating {len(payload_indices)} cues EN->IT via DeepL...")

    out_blocks = list(blocks)
    total_chars = 0
    BATCH = 50

    for start in range(0, len(payload_indices), BATCH):
        slice_idx = payload_indices[start:start + BATCH]
        texts = [blocks[i][2] for i in slice_idx]
        total_chars += sum(len(t) for t in texts)

        try:
            payload = json.dumps({
                "text": texts,
                "source_lang": "EN",
                "target_lang": "IT",
                "preserve_formatting": True,
                "tag_handling": "html",  # Preserve <i>...</i> italics common in SRT
            }).encode("utf-8")

            req = urllib.request.Request(
                DEEPL_API_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            translations = result.get("translations", [])
            if len(translations) != len(texts):
                log.error(f"  DeepL returned {len(translations)} translations for {len(texts)} inputs — aborting")
                return None

            for local_i, idx in enumerate(slice_idx):
                idx_str, timecode, _ = out_blocks[idx]
                # tag_handling=html makes DeepL escape ' and " as &#x27;/&quot;.
                out_blocks[idx] = (idx_str, timecode, html.unescape(translations[local_i]["text"]))

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            log.error(f"  DeepL HTTP {e.code} (batch {start//BATCH + 1}): {body[:200]}")
            return None
        except Exception as e:
            log.error(f"  DeepL error (batch {start//BATCH + 1}): {e}")
            return None

    cost = total_chars * DEEPL_PRICE_PER_MILLION_CHARS / 1_000_000
    log.info(f"  DeepL: {total_chars} chars translated (~${cost:.4f} if on paid tier, free tier is free up to 500k/month)")

    # Track DeepL usage for visibility.
    try:
        state = load_state()
        stats = state.setdefault("deepl_stats", {"total_chars": 0, "translations": 0})
        stats["total_chars"] += total_chars
        stats["translations"] += 1
        save_state(state)
    except Exception:
        pass

    return out_blocks, total_chars


# =============================================================================
# CLAUDE POLISH PASS (refines weak DeepL translations)
# =============================================================================

# Haiku 4.5 pricing — used for the polish pass (much cheaper than Sonnet).
CLAUDE_HAIKU_INPUT_PRICE = 1.0   # $1/M input tokens
CLAUDE_HAIKU_OUTPUT_PRICE = 5.0  # $5/M output tokens


def polish_translation_with_claude(en_blocks, it_blocks, video_name):
    """Send Claude both EN and DeepL-IT cues; ask it to rewrite ONLY the lines
    that sound unnatural in Italian. Output is sparse, so truncation is unlikely.
    Returns (polished_blocks, num_rewritten)."""

    if not CLAUDE_API_KEY:
        log.warning("  CLAUDE_API_KEY not set, skipping polish pass")
        return it_blocks, 0

    pairs = []
    for i, ((_, _, en_text), (_, _, it_text)) in enumerate(zip(en_blocks, it_blocks)):
        if en_text.strip() and it_text.strip():
            single_en = en_text.replace("\n", " ").strip()
            single_it = it_text.replace("\n", " ").strip()
            pairs.append((i, single_en, single_it))

    if not pairs:
        return it_blocks, 0

    log.info(f"  Polishing {len(pairs)} translated cues via Claude ({CLAUDE_POLISH_MODEL})...")

    BATCH_SIZE = 80
    rewrites = {}
    total_in = 0
    total_out = 0

    for batch_start in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(pairs) + BATCH_SIZE - 1) // BATCH_SIZE

        lines = [f"[{i}] EN: {en} | IT: {it_}" for (i, en, it_) in batch]
        prompt = (
            f"Sei un adattatore di sottotitoli italiani. Film: {video_name}.\n"
            f"Per ogni riga sotto trovi la frase inglese e una traduzione italiana automatica.\n"
            f"Se la traduzione italiana suona innaturale, troppo letterale, o sbagliata per un sottotitolo "
            f"cinematografico (in cui si usa italiano parlato/colloquiale, come in un doppiaggio), riscrivila. "
            f"Mantieni coerenza di genere grammaticale tra i cue consecutivi dello stesso personaggio. "
            f"Altrimenti SALTA quella riga.\n\n"
            f"Output: SOLO le righe da riscrivere, una per riga, nel formato esatto:\n"
            f"[N] testo italiano riscritto\n\n"
            f"IMPORTANTE: [N] è il numero di riga. Dopo [N] scrivi SOLO il testo italiano finale, "
            f"senza 'EN:', senza 'IT:', senza '|', senza spiegazioni.\n"
            f"Niente intestazioni, niente commenti. Se nessuna riga va riscritta, restituisci stringa vuota.\n\n"
            + "\n".join(lines)
        )

        try:
            payload = json.dumps({
                "model": CLAUDE_POLISH_MODEL,
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": prompt}],
            }).encode("utf-8")

            req = urllib.request.Request(
                CLAUDE_API_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            usage = result.get("usage", {})
            total_in += usage.get("input_tokens", 0)
            total_out += usage.get("output_tokens", 0)

            text = ""
            for chunk in result.get("content", []):
                if chunk.get("type") == "text":
                    text += chunk["text"]

            parsed_in_batch = {}
            for line in text.strip().split("\n"):
                line = line.strip()
                m = re.match(r"\[(\d+)\]\s*(.+)", line)
                if m:
                    raw = m.group(2)
                    # Guard: strip double-translation artifact if Claude echoed "EN: ... | IT: ..."
                    if "| IT:" in raw:
                        raw = raw.split("| IT:", 1)[1].strip()
                    elif raw.upper().startswith("IT:"):
                        raw = raw[3:].strip()
                    elif raw.upper().startswith("EN:"):
                        continue  # Claude echoed the English — skip
                    parsed_in_batch[int(m.group(1))] = html.unescape(raw)

            # If the model was cut off, drop the last rewrite — it may be truncated.
            if result.get("stop_reason") == "max_tokens" and parsed_in_batch:
                last_idx = max(parsed_in_batch)
                log.warning(f"  Polish batch {batch_num}/{total_batches} hit max_tokens; dropping cue [{last_idx}]")
                del parsed_in_batch[last_idx]

            rewrites.update(parsed_in_batch)
            log.info(f"  Polish batch {batch_num}/{total_batches}: {len(parsed_in_batch)} rewrites")
            time.sleep(0.5)

        except Exception as e:
            log.error(f"  Polish error (batch {batch_num}): {e} — keeping DeepL output for this batch")

    cost = (total_in * CLAUDE_HAIKU_INPUT_PRICE + total_out * CLAUDE_HAIKU_OUTPUT_PRICE) / 1_000_000
    log.info(f"  Polish: {len(rewrites)}/{len(pairs)} cues rewritten, {total_in} in + {total_out} out = ${cost:.4f}")

    polished = list(it_blocks)
    for i, new_text in rewrites.items():
        if 0 <= i < len(polished):
            idx_str, timecode, _ = polished[i]
            polished[i] = (idx_str, timecode, new_text)

    # Track polish cost separately so claude_costs (Sonnet full-translate) stays clean.
    try:
        state = load_state()
        costs = state.setdefault("claude_polish_costs", {"total_input_tokens": 0, "total_output_tokens": 0, "total_cost_usd": 0.0, "polishes": 0})
        costs["total_input_tokens"] += total_in
        costs["total_output_tokens"] += total_out
        costs["total_cost_usd"] += cost
        costs["polishes"] += 1
        save_state(state)
    except Exception:
        pass

    return polished, len(rewrites)


def translate_srt(srt_content, video_name):
    """Primary EN->IT translation entry point.
    Strategy: DeepL (cue-by-cue, no truncation) + optional Claude polish pass.
    Falls back to full Claude translation if DeepL is unavailable.
    Returns translated SRT string or None on failure."""

    if DEEPL_API_KEY:
        deepl_result = translate_srt_with_deepl(srt_content)
        if deepl_result is not None:
            it_blocks, _ = deepl_result
            en_blocks = parse_srt(srt_content)
            if POLISH_TRANSLATION and CLAUDE_API_KEY:
                it_blocks, _ = polish_translation_with_claude(en_blocks, it_blocks, video_name)
            else:
                log.info("  Polish pass skipped (POLISH_TRANSLATION disabled or no CLAUDE_API_KEY)")
            return blocks_to_srt(it_blocks)
        log.warning("  DeepL translation failed, falling back to Claude full-translate")

    return translate_srt_with_claude(srt_content, video_name)


def _cascade_search(client, video_path, language, file_hash=None, file_size=0, trace=None):
    """Search OpenSubtitles using a cascade: hash -> IMDB ID -> name variants.
    Returns a list of results (may be empty).
    If trace is a list, appends per-attempt entries describing what was tried."""
    parsed = parse_video(video_path)
    season = parsed.get("season") if parsed["type"] == "episode" else None
    episode = parsed.get("episode") if parsed["type"] == "episode" else None
    lang_label = language.upper()

    # 1. Hash search
    if file_hash:
        time.sleep(DELAY_BETWEEN_API_CALLS)
        try:
            results = client.search_hash(file_hash, file_size)
            log.info(f"  {lang_label} hash search: {len(results)} results")
            if trace is not None:
                trace.append({"provider": "OpenSubtitles", "lang": lang_label,
                              "method": "hash", "query": file_hash, "results": len(results)})
            if results:
                return results
        except Exception as e:
            log.error(f"  {lang_label} hash search error: {e}")
            if trace is not None:
                trace.append({"provider": "OpenSubtitles", "lang": lang_label,
                              "method": "hash", "query": file_hash, "results": 0,
                              "error": str(e)})

    # 2. IMDB ID search
    imdb_id = find_imdb_id(video_path)
    if imdb_id:
        time.sleep(DELAY_BETWEEN_API_CALLS)
        results = client.search_imdb(imdb_id, season, episode, language=language)
        log.info(f"  {lang_label} IMDB search ({imdb_id}): {len(results)} results")
        if trace is not None:
            trace.append({"provider": "OpenSubtitles", "lang": lang_label,
                          "method": "imdb", "query": imdb_id, "results": len(results)})
        if results:
            return results
    elif trace is not None:
        trace.append({"provider": "OpenSubtitles", "lang": lang_label,
                      "method": "imdb", "query": "(non risolto)", "results": 0})

    # 3. Name search cascade
    queries = get_search_queries(video_path)
    for query in queries:
        time.sleep(DELAY_BETWEEN_API_CALLS)
        results = client.search_name(query, season, episode, language=language)
        log.info(f"  {lang_label} name search \"{query}\": {len(results)} results")
        if trace is not None:
            trace.append({"provider": "OpenSubtitles", "lang": lang_label,
                          "method": "nome", "query": query, "results": len(results)})
        if results:
            return results

    return []


def _download_first_valid(client, results, video_path, max_tries=15):
    """Try downloading subtitles from results in score order, skipping placeholders.
    Returns (content_bytes, sub_info) or (None, None)."""
    if not results:
        return None, None

    srt = [r for r in results if (r.get("SubFormat", "") or "").lower() == "srt"] or results
    video_base = os.path.splitext(os.path.basename(video_path))[0].lower()

    scored = []
    for sub in srt:
        score = 0
        sf = (sub.get("SubFileName", "") or "").lower()
        mr = (sub.get("MovieReleaseName", "") or "").lower()
        if sub.get("MatchedBy") == "moviehash":
            score += 100
        if video_base in sf or video_base in mr:
            score += 50
        for tag in ["galaxytv", "webrip", "720p", "1080p", "amzn", "bluray", "web-dl"]:
            if tag in video_base and tag in (sf + mr):
                score += 10
        score += min(int(sub.get("SubDownloadsCnt", 0) or 0) // 100, 20)
        score += int(float(sub.get("SubRating", 0) or 0))
        scored.append((score, sub))

    scored.sort(key=lambda x: x[0], reverse=True)

    for i, (score, sub) in enumerate(scored[:max_tries]):
        time.sleep(DELAY_BETWEEN_API_CALLS)
        content = client.download(sub.get("IDSubtitleFile", ""))
        if content and not is_placeholder_sub(content):
            log.info(f"  Valid sub found (attempt {i+1}): {sub.get('SubFileName', '?')}")
            return content, sub
        elif content:
            log.warning(f"  Placeholder rejected (attempt {i+1}): {sub.get('SubFileName', '?')}")
        else:
            log.warning(f"  Empty download (attempt {i+1}): {sub.get('SubFileName', '?')}")

    log.warning(f"  All {min(len(scored), max_tries)} attempts were placeholders")
    return None, None


def search_and_download_english(client, video_path, file_hash=None, file_size=0, trace=None):
    """Search for English subtitles as fallback. Returns content bytes or None."""
    results = _cascade_search(client, video_path, "eng", file_hash, file_size, trace=trace)

    content, sub = _download_first_valid(client, results, video_path)
    if content:
        log.info(f"  ENG sub downloaded: {sub.get('SubFileName', '?')}")
        return content
    if trace is not None and results:
        trace.append({"provider": "OpenSubtitles", "lang": "ENG",
                      "method": "download", "query": "",
                      "results": len(results), "rejected": "tutti rifiutati (placeholder/invalid)"})
    return None


# =============================================================================
# DOWNLOAD WORKFLOW
# =============================================================================

def _save_sub_and_update_state(video_path, sub_path, source, state):
    """Save subtitle state and send Telegram notification."""
    state["downloaded"][video_path] = {
        "sub": os.path.basename(sub_path),
        "source": source,
        "time": datetime.now().isoformat(),
    }
    state["asked"].pop(video_path, None)
    save_state(state)
    _notify_radarr_request_ready(video_path)


def _notify_radarr_request_ready(video_path):
    """If this video matches a pending /scarica request, verify the audio
    language against the user-requested filter (if any) and send the matching
    Telegram notification.

    Three paths:
      1. No required_lang stored -> classic "📬 Pronto" notification, entry cleared.
      2. required_lang stored and audio matches -> "📬 Pronto" + cleared.
      3. required_lang stored and audio does NOT match -> warning prompt with
         [✅ Tieni] / [🔁 Ri-richiedi] inline buttons; the pending entry stays
         in requests.json until the user picks one of the two."""
    try:
        parsed = parse_video(video_path)
    except Exception:
        return
    name = (parsed.get("name") or "").lower().strip()
    year = parsed.get("year")
    if not name:
        return

    requests_state = load_requests()
    pending = requests_state.get("pending_radarr", {})
    matched_key = None
    for key, entry in list(pending.items()):
        if not key.startswith("download:"):
            continue
        if (entry.get("title", "").lower().strip() == name and
                (not year or not entry.get("year") or entry["year"] == year)):
            matched_key = key
            break
    if not matched_key:
        return

    entry = pending[matched_key]
    title_label = entry["title"] + (f" ({entry['year']})" if entry.get("year") else "")
    required_lang = entry.get("required_lang")

    if required_lang and not verify_audio_language(video_path, required_lang):
        # Audio mismatch: keep the entry around so the user can choose to
        # tolerate the release or trigger a re-request from the saved film_hash.
        entry["video_path"] = video_path
        entry["mismatch_at"] = datetime.now().isoformat()
        save_requests(requests_state)
        flag = LANGUAGE_REGISTRY.get(required_lang, {}).get("flag", "🏳️")
        tmdb_id = entry.get("tmdb_id")
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Tieni", "callback_data": f"lang_keep:{tmdb_id}"},
            {"text": "🔁 Ri-richiedi", "callback_data": f"lang_redo:{tmdb_id}"},
        ]]}
        tg_send(
            f"⚠️ <b>{title_label}</b> — atteso audio {flag} <b>{required_lang}</b> "
            f"ma il file non lo contiene.\n"
            f"📁 {os.path.basename(video_path)}\n\n"
            f"Vuoi tenerlo lo stesso o ri-richiedere un altro rilascio?",
            reply_markup=keyboard,
        )
        return

    # Happy path: clear the entry, send the "Pronto" notification.
    pending.pop(matched_key, None)
    save_requests(requests_state)
    lang_line = _format_lang_filter_line(required_lang, entry.get("lang_source"))
    tg_send(
        f"📬 <b>Pronto:</b> {title_label}\n"
        f"📁 {os.path.basename(video_path)}\n"
        f"{lang_line}"
        f"📡 Era stato richiesto via /scarica ({entry.get('indexer', '?')})."
    )


def _translate_and_save(eng_content, video_path, state, silent=False, skip_sync=False):
    """Translate English content to Italian and save. Returns True on success.
    Also saves the English subtitle as .en.srt alongside the Italian one.
    skip_sync=True when the English sub is already local (timecodes match the video)."""
    if isinstance(eng_content, bytes):
        eng_text = eng_content.decode("utf-8", errors="ignore")
    elif isinstance(eng_content, str) and os.path.isfile(eng_content):
        try:
            with open(eng_content, "r", encoding="utf-8", errors="ignore") as f:
                eng_text = f.read()
        except Exception as e:
            log.error(f"  Failed to read English subtitle: {e}")
            return False
    else:
        eng_text = eng_content

    # Save English subtitle alongside the video
    en_srt_path = os.path.splitext(video_path)[0] + ".en.srt"
    if not os.path.exists(en_srt_path):
        try:
            with open(en_srt_path, "w", encoding="utf-8") as f:
                f.write(eng_text)
            log.info(f"  Saved English sub: {os.path.basename(en_srt_path)}")
        except Exception as e:
            log.warning(f"  Failed to save English sub: {e}")

    video_name = friendly_name(video_path)

    est_cost, num_blocks = estimate_translation_cost(eng_text)
    if DEEPL_API_KEY:
        log.info(f"  Using DeepL primary path ({num_blocks} cues)" + (" + Claude polish" if POLISH_TRANSLATION and CLAUDE_API_KEY else ""))
    else:
        log.info(f"  Estimated Claude translation cost: ${est_cost:.4f} ({num_blocks} blocks)")

    translated_srt = translate_srt(eng_text, video_name)

    if not translated_srt:
        log.error(f"  Translation failed for: {os.path.basename(video_path)}")
        return False

    sub_path = os.path.splitext(video_path)[0] + ".it.srt"
    with open(sub_path, "w", encoding="utf-8") as f:
        f.write(translated_srt)

    if skip_sync:
        log.info(f"  Skipping sync (local English sub, timecodes already correct)")
    else:
        sync_subtitle(video_path, sub_path)

    # Get actual cost from state (updated by translate_srt_with_claude)
    actual_state = load_state()
    claude_spent = actual_state.get("claude_costs", {}).get("total_cost_usd", 0.0)
    polish_spent = actual_state.get("claude_polish_costs", {}).get("total_cost_usd", 0.0)
    deepl_chars = actual_state.get("deepl_stats", {}).get("total_chars", 0)
    total_spent = claude_spent + polish_spent

    if DEEPL_API_KEY:
        engine = "DeepL + Claude polish" if (POLISH_TRANSLATION and CLAUDE_API_KEY) else "DeepL"
    else:
        engine = "Claude"

    _save_sub_and_update_state(video_path, sub_path, f"{engine} EN→IT translation", state)
    log.info(f"  ✅ Translated & saved: {os.path.basename(sub_path)}")
    if not silent:
        tg_send(
            f"🤖 Sub ITA tradotto da ENG ({engine}):\n"
            f"<b>{friendly_name(video_path)}</b>\n"
            f"📁 {os.path.basename(sub_path)}\n"
            f"💰 Totale speso: ${total_spent:.4f}"
            + (f" | DeepL: {deepl_chars:,} char" if deepl_chars else "")
        )
    return True


def format_search_trace(trace):
    """Render a search trace into a compact, Telegram-friendly text block."""
    if not trace:
        return ""
    lines = []
    quota = None
    for entry in trace:
        if "_quota" in entry:
            quota = entry["_quota"]
            continue
        provider = entry.get("provider", "?")
        lang = entry.get("lang", "?")
        method = entry.get("method", "")
        query = entry.get("query", "")
        n = entry.get("results", 0)
        rejected = entry.get("rejected") or entry.get("error")
        loc = f"{provider} {method}".strip()
        q_part = f" '{query}'" if query else ""
        suffix = f" → {rejected}" if rejected else ""
        lines.append(f"  • {loc} {lang}{q_part}: {n}{suffix}")
    if quota is not None:
        lines.append(f"  📊 OpenSubtitles download rimanenti: {quota}")
    return "\n".join(lines)


def do_download(video_path, state, silent=False, translate=True, trace=None):
    """Search and download subtitle for a video file.
    Returns: True (ITA found), "en_only" (EN saved, needs translation), False (nothing found).
    When translate=False, downloads EN sub but does NOT translate (saves money, user decides later).
    If trace is a list, it is populated with per-attempt search entries."""
    fname = os.path.basename(video_path)
    log.info(f"Downloading sub for: {fname}")

    # =================================================================
    # STEP 0: Check for existing subtitles already in the folder
    # =================================================================
    existing = find_existing_srt(video_path)
    if existing:
        if existing["lang"] == "it":
            log.info(f"  Found existing Italian sub: {existing['path']}")
            sub_path = os.path.splitext(video_path)[0] + ".it.srt"
            if existing["path"] != sub_path:
                import shutil
                shutil.copy2(existing["path"], sub_path)
            _save_sub_and_update_state(video_path, sub_path, f"existing file: {os.path.basename(existing['path'])}", state)
            if not silent:
                tg_send(f"✅ Sub ITA trovato nella cartella:\n<b>{friendly_name(video_path)}</b>")
            return True
        elif existing["lang"] == "en":
            en_srt_path = os.path.splitext(video_path)[0] + ".en.srt"
            if existing["path"] != en_srt_path:
                import shutil
                shutil.copy2(existing["path"], en_srt_path)
            log.info(f"  Found existing English sub: {existing['path']}")
            if translate and (DEEPL_API_KEY or CLAUDE_API_KEY):
                log.info(f"  Translating existing EN sub...")
                if _translate_and_save(en_srt_path, video_path, state, silent=silent, skip_sync=True):
                    return True
            else:
                return "en_only"

    # =================================================================
    # STEP 1: Search & save ITA (if available + sync OK)
    # =================================================================
    subdl = SubdlClient()
    client = OSClient()
    os_logged_in = client.login()
    sub_path = os.path.splitext(video_path)[0] + ".it.srt"
    en_srt_path = os.path.splitext(video_path)[0] + ".en.srt"

    ita_saved = _try_save_ita(subdl, client, os_logged_in, video_path, sub_path, state, trace)

    # =================================================================
    # STEP 2: Search & save ENG (always — useful as backup or for translation)
    # =================================================================
    eng_saved = _try_save_eng(subdl, client, os_logged_in, video_path, en_srt_path, trace)

    if os_logged_in:
        if trace is not None and client.downloads_remaining is not None:
            trace.append({"_quota": client.downloads_remaining})
        client.logout()

    # =================================================================
    # STEP 3: Decide outcome and notify user
    # =================================================================
    if ita_saved:
        log.info(f"  ✅ ITA saved" + (" + ENG saved" if eng_saved else ""))
        if not silent:
            files = f"📁 {os.path.basename(sub_path)}"
            if eng_saved:
                files += f"\n📁 {os.path.basename(en_srt_path)}"
            tg_send(f"✅ Sub ITA scaricato:\n<b>{friendly_name(video_path)}</b>\n{files}")
        return True

    # No ITA — fall through to ENG-only path
    if not eng_saved:
        log.warning(f"  No valid subs (ITA or ENG) found for: {fname}")
        state["asked"][video_path] = {"time": datetime.now().isoformat(), "status": "failed"}
        save_state(state)
        if not silent:
            trace_text = format_search_trace(trace)
            msg = f"❌ Nessun sub ITA né ENG trovato per:\n<b>{friendly_name(video_path)}</b>"
            if trace_text:
                msg += f"\n\n<b>Tentativi:</b>\n{trace_text}"
            tg_send(msg)
        return False

    # Only ENG was saved — caller decides about translation
    log.info(f"  ✅ EN sub saved (no ITA found, translation deferred)")
    if translate and (DEEPL_API_KEY or CLAUDE_API_KEY):
        if _translate_and_save(en_srt_path, video_path, state, silent=silent, skip_sync=True):
            return True
    return "en_only"


def _try_save_ita(subdl, client, os_logged_in, video_path, sub_path, state, trace):
    """Search ITA on Subdl then OpenSubtitles, validate sync, save .it.srt.
    Returns True if a synced ITA sub was saved."""
    # --- Subdl ITA ---
    ita_content = subdl.search_and_download(video_path, language="it", trace=trace)
    if ita_content:
        sync_result = validate_sync(video_path, ita_content, sub_path)
        if sync_result and sync_result.get("ok"):
            _save_sub_and_update_state(video_path, sub_path, "Subdl.com", state)
            log.info(f"  ✅ Saved ITA (Subdl, sync score {sync_result['score']:.0f}): {os.path.basename(sub_path)}")
            return True
        score_val = sync_result.get("score", "?") if isinstance(sync_result, dict) else "N/A"
        log.warning(f"  Subdl ITA sync score too low ({score_val}), discarding")
        if trace is not None:
            trace.append({"provider": "Subdl", "lang": "ITA", "method": "sync", "query": "",
                          "results": 1, "rejected": f"sync score troppo basso ({score_val})"})

    # --- OpenSubtitles ITA ---
    if os_logged_in:
        try:
            file_hash, file_size = compute_hash(video_path)
            results = _cascade_search(client, video_path, "ita", file_hash, file_size, trace=trace)
            content, best = _download_first_valid(client, results, video_path)
            if trace is not None and results and not content:
                trace.append({"provider": "OpenSubtitles", "lang": "ITA", "method": "download",
                              "query": "", "results": len(results),
                              "rejected": "tutti rifiutati (placeholder/invalid)"})
            if content:
                sync_result = validate_sync(video_path, content, sub_path)
                if sync_result and sync_result.get("ok"):
                    _save_sub_and_update_state(video_path, sub_path,
                                               f"OpenSubtitles: {best.get('SubFileName', '')}", state)
                    log.info(f"  ✅ Saved ITA (OS, sync score {sync_result['score']:.0f}): {os.path.basename(sub_path)}")
                    return True
                score_val = sync_result.get("score", "?") if isinstance(sync_result, dict) else "N/A"
                log.warning(f"  OS ITA sync score too low ({score_val}), discarding")
                if trace is not None:
                    trace.append({"provider": "OpenSubtitles", "lang": "ITA", "method": "sync",
                                  "query": "", "results": 1,
                                  "rejected": f"sync score troppo basso ({score_val})"})
        except Exception as e:
            log.error(f"  OpenSubtitles ITA error: {e}")
    return False


def _try_save_eng(subdl, client, os_logged_in, video_path, en_srt_path, trace):
    """Search ENG on Subdl then OpenSubtitles, validate sync, save .en.srt.
    Returns True if a synced ENG sub was saved."""
    eng_content = subdl.search_and_download(video_path, language="en", trace=trace)
    if not eng_content and os_logged_in:
        file_hash, file_size = compute_hash(video_path)
        eng_content = search_and_download_english(client, video_path, file_hash, file_size, trace=trace)
    if not eng_content:
        return False

    sync_result = validate_sync(video_path, eng_content, en_srt_path)
    if not sync_result or not sync_result.get("ok"):
        score_val = sync_result.get("score", "?") if isinstance(sync_result, dict) else "N/A"
        log.warning(f"  ENG sync score too low ({score_val}), discarding")
        if trace is not None:
            trace.append({"provider": "ENG", "lang": "ENG", "method": "sync", "query": "",
                          "results": 1, "rejected": f"sync score troppo basso ({score_val})"})
        return False

    log.info(f"  ✅ Saved ENG (score {sync_result['score']:.0f}): {os.path.basename(en_srt_path)}")
    return True


# =============================================================================
# TELEGRAM INTERACTION
# =============================================================================

def ask_user(video_path, state):
    """Send a Telegram message asking if user wants to download subs (single file)."""
    name = friendly_name(video_path)
    folder = get_series_folder(video_path)
    path_hash = str(abs(hash(video_path)))[:8]

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Scarica", "callback_data": f"yes:{path_hash}"},
                {"text": "❌ No", "callback_data": f"no:{path_hash}"},
            ],
            [
                {"text": f"🚫 Escludi '{folder}'", "callback_data": f"exclude:{path_hash}"},
            ],
        ]
    }

    result = tg_send(
        f"🎬 <b>Sub ITA mancante</b>\n\n"
        f"📺 {name}\n"
        f"📁 {folder}\n"
        f"📄 {os.path.basename(video_path)}\n\n"
        f"Scarico il sottotitolo italiano?",
        reply_markup=keyboard,
    )

    msg_id = None
    if result and result.get("ok"):
        msg_id = result["result"]["message_id"]

    state["asked"][video_path] = {
        "time": datetime.now().isoformat(),
        "status": "pending",
        "path_hash": path_hash,
        "msg_id": msg_id,
    }
    save_state(state)


def group_by_series(video_paths):
    """Group video paths by their series/movie folder."""
    groups = {}
    for path in video_paths:
        folder = get_series_folder(path)
        groups.setdefault(folder, []).append(path)
    # Sort episodes within each group
    for folder in groups:
        groups[folder].sort()
    return groups


def _send_batch_message(folder, paths, state):
    """Send a single batch message for a group of files. Returns batch_hash."""
    batch_hash = str(abs(hash(folder + str(len(paths)) + paths[0])))[:8]

    episode_list = "\n".join(
        f"  • {friendly_name(p)}" for p in paths[:20]
    )
    if len(paths) > 20:
        episode_list += f"\n  ... e altri {len(paths) - 20}"

    keyboard = {
        "inline_keyboard": [
            [
                {"text": f"✅ Scarica tutti ({len(paths)})", "callback_data": f"batch_yes:{batch_hash}"},
                {"text": "❌ No", "callback_data": f"batch_no:{batch_hash}"},
            ],
            [
                {"text": f"🚫 Escludi '{folder}'", "callback_data": f"grp_exclude:{batch_hash}"},
            ],
        ]
    }

    result = tg_send(
        f"🎬 <b>Sub ITA mancanti</b>\n\n"
        f"📁 <b>{folder}</b> — {len(paths)} file\n\n"
        f"{episode_list}\n\n"
        f"Scarico i sottotitoli italiani?",
        reply_markup=keyboard,
    )

    msg_id = None
    if result and result.get("ok"):
        msg_id = result["result"]["message_id"]

    batches = load_batches()
    batches[batch_hash] = {
        "paths": paths,
        "folder": folder,
        "time": datetime.now().isoformat(),
        "msg_id": msg_id,
    }
    save_batches(batches)

    for p in paths:
        path_hash = str(abs(hash(p)))[:8]
        state["asked"][p] = {
            "time": datetime.now().isoformat(),
            "status": "pending",
            "path_hash": path_hash,
            "batch": batch_hash,
        }

    save_state(state)
    return batch_hash


def auto_enqueue_missing(missing, state):
    """Auto-queue all missing videos for download. Notify Telegram with a
    single 'in coda' message per file/series. The user does not need to
    confirm — subs are always wanted (the cost gate is on Claude translation,
    enforced later in the queue worker)."""
    groups = group_by_series(missing)

    # Multi-episode series → one batch job per series
    singles = []
    for folder, paths in groups.items():
        if len(paths) > 1:
            pos = queue_position()
            result = tg_send(
                f"⬇️ <b>Scaricando sub per {folder}</b>\n"
                f"📁 {len(paths)} file"
                + (f"\n⏳ In coda (posizione {pos + 1})" if pos > 0 else "")
            )
            msg_id = result["result"]["message_id"] if result and result.get("ok") else None
            for p in paths:
                state["asked"][p] = {"time": datetime.now().isoformat(), "status": "pending"}
            download_queue.put({"type": "batch", "paths": paths, "msg_id": msg_id})
            time.sleep(0.5)
        else:
            singles.append(paths[0])

    # Single films → one job each
    for p in singles:
        pos = queue_position()
        result = tg_send(
            f"⬇️ <b>Scaricando sub per</b>\n"
            f"📁 {friendly_name(p)}"
            + (f"\n⏳ In coda (posizione {pos + 1})" if pos > 0 else "")
        )
        msg_id = result["result"]["message_id"] if result and result.get("ok") else None
        state["asked"][p] = {"time": datetime.now().isoformat(), "status": "pending"}
        download_queue.put({"type": "single", "path": p, "msg_id": msg_id})
        time.sleep(0.3)

    save_state(state)


# Backwards-compatible alias — kept in case anything still references it.
ask_user_grouped = auto_enqueue_missing


def find_path_by_hash(state, path_hash):
    """Find video path from callback hash."""
    for path, info in state["asked"].items():
        if info.get("path_hash") == path_hash:
            return path
    return None


# =============================================================================
# TELEGRAM COMMAND DISPATCHER
# =============================================================================

def _levenshtein(a, b):
    """Simple Levenshtein distance, used to suggest the closest known command
    when the user mistypes a slash command. O(len(a)*len(b)) — fine for the
    short command strings we use."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def do_scarica_search(query, progress_msg_id=None, lang=None, lang_source=None):
    """Step 1: search TMDb and show film disambiguation buttons.
    Accepts queries like 'Punch-Drunk Love' or 'Punch-Drunk Love 2002'.
    `lang` is a 3-letter ISO code from LANGUAGE_REGISTRY (or None = default to
    the picked film's TMDb original language at step 2). `lang_source` is
    "explicit" when the user passed --lang, otherwise None — used only for
    user-facing labels."""
    if not RADARR_URL or not RADARR_API_KEY:
        msg = "❌ Radarr non configurato (mancano RADARR_URL e/o RADARR_API_KEY)."
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return
    if not TMDB_API_KEY:
        msg = "❌ TMDB_API_KEY non configurato — serve per cercare il film."
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    # Heuristic: trailing 4-digit token is a year hint.
    year = None
    parts = query.rsplit(None, 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
        year = int(parts[1])
        query = parts[0]

    candidates = tmdb_search_movies(query, year=year, limit=5)
    if not candidates:
        msg = f"❌ Nessun film trovato per '<b>{query}</b>'. Prova con titolo + anno."
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    # Persist the candidate list under a single hash so the user can pick by tapping.
    film_hash = str(abs(hash(("scarica", tuple(c["tmdb_id"] for c in candidates)))))[:8]
    requests = load_requests()
    pending = requests.setdefault("pending_radarr", {})
    pending[f"candidates:{film_hash}"] = {
        "candidates": candidates,
        "ts": datetime.now().isoformat(),
        "lang": lang,
        "lang_source": lang_source,
    }
    save_requests(requests)

    rows = []
    for c in candidates:
        ryear = f" ({c['year']})" if c["year"] else ""
        lang = (c["original_language"] or "?").upper()
        label = f"🎬 {c['title']}{ryear} — {lang}"[:80]
        rows.append([{"text": label, "callback_data": f"radarr_pick:{film_hash}:{c['tmdb_id']}"}])
    rows.append([{"text": "❌ Annulla", "callback_data": f"radarr_cancel:{film_hash}"}])

    body = f"🔎 Trovati {len(candidates)} risultati per '<b>{query}</b>'. Quale?"
    if progress_msg_id:
        tg_edit_message(progress_msg_id, body, reply_markup={"inline_keyboard": rows})
    else:
        tg_send(body, reply_markup={"inline_keyboard": rows})


def do_scarica_releases(film_hash, tmdb_id, progress_msg_id=None):
    """Step 2: add the film to Radarr (no auto-grab) and show the release list.
    Reads the persisted `lang` / `lang_source` from the candidates entry (set
    at step 1 by `do_scarica_search`); when `lang` is None, defaults to the
    film's TMDb original language and labels the source as "VO from TMDb"."""
    client = RadarrClient()
    requests_state = load_requests()
    pending = requests_state.setdefault("pending_radarr", {})

    entry = pending.get(f"candidates:{film_hash}")
    candidate = None
    explicit_lang = None
    explicit_lang_source = None
    if entry:
        for c in entry.get("candidates", []):
            if c["tmdb_id"] == tmdb_id:
                candidate = c
                break
        explicit_lang = entry.get("lang")
        explicit_lang_source = entry.get("lang_source")
    if not candidate:
        # Re-fetch from TMDb so the flow doesn't dead-end if requests.json was cleared.
        try:
            meta = client.lookup(tmdb_id)
        except Exception as e:
            msg = f"❌ Errore Radarr lookup: {e}"
            if progress_msg_id:
                tg_edit_message(progress_msg_id, msg)
            else:
                tg_send(msg)
            return
        candidate = {
            "tmdb_id": tmdb_id,
            "title": meta.get("title", "?"),
            "year": meta.get("year"),
            "original_language": meta.get("originalLanguage", {}).get("name"),
        }

    title_label = f"{candidate['title']}" + (f" ({candidate['year']})" if candidate.get("year") else "")

    # Resolve the language filter for this request.
    target_lang = explicit_lang
    target_source = explicit_lang_source  # "explicit" | "vo" | None
    if target_lang is None:
        if not TMDB_API_KEY:
            tg_send("ℹ️ TMDB_API_KEY non configurata, scarico senza filtro lingua.")
        else:
            iso1 = tmdb_get_original_language_for_tmdb_id(tmdb_id)
            mapped = tmdb_iso1_to_code(iso1)
            if mapped:
                target_lang = mapped
                target_source = "vo"
            elif iso1:
                tg_send(
                    f"ℹ️ Lingua originale TMDb '<code>{html.escape(iso1)}</code>' "
                    f"non riconosciuta, mostro tutti i rilasci."
                )

    # Check if Radarr already has the file on disk — short-circuit if so.
    try:
        existing = client.find_existing(tmdb_id)
    except Exception as e:
        msg = f"❌ Radarr non raggiungibile: {e}"
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    if existing and existing.get("hasFile"):
        path = existing.get("movieFile", {}).get("path") or existing.get("path", "?")
        msg = (
            f"ℹ️ <b>{title_label}</b> è già scaricato in Radarr.\n"
            f"📁 <code>{path}</code>\n\n"
            f"Per ri-scaricarlo usa direttamente Radarr; per rifare i sub: <code>/cancella {candidate['title']}</code>."
        )
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    if progress_msg_id:
        tg_edit_message(progress_msg_id, f"🔎 Cerco rilasci per <b>{title_label}</b>… (può richiedere qualche secondo)")

    try:
        movie_id = client.add(tmdb_id)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        msg = f"❌ Radarr add HTTP {e.code}: {body[:200]}"
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return
    except Exception as e:
        msg = f"❌ Errore aggiunta in Radarr: {e}"
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    try:
        releases = client.releases(movie_id)
    except Exception as e:
        msg = f"❌ Errore Interactive Search: {e}"
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    if not releases:
        msg = (
            f"⚠️ Nessun rilascio trovato per <b>{title_label}</b> sugli indexer configurati.\n"
            f"Riprova più tardi o controlla i profile in Radarr."
        )
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    # Warm the audio-language cache BEFORE ranking/filtering so the user sees
    # progress feedback while the indexer scrape is in flight. When no target
    # language is set, no scrape is needed and this is a no-op.
    _run_prefetch_with_interlude(
        releases, progress_msg_id, target_lang, title_label,
    )

    # Apply the language pre-filter. Multi / dual-audio releases pass through
    # and will be verified post-grab via ffprobe.
    filtered = filter_releases_by_language(releases, target_lang)
    if target_lang and not filtered:
        flag = LANGUAGE_REGISTRY[target_lang]["flag"]
        msg = (
            f"⚠️ Nessun rilascio in {flag} <b>{target_lang}</b> trovato per "
            f"<b>{title_label}</b>.\n"
            f"Prova senza <code>--lang</code> per vedere tutti i rilasci, "
            f"o riprova più tardi."
        )
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    ranked = rank_releases(filtered, target_code=target_lang)
    # Strip Radarr's bulky fields we don't need to persist (full title list comments etc.)
    slim = []
    for r in ranked:
        slim.append({
            "guid": r.get("guid"),
            "indexerId": r.get("indexerId"),
            "indexer": r.get("indexer"),
            "title": r.get("title"),
            "size": r.get("size"),
            "seeders": r.get("seeders"),
            "leechers": r.get("leechers"),
            "quality": r.get("quality"),
            "languages": r.get("languages"),
            "rejections": r.get("rejections"),
        })

    pending[f"film:{film_hash}"] = {
        "tmdb_id": tmdb_id,
        "movie_id": movie_id,
        "title": candidate["title"],
        "year": candidate.get("year"),
        "releases": slim,
        "lang": target_lang,
        "lang_source": target_source,
        "show_refuted": False,
        "ts": datetime.now().isoformat(),
    }
    pending.pop(f"candidates:{film_hash}", None)
    save_requests(requests_state)

    _render_release_page(film_hash, page=0, progress_msg_id=progress_msg_id)


def do_scarica_redo(tmdb_id, title, year, lang, lang_source, excluded_guid, progress_msg_id=None):
    """Re-render the release list for an already-known TMDb film, applying the
    same language filter and skipping the release the user just rejected.

    Unlike `do_scarica_releases`, this entry point does NOT re-add the film to
    Radarr (it's already there) and does not re-run TMDb disambiguation."""
    if not RADARR_URL or not RADARR_API_KEY:
        msg = "❌ Radarr non configurato (mancano RADARR_URL e/o RADARR_API_KEY)."
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    client = RadarrClient()
    try:
        existing = client.find_existing(tmdb_id)
    except Exception as e:
        msg = f"❌ Radarr non raggiungibile: {e}"
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    title_label = f"{title}" + (f" ({year})" if year else "")
    if not existing:
        msg = f"❌ <b>{title_label}</b> non risulta in Radarr — annullato."
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return
    movie_id = existing.get("id")

    try:
        releases = client.releases(movie_id)
    except Exception as e:
        msg = f"❌ Errore Interactive Search: {e}"
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    if excluded_guid:
        releases = [r for r in releases if r.get("guid") != excluded_guid]

    if not releases:
        msg = f"⚠️ Nessun altro rilascio disponibile per <b>{title_label}</b>."
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    _run_prefetch_with_interlude(
        releases, progress_msg_id, lang, title_label,
    )

    filtered = filter_releases_by_language(releases, lang)
    if lang and not filtered:
        flag = LANGUAGE_REGISTRY[lang]["flag"]
        msg = (
            f"⚠️ Nessun altro rilascio in {flag} <b>{lang}</b> per "
            f"<b>{title_label}</b>."
        )
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    ranked = rank_releases(filtered, target_code=lang)
    slim = []
    for r in ranked:
        slim.append({
            "guid": r.get("guid"),
            "indexerId": r.get("indexerId"),
            "indexer": r.get("indexer"),
            "title": r.get("title"),
            "size": r.get("size"),
            "seeders": r.get("seeders"),
            "leechers": r.get("leechers"),
            "quality": r.get("quality"),
            "languages": r.get("languages"),
            "rejections": r.get("rejections"),
        })

    film_hash = str(abs(hash(("scarica_redo", tmdb_id, datetime.now().isoformat()))))[:8]
    requests_state = load_requests()
    pending = requests_state.setdefault("pending_radarr", {})
    pending[f"film:{film_hash}"] = {
        "tmdb_id": tmdb_id,
        "movie_id": movie_id,
        "title": title,
        "year": year,
        "releases": slim,
        "lang": lang,
        "lang_source": lang_source,
        "show_refuted": False,
        "ts": datetime.now().isoformat(),
    }
    save_requests(requests_state)

    _render_release_page(film_hash, page=0, progress_msg_id=progress_msg_id)


RELEASES_PER_PAGE = 8


def _render_release_page(film_hash, page, progress_msg_id=None):
    requests_state = load_requests()
    entry = requests_state.get("pending_radarr", {}).get(f"film:{film_hash}")
    if not entry:
        msg = "⚠️ Sessione di download scaduta. Lancia di nuovo /scarica."
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    releases = entry["releases"]
    target_code = entry.get("lang")
    show_refuted = bool(entry.get("show_refuted", False))
    visible, refuted_count = _partition_visible_releases(
        releases, target_code, show_refuted,
    )

    # _partition_visible_releases auto-falls-back to the full list when the
    # filter would leave the picker empty; persist that decision so the toggle
    # label matches the rendered state on the next click.
    if not show_refuted and refuted_count > 0 and len(visible) == len(releases):
        show_refuted = True
        entry["show_refuted"] = True
        save_requests(requests_state)

    total_pages = max(1, (len(visible) + RELEASES_PER_PAGE - 1) // RELEASES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * RELEASES_PER_PAGE
    chunk = visible[start:start + RELEASES_PER_PAGE]

    rows = []
    for rel in chunk:
        global_idx = releases.index(rel)
        label = format_release_button(rel, target_code=target_code)
        rows.append([{"text": label, "callback_data": f"radarr_rel:{film_hash}:{global_idx}"}])

    nav = []
    if page > 0:
        nav.append({"text": "◀️ Indietro", "callback_data": f"radarr_page:{film_hash}:{page - 1}"})
    if page < total_pages - 1:
        nav.append({"text": "Avanti ▶️", "callback_data": f"radarr_page:{film_hash}:{page + 1}"})
    if nav:
        rows.append(nav)

    if refuted_count > 0:
        if show_refuted:
            toggle_label = f"🙈 Nascondi {refuted_count} rilasci a bassa probabilità"
        else:
            toggle_label = f"🚫 Mostra altri {refuted_count} rilasci a bassa probabilità"
        rows.append([{
            "text": toggle_label,
            "callback_data": f"radarr_toggle_refuted:{film_hash}:{page}",
        }])

    rows.append([{"text": "❌ Annulla", "callback_data": f"radarr_cancel:{film_hash}"}])

    title_label = entry["title"] + (f" ({entry['year']})" if entry.get("year") else "")
    rejected = sum(1 for r in chunk if r.get("rejections"))
    warning = ""
    if chunk and rejected == len(chunk):
        warning = "\n⚠️ Tutti i rilasci in questa pagina hanno rejection di Radarr — verifica prima di scegliere."

    lang_line = _format_lang_filter_line(entry.get("lang"), entry.get("lang_source"))
    legend = _picker_confidence_legend(
        visible, target_code, include_refuted=(show_refuted and refuted_count > 0),
    )
    body = (
        f"🎬 <b>{title_label}</b>\n"
        f"{lang_line}"
        f"{legend}"
        f"Scegli il rilascio (pagina {page + 1}/{total_pages}, {len(visible)} totali):\n"
        f"<i>quality · size · lingua · seeders</i>{warning}"
    )
    if progress_msg_id:
        tg_edit_message(progress_msg_id, body, reply_markup={"inline_keyboard": rows})
    else:
        tg_send(body, reply_markup={"inline_keyboard": rows})


def _render_release_confirm(film_hash, release_idx, progress_msg_id=None):
    requests_state = load_requests()
    entry = requests_state.get("pending_radarr", {}).get(f"film:{film_hash}")
    if not entry:
        return False
    releases = entry["releases"]
    if not (0 <= release_idx < len(releases)):
        return False
    rel = releases[release_idx]
    langs = detect_release_languages(rel)
    flag = _flag_for(langs)
    quality = ((rel.get("quality") or {}).get("quality") or {}).get("name", "?")
    size = _human_size(rel.get("size"))
    seeders = rel.get("seeders") or 0
    leechers = rel.get("leechers") or 0
    indexer = rel.get("indexer") or "?"
    rejections = rel.get("rejections") or []

    title_label = entry["title"] + (f" ({entry['year']})" if entry.get("year") else "")
    lang_line = _format_lang_filter_line(entry.get("lang"), entry.get("lang_source"))
    confidence_line = ""
    if entry.get("lang"):
        desc = _confidence_description(rel, entry["lang"])
        if desc:
            confidence_line = f"{desc}\n"
    body = (
        f"⬇️ <b>Conferma grab</b>\n\n"
        f"🎬 <b>{title_label}</b>\n"
        f"{lang_line}"
        f"{flag} {'+'.join(langs)} · {quality} · {size}\n"
        f"{confidence_line}"
        f"📡 Indexer: {indexer}\n"
        f"👥 {seeders}↑ / {leechers}↓\n\n"
        f"<i>{rel.get('title','?')[:200]}</i>"
    )
    if rejections:
        body += "\n\n🚫 <b>Radarr non lo avrebbe scelto:</b>\n" + "\n".join(f"  • {r}" for r in rejections)

    rows = [[
        {"text": "⬇️ Conferma grab", "callback_data": f"radarr_grab:{film_hash}:{release_idx}"},
        {"text": "↩️ Cambia release", "callback_data": f"radarr_page:{film_hash}:0"},
    ], [
        {"text": "❌ Annulla", "callback_data": f"radarr_cancel:{film_hash}"},
    ]]
    if progress_msg_id:
        tg_edit_message(progress_msg_id, body, reply_markup={"inline_keyboard": rows})
    else:
        tg_send(body, reply_markup={"inline_keyboard": rows})
    return True


# =============================================================================
# /cerca FLOW (multi-source film download — archive.org + YouTube)
# =============================================================================

# yt-dlp/archive.org results are kept in `requests.json` under a short-lived
# `pending_cerca` namespace so the user can take their time picking from the
# inline buttons without re-running the search.

CERCA_RESULT_LIMIT_PER_SOURCE = 5


def _parse_cerca_query(arg):
    """Split user input into (title, year_or_None). Accepts trailing 4-digit
    year, with or without parentheses: 'Il Ciclone 1996', 'Il Ciclone (1996)'.
    Falls back to (arg, None) when no year token is recognised."""
    if not arg:
        return ("", None)
    cleaned = arg.strip()
    paren_match = re.search(r"\((\d{4})\)\s*$", cleaned)
    if paren_match:
        year = int(paren_match.group(1))
        title = cleaned[: paren_match.start()].strip()
        return (title, year)
    parts = cleaned.rsplit(None, 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
        return (parts[0].strip(), int(parts[1]))
    return (cleaned, None)


def _cerca_film_hash(film_dict):
    return str(abs(hash(("cerca", film_dict.get("tmdb_id"), film_dict.get("title"), film_dict.get("year")))))[:8]


def _parallel_search_sources(title, year):
    """Run archive.org + YouTube searches in parallel. Both calls degrade to
    [] on error; never raises. Returns (archive_results, youtube_results)."""
    query = f"{title} {year}".strip() if year else title
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_archive = pool.submit(search_archive_org, title, year, CERCA_RESULT_LIMIT_PER_SOURCE)
        f_youtube = pool.submit(search_youtube, query, CERCA_RESULT_LIMIT_PER_SOURCE)
        archive_results = []
        youtube_results = []
        for fut in as_completed([f_archive, f_youtube]):
            try:
                value = fut.result()
            except Exception as e:
                log.warning(f"  /cerca source error: {e}")
                value = []
            if fut is f_archive:
                archive_results = value or []
            else:
                youtube_results = value or []
    return archive_results, youtube_results


def _annotate_youtube_quality(results):
    """yt-dlp's flat-playlist mode doesn't expose resolution, so we cannot
    pre-filter by min height. Mark each entry with `quality_hint` derived from
    the title keywords — best-effort; the badge is decorative only."""
    out = []
    for r in results:
        title_lower = (r.get("title") or "").lower()
        if any(tag in title_lower for tag in ("4k", "2160p")):
            hint = "2160p"
        elif "1080" in title_lower:
            hint = "1080p"
        elif "720" in title_lower:
            hint = "720p"
        elif "480" in title_lower or "360" in title_lower:
            hint = "480p"
        else:
            hint = "?"
        new = dict(r)
        new["quality_hint"] = hint
        out.append(new)
    return out


def _format_cerca_button(result):
    """Compact, Telegram-safe inline-button label. Mirrors the user's UX brief:
    `🎞 archive.org · 1080p · 1h35m · 1.2GB` and the YouTube counterpart."""
    duration = _format_duration(result.get("duration_sec"))
    if result["source"] == "archive":
        size = _human_size(result.get("size")) if result.get("size") else "?"
        ident = result.get("identifier") or ""
        label = f"🎞 archive · {duration} · {size} · {ident[:20]}"
    else:
        hint = result.get("quality_hint") or "?"
        title = (result.get("title") or "")[:30]
        label = f"📺 YouTube · {hint} · {duration} · {title}"
        if hint in ("480p", "?"):
            label = f"⚠️ {label}"
    return label[:80]


def do_cerca_search(query, progress_msg_id=None):
    """Step 1: resolve the title via TMDb (when available), then trigger a
    parallel search on archive.org + YouTube. Surfaces the consolidated list as
    inline buttons so the user can pick one."""
    title, year = _parse_cerca_query(query)
    if not title:
        msg = "❌ Titolo mancante. Usa <code>/cerca titolo film [anno]</code>."
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    tmdb_id = None
    canonical_title = title
    canonical_year = year
    if TMDB_API_KEY:
        candidates = tmdb_search_movies(title, year=year, limit=1)
        if candidates:
            top = candidates[0]
            tmdb_id = top.get("tmdb_id")
            canonical_title = top.get("title") or title
            canonical_year = top.get("year") or year

    label = canonical_title + (f" ({canonical_year})" if canonical_year else "")
    if progress_msg_id:
        tg_edit_message(
            progress_msg_id,
            f"🔎 Cerco <b>{html.escape(label)}</b> su archive.org e YouTube…"
        )

    archive_results, youtube_results = _parallel_search_sources(canonical_title, canonical_year)
    youtube_results = _annotate_youtube_quality(youtube_results)

    if not archive_results and not youtube_results:
        msg = (
            f"❌ Nessun risultato trovato per <b>{html.escape(label)}</b> "
            f"né su archive.org né su YouTube.\n"
            f"Prova con un titolo diverso o aggiungi l'anno."
        )
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    combined = []
    for r in archive_results:
        combined.append(r)
    for r in youtube_results:
        combined.append(r)

    film_hash = _cerca_film_hash({"tmdb_id": tmdb_id, "title": canonical_title, "year": canonical_year})
    requests_state = load_requests()
    pending = requests_state.setdefault("pending_cerca", {})
    pending[f"film:{film_hash}"] = {
        "tmdb_id": tmdb_id,
        "title": canonical_title,
        "year": canonical_year,
        "results": combined,
        "ts": datetime.now().isoformat(),
    }
    save_requests(requests_state)

    rows = []
    for idx, r in enumerate(combined):
        rows.append([{
            "text": _format_cerca_button(r),
            "callback_data": f"cerca_pick:{film_hash}:{idx}",
        }])
    rows.append([{"text": "❌ Annulla", "callback_data": f"cerca_cancel:{film_hash}"}])

    warning = ""
    if not archive_results:
        warning = "\n⚠️ archive.org non ha restituito risultati."
    elif not youtube_results:
        warning = "\n⚠️ YouTube non ha restituito risultati."
    body = (
        f"🎬 <b>{html.escape(label)}</b>\n"
        f"Trovati {len(combined)} risultati. Quale scarico?{warning}"
    )
    if progress_msg_id:
        tg_edit_message(progress_msg_id, body, reply_markup={"inline_keyboard": rows})
    else:
        tg_send(body, reply_markup={"inline_keyboard": rows})


def _render_cerca_confirm(film_hash, result_idx, progress_msg_id=None):
    """Show the confirmation card for a single picked result before triggering
    the actual download. Returns False if the session has expired."""
    requests_state = load_requests()
    entry = requests_state.get("pending_cerca", {}).get(f"film:{film_hash}")
    if not entry:
        return False
    results = entry.get("results") or []
    if not (0 <= result_idx < len(results)):
        return False
    r = results[result_idx]
    label = entry["title"] + (f" ({entry['year']})" if entry.get("year") else "")
    duration = _format_duration(r.get("duration_sec"))
    if r["source"] == "archive":
        size = _human_size(r.get("size")) if r.get("size") else "?"
        source_line = f"🎞 archive.org · <code>{html.escape(r.get('identifier') or '')}</code>"
        size_line = f"💾 {size} · {duration}"
        details_line = f"📁 {html.escape(r.get('file_name') or '')}"
    else:
        uploader = r.get("uploader") or "?"
        hint = r.get("quality_hint") or "?"
        warning = " ⚠️ <i>bassa qualità</i>" if hint in ("480p", "?") else ""
        source_line = f"📺 YouTube · <i>{html.escape(uploader)}</i>"
        size_line = f"📐 {hint} · {duration}{warning}"
        details_line = f"🔗 <code>{html.escape(r.get('url') or '')}</code>"

    body = (
        f"🎬 <b>{html.escape(label)}</b>\n"
        f"{source_line}\n"
        f"{size_line}\n"
        f"{details_line}\n\n"
        f"<i>{html.escape(r.get('title') or '')[:200]}</i>"
    )
    rows = [
        [{"text": "⬇️ Conferma download", "callback_data": f"cerca_grab:{film_hash}:{result_idx}"}],
        [{"text": "↩️ Cambia fonte", "callback_data": f"cerca_back:{film_hash}"}],
        [{"text": "❌ Annulla", "callback_data": f"cerca_cancel:{film_hash}"}],
    ]
    if progress_msg_id:
        tg_edit_message(progress_msg_id, body, reply_markup={"inline_keyboard": rows})
    else:
        tg_send(body, reply_markup={"inline_keyboard": rows})
    return True


def _render_cerca_results_page(film_hash, progress_msg_id=None):
    """Re-render the results list (used when the user clicks 'Cambia fonte')."""
    requests_state = load_requests()
    entry = requests_state.get("pending_cerca", {}).get(f"film:{film_hash}")
    if not entry:
        return False
    results = entry.get("results") or []
    label = entry["title"] + (f" ({entry['year']})" if entry.get("year") else "")
    rows = []
    for idx, r in enumerate(results):
        rows.append([{
            "text": _format_cerca_button(r),
            "callback_data": f"cerca_pick:{film_hash}:{idx}",
        }])
    rows.append([{"text": "❌ Annulla", "callback_data": f"cerca_cancel:{film_hash}"}])
    body = (
        f"🎬 <b>{html.escape(label)}</b>\n"
        f"Trovati {len(results)} risultati. Quale scarico?"
    )
    if progress_msg_id:
        tg_edit_message(progress_msg_id, body, reply_markup={"inline_keyboard": rows})
    else:
        tg_send(body, reply_markup={"inline_keyboard": rows})
    return True


def do_cerca_download(film_hash, result_idx, msg_id=None):
    """Queue-worker entry point: download the picked result, remux to MKV,
    install under /media/films. Reports progress in place via tg_edit_message.
    All recoverable failures (network, encoder, disk) are surfaced as a
    Telegram message instead of a stack trace."""
    requests_state = load_requests()
    entry = requests_state.get("pending_cerca", {}).get(f"film:{film_hash}")
    if not entry:
        if msg_id:
            tg_edit_message(msg_id, "⚠️ Sessione scaduta. Rilancia <code>/cerca</code>.")
        return

    results = entry.get("results") or []
    if not (0 <= result_idx < len(results)):
        if msg_id:
            tg_edit_message(msg_id, "⚠️ Risultato non valido. Rilancia <code>/cerca</code>.")
        return

    r = results[result_idx]
    title = entry["title"]
    year = entry.get("year")
    label = title + (f" ({year})" if year else "")

    # Use /tmp as the scratch dir so half-downloads never pollute /media/films.
    tmp_dir = tempfile.mkdtemp(prefix="cerca_")
    log.info(f"  /cerca: downloading '{label}' source={r['source']} → {tmp_dir}")

    def render_dl_progress(downloaded, total):
        if not msg_id:
            return
        if total:
            bar = _progress_bar(downloaded, total)
            tg_edit_message(
                msg_id,
                f"⬇️ Scarico <b>{html.escape(label)}</b>\n"
                f"{bar}\n"
                f"{_human_size(downloaded)} / {_human_size(total)}"
            )

    try:
        if r["source"] == "archive":
            ext = os.path.splitext(r.get("file_name") or "movie.mp4")[1] or ".mp4"
            raw_path = os.path.join(tmp_dir, f"movie{ext}")
            if msg_id:
                tg_edit_message(
                    msg_id,
                    f"⬇️ Scarico <b>{html.escape(label)}</b> da archive.org…"
                )
            download_archive_org(r["url"], raw_path, progress_cb=render_dl_progress)
        else:
            if msg_id:
                tg_edit_message(
                    msg_id,
                    f"⬇️ Scarico <b>{html.escape(label)}</b> da YouTube…"
                )
            raw_path = download_youtube(r["video_id"], tmp_dir)

        if msg_id:
            tg_edit_message(
                msg_id,
                f"🔄 Remux MKV per <b>{html.escape(label)}</b>…"
            )
        mkv_path = os.path.join(tmp_dir, "final.mkv")
        if raw_path.endswith(".mkv"):
            mkv_path = raw_path
        else:
            remux_to_mkv(raw_path, mkv_path)

        if msg_id:
            tg_edit_message(
                msg_id,
                f"📦 Installo in libreria <b>{html.escape(label)}</b>…"
            )
        final_path = install_to_library(mkv_path, title, year)

        # Clear the pending entry; we don't keep cerca state around once the
        # download is done.
        rs = load_requests()
        rs.get("pending_cerca", {}).pop(f"film:{film_hash}", None)
        save_requests(rs)

        if msg_id:
            tg_edit_message(
                msg_id,
                f"✅ <b>{html.escape(label)}</b> pronto!\n"
                f"📁 <code>{html.escape(final_path)}</code>\n"
                f"Visibile su Emby/Jellyfin. I sub ITA arrivano al prossimo giro di scansione."
            )
    except CercaDownloadError as e:
        log.warning(f"  /cerca download failed for '{label}': {e}")
        if msg_id:
            tg_edit_message(
                msg_id,
                f"❌ Download fallito per <b>{html.escape(label)}</b>:\n"
                f"<code>{html.escape(str(e))}</code>"
            )
    except Exception as e:
        log.error(f"  /cerca unexpected error for '{label}': {e}", exc_info=True)
        if msg_id:
            tg_edit_message(
                msg_id,
                f"❌ Errore inatteso durante <b>{html.escape(label)}</b>:\n"
                f"<code>{html.escape(str(e))[:200]}</code>"
            )
    finally:
        try:
            import shutil as _shutil
            _shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def do_retranslate(query, state, progress_msg_id=None):
    """Force re-translation of all .it.srt matching `query`:
    delete the existing .it.srt, then run the standard translate-prep flow
    (which will find them eligible because no .it.srt remains).
    The user still confirms via the cost-button to avoid accidental spend."""
    matches = find_videos_by_name(query)
    if not matches:
        msg = f"❌ Nessun video trovato per '<b>{query}</b>'"
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    deleted = 0
    no_en = 0
    for video_path in matches:
        sub_path = os.path.splitext(video_path)[0] + ".it.srt"
        if not os.path.exists(sub_path):
            continue
        if not find_english_sub(video_path):
            no_en += 1
            continue
        try:
            os.remove(sub_path)
            deleted += 1
            state["downloaded"].pop(video_path, None)
            state["asked"].pop(video_path, None)
        except Exception as e:
            log.warning(f"  retranslate: failed to delete {sub_path}: {e}")

    save_state(state)

    if deleted == 0:
        warn = f"⚠️ Nessuna traduzione da rifare per '<b>{query}</b>'."
        if no_en:
            warn += f"\n{no_en} video hanno .it.srt ma manca .en.srt — usa /cancella per ri-cercare."
        if progress_msg_id:
            tg_edit_message(progress_msg_id, warn)
        else:
            tg_send(warn)
        return

    if progress_msg_id:
        tg_edit_message(progress_msg_id, f"🗑 Cancellati {deleted} .it.srt. Preparo la ritraduzione...")

    do_translate_prep(query, state, progress_msg_id=progress_msg_id)


def _cmd_stato(arg, state, excludes):
    fresh = load_state()
    pending = sum(1 for v in fresh["asked"].values() if v["status"] == "pending")
    downloaded = len(fresh["downloaded"])
    failed = sum(1 for v in fresh["asked"].values() if v["status"] == "failed")
    excluded = len(excludes)
    tg_send(
        f"📊 <b>Stato</b>\n\n"
        f"⏳ In attesa di risposta: {pending}\n"
        f"✅ Scaricati: {downloaded}\n"
        f"❌ Non trovati: {failed}\n"
        f"🚫 Cartelle escluse: {excluded}\n"
        f"   ({', '.join(sorted(excludes)) if excludes else 'nessuna'})"
    )


def _cmd_scansiona(arg, state, excludes):
    tg_send("🔍 Avvio scansione manuale...")
    missing = scan_missing(state, excludes)
    tg_send(f"🔍 Trovati {len(missing)} video senza sub ITA")


def _cmd_costi(arg, state, excludes):
    s = load_state()
    sonnet = s.get("claude_costs", {})
    polish = s.get("claude_polish_costs", {})
    deepl = s.get("deepl_stats", {})

    deepl_chars = deepl.get("total_chars", 0)
    deepl_runs = deepl.get("translations", 0)
    deepl_eq_cost = deepl_chars * DEEPL_PRICE_PER_MILLION_CHARS / 1_000_000

    pol_in = polish.get("total_input_tokens", 0)
    pol_out = polish.get("total_output_tokens", 0)
    pol_cost = polish.get("total_cost_usd", 0.0)
    pol_runs = polish.get("polishes", 0)

    son_in = sonnet.get("total_input_tokens", 0)
    son_out = sonnet.get("total_output_tokens", 0)
    son_cost = sonnet.get("total_cost_usd", 0.0)
    son_runs = sonnet.get("translations", 0)

    total_paid = pol_cost + son_cost

    sections = [f"💰 <b>Costi traduzione</b>"]

    sections.append(
        f"\n🌐 <b>DeepL</b> (primario)\n"
        f"  Caratteri: {deepl_chars:,}\n"
        f"  Traduzioni: {deepl_runs}\n"
        f"  Costo equivalente: ${deepl_eq_cost:.4f} (gratis fino a 500k/mese su free tier)"
    )

    sections.append(
        f"\n✨ <b>Polish</b> (Claude Haiku 4.5)\n"
        f"  Token: {pol_in:,} in + {pol_out:,} out\n"
        f"  Polish eseguiti: {pol_runs}\n"
        f"  Costo: ${pol_cost:.4f}"
    )

    sections.append(
        f"\n🤖 <b>Fallback</b> (Claude Sonnet)\n"
        f"  Token: {son_in:,} in + {son_out:,} out\n"
        f"  Traduzioni: {son_runs}\n"
        f"  Costo: ${son_cost:.4f}"
    )

    sections.append(f"\n📈 <b>Totale Claude: ${total_paid:.4f}</b>")
    tg_send("\n".join(sections))


def _cmd_esclusi(arg, state, excludes):
    if excludes:
        lines = "\n".join(f"  • {e}" for e in sorted(excludes))
        tg_send(f"🚫 <b>Cartelle escluse:</b>\n{lines}")
    else:
        tg_send("🚫 Nessuna cartella esclusa")


def _cmd_reset(arg, state, excludes):
    state["asked"] = {}
    state["downloaded"] = {}
    save_state(state)
    tg_send("🔄 Cache resettata. La prossima scansione ripartirà da zero.")


def _cmd_sincronizza(arg, state, excludes):
    if not arg:
        tg_send(
            "Usa: <code>/sincronizza nome serie o film</code>\n"
            "Es: <code>/sincronizza Pluribus</code> oppure <code>/sincronizza all</code> per tutti."
        )
        return
    pos = queue_position()
    result = tg_send(
        f"🔄 Sync <b>{arg}</b> in coda..."
        + (f"\n⏳ Posizione {pos + 1}" if pos > 0 else "")
    )
    msg_id = result["result"]["message_id"] if result and result.get("ok") else None
    download_queue.put({"type": "sync", "query": arg, "msg_id": msg_id})


def _cmd_cancella(arg, state, excludes):
    if not arg:
        tg_send(
            "Usa: <code>/cancella nome film o serie</code>\n"
            "Cancella tutti i sub (IT/EN) e rimette in coda per nuova ricerca."
        )
        return
    offer_delete(arg, state)


def _cmd_traduci(arg, state, excludes):
    if not arg:
        tg_send(
            "Usa: <code>/traduci nome film o serie</code>\n"
            "Sincronizza .en.srt all'audio e chiede conferma per tradurre EN→IT."
        )
        return
    pos = queue_position()
    result = tg_send(
        f"🔄 Preparo traduzione <b>{arg}</b>..."
        + (f"\n⏳ Posizione {pos + 1}" if pos > 0 else "")
    )
    msg_id = result["result"]["message_id"] if result and result.get("ok") else None
    download_queue.put({"type": "translate_prep", "query": arg, "msg_id": msg_id})


def _cmd_ritraduci(arg, state, excludes):
    if not arg:
        tg_send(
            "Usa: <code>/ritraduci nome film o serie</code>\n"
            "Cancella il .it.srt esistente e ritraduce dal .en.srt (senza ri-scaricare nulla)."
        )
        return
    pos = queue_position()
    result = tg_send(
        f"🔁 Preparo ritraduzione <b>{arg}</b>..."
        + (f"\n⏳ Posizione {pos + 1}" if pos > 0 else "")
    )
    msg_id = result["result"]["message_id"] if result and result.get("ok") else None
    download_queue.put({"type": "retranslate", "query": arg, "msg_id": msg_id})


def _cmd_pulisci(arg, state, excludes):
    pos = queue_position()
    result = tg_send(
        f"🧹 Cleanup in coda..."
        + (f"\n⏳ Posizione {pos + 1}" if pos > 0 else "")
    )
    msg_id = result["result"]["message_id"] if result and result.get("ok") else None
    download_queue.put({"type": "cleanup", "msg_id": msg_id})


def _cmd_scarica(arg, state, excludes):
    if not arg:
        tg_send(
            "Usa: <code>/scarica nome film [anno] [--lang CODE]</code>\n"
            "Es: <code>/scarica Punch-Drunk Love 2002 --lang ITA</code>\n"
            "Senza <code>--lang</code> uso la lingua originale TMDb. "
            "Scorciatoia: <code>/ita &lt;titolo&gt;</code>."
        )
        return
    if not RADARR_URL or not RADARR_API_KEY:
        tg_send("❌ Radarr non configurato — imposta RADARR_URL e RADARR_API_KEY.")
        return

    query, lang_code, raw_lang = extract_lang_flag(arg)
    if raw_lang and lang_code is None:
        tg_send(
            f"❌ Lingua sconosciuta: <code>{html.escape(raw_lang)}</code>.\n"
            f"Codici accettati: {', '.join(sorted(LANGUAGE_REGISTRY))}."
        )
        return
    if not query:
        tg_send(
            "Usa: <code>/scarica nome film [anno] [--lang CODE]</code>\n"
            "Mi manca il titolo del film."
        )
        return

    pos = queue_position()
    lang_hint = f"\n🔤 Filtro lingua: {LANGUAGE_REGISTRY[lang_code]['flag']} {lang_code}" if lang_code else ""
    result = tg_send(
        f"🔎 Cerco <b>{html.escape(query)}</b> su TMDb…{lang_hint}"
        + (f"\n⏳ Posizione {pos + 1}" if pos > 0 else "")
    )
    msg_id = result["result"]["message_id"] if result and result.get("ok") else None
    download_queue.put({
        "type": "scarica",
        "query": query,
        "msg_id": msg_id,
        "lang": lang_code,
        "lang_source": "explicit" if lang_code else None,
    })


def _cmd_ita(arg, state, excludes):
    """Shortcut for `/scarica <arg> --lang ITA`. Strips any user-supplied
    --lang flag so the alias semantics are unambiguous."""
    if not arg:
        tg_send(
            "Usa: <code>/ita nome film [anno]</code>\n"
            "Es: <code>/ita Punch-Drunk Love 2002</code>\n"
            "Scorciatoia per <code>/scarica ... --lang ITA</code>."
        )
        return
    cleaned, _, _ = extract_lang_flag(arg)
    _cmd_scarica(f"{cleaned} --lang ITA".strip(), state, excludes)


def _cmd_sub(arg, state, excludes):
    """Manual sub search across the local library. Used to be exposed as
    /cerca; now /cerca is the multi-source film downloader and library search
    keeps a dedicated /sub canonical (still discoverable via free text)."""
    if not arg:
        tg_send(
            "Usa: <code>/sub nome film o serie</code>\n"
            "Oppure scrivi semplicemente il nome senza slash."
        )
        return
    search_and_offer(arg, state)


def _cmd_cerca(arg, state, excludes):
    """Multi-source film download. TMDb disambiguation, then a parallel search
    on archive.org + YouTube. Picked result is downloaded, remuxed to MKV and
    installed under /media/films so Emby/Jellyfin pick it up automatically."""
    if not arg:
        tg_send(
            "Usa: <code>/cerca titolo film [anno]</code>\n"
            "Es: <code>/cerca Il Ciclone 1996</code>\n"
            "Cerca su archive.org e YouTube e ti propone i risultati."
        )
        return
    pos = queue_position()
    result = tg_send(
        f"🔎 Cerco <b>{html.escape(arg)}</b> su TMDb…"
        + (f"\n⏳ Posizione {pos + 1}" if pos > 0 else "")
    )
    msg_id = result["result"]["message_id"] if result and result.get("ok") else None
    download_queue.put({
        "type": "cerca_search",
        "query": arg,
        "msg_id": msg_id,
    })


def _cmd_coda(arg, state, excludes):
    """Snapshot of the current download queue."""
    try:
        snapshot = list(download_queue.queue)
    except Exception:
        snapshot = []
    if not snapshot:
        tg_send("📋 <b>Coda</b>\nNessun job in coda.")
        return

    lines = ["📋 <b>Coda</b>"]
    for i, job in enumerate(snapshot, 1):
        jt = job.get("type", "?")
        # Escape user-supplied strings (queries, filenames) before HTML-interpolating.
        if jt == "single":
            desc = f"⬇️ download <i>{html.escape(os.path.basename(job.get('path', '?')))}</i>"
        elif jt == "batch":
            desc = f"⬇️ batch ({len(job.get('paths', []))} file)"
        elif jt == "translate":
            desc = f"🤖 traduzione batch ({len(job.get('paths', []))} file)"
        elif jt == "translate_prep":
            desc = f"🔄 prep traduzione <i>{html.escape(str(job.get('query', '?')))}</i>"
        elif jt == "retranslate":
            desc = f"🔁 ritraduzione <i>{html.escape(str(job.get('query', '?')))}</i>"
        elif jt == "sync":
            desc = f"🔄 sync <i>{html.escape(str(job.get('query', '?')))}</i>"
        elif jt == "cleanup":
            desc = f"🧹 cleanup placeholder"
        else:
            desc = html.escape(jt)
        lines.append(f"{i}. {desc}")
    tg_send("\n".join(lines))


def _cmd_log(arg, state, excludes):
    """Send the last N log lines (default 30, max 200)."""
    try:
        n = int(arg) if arg else 30
    except ValueError:
        tg_send("Usa: <code>/log [n]</code> dove n è il numero di righe (max 200).")
        return
    n = max(1, min(n, 200))

    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read_size = min(size, 60_000)
            f.seek(size - read_size)
            tail = f.read().decode("utf-8", errors="ignore")
    except Exception as e:
        tg_send(f"❌ Impossibile leggere {LOG_FILE}: {e}")
        return

    lines = tail.splitlines()[-n:]
    body = "\n".join(lines)
    if len(body) > 3500:
        body = body[-3500:]
        body = body[body.find("\n") + 1:]
    # Log lines can contain '<'/'>' from URLs, file paths, etc. — escape so
    # Telegram's HTML parser doesn't reject the message.
    tg_send(f"📜 <b>Ultime {len(lines)} righe di log</b>\n<pre>{html.escape(body)}</pre>")


def _cmd_falliti(arg, state, excludes):
    """List videos in 'failed' state and offer to retry them all."""
    fresh = load_state()
    failed = [(p, info) for p, info in fresh["asked"].items() if info.get("status") == "failed"]
    if not failed:
        tg_send("✅ Nessun video in stato fallito.")
        return

    failed.sort(key=lambda x: x[1].get("time", ""), reverse=True)
    shown = failed[:15]
    lines = [f"❌ <b>{len(failed)} video falliti</b>"]
    for p, _ in shown:
        lines.append(f"  • <i>{friendly_name(p)}</i>")
    if len(failed) > 15:
        lines.append(f"  …e altri {len(failed) - 15}")

    retry_hash = str(abs(hash(("retry", tuple(p for p, _ in failed)))))[:8]
    batches = load_batches()
    batches[retry_hash] = {"paths": [p for p, _ in failed], "type": "retry_failed"}
    save_batches(batches)

    keyboard = {"inline_keyboard": [[
        {"text": f"🔄 Riprova tutti ({len(failed)})", "callback_data": f"retry_failed:{retry_hash}"},
        {"text": "❌ Annulla", "callback_data": f"retry_cancel:{retry_hash}"},
    ]]}
    tg_send("\n".join(lines), reply_markup=keyboard)


# Registry — single source of truth for /aiuto and the dispatcher.
# `canonical` is the form shown in /aiuto. `aliases` are interchangeable.
# `group` controls the section in /aiuto.
COMMANDS = [
    # Search & download
    {"canonical": "/sub", "aliases": [], "handler": _cmd_sub,
     "group": "Cerca & scarica", "args": "<nome>",
     "desc": "Cerca nella libreria (o scrivi direttamente il nome senza slash)"},
    {"canonical": "/cerca", "aliases": ["/search", "/find"], "handler": _cmd_cerca,
     "group": "Cerca & scarica", "args": "<titolo [anno]>",
     "desc": "Cerca un film su archive.org + YouTube e lo scarica in /media/films"},
    {"canonical": "/scarica", "aliases": ["/download", "/req"], "handler": _cmd_scarica,
     "group": "Cerca & scarica", "args": "<nome [anno]> [--lang CODE]",
     "desc": "Richiedi un nuovo film via Radarr: scegli il rilascio (qualità, lingua, indexer). Senza --lang, default = lingua originale TMDb"},
    {"canonical": "/ita", "aliases": [], "handler": _cmd_ita,
     "group": "Cerca & scarica", "args": "<nome [anno]>",
     "desc": "Scorciatoia per /scarica con --lang ITA"},

    # Sub management
    {"canonical": "/sincronizza", "aliases": ["/sync"], "handler": _cmd_sincronizza,
     "group": "Gestione sub", "args": "<nome|all>",
     "desc": "Riallinea sub esistenti all'audio"},
    {"canonical": "/traduci", "aliases": ["/translate", "/tr", "/t"], "handler": _cmd_traduci,
     "group": "Gestione sub", "args": "<nome>",
     "desc": "Traduce .en.srt → .it.srt (DeepL + Claude polish)"},
    {"canonical": "/ritraduci", "aliases": ["/retranslate", "/rt"], "handler": _cmd_ritraduci,
     "group": "Gestione sub", "args": "<nome>",
     "desc": "Cancella .it.srt e ritraduce dal .en.srt esistente"},
    {"canonical": "/cancella", "aliases": ["/delete", "/del"], "handler": _cmd_cancella,
     "group": "Gestione sub", "args": "<nome>",
     "desc": "Cancella tutti i sub e riaccoda per nuova ricerca"},

    # Status & maintenance
    {"canonical": "/stato", "aliases": ["/status", "/st"], "handler": _cmd_stato,
     "group": "Stato & manutenzione", "args": "",
     "desc": "Riepilogo dello stato attuale"},
    {"canonical": "/coda", "aliases": ["/queue", "/q"], "handler": _cmd_coda,
     "group": "Stato & manutenzione", "args": "",
     "desc": "Mostra i job attualmente in coda"},
    {"canonical": "/costi", "aliases": ["/costs", "/cost"], "handler": _cmd_costi,
     "group": "Stato & manutenzione", "args": "",
     "desc": "Costi DeepL + Claude polish + Claude fallback"},
    {"canonical": "/falliti", "aliases": ["/failed"], "handler": _cmd_falliti,
     "group": "Stato & manutenzione", "args": "",
     "desc": "Lista video falliti, con pulsante per riprovare tutti"},
    {"canonical": "/esclusi", "aliases": ["/excludes"], "handler": _cmd_esclusi,
     "group": "Stato & manutenzione", "args": "",
     "desc": "Lista cartelle escluse dalla scansione"},
    {"canonical": "/log", "aliases": ["/logs"], "handler": _cmd_log,
     "group": "Stato & manutenzione", "args": "[n]",
     "desc": "Ultime n righe di log (default 30, max 200)"},
    {"canonical": "/scansiona", "aliases": ["/scan"], "handler": _cmd_scansiona,
     "group": "Stato & manutenzione", "args": "",
     "desc": "Forza una scansione manuale"},
    {"canonical": "/pulisci", "aliases": ["/cleanup"], "handler": _cmd_pulisci,
     "group": "Stato & manutenzione", "args": "",
     "desc": "Trova e rimuove sub placeholder/VIP"},
    {"canonical": "/reset", "aliases": ["/azzera"], "handler": _cmd_reset,
     "group": "Stato & manutenzione", "args": "",
     "desc": "Azzera la cache (riparte da zero)"},

    # Help (defined last so it can introspect the others)
    {"canonical": "/aiuto", "aliases": ["/help", "/?"], "handler": None,
     "group": "Stato & manutenzione", "args": "",
     "desc": "Mostra questo messaggio"},
]


def _all_aliases():
    """Flat list of every accepted command form (canonical + aliases)."""
    out = []
    for c in COMMANDS:
        out.append(c["canonical"])
        out.extend(c["aliases"])
    return out


def _find_command(token):
    """Match a slash-token (e.g. '/t') to a command spec; None if not found."""
    token = token.lower()
    for c in COMMANDS:
        if token == c["canonical"] or token in c["aliases"]:
            return c
    return None


def _suggest_command(token):
    """Return the closest known command alias to `token` if within edit distance 2."""
    best = None
    best_dist = 99
    for alias in _all_aliases():
        d = _levenshtein(token, alias)
        if d < best_dist:
            best_dist = d
            best = alias
    if best_dist <= 2:
        return best
    return None


def _cmd_aiuto(arg, state, excludes):
    groups = {}
    for c in COMMANDS:
        groups.setdefault(c["group"], []).append(c)

    section_order = ["Cerca & scarica", "Gestione sub", "Stato & manutenzione"]
    emoji = {"Cerca & scarica": "🔍", "Gestione sub": "🔄", "Stato & manutenzione": "ℹ️"}

    lines = ["🤖 <b>Sub ITA Fetcher — comandi</b>"]
    for section in section_order:
        cmds = groups.get(section, [])
        if not cmds:
            continue
        lines.append(f"\n{emoji.get(section, '•')} <b>{section}</b>")
        for c in cmds:
            # HTML-escape the args placeholder: it contains literal `<nome>` etc.
            # which Telegram's HTML parser would otherwise reject as an invalid tag.
            arg_part = f" <code>{html.escape(c['args'])}</code>" if c["args"] else ""
            aliases_part = ""
            if c["aliases"]:
                aliases_part = f"  <i>(alias: {', '.join(c['aliases'])})</i>"
            lines.append(f"  <code>{c['canonical']}</code>{arg_part} — {c['desc']}{aliases_part}")

    lines.append(
        "\n💡 <b>Suggerimento:</b> scrivi direttamente il nome di un film/serie "
        "(senza slash) per cercarlo nella libreria."
    )
    tg_send("\n".join(lines))


# Wire /aiuto handler now that _cmd_aiuto is defined.
for _c in COMMANDS:
    if _c["canonical"] == "/aiuto":
        _c["handler"] = _cmd_aiuto
        break


def dispatch_command(text, state, excludes):
    """Try to dispatch `text` as a slash command. Returns True if handled
    (including unknown-slash with a 'did you mean?' reply); False to let the
    caller fall through to free-text search."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False

    parts = stripped.split(None, 1)
    token = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    spec = _find_command(token)
    if spec:
        spec["handler"](arg, state, excludes)
        return True

    suggestion = _suggest_command(token)
    if suggestion:
        tg_send(
            f"❓ Comando sconosciuto <code>{token}</code>. "
            f"Forse intendevi <code>{suggestion}</code>?\n"
            f"Scrivi <code>/aiuto</code> per la lista completa."
        )
    else:
        tg_send(
            f"❓ Comando sconosciuto <code>{token}</code>.\n"
            f"Scrivi <code>/aiuto</code> per la lista completa."
        )
    return True


def process_callbacks(state, excludes):
    """Check for Telegram callback responses."""
    offset = state.get("last_offset", 0)
    updates = tg_get_updates(offset)

    for update in updates:
        state["last_offset"] = update["update_id"] + 1

        # Handle callback queries (button presses)
        cb = update.get("callback_query")
        if cb:
            cb_id = cb["id"]
            data = cb.get("data", "")
            msg_id = cb.get("message", {}).get("message_id")

            if ":" not in data:
                tg_answer_callback(cb_id, "⚠️ Errore")
                continue

            action, path_hash = data.split(":", 1)
            video_path = find_path_by_hash(state, path_hash)

            # /scarica callbacks: film pick, release pick, pagination, grab, cancel.
            # Hash carried in `data` is the film_hash, not a video path hash, so we
            # short-circuit the path-hash lookup that runs further down.
            if action in ("radarr_pick", "radarr_rel", "radarr_page", "radarr_grab", "radarr_cancel", "radarr_toggle_refuted"):
                # data format is action:film_hash[:extra]
                _, _, rest = data.partition(":")
                pieces = rest.split(":")
                film_hash = pieces[0] if pieces else ""

                if action == "radarr_cancel":
                    tg_answer_callback(cb_id, "❌ Annullato")
                    if msg_id:
                        tg_edit_message(msg_id, "❌ Richiesta annullata.")
                    rs = load_requests()
                    pending = rs.setdefault("pending_radarr", {})
                    pending.pop(f"candidates:{film_hash}", None)
                    pending.pop(f"film:{film_hash}", None)
                    save_requests(rs)
                    continue

                if action == "radarr_pick" and len(pieces) >= 2:
                    try:
                        tmdb_id = int(pieces[1])
                    except ValueError:
                        tg_answer_callback(cb_id, "⚠️ Errore")
                        continue
                    tg_answer_callback(cb_id, "🔎 Cerco rilasci...")
                    do_scarica_releases(film_hash, tmdb_id, progress_msg_id=msg_id)
                    continue

                if action == "radarr_page" and len(pieces) >= 2:
                    try:
                        page = int(pieces[1])
                    except ValueError:
                        tg_answer_callback(cb_id, "⚠️ Errore")
                        continue
                    tg_answer_callback(cb_id, "")
                    _render_release_page(film_hash, page, progress_msg_id=msg_id)
                    continue

                if action == "radarr_toggle_refuted" and len(pieces) >= 2:
                    try:
                        page = int(pieces[1])
                    except ValueError:
                        tg_answer_callback(cb_id, "⚠️ Errore")
                        continue
                    rs = load_requests()
                    pending = rs.setdefault("pending_radarr", {})
                    entry = pending.get(f"film:{film_hash}")
                    if not entry:
                        tg_answer_callback(cb_id, "⚠️ Sessione scaduta")
                        continue
                    entry["show_refuted"] = not entry.get("show_refuted", False)
                    save_requests(rs)
                    tg_answer_callback(cb_id, "")
                    _render_release_page(film_hash, page, progress_msg_id=msg_id)
                    continue

                if action == "radarr_rel" and len(pieces) >= 2:
                    try:
                        release_idx = int(pieces[1])
                    except ValueError:
                        tg_answer_callback(cb_id, "⚠️ Errore")
                        continue
                    if not _render_release_confirm(film_hash, release_idx, progress_msg_id=msg_id):
                        tg_answer_callback(cb_id, "⚠️ Sessione scaduta")
                    else:
                        tg_answer_callback(cb_id, "")
                    continue

                if action == "radarr_grab" and len(pieces) >= 2:
                    try:
                        release_idx = int(pieces[1])
                    except ValueError:
                        tg_answer_callback(cb_id, "⚠️ Errore")
                        continue
                    rs = load_requests()
                    pending = rs.setdefault("pending_radarr", {})
                    entry = pending.get(f"film:{film_hash}")
                    if not entry or not (0 <= release_idx < len(entry["releases"])):
                        tg_answer_callback(cb_id, "⚠️ Sessione scaduta")
                        if msg_id:
                            tg_edit_message(msg_id, "⚠️ Sessione scaduta. Lancia di nuovo /scarica.")
                        continue
                    chosen = entry["releases"][release_idx]
                    try:
                        RadarrClient().grab(chosen["guid"], chosen["indexerId"])
                    except Exception as e:
                        tg_answer_callback(cb_id, "❌ Errore")
                        if msg_id:
                            tg_edit_message(msg_id, f"❌ Errore grab: {e}")
                        continue
                    tg_answer_callback(cb_id, "📥 Avviato")

                    title_label = entry["title"] + (f" ({entry['year']})" if entry.get("year") else "")
                    langs = detect_release_languages(chosen)
                    required_lang = entry.get("lang")
                    # Only single-language entries trigger a post-grab audio
                    # check. MULTI / DUAL is a marker on the release, not a
                    # verification target.
                    if required_lang and LANGUAGE_REGISTRY.get(required_lang, {}).get("verify"):
                        required_lang = None
                    pending[f"download:{entry['tmdb_id']}"] = {
                        "title": entry["title"],
                        "year": entry.get("year"),
                        "tmdb_id": entry["tmdb_id"],
                        "movie_id": entry["movie_id"],
                        "indexer": chosen.get("indexer"),
                        "release_title": chosen.get("title"),
                        "release_languages": langs,
                        "release_guid": chosen.get("guid"),
                        "release_indexer_id": chosen.get("indexerId"),
                        "required_lang": required_lang,
                        "lang_source": entry.get("lang_source"),
                        "film_hash": film_hash,
                        "requested_at": datetime.now().isoformat(),
                        "telegram_msg_id": msg_id,
                    }
                    pending.pop(f"film:{film_hash}", None)
                    save_requests(rs)
                    if msg_id:
                        lang_line = _format_lang_filter_line(
                            entry.get("lang"), entry.get("lang_source")
                        )
                        tg_edit_message(
                            msg_id,
                            f"📥 <b>{title_label}</b> in download via <i>{chosen.get('indexer')}</i>.\n"
                            f"{lang_line}"
                            f"Ti avviso quando arriva il file + sub ITA."
                        )
                    continue

                continue

            # /cerca callbacks: result pick, back-to-list, grab confirm, cancel.
            # data format: `cerca_<action>:<film_hash>[:<result_idx>]`.
            if action in ("cerca_pick", "cerca_back", "cerca_grab", "cerca_cancel"):
                _, _, rest = data.partition(":")
                pieces = rest.split(":")
                film_hash = pieces[0] if pieces else ""

                if action == "cerca_cancel":
                    tg_answer_callback(cb_id, "❌ Annullato")
                    if msg_id:
                        tg_edit_message(msg_id, "❌ Richiesta annullata.")
                    rs = load_requests()
                    rs.get("pending_cerca", {}).pop(f"film:{film_hash}", None)
                    save_requests(rs)
                    continue

                if action == "cerca_back":
                    tg_answer_callback(cb_id, "")
                    _render_cerca_results_page(film_hash, progress_msg_id=msg_id)
                    continue

                if action == "cerca_pick" and len(pieces) >= 2:
                    try:
                        result_idx = int(pieces[1])
                    except ValueError:
                        tg_answer_callback(cb_id, "⚠️ Errore")
                        continue
                    if not _render_cerca_confirm(film_hash, result_idx, progress_msg_id=msg_id):
                        tg_answer_callback(cb_id, "⚠️ Sessione scaduta")
                    else:
                        tg_answer_callback(cb_id, "")
                    continue

                if action == "cerca_grab" and len(pieces) >= 2:
                    try:
                        result_idx = int(pieces[1])
                    except ValueError:
                        tg_answer_callback(cb_id, "⚠️ Errore")
                        continue
                    rs = load_requests()
                    entry = rs.get("pending_cerca", {}).get(f"film:{film_hash}")
                    if not entry or not (0 <= result_idx < len(entry.get("results") or [])):
                        tg_answer_callback(cb_id, "⚠️ Sessione scaduta")
                        if msg_id:
                            tg_edit_message(msg_id, "⚠️ Sessione scaduta. Rilancia <code>/cerca</code>.")
                        continue
                    tg_answer_callback(cb_id, "⬇️ Avvio")
                    download_queue.put({
                        "type": "cerca_download",
                        "film_hash": film_hash,
                        "result_idx": result_idx,
                        "msg_id": msg_id,
                    })
                    continue

                continue

            # Audio-mismatch callbacks emitted by _notify_radarr_request_ready
            # when the grabbed file's ffprobe audio does NOT contain the
            # required_lang track. data format: `lang_keep:<tmdb_id>` /
            # `lang_redo:<tmdb_id>`.
            if action in ("lang_keep", "lang_redo"):
                _, _, rest = data.partition(":")
                try:
                    tmdb_id = int(rest.split(":", 1)[0])
                except (ValueError, IndexError):
                    tg_answer_callback(cb_id, "⚠️ Errore")
                    continue
                rs = load_requests()
                pending = rs.setdefault("pending_radarr", {})
                key = f"download:{tmdb_id}"
                entry = pending.get(key)
                if not entry:
                    tg_answer_callback(cb_id, "⚠️ Operazione scaduta")
                    continue

                title_label = entry["title"] + (
                    f" ({entry['year']})" if entry.get("year") else ""
                )
                if action == "lang_keep":
                    pending.pop(key, None)
                    save_requests(rs)
                    tg_answer_callback(cb_id, "✅ Tenuto")
                    if msg_id:
                        tg_edit_message(
                            msg_id,
                            f"✅ <b>{title_label}</b> tenuto nonostante il "
                            f"mismatch audio. Sub ITA in arrivo come al solito."
                        )
                    continue

                # action == "lang_redo"
                required_lang = entry.get("required_lang")
                excluded_guid = entry.get("release_guid")
                pending.pop(key, None)
                save_requests(rs)
                tg_answer_callback(cb_id, "🔁 Riprovo")
                if msg_id:
                    tg_edit_message(
                        msg_id,
                        f"🔁 Ri-richiesta per <b>{title_label}</b> in coda…"
                    )
                # Re-trigger the release-list flow with the same lang filter,
                # excluding the release we just grabbed so the user doesn't
                # pick it twice.
                queue_msg = tg_send(
                    f"🔁 Ricerca rilasci per <b>{title_label}</b>…"
                )
                queue_msg_id = (
                    queue_msg["result"]["message_id"]
                    if queue_msg and queue_msg.get("ok") else None
                )
                download_queue.put({
                    "type": "scarica_redo",
                    "tmdb_id": tmdb_id,
                    "title": entry["title"],
                    "year": entry.get("year"),
                    "lang": required_lang,
                    "lang_source": entry.get("lang_source") or "explicit",
                    "excluded_guid": excluded_guid,
                    "msg_id": queue_msg_id,
                })
                continue

            # Retry-failed callbacks from /falliti
            if action in ("retry_failed", "retry_cancel"):
                state = load_state()
                batches = load_batches()
                batch = batches.get(path_hash)
                if not batch:
                    tg_answer_callback(cb_id, "⚠️ Operazione non trovata")
                    continue

                if action == "retry_cancel":
                    tg_answer_callback(cb_id, "❌ Annullato")
                    if msg_id:
                        tg_edit_message(msg_id, "❌ Riprova annullata.")
                    batches.pop(path_hash, None)
                    save_batches(batches)
                    continue

                paths = batch.get("paths", [])
                tg_answer_callback(cb_id, f"🔄 Riaccodati {len(paths)} video")
                if msg_id:
                    tg_edit_message(msg_id, f"🔄 Riaccodati <b>{len(paths)}</b> video falliti.")
                for p in paths:
                    state["asked"].pop(p, None)
                    pos = queue_position()
                    queue_msg = tg_send(
                        f"🔍 In coda: <b>{friendly_name(p)}</b>"
                        + (f"\n⏳ Posizione {pos + 1}" if pos > 0 else "")
                    )
                    queue_msg_id = queue_msg["result"]["message_id"] if queue_msg and queue_msg.get("ok") else None
                    download_queue.put({"type": "single", "path": p, "msg_id": queue_msg_id})
                save_state(state)
                batches.pop(path_hash, None)
                save_batches(batches)
                continue

            # Handle batch callbacks
            if action in ("delete_yes", "delete_no"):
                state = load_state()
                batches = load_batches()
                batch = batches.get(path_hash)
                if not batch:
                    tg_answer_callback(cb_id, "⚠️ Operazione non trovata")
                    continue

                if action == "delete_no":
                    tg_answer_callback(cb_id, "❌ Annullato")
                    if msg_id:
                        tg_edit_message(msg_id, "❌ Cancellazione annullata.")
                    batches.pop(path_hash, None)
                    save_batches(batches)
                    continue

                # delete_yes
                deleted_files = 0
                for sub_path in batch.get("subs", []):
                    try:
                        if os.path.exists(sub_path):
                            os.remove(sub_path)
                            deleted_files += 1
                    except Exception as e:
                        log.warning(f"  Failed to delete {sub_path}: {e}")

                paths = batch.get("paths", [])
                for p in paths:
                    state["downloaded"].pop(p, None)
                    state["asked"].pop(p, None)
                save_state(state)

                tg_answer_callback(cb_id, f"🗑 Cancellati {deleted_files} file")
                if msg_id:
                    tg_edit_message(msg_id,
                        f"✅ Cancellati <b>{deleted_files}</b> file per <b>{len(paths)}</b> video.\n"
                        f"🔄 Rimessi in coda per nuova ricerca...")

                # Auto-requeue the videos for re-download
                for p in paths:
                    pos = queue_position()
                    queue_msg = tg_send(
                        f"🔍 In coda: <b>{friendly_name(p)}</b>"
                        + (f"\n⏳ Posizione {pos + 1}" if pos > 0 else "")
                    )
                    queue_msg_id = queue_msg["result"]["message_id"] if queue_msg and queue_msg.get("ok") else None
                    download_queue.put({"type": "single", "path": p, "msg_id": queue_msg_id})

                batches.pop(path_hash, None)
                save_batches(batches)
                continue

            if action in ("batch_yes", "batch_no", "grp_exclude", "batch_translate", "batch_keep_en"):
                state = load_state()
                batches = load_batches()
                batch = batches.get(path_hash)
                if not batch:
                    tg_answer_callback(cb_id, "⚠️ Batch non trovato")
                    continue

                if action == "batch_translate":
                    tg_answer_callback(cb_id, "🤖 Traduzione avviata...")
                    if msg_id:
                        tg_edit_message(msg_id,
                            f"🤖 <b>Traducendo EN→IT...</b>\n\n"
                            f"[░░░░░░░░░░] 0%\n"
                            f"📊 0/{len(batch['paths'])}")
                    download_queue.put({"type": "translate", "paths": batch["paths"], "msg_id": msg_id})
                    batches.pop(path_hash, None)
                    save_batches(batches)
                    continue

                elif action == "batch_keep_en":
                    tg_answer_callback(cb_id, "🇬🇧 Mantenuti solo ENG")
                    for p in batch.get("paths", []):
                        state["asked"][p] = {"time": datetime.now().isoformat(), "status": "en_only"}
                    if msg_id:
                        tg_edit_message(msg_id, f"🇬🇧 Sub inglesi mantenuti senza traduzione.\nPuoi tradurre in seguito con /traduci")
                    batches.pop(path_hash, None)
                    save_batches(batches)
                    save_state(state)
                    continue

                elif action == "batch_yes":
                    pos = queue_position()
                    tg_answer_callback(cb_id, f"⬇️ In coda{f' (pos. {pos+1})' if pos > 0 else ''}...")
                    if msg_id:
                        tg_edit_message(msg_id,
                            f"⬇️ <b>Scaricando sottotitoli...</b>\n\n"
                            f"[░░░░░░░░░░] 0%\n"
                            f"📊 0/{len(batch['paths'])}"
                            + (f"\n⏳ In coda (posizione {pos+1})" if pos > 0 else ""))
                    download_queue.put({"type": "batch", "paths": batch["paths"], "msg_id": msg_id})
                elif action == "grp_exclude":
                    folder = batch.get("folder", "")
                    if folder:
                        excludes.add(folder)
                        save_excludes(excludes)
                        # Remove all pending asks for this folder
                        to_remove = [p for p in state["asked"] if get_series_folder(p).lower() == folder.lower()]
                        for p in to_remove:
                            del state["asked"][p]
                        tg_answer_callback(cb_id, f"🚫 '{folder}' esclusa")
                        if msg_id:
                            tg_edit_message(msg_id, f"🚫 <b>{folder}</b> esclusa dalla ricerca sub ITA.")
                    else:
                        tg_answer_callback(cb_id, "⚠️ Errore")
                else:
                    tg_answer_callback(cb_id, "⏭ Saltato")
                    # Mark all batch paths as "no"
                    for p in batch.get("paths", []):
                        state["asked"][p] = {"time": datetime.now().isoformat(), "status": "no"}
                    if msg_id:
                        tg_edit_message(msg_id, f"⏭ Batch saltato.\nRichiederò tra 3 giorni.")

                batches.pop(path_hash, None)
                save_batches(batches)
                save_state(state)
                continue

            if not video_path:
                tg_answer_callback(cb_id, "⚠️ Non trovato")
                continue

            name = friendly_name(video_path)

            if action == "yes":
                pos = queue_position()
                tg_answer_callback(cb_id, f"⬇️ In coda{f' (pos. {pos+1})' if pos > 0 else ''}...")
                if msg_id:
                    tg_edit_message(msg_id, f"⬇️ Scaricando sub ITA per:\n<b>{name}</b>..."
                                   + (f"\n⏳ In coda (posizione {pos+1})" if pos > 0 else ""))
                download_queue.put({"type": "single", "path": video_path, "msg_id": msg_id})

            elif action == "no":
                tg_answer_callback(cb_id, "⏭ Saltato")
                state["asked"][video_path] = {
                    "time": datetime.now().isoformat(),
                    "status": "no",
                    "path_hash": path_hash,
                }
                save_state(state)
                if msg_id:
                    tg_edit_message(msg_id, f"⏭ Saltato:\n<b>{name}</b>\nRichiederò tra 3 giorni.")

            elif action == "exclude":
                folder = get_series_folder(video_path)
                excludes.add(folder)
                save_excludes(excludes)
                tg_answer_callback(cb_id, f"🚫 '{folder}' esclusa")

                # Remove all pending asks for this folder
                to_remove = [p for p in state["asked"] if get_series_folder(p).lower() == folder.lower()]
                for p in to_remove:
                    del state["asked"][p]
                save_state(state)

                if msg_id:
                    tg_edit_message(msg_id, f"🚫 <b>{folder}</b> esclusa dalla ricerca sub ITA.\nPer rimuoverla, modifica /config/exclude_folders.txt")

        # Handle text commands
        msg = update.get("message")
        if msg and msg.get("text"):
            text = msg["text"].strip().lower()
            chat_id = str(msg["chat"]["id"])

            if chat_id != str(TELEGRAM_CHAT_ID):
                continue

            if text.startswith("/"):
                # All slash commands go through the registry dispatcher.
                # Unknown slash commands trigger a "did you mean?" reply, so we
                # never silently fall through to free-text search on a typo.
                dispatch_command(text, state, excludes)
            elif len(text) >= 3:
                # Free-text (no slash) is always interpreted as a library search.
                search_and_offer(text.strip(), state)

    save_state(state)


# =============================================================================
# MANUAL SEARCH BY NAME
# =============================================================================

def find_videos_by_name(query):
    """Search media folders for videos matching a query string."""
    query_lower = query.lower()
    matches = []

    for media_path in [FILMS_PATH, SERIES_PATH]:
        if not os.path.exists(media_path):
            continue
        for root, dirs, files in os.walk(media_path):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in VIDEO_EXTENSIONS:
                    continue
                full_path = os.path.join(root, fname)

                # Match against filename, folder name, and parent folder
                folder_name = get_series_folder(full_path).lower()
                fname_lower = fname.lower()

                # Also match with dots/underscores replaced by spaces
                fname_clean = re.sub(r"[._\-]", " ", fname_lower)
                folder_clean = re.sub(r"[._\-]", " ", folder_name)
                query_clean = re.sub(r"[._\-]", " ", query_lower)

                if (query_lower in folder_name or query_lower in fname_lower
                    or query_clean in folder_clean or query_clean in fname_clean):
                    matches.append(full_path)

    return matches


SUB_SUFFIXES_TO_DELETE = [".it.srt", ".ita.srt", ".italian.srt", ".it.hi.srt",
                          ".en.srt", ".eng.srt", ".english.srt"]


def list_video_subs(video_path):
    """Return the list of subtitle file paths existing alongside a video."""
    base = os.path.splitext(video_path)[0]
    return [base + suffix for suffix in SUB_SUFFIXES_TO_DELETE
            if os.path.exists(base + suffix)]


def offer_delete(query, state):
    """Find videos matching query, list their subs, ask for confirmation."""
    matches = find_videos_by_name(query)
    if not matches:
        tg_send(f"❌ Nessun video trovato per '<b>{query}</b>'")
        return

    plan = []  # list of (video_path, [sub_paths])
    for video in matches:
        subs = list_video_subs(video)
        if subs:
            plan.append((video, subs))

    if not plan:
        tg_send(f"❌ Nessun sub da cancellare per '<b>{query}</b>'")
        return

    lines = [f"🗑 Trovati <b>{len(plan)}</b> video matching '<b>{query}</b>':\n"]
    for video, subs in plan[:10]:
        lines.append(f"📁 <b>{friendly_name(video)}</b>")
        for s in subs:
            try:
                size_kb = os.path.getsize(s) / 1024
                lines.append(f"   • {os.path.basename(s)} ({size_kb:.0f} KB)")
            except Exception:
                lines.append(f"   • {os.path.basename(s)}")
    if len(plan) > 10:
        lines.append(f"\n<i>… e altri {len(plan) - 10} video</i>")

    delete_hash = str(abs(hash(tuple(v for v, _ in plan))))[:8]
    batches = load_batches()
    batches[delete_hash] = {
        "type": "delete",
        "paths": [v for v, _ in plan],
        "subs": [s for _, subs in plan for s in subs],
    }
    save_batches(batches)

    keyboard = {"inline_keyboard": [[
        {"text": "✅ Cancella e ricerca di nuovo", "callback_data": f"delete_yes:{delete_hash}"},
        {"text": "❌ Annulla", "callback_data": f"delete_no:{delete_hash}"},
    ]]}
    tg_send("\n".join(lines), reply_markup=keyboard)


def search_and_offer(query, state):
    """Find videos matching query and offer to download subs."""
    tg_send(f"🔍 Cerco '<b>{query}</b>' nella libreria...")

    matches = find_videos_by_name(query)

    if not matches:
        tg_send(f"❌ Nessun video trovato per '<b>{query}</b>'")
        return

    # Separate into: already has sub, missing sub
    with_sub = []
    without_sub = []
    for m in matches:
        if has_italian_sub(m):
            with_sub.append(m)
        else:
            without_sub.append(m)

    if not without_sub:
        tg_send(
            f"✅ Trovati {len(matches)} video per '<b>{query}</b>'\n"
            f"Tutti hanno già il sub ITA!"
        )
        return

    # Report what we found
    tg_send(
        f"📂 Trovati {len(matches)} video per '<b>{query}</b>'\n"
        f"  ✅ Con sub ITA: {len(with_sub)}\n"
        f"  ❌ Senza sub ITA: {len(without_sub)}"
    )

    # If there are many, offer to download all at once
    if len(without_sub) > 1:
        # Create a unique hash for this batch
        batch_hash = str(abs(hash(query + str(len(without_sub)))))[:8]

        batches = load_batches()
        batches[batch_hash] = {
            "paths": without_sub,
            "query": query,
            "time": datetime.now().isoformat(),
        }
        save_batches(batches)

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": f"✅ Scarica tutti ({len(without_sub)})", "callback_data": f"batch_yes:{batch_hash}"},
                    {"text": "❌ No", "callback_data": f"batch_no:{batch_hash}"},
                ],
            ]
        }

        # Show list of files
        file_list = "\n".join(
            f"  • {friendly_name(p)}" for p in without_sub[:15]
        )
        if len(without_sub) > 15:
            file_list += f"\n  ... e altri {len(without_sub) - 15}"

        tg_send(
            f"📋 <b>File senza sub ITA:</b>\n{file_list}\n\n"
            f"Scarico i sottotitoli italiani per tutti?",
            reply_markup=keyboard,
        )
    else:
        # Just one file, ask directly
        video_path = without_sub[0]
        path_hash = str(abs(hash(video_path)))[:8]

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Scarica", "callback_data": f"yes:{path_hash}"},
                    {"text": "❌ No", "callback_data": f"no:{path_hash}"},
                ],
            ]
        }

        result = tg_send(
            f"🎬 <b>Sub ITA mancante</b>\n\n"
            f"📄 {friendly_name(video_path)}\n"
            f"📁 {os.path.basename(video_path)}\n\n"
            f"Scarico il sottotitolo italiano?",
            reply_markup=keyboard,
        )

        msg_id = None
        if result and result.get("ok"):
            msg_id = result["result"]["message_id"]

        state["asked"][video_path] = {
            "time": datetime.now().isoformat(),
            "status": "pending",
            "path_hash": path_hash,
            "msg_id": msg_id,
        }
        save_state(state)


def do_sync(query, state, progress_msg_id=None):
    """Sync subtitles for a specific series/film or all."""
    is_all = query.lower() == "all"
    query_lower = query.lower()

    pairs = []
    for media_path in [SERIES_PATH, FILMS_PATH]:
        if not os.path.exists(media_path):
            continue
        for root, dirs, files in os.walk(media_path):
            for f in files:
                if not f.endswith(".it.srt"):
                    continue
                srt_path = os.path.join(root, f)
                folder = get_series_folder(srt_path)

                if not is_all and query_lower not in folder.lower() and query_lower not in f.lower():
                    continue

                video_base = srt_path.rsplit(".it.srt", 1)[0]
                for ext in VIDEO_EXTENSIONS:
                    candidate = video_base + ext
                    if os.path.exists(candidate):
                        pairs.append((candidate, srt_path))
                        break

    if not pairs:
        msg = f"❌ Nessun sub ITA trovato per '<b>{query}</b>'"
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    msg_id = progress_msg_id
    if not msg_id:
        result = tg_send(
            f"🔄 Sync <b>{query}</b>: {len(pairs)} sottotitoli...\n"
            f"[{'░' * 10}] 0%"
        )
        msg_id = result["result"]["message_id"] if result and result.get("ok") else None
    else:
        tg_edit_message(msg_id,
            f"🔄 Sync <b>{query}</b>: {len(pairs)} sottotitoli...\n"
            f"[{'░' * 10}] 0%")

    synced = 0
    failed = 0
    for i, (video_path, srt_path) in enumerate(pairs):
        if msg_id and (i % 3 == 0 or i == len(pairs) - 1):
            bar = _progress_bar(i, len(pairs))
            tg_edit_message(msg_id,
                f"🔄 Sync <b>{query}</b>...\n\n"
                f"{bar}\n"
                f"📊 {i}/{len(pairs)} — ✅ {synced} | ❌ {failed}")
        result = sync_subtitle(video_path, srt_path)
        if result and (result is True or (isinstance(result, dict) and result["ok"])):
            synced += 1
        else:
            failed += 1

    summary = (
        f"🔄 <b>Sync completato — {query}</b>\n\n"
        f"✅ Sincronizzati: {synced}\n"
        f"❌ Falliti: {failed}"
    )
    if msg_id:
        tg_edit_message(msg_id, summary)
    else:
        tg_send(summary)


def do_cleanup(state, progress_msg_id=None):
    """Scan all .it.srt files, remove placeholders and re-queue the matching videos."""
    removed = 0
    requeued = 0
    for media_path in [FILMS_PATH, SERIES_PATH]:
        if not os.path.exists(media_path):
            continue
        for root, dirs, files in os.walk(media_path):
            for f in files:
                if f.endswith(".it.srt"):
                    srt_path = os.path.join(root, f)
                    try:
                        with open(srt_path, "rb") as fh:
                            content = fh.read()
                        if is_placeholder_sub(content):
                            os.remove(srt_path)
                            removed += 1
                            log.info(f"  🗑 Placeholder rimosso: {f}")
                            video_base = srt_path.rsplit(".it.srt", 1)[0]
                            for ext in VIDEO_EXTENSIONS:
                                video_candidate = video_base + ext
                                if os.path.exists(video_candidate):
                                    state["downloaded"].pop(video_candidate, None)
                                    state["asked"].pop(video_candidate, None)
                                    requeued += 1
                                    break
                    except Exception as e:
                        log.error(f"  Errore leggendo {srt_path}: {e}")
    save_state(state)
    summary = (
        f"🧹 <b>Cleanup completato</b>\n\n"
        f"🗑 Placeholder rimossi: {removed}\n"
        f"🔄 Video rimessi in coda: {requeued}\n\n"
        f"{'Alla prossima scansione verranno cercati di nuovo.' if requeued else 'Nessun placeholder trovato.'}"
    )
    if progress_msg_id:
        tg_edit_message(progress_msg_id, summary)
    else:
        tg_send(summary)


def do_batch_download(paths, state, progress_msg_id=None):
    """Download subs for a batch of files with progress on a single Telegram message.
    Step 1: Download EN subs (free). Step 2: Ask user to confirm translation (paid)."""
    total = len(paths)
    ita_found = 0
    en_found = 0
    not_found = 0
    ita_names = []
    en_paths = []
    en_names = []
    failed_names = []

    for i, video_path in enumerate(paths):
        if has_italian_sub(video_path):
            ita_found += 1
            ita_names.append(friendly_name(video_path))
            continue

        if progress_msg_id and (i % 3 == 0 or i == total - 1):
            bar = _progress_bar(i, total)
            tg_edit_message(progress_msg_id,
                f"⬇️ <b>Scaricando sottotitoli...</b>\n\n"
                f"{bar}\n"
                f"📊 {i}/{total} — 🇮🇹 {ita_found} | 🇬🇧 {en_found} | ❌ {not_found}")

        result = do_download(video_path, state, silent=True, translate=False)
        if result is True:
            ita_found += 1
            ita_names.append(friendly_name(video_path))
        elif result == "en_only":
            en_found += 1
            en_paths.append(video_path)
            en_names.append(friendly_name(video_path))
        else:
            not_found += 1
            failed_names.append(friendly_name(video_path))
        time.sleep(1)

    # Build summary
    summary = f"📊 <b>Download completato</b>\n\n"
    summary += f"🇮🇹 Sub ITA trovati: {ita_found}/{total}\n"
    summary += f"🇬🇧 Solo ENG (da tradurre): {en_found}/{total}\n"
    summary += f"❌ Non trovati: {not_found}/{total}"

    if ita_names and len(ita_names) <= 10:
        summary += "\n\n<b>Sub ITA:</b>\n" + "\n".join(f"  🇮🇹 {n}" for n in ita_names)
    if en_names and len(en_names) <= 15:
        summary += "\n\n<b>Solo ENG (servono traduzione):</b>\n" + "\n".join(f"  🇬🇧 {n}" for n in en_names)
    if failed_names and len(failed_names) <= 10:
        summary += "\n\n<b>Non trovati:</b>\n" + "\n".join(f"  ❌ {n}" for n in failed_names)

    if en_paths:
        total_cost, total_blocks = _estimate_batch_translation_cost(en_paths)
        summary += f"\n\n💰 <b>Costo traduzione stimato: ${total_cost:.2f}</b> ({total_blocks} blocchi)"

        translate_hash = str(abs(hash(tuple(en_paths))))[:8]
        batches = load_batches()
        batches[translate_hash] = {
            "paths": en_paths,
            "type": "translate",
        }
        save_batches(batches)

        keyboard = {"inline_keyboard": [
            [
                {"text": f"🤖 Traduci in italiano (${total_cost:.2f})", "callback_data": f"batch_translate:{translate_hash}"},
                {"text": "🇬🇧 Tieni solo ENG", "callback_data": f"batch_keep_en:{translate_hash}"},
            ]
        ]}
        if progress_msg_id:
            tg_edit_message(progress_msg_id, summary, reply_markup=keyboard)
        else:
            tg_send(summary, reply_markup=keyboard)
    else:
        if progress_msg_id:
            tg_edit_message(progress_msg_id, summary)
        else:
            tg_send(summary)


def do_translate_prep(query, state, progress_msg_id=None):
    """For videos matching `query`, sync their .en.srt to audio, then ask the
    user (via Telegram) whether to translate them to Italian.

    Flow:
      1. Find videos matching the query
      2. Keep only those with a .en.srt but no .it.srt
      3. Sync each .en.srt to the video audio (ffsubsync)
      4. Estimate total cost and post a single message with Traduci/Tieni ENG buttons
    """
    matches = find_videos_by_name(query)
    if not matches:
        msg = f"❌ Nessun video trovato per '<b>{query}</b>'"
        if progress_msg_id:
            tg_edit_message(progress_msg_id, msg)
        else:
            tg_send(msg)
        return

    eligible = []
    already_it = []
    no_en = []
    for video_path in matches:
        if has_italian_sub(video_path):
            already_it.append(video_path)
            continue
        en_srt = find_english_sub(video_path)
        if not en_srt:
            no_en.append(video_path)
            continue
        eligible.append((video_path, en_srt))

    if not eligible:
        summary = f"📊 <b>Translate prep '{query}'</b>\n\n"
        summary += f"🇮🇹 Già con sub ITA: {len(already_it)}\n"
        summary += f"🇬🇧 Senza .en.srt: {len(no_en)}\n\n"
        summary += "Niente da tradurre. Usa prima la ricerca per scaricare il sub EN."
        if progress_msg_id:
            tg_edit_message(progress_msg_id, summary)
        else:
            tg_send(summary)
        return

    total = len(eligible)
    synced = 0
    for i, (video_path, en_srt) in enumerate(eligible):
        if progress_msg_id:
            bar = _progress_bar(i, total)
            tg_edit_message(
                progress_msg_id,
                f"🔄 <b>Sync EN su audio...</b>\n\n{bar}\n📊 {i}/{total} sincronizzati\n\n"
                f"<i>{friendly_name(video_path)}</i>",
            )
        try:
            sync_subtitle(video_path, en_srt)
            synced += 1
        except Exception as e:
            log.warning(f"  translate_prep sync failed for {video_path}: {e}")

    en_paths = [vp for vp, _ in eligible]
    total_cost, total_blocks = _estimate_batch_translation_cost(en_paths)

    summary = f"📊 <b>Translate prep '{query}'</b>\n\n"
    summary += f"🔄 Sincronizzati: {synced}/{total}\n"
    if already_it:
        summary += f"🇮🇹 Già con sub ITA (saltati): {len(already_it)}\n"
    if no_en:
        summary += f"🇬🇧 Senza .en.srt (saltati): {len(no_en)}\n"
    summary += f"\n💰 <b>Costo traduzione stimato: ${total_cost:.2f}</b> ({total_blocks} blocchi)"

    translate_hash = str(abs(hash(tuple(en_paths))))[:8]
    batches = load_batches()
    batches[translate_hash] = {"paths": en_paths, "type": "translate"}
    save_batches(batches)

    keyboard = {"inline_keyboard": [[
        {"text": f"🤖 Traduci in italiano (${total_cost:.2f})", "callback_data": f"batch_translate:{translate_hash}"},
        {"text": "🇬🇧 Tieni solo ENG", "callback_data": f"batch_keep_en:{translate_hash}"},
    ]]}
    if progress_msg_id:
        tg_edit_message(progress_msg_id, summary, reply_markup=keyboard)
    else:
        tg_send(summary, reply_markup=keyboard)


def _estimate_batch_translation_cost(paths):
    """Estimate total translation cost for a list of video paths with English sub files.
    Accepts .en.srt, .eng.srt, .english.srt."""
    total_cost = 0.0
    total_blocks = 0
    for video_path in paths:
        en_srt = find_english_sub(video_path)
        if not en_srt:
            continue
        try:
            with open(en_srt, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            cost, blocks = estimate_translation_cost(content)
            total_cost += cost
            total_blocks += blocks
        except Exception:
            pass
    return total_cost, total_blocks


def do_batch_translate(paths, state, progress_msg_id=None):
    """Translate a batch of EN subs to IT. Called after user confirms translation."""
    total = len(paths)
    success = 0
    failed = 0
    success_names = []
    failed_names = []

    for i, video_path in enumerate(paths):
        if has_italian_sub(video_path):
            success += 1
            success_names.append(friendly_name(video_path))
            continue

        en_srt = find_english_sub(video_path)
        if not en_srt:
            failed += 1
            failed_names.append(friendly_name(video_path))
            continue

        if progress_msg_id and (i % 2 == 0 or i == total - 1):
            bar = _progress_bar(i, total)
            tg_edit_message(progress_msg_id,
                f"🤖 <b>Traducendo EN→IT...</b>\n\n"
                f"{bar}\n"
                f"📊 {i}/{total} — ✅ {success} | ❌ {failed}")

        if _translate_and_save(en_srt, video_path, state, silent=True, skip_sync=True):
            success += 1
            success_names.append(friendly_name(video_path))
        else:
            failed += 1
            failed_names.append(friendly_name(video_path))
        time.sleep(1)

    actual_state = load_state()
    costs = actual_state.get("claude_costs", {})
    total_spent = costs.get("total_cost_usd", 0.0)

    summary = f"🤖 <b>Traduzione completata</b>\n\n"
    summary += f"✅ Tradotti: {success}/{total}\n❌ Falliti: {failed}/{total}\n"
    summary += f"💰 Costo totale sessione: ${total_spent:.4f}"

    if success_names and len(success_names) <= 10:
        summary += "\n\n<b>Tradotti:</b>\n" + "\n".join(f"  ✅ {n}" for n in success_names)
    if failed_names and len(failed_names) <= 10:
        summary += "\n\n<b>Falliti:</b>\n" + "\n".join(f"  ❌ {n}" for n in failed_names)

    if progress_msg_id:
        tg_edit_message(progress_msg_id, summary)
    else:
        tg_send(summary)


def _progress_bar(current, total, width=10):
    """Generate a text progress bar."""
    if total == 0:
        return ""
    filled = int(width * current / total)
    bar = "▓" * filled + "░" * (width - filled)
    pct = int(100 * current / total)
    return f"[{bar}] {pct}%"


# =============================================================================
# MAIN LOOP
# =============================================================================

# =============================================================================
# DOWNLOAD QUEUE
# =============================================================================

download_queue = Queue()


def _queue_worker(state_ref):
    """Background worker that processes download requests sequentially."""
    while True:
        try:
            job = download_queue.get(timeout=5)
        except Empty:
            continue
        try:
            job_type = job.get("type", "single")
            state = load_state()
            if job_type == "batch":
                paths = job["paths"]
                msg_id = job.get("msg_id")
                log.info(f"  Queue: processing batch of {len(paths)} files")
                do_batch_download(paths, state, progress_msg_id=msg_id)
            elif job_type == "translate":
                paths = job["paths"]
                msg_id = job.get("msg_id")
                log.info(f"  Queue: translating batch of {len(paths)} files")
                do_batch_translate(paths, state, progress_msg_id=msg_id)
            elif job_type == "sync":
                query = job["query"]
                msg_id = job.get("msg_id")
                log.info(f"  Queue: sync '{query}'")
                do_sync(query, state, progress_msg_id=msg_id)
            elif job_type == "translate_prep":
                query = job["query"]
                msg_id = job.get("msg_id")
                log.info(f"  Queue: translate_prep '{query}'")
                do_translate_prep(query, state, progress_msg_id=msg_id)
            elif job_type == "retranslate":
                query = job["query"]
                msg_id = job.get("msg_id")
                log.info(f"  Queue: retranslate '{query}'")
                do_retranslate(query, state, progress_msg_id=msg_id)
            elif job_type == "scarica":
                query = job["query"]
                msg_id = job.get("msg_id")
                lang = job.get("lang")
                lang_source = job.get("lang_source")
                log.info(f"  Queue: scarica '{query}' lang={lang} source={lang_source}")
                do_scarica_search(query, progress_msg_id=msg_id, lang=lang, lang_source=lang_source)
            elif job_type == "cerca_search":
                query = job["query"]
                msg_id = job.get("msg_id")
                log.info(f"  Queue: cerca_search '{query}'")
                do_cerca_search(query, progress_msg_id=msg_id)
            elif job_type == "cerca_download":
                film_hash = job["film_hash"]
                result_idx = job["result_idx"]
                msg_id = job.get("msg_id")
                log.info(f"  Queue: cerca_download film_hash={film_hash} idx={result_idx}")
                do_cerca_download(film_hash, result_idx, msg_id=msg_id)
            elif job_type == "scarica_redo":
                msg_id = job.get("msg_id")
                log.info(
                    f"  Queue: scarica_redo tmdb_id={job.get('tmdb_id')} "
                    f"lang={job.get('lang')} excl={job.get('excluded_guid')}"
                )
                do_scarica_redo(
                    tmdb_id=job["tmdb_id"],
                    title=job["title"],
                    year=job.get("year"),
                    lang=job.get("lang"),
                    lang_source=job.get("lang_source"),
                    excluded_guid=job.get("excluded_guid"),
                    progress_msg_id=msg_id,
                )
            elif job_type == "cleanup":
                msg_id = job.get("msg_id")
                log.info("  Queue: cleanup placeholders")
                do_cleanup(state, progress_msg_id=msg_id)
            else:
                video_path = job["path"]
                msg_id = job.get("msg_id")
                name = friendly_name(video_path)
                log.info(f"  Queue: processing {os.path.basename(video_path)}")
                trace = []
                result = do_download(video_path, state, silent=True, translate=False, trace=trace)
                if result is True and msg_id:
                    tg_edit_message(msg_id, f"✅ Sub ITA scaricato per:\n<b>{name}</b>")
                elif result == "en_only" and msg_id:
                    en_srt = find_english_sub(video_path)
                    cost, blocks = (0, 0)
                    if en_srt:
                        with open(en_srt, "r", encoding="utf-8", errors="ignore") as f:
                            cost, blocks = estimate_translation_cost(f.read())
                    tr_hash = str(abs(hash(video_path)))[:8]
                    batches = load_batches()
                    batches[tr_hash] = {"paths": [video_path], "type": "translate"}
                    save_batches(batches)
                    keyboard = {"inline_keyboard": [
                        [
                            {"text": f"🤖 Traduci (${cost:.2f})", "callback_data": f"batch_translate:{tr_hash}"},
                            {"text": "🇬🇧 Tieni ENG", "callback_data": f"batch_keep_en:{tr_hash}"},
                        ]
                    ]}
                    tg_edit_message(msg_id,
                        f"🇬🇧 Sub ENG trovato per:\n<b>{name}</b>\n\n"
                        f"💰 Tradurre in italiano? Costo: <b>${cost:.2f}</b> ({blocks} blocchi)",
                        reply_markup=keyboard)
                elif not result and msg_id:
                    trace_text = format_search_trace(trace)
                    fail_msg = f"❌ Nessun sub ITA né ENG trovato per:\n<b>{name}</b>\nRiproverò tra 24h."
                    if trace_text:
                        fail_msg += f"\n\n<b>Tentativi:</b>\n{trace_text}"
                    tg_edit_message(msg_id, fail_msg)
        except Exception as e:
            log.error(f"  Queue worker error: {e}", exc_info=True)
        finally:
            download_queue.task_done()


def queue_position():
    """Return current queue size."""
    return download_queue.qsize()


def main():
    log.info("=== Sub ITA Fetcher starting ===")
    log.info(f"Series path: {SERIES_PATH}")
    log.info(f"Films path: {FILMS_PATH}")
    log.info(f"Scan interval: {SCAN_INTERVAL}s")

    state = load_state()
    excludes = load_excludes()

    # Start background download worker
    worker = Thread(target=_queue_worker, args=(state,), daemon=True)
    worker.start()

    # Send startup message
    tg_send("🚀 <b>Sub ITA Fetcher avviato!</b>\nDigita /help per i comandi.")

    while True:
        try:
            # Process any pending Telegram callbacks/commands
            process_callbacks(state, excludes)

            # Scan for missing subs
            excludes = load_excludes()  # Reload in case user edited file
            missing = scan_missing(state, excludes)

            if missing:
                log.info(f"Found {len(missing)} videos missing Italian subs")
                ask_user_grouped(missing, state)

            # Wait, but check for callbacks more frequently
            for _ in range(SCAN_INTERVAL // 5):
                time.sleep(5)
                process_callbacks(state, excludes)

        except KeyboardInterrupt:
            log.info("Shutting down...")
            tg_send("🛑 Sub ITA Fetcher spento.")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            time.sleep(30)


if __name__ == "__main__":
    main()
