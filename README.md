# HAL

Automated Italian subtitle downloader with Telegram bot interface. Scans your media library, finds videos missing Italian subtitles, and downloads/translates them automatically.

## Features

- **Multi-provider search**: Subdl.com (primary) + OpenSubtitles.com REST v1 (fallback)
- **TMDb lookup**: resolves IMDb ID from title + year when no `.nfo` file is present, so search runs on a canonical ID instead of a dirty filename
- **Two-phase download**: EN subs downloaded for free, translation is optional and cost-estimated upfront
- **AI translation**: English → Italian via DeepL (primary, cue-by-cue, no truncation) with optional Claude polish pass to rewrite unnatural lines. Falls back to full Claude translation if DeepL is unavailable.
- **Audio sync**: ffsubsync aligns subtitles to video audio (handles different releases)
- **Episode matching**: Ensures correct episode subtitles are downloaded (not random episodes)
- **Italian audio detection**: Skips films already in Italian (via ffprobe)
- **Italian-original detection**: Also skips films whose original language is Italian according to TMDb (e.g. "La Grande Bellezza"), even when the audio file has no language tags
- **VIP placeholder rejection**: Detects and skips fake/ad subtitles from OpenSubtitles
- **Forced sub filtering**: Rejects signs-only/forced subtitles, prefers full dialogue
- **Telegram bot**: Grouped notifications, batch downloads with progress bar, download queue
- **Dual save**: Keeps both `.en.srt` (synced) and `.it.srt` when translating
- **Cost tracking**: Tracks DeepL character usage and Claude API token costs, shows estimate before translating

## How It Works

```
Scan media folders → Find missing .it.srt → Group by series/film
        ↓
Auto-enqueue download (no "Scarica?" prompt — subs are always wanted)
        ↓
Phase 1 (FREE):
  1. Check local folder for existing subs
  2. Search Subdl.com (ITA) → download directly
  3. Search OpenSubtitles (ITA) → download (skip placeholders)
  4. Search Subdl.com (ENG) → save .en.srt + sync to audio
  5. Search OpenSubtitles (ENG) → save .en.srt + sync to audio
        ↓
Summary on Telegram: "🇮🇹 3 ITA, 🇬🇧 6 EN only. Tradurre? Costo: $0.57"
        ↓
Phase 2 (PAID, user confirms):
  User clicks "Traduci ($0.57)" → Claude translates EN→IT
  User clicks "Tieni solo ENG" → keeps English only (free)
```

## Quick Start

### 1. Get API Keys

- **Telegram Bot**: Create via [@BotFather](https://t.me/BotFather)
- **Subdl.com**: Register at [subdl.com](https://subdl.com) for a free API key
- **OpenSubtitles.com** (recommended): Register at [opensubtitles.com](https://www.opensubtitles.com), then create an API consumer at `/en/consumers` with `dev_mode` enabled (100 downloads/day free). Copy the API key.
- **TMDb** (recommended): Register at [themoviedb.org](https://www.themoviedb.org/settings/api), request a Developer key (free, unlimited). Used to resolve IMDb IDs from title + year.
- **DeepL API** (recommended for translation): Register at [deepl.com/pro-api](https://www.deepl.com/pro-api) — Free tier gives 500k chars/month (≈10 films). Key ends with `:fx` for free, plain for paid.
- **Claude API** (optional): Get from [console.anthropic.com](https://console.anthropic.com). Used as fallback if DeepL is missing, and (with Haiku 4.5) to polish unnatural lines in DeepL output.
- **Radarr** (optional, enables `/scarica`): point at your existing Radarr v3 instance. Required env vars: `RADARR_URL` (e.g. `http://radarr:7878`) + `RADARR_API_KEY` (Settings → General).

### 2. Docker Compose

Add to your `docker-compose.yml`:

```yaml
hal:
  build:
    context: ./hal
    dockerfile: Dockerfile
  container_name: hal
  restart: unless-stopped
  volumes:
    - /path/to/series:/media/series
    - /path/to/films:/media/films
    - ./hal:/config
  environment:
    - TELEGRAM_BOT_TOKEN=your_token
    - TELEGRAM_CHAT_ID=your_chat_id
    - SUBDL_API_KEY=your_subdl_key
    - OPENSUBTITLES_API_KEY=your_opensubtitles_consumer_key   # recommended
    - TMDB_API_KEY=your_tmdb_v3_api_key                       # recommended
    - DEEPL_API_KEY=your_deepl_key                            # recommended (free tier OK)
    - CLAUDE_API_KEY=your_claude_key                          # optional (polish + fallback)
    - POLISH_TRANSLATION=true                                 # optional, default true
    - RADARR_URL=http://radarr:7878                           # optional, enables /scarica
    - RADARR_API_KEY=your_radarr_key                          # optional, enables /scarica
    - RADARR_PREFERRED_LANGUAGES=ITA,ENG                      # optional, default ITA,ENG
    # /cerca (multi-source film download) — optional overrides
    - YT_COOKIES_FILE=/config/secrets/yt_cookies.txt          # optional, default shown
    - CERCA_DOWNLOAD_DIR=/media/films                         # optional, default shown
    - CERCA_MIN_RESOLUTION=720                                # optional, default 720
```

For `/cerca` to use YouTube reliably, drop a Netscape cookies.txt at `hal/secrets/yt_cookies.txt` on the host. The directory is gitignored (`secrets/` in `.gitignore`). yt-dlp uses those cookies to bypass age-gate / consent walls. Without cookies, archive.org still works.

### 3. Run

```bash
docker compose up -d --build hal
```

## Telegram Commands

The canonical command names are in Italian (the bot speaks Italian), and each one accepts English aliases plus short forms. Examples below show canonical + aliases.

### Search & download
| Command | Aliases | Description |
|---|---|---|
| `/sub <name>` | — | Search the local library (or simply type the name without a slash). Was `/cerca` before the multi-source download took that slot. |
| `/cerca <title [year]>` | `/search`, `/find` | Multi-source film download. Searches archive.org and YouTube in parallel, lets you pick the best result, downloads it, remuxes to MKV and installs it under `/media/films/<Title (Year)>/`. Useful for films that don't show up on the configured Radarr indexers. |
| `/scarica <name [year]> [--lang CODE]` | `/download`, `/req` | Request a new film via Radarr — TMDb disambiguates, then you pick the release (quality, size, language, indexer) from an inline list. Optional `--lang CODE` filters the release list (e.g. `/scarica Akira --lang JPN`, `/scarica Punch-Drunk Love --lang ITA`). Without `--lang`, defaults to the film's original language from TMDb. Codes: `ITA`, `ENG`, `JPN`, `KOR`, `FRA`, `GER`, `SPA`, `CHI`, `RUS` (2-letter `it`/`en`/`ja`/… and full names also work). Multi-track releases (`MULTI`, `Dual.Audio.JP-EN`) pass the filter and are verified post-grab via ffprobe. |
| `/ita <name [year]>` | — | Shortcut for `/scarica ... --lang ITA`. |

### Sub management
| Command | Aliases | Description |
|---|---|---|
| `/sincronizza <name\|all>` | `/sync` | Sync existing subs to video audio |
| `/subeng <name>` | `/eng`, `/en` | Search and download the correct English sub (Subdl → OpenSubtitles), synced to the audio. Unlike `/sub` it runs even when the video already has an `.it.srt`, so it fixes a wrong or missing `.en.srt` without touching the Italian one. Videos that already have an English sub trigger a `[🔄 Sostituisci] / [⏭️ Salta esistenti]` prompt; a failed search restores the previous file. |
| `/traduci <name>` | `/translate`, `/tr`, `/t` | Sync `.en.srt` and translate EN→IT (DeepL + Claude polish) |
| `/ritraduci <name>` | `/retranslate`, `/rt` | Delete `.it.srt` and retranslate from the existing `.en.srt` (no re-download) |
| `/cancella <name>` | `/delete`, `/del` | Delete all subs and re-queue for a fresh search |

### Status & maintenance
| Command | Aliases | Description |
|---|---|---|
| `/stato` | `/status`, `/st` | Current state summary |
| `/coda` | `/queue`, `/q` | Show currently queued jobs |
| `/costi` | `/costs`, `/cost` | DeepL chars + Claude polish cost + Claude fallback cost (separate sections) |
| `/falliti` | `/failed` | List failed videos, with a "Retry all" inline button |
| `/esclusi` | `/excludes` | List excluded folders |
| `/log [n]` | `/logs` | Tail the last n log lines (default 30, max 200) |
| `/scansiona` | `/scan` | Force a manual scan |
| `/pulisci` | `/cleanup` | Find and remove placeholder/VIP subs |
| `/reset` | `/azzera` | Clear cache (start from scratch) |
| `/aiuto` | `/help`, `/?` | Show this command list |

Type any text to search your media library (handles dots in filenames). Typos on slash commands produce a "did you mean?" reply, never a silent fallthrough to search.

## Subtitle Search Cascade

For each video, the bot tries multiple strategies in order:

1. **Local files**: Check if `.it.srt` or `.en.srt` / `.eng.srt` already exists in the folder
2. **Filename parsing**: Strips scraper/tracker prefixes (`www.SceneTime.com -`, `[YTS.MX]`) and handles year in parentheses (`Film (2002).mkv`)
3. **IMDb ID resolution**: Reads `.nfo` files written by Radarr/Sonarr; falls back to TMDb lookup by title + year
4. **Try ITA**: Subdl → OpenSubtitles → validate sync (score ≥ 800) → save `.it.srt` if good
5. **Always try ENG too**: Subdl → OpenSubtitles → validate sync → save `.en.srt` if good (runs regardless of whether ITA was found, so EN is always available as backup or for later translation)
6. **Outcome**:
   - **Both saved** → notify "ITA scaricato" + list both files
   - **Only ENG saved** → notify "ENG salvato, tradurre?" with `[Traduci]` / `[Tieni ENG]` buttons
   - **Neither saved** → notify failure with full search trace and OpenSubtitles quota remaining
7. **Failure trace** shows all attempted providers, methods, sync scores, and OpenSubtitles download quota

## Architecture

Single Python file (`hal.py`), no frameworks. Runs as a long-lived process inside Docker.

| Component | Technology |
|---|---|
| Runtime | Python 3.11 (slim) |
| Subtitle sync | ffsubsync (Voice Activity Detection) |
| Audio detection | ffprobe (Italian audio track detection) |
| Primary provider | Subdl.com REST API |
| Fallback provider | OpenSubtitles.com REST API v1 |
| IMDb ID resolution | `.nfo` files → TMDb search fallback |
| Translation | DeepL (cue-by-cue, batches of 50) → Claude Haiku polish (batches of 80); Claude Sonnet full-translate as fallback (batches of 40) |
| Bot interface | Telegram Bot API (polling) |
| State | JSON file (`/config/state.json`) |
| Queue | Thread-safe FIFO (download, translate, single jobs) |

## File Structure

```
hal.py           # Main application (single file)
test_hal.py      # Unit tests (55 tests)
Dockerfile               # Container definition
SPEC.md                  # Technical specification
AGENTS.md                # Agent/AI coding guidelines
```

## Development

### Run tests
```bash
python3 -m unittest test_hal -v
```

### Pre-commit hook
A tracked hook in `.githooks/pre-commit` runs the full test suite before every commit. Activate it once after cloning:
```bash
git config core.hooksPath .githooks
```

### Rebuild and restart
```bash
docker compose up -d --build hal
```

### View logs
```bash
docker logs -f hal --tail 50
```

## License

Private project.
