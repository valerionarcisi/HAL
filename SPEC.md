# Sub ITA Fetcher - Technical Specification

## Full Flow Overview

```
VIDEO FILE DISCOVERED
        |
        v
    [SCAN PHASE]
        |-- Has .it.srt / .it.hi.srt already? → SKIP
        |-- Folder in exclude list? → SKIP
        |-- Italian audio track (ffprobe)? → SKIP (e.g. "La Battaglia di Algeri")
        |-- Cached as Italian-original (state["italian_original"])? → SKIP
        |-- Already asked/downloaded/failed (within cooldown)? → SKIP
        |-- TMDb original_language="it"? → SKIP + cache (e.g. "La Grande Bellezza")
        |
        v
    [AUTO-ENQUEUE FOR DOWNLOAD]
        |-- Multiple episodes in same series → 1 batch job + 1 "in coda" message
        |-- Single file → 1 single job + 1 "in coda" message
        |-- No user confirmation prompt — subs are always wanted.
        |   The cost gate is on Claude translation, asked later if only ENG was found.
        |
        v
    [STEP 0: CHECK LOCAL FILES]
        |-- Found .it.srt / .ita.srt in folder? → DONE
        |-- Found .en.srt / .eng.srt in folder? → "en_only" (ask to translate)
        |-- Found generic .srt? → Detect language:
        |     |-- Italian → Copy as .it.srt → DONE
        |     |-- English → "en_only" (ask to translate)
        |     |-- Unknown → Continue
        |
        v
    [STEP 1: TRY ITA — _try_save_ita(subdl, os_client, ...)]
        |-- Subdl ITA → validate_sync(min_score=800) → save .it.srt if OK
        |-- OpenSubtitles ITA → validate_sync → save .it.srt if OK
        |-- Returns True if .it.srt was saved with a good sync
        |
        v
    [STEP 2: ALWAYS TRY ENG TOO — _try_save_eng(...)]
        |-- Subdl ENG → validate_sync → save .en.srt if OK
        |-- OpenSubtitles ENG → validate_sync → save .en.srt if OK
        |-- Returns True if .en.srt was saved with a good sync
        |-- Runs regardless of whether ITA was found, so .en.srt is
        |   always available as a backup / for translation later
        |
        v
    [STEP 3: DECIDE OUTCOME]
        |-- ITA saved? → return True (notify "ITA scaricato" + ENG file if also saved)
        |-- Only ENG saved? → return "en_only" (notify "ENG salvato, tradurre?")
        |-- Neither? → return False (notify failure with trace)
        |
        v
    [TELEGRAM: ASK USER TO TRANSLATE]
        |-- Show cost estimate: "Tradurre in italiano? Costo: $X.XX"
        |-- User clicks "Traduci" → translate EN→IT (paid)
        |-- User clicks "Tieni ENG" → keep English only (free)
        |
        v (if user confirms translation)
    [STEP 4: CLAUDE TRANSLATION]
        |-- Read synced .en.srt
        |-- Translate EN→IT with Claude API (batches of 100 blocks)
        |-- Save .it.srt with SAME timecodes as synced .en.srt
        |-- Track cost in state.json → DONE
        |
        v
    FAIL: Mark as "failed", retry after 24h
```

## Subtitle Providers

### Subdl.com (Primary)
- **API**: REST `https://api.subdl.com/api/v1/subtitles`
- **Auth**: API key (free, via env `SUBDL_API_KEY`)
- **Search**: by film name, IMDB ID, TMDB ID, season/episode
- **Download**: ZIP files from `https://dl.subdl.com/subtitle/{id}.zip`, extracts first .srt
- **Advantages**: No VIP placeholders, real subtitles, 64+ languages
- **Rate limits**: Per API key
- **Episode matching**: Scoring system ensures correct episode is selected (+500 match, -1000 mismatch)

### OpenSubtitles.com REST API v1 (Fallback)
- **API**: REST JSON `https://api.opensubtitles.com/api/v1`
- **Auth**: API key via `Api-Key` header, obtained by registering a consumer at `/en/consumers`. Enable `dev_mode` for 100 downloads/day free.
- **Search**: `GET /subtitles` with any of `moviehash`, `imdb_id`, `query`, plus `season_number`, `episode_number`, `languages` (ISO-639-1 two-letter codes).
- **Download**: `POST /download {file_id}` returns a one-time signed URL; fetch the URL to get raw SRT bytes.
- **Advantage over legacy XML-RPC**: the REST API does NOT return VIP placeholder ads. This alone eliminates the main source of download failures.
- `OSClient._to_legacy` maps REST response items to the old XML-RPC field names (`SubFileName`, `MovieReleaseName`, `IDSubtitleFile`, `SubFormat`, `SubDownloadsCnt`, `SubRating`, `MatchedBy`) so downstream scoring code (`pick_best`, `_download_first_valid`) is untouched.

### TMDb (IMDb ID resolver)
- **API**: `https://api.themoviedb.org/3` (v3 key, passed as `api_key` query param).
- **Flow**: `tmdb_find_imdb_id(title, year, is_tv)` calls `/search/movie` (or `/search/tv`) to get a TMDb ID, then `/movie/{id}/external_ids` to extract the IMDb ID.
- **Used by**: `find_imdb_id`, as a fallback when no `.nfo` file provides an IMDb ID. Turns a dirty filename into a canonical `ttXXXXXXX` so both Subdl and OpenSubtitles can search reliably.
- **No-op if `TMDB_API_KEY` is empty** — the resolver returns `None` and callers degrade to name-based search.

## Key Components

### Italian Audio Detection (`has_italian_audio`)
Uses `ffprobe` to inspect audio stream metadata. Checks `language` tag for "ita"/"it"/"italian" and `title` tag for Italian keywords. Videos with Italian audio are skipped (no need for Italian subtitles).

### Italian-Original Detection (`is_italian_original`)
Complementary check that handles Italian-original films whose audio metadata lacks proper language tags (e.g. badly muxed downloads of "La Grande Bellezza"). Calls `tmdb_get_original_language(title, year)` and skips the file if TMDb reports `original_language="it"`. Result is cached in `state["italian_original"]` so subsequent scans don't re-query TMDb. Runs after `has_italian_audio` and after the state checks, so only NEW videos pay the API cost — and only once per file.

### Sync Validation (`validate_sync`)
Writes subtitle content to a temp path, syncs it via `sync_subtitle(min_score=SYNC_MIN_SCORE)`, and validates the score. If the score is too low (< 800 by default), removes the file and returns `{"ok": False, ...}`. This is the gate that decides whether an ITA sub is good enough to save or should be discarded in favour of an ENG fallback.

### Subtitle Sync (`sync_subtitle`)
Uses `ffsubsync` to align subtitle timecodes to the video's audio track via Voice Activity Detection. Analyzes when speech occurs in the audio and aligns subtitle timecodes accordingly. Calculates both time offset (seconds) and framerate scale factor for different video releases. Uses `os.system()` shell execution (not `subprocess.run` with pipes, which interferes with ffsubsync's `rich` library). Non-blocking: if sync fails, the unsynchronized subtitle is kept. Timeout: 5 minutes.

**Sync strategy:**
- **Downloaded EN subs**: Sync `.en.srt` to audio FIRST (with `min_score=1000` validation), THEN translate to `.it.srt` preserving synced timecodes. The `.it.srt` is NEVER synced separately.
- **Local `.en.srt` already present**: Skip sync (timecodes already match the video), translate directly.
- **Downloaded ITA subs**: Sync `.it.srt` directly to audio (no English intermediate).
- **Score validation**: ffsubsync returns a confidence score. Syncs with score < min_score are logged as warnings.
- The `/sync` command re-syncs existing `.it.srt` files on demand.

### Two-Phase Download (Free + Paid)
Download and translation are separated into two phases:
1. **Phase 1 (Free)**: Search and download subtitles. ITA subs saved directly. EN subs saved as `.en.srt` and synced to audio.
2. **Phase 2 (Paid, user-confirmed)**: Translate EN→IT with Claude API. Cost estimate shown BEFORE user confirms. User can keep EN-only for free.

This prevents accidental spending on Claude API translations.

### Dual Subtitle Save
When translating English subtitles, both versions are saved:
- `video.en.srt` — original English (synced to audio)
- `video.it.srt` — Italian translation by Claude (same timecodes as synced EN)

### VIP Placeholder Detection (`is_placeholder_sub`)
Rejects fake subtitles by checking for known ad patterns ("opensubtitles", "vip member", "osdb.link"), fewer than 3 blocks, or single blocks spanning >10 minutes. `_download_first_valid()` tries up to 5 results before giving up.

### Forced/Signs-Only Sub Rejection
Subtitles that only contain foreign language signs or forced dialogue (e.g. `eng-forced.srt`) are rejected:
- **Scoring penalty**: -200 points for subs with "forced", "signs", "songs" in release name
- **ZIP extraction**: Prefers non-forced `.srt` files within ZIP archives
- **Block count check**: Rejects downloaded subs with fewer than 10 dialogue blocks

### Episode Matching (Subdl Scoring)
When searching for series episodes, the scoring system ensures the correct episode is downloaded:
- **Correct episode** (e.g. S01E01 sub for S01E01 video): **+500 points**
- **Wrong episode** (e.g. S01E08 sub for S01E01 video): **-1000 points**
- This prevents downloading a random episode's subtitles

### Filename Parsing (`parse_video`)
Extracts `{type, name, year|season+episode}` from the basename:
1. **Scraper noise stripping** (`_strip_scraper_noise`) — removes leading tracker/scraper prefixes via two regexes applied in a loop: domain-like prefixes (`www.SceneTime.com -`, `rarbg.to.`) and bracketed tags (`[YTS.MX]`, `(RARBG)`). Loops until stable to handle chained prefixes.
2. **Series regex** — `(.+?)[.\s_-]+[Ss]\d+[Ee]\d+` for `SxxEyy` patterns.
3. **Movie regex** — `(.+?)[.\s_-]*\(?(\d{4})\)?(?:[.\s_\-)\]]|$)` supports year with or without parentheses, with or without trailing separator. Crucial for files like `Title (2002).mkv` where `)` is not a separator.
4. **Fallback** — strips common quality/codec tags (`720p`, `x264`, `bluray`, etc.) and returns `type: "unknown"`.
5. **Title normalization** (`_clean_title`) — converts dots/underscores to spaces but preserves internal hyphens (so `Punch-Drunk` is not split).

### English Sub Discovery (`find_english_sub`)
Checks for an existing English subtitle file alongside the video, trying the common suffixes in priority order: `.en.srt` → `.eng.srt` → `.english.srt`. Used everywhere the code needs to look up an English sub (translate prep, batch translate, cost estimation, single-job worker fallback). Centralising this in one helper prevents the class of bugs where a file saved by Radarr/Sonarr as `.eng.srt` is missed by code that hardcodes `.en.srt`.

### IMDB ID Discovery (`find_imdb_id`)
Two-step resolver:
1. **`_find_imdb_id_from_nfo`** — reads `.nfo` files (Sonarr/Radarr) in the video's directory and parent directory, extracts IMDb ID via regex `tt\d{7,}`.
2. **`tmdb_find_imdb_id`** (fallback) — if no `.nfo` is found, uses `parse_video` to get cleaned title + year, queries TMDb, and returns the IMDb ID.

Used by both Subdl and OpenSubtitles for accurate search. Having an IMDb ID routes around messy filenames entirely.

### Search Query Generation (`get_search_queries`)
Returns a deduplicated list of search terms:
1. Name from filename (e.g. "Pluribus")
2. Series folder name (e.g. "PLUR1BUS")
3. Cleaned folder name, alpha-only (e.g. "PLURIBUS")

### OpenSubtitles Quota Tracking
`OSClient.downloads_remaining` is populated from the `remaining` field returned by `POST /download`. The value is appended to the search trace as a `{"_quota": N}` entry and rendered by `format_search_trace` as a final line (`📊 OpenSubtitles download rimanenti: N`). Visible in Telegram failure messages so the user can tell if a failure was caused by quota exhaustion vs. no subs available.

### Claude API Cost Tracking
Each translation tracks `input_tokens`, `output_tokens`, and cost (USD) in `state.json`. Accessible via `/costs` Telegram command. Pre-translation estimate shown on Telegram before user confirms.

## Telegram UX

### Notifications
- **Auto-enqueue on scan**: scan results are automatically queued for download — no "Scarica?" prompt. The bot sends one "⬇️ Scaricando sub per ..." message per video / per series and updates it in place when the download completes (✅ ITA / 🇬🇧 ENG-only / ❌ failed).
- **Cost gate stays**: when only an ENG sub is found, the user is asked whether to translate via Claude (cost shown). This is the only confirmation prompt the bot still issues.
- The legacy `ask_user` and `_send_batch_message` keyboards are still in the codebase for `/sub` manual searches (`search_and_offer`) but are no longer triggered by the periodic scan.

### Two-Phase Batch Flow
1. User clicks "Scarica tutti (9)" → downloads EN subs (free)
2. Bot shows summary: "🇮🇹 3 ITA found, 🇬🇧 6 EN only. Tradurre? Costo: $0.57"
3. User clicks "Traduci in italiano ($0.57)" → translation starts
4. Or clicks "Tieni solo ENG" → keeps English subs only (free)

### Batch Progress
Single message updated in-place with progress bar:
```
⬇️ Scaricando sottotitoli...
[▓▓▓▓░░░░░░] 40%
📊 4/10 — 🇮🇹 2 | 🇬🇧 1 | ❌ 1
```

### Download Queue
All download/translate requests go through a thread-safe FIFO queue processed by a background worker. Supports job types: `batch` (download), `translate` (EN→IT), `single` (individual film). Queue position shown when multiple jobs are pending.

### Manual Search
Type any text in Telegram to search. Handles dots/underscores in filenames (e.g. "Pluribus S01E01" matches "Pluribus.S01E01.720p.x264-FENiX.mkv").

## Telegram Bot Commands
| Command | Description |
|---|---|
| `/status` | Pending/downloaded/failed counts |
| `/scan` | Force manual scan |
| `/costs` | Claude API translation costs |
| `/cleanup` | Remove placeholder subtitles |
| `/excludes` | List excluded folders |
| `/reset` | Clear state cache |
| `/help` | Help |
| `/sync [name]` | Sync all/matching .it.srt to video audio |
| `/translate <name>` | For videos matching `<name>` with an English sub (`.en.srt`, `.eng.srt`, `.english.srt`) but no `.it.srt`, sync each EN sub to audio and then ask the user to confirm the EN→IT translation (Claude). Reuses the batch translate flow. |
| `/delete <name>` | List all subtitle files (`.it.srt`, `.ita.srt`, `.en.srt`, `.eng.srt`, `.english.srt`, `.italian.srt`, `.it.hi.srt`) for videos matching `<name>` and offer a confirmation. On confirm: deletes the files, removes the videos from `state["downloaded"]` and `state["asked"]`, and re-queues each video for a fresh download. |
| `/sub <name>` | Manual library search by title (was `/cerca` pre-multi-source) |
| `/cerca <title [year]>` | Multi-source film download — see "Multi-source download (`/cerca`)" below |
| `/scarica <name [year]> [--lang CODE]` | Request a new film via Radarr with optional language filter (see "Language-targeted download" below) |
| `/ita <name [year]>` | Shortcut for `/scarica ... --lang ITA` |
| `<text>` | Search for videos matching text |

### Multi-source download (`/cerca`)

`/cerca <title [year]>` is an alternative to `/scarica` for films that are not easily found on the configured Radarr indexers. The bot searches the **Internet Archive (`archive.org`) and YouTube in parallel**, downloads the chosen result, remuxes it to MKV and installs it under `/media/films/<Title (Year)>/<Title (Year)>.mkv` — the same layout Radarr/Emby/Jellyfin expect, so the file is picked up automatically.

- **Aliases**: `/search`, `/find`.
- **Sources**:
  - **archive.org**: `advancedsearch.php` with `mediatype:movies`, followed by a `/metadata/<id>` call per result to find the best video file. Prefers MP4 over MKV/WEBM/AVI; bigger files within a format are preferred (better encode). The picked file's `width`/`height` / `format` / `bitrate` is inspected to attach a real `resolution_label` (`2160p` / `1080p` / `720p` / `480p` / `360p` / `None`). The picker uses the readable `metadata.title` from `/metadata/<id>` rather than the ugly slug identifier.
  - **YouTube**: `yt-dlp ytsearchN:<query> --flat-playlist` for the fast initial sweep. For the top 5 candidates the bot then runs a parallel (`ThreadPoolExecutor(max_workers=4)`) `yt-dlp --skip-download --print '%(height)s|%(format_id)s|%(filesize_approx)s'` to discover the real resolution of the best stream. Cookies file (`YT_COOKIES_FILE`, default `/config/secrets/yt_cookies.txt`) is passed when present to bypass age-gates and consent walls; missing cookies degrade silently (fewer results, no hard error).
- **Filters**: YouTube results outside the `60–240 minute` band are dropped (clips, trailers, live streams). Sub-720p / unknown-resolution results are still shown but tagged `⚠️ Bassa qualità`. Any result whose title matches a `_CERCA_PENALTY_PATTERNS` rule that fires `trailer` / `clip` / `behind-the-scenes` / `making-of` / `commentary` / `preview` / `scene` is dropped from the picker entirely (score `< -1500`).
- **Scoring + ordering**: every surviving candidate is run through `score_result(result, tmdb_runtime_min)`. The score combines penalty/bonus patterns (`rescored: -500`, `fan edit: -700`, `recut: -400`, `extended: +100`, `director's cut: +100`), source rank (archive > YouTube), resolution rank (2160p > 1080p > 720p > 480p), file size (capped at 5 GB; bigger == suspect), and duration coherence with TMDb's reported runtime (±5 min → +30, ±15 min → +10, off by 30+ min → -15). Results are sorted by score DESC so the cleanest, most-likely-the-actual-film entries appear first.
- **Confirmation card**: shows the TMDb director (when resolved via `tmdb_get_movie_details`), the real resolution, file size and the readable archive.org metadata title. Results flagged by penalty patterns get an inline `⚠️ Possibilmente versione alternativa: 🎵 rescored / ✂️ fan edit / …` warning line so the user sees the caveat BEFORE confirming.
- **Flow**: TMDb disambiguation (best match) → TMDb runtime + director lookup → parallel search archive.org + YouTube → YouTube resolution enrichment → penalty filtering + score sort → consolidated picker → confirmation card → instant `⬇️ Avvio download…` feedback edit BEFORE enqueueing the job → download (progress bar) → ffmpeg `-c copy` remux to MKV → move into the library folder → Radarr `DownloadedMoviesScan` trigger.
- **Encoding policy**: **remux only** (`ffmpeg -c copy`). No re-encode. If the source is already MKV, the remux step is skipped.
- **State**: a short-lived `pending_cerca` namespace in `requests.json` keyed by film hash holds the picker results between step 1 and step 3, plus the resolved `director`, `tmdb_runtime_min`, `downloading_idx` (idempotency guard) and `last_grab_at` so the user can take their time.
- **Idempotency**: `cerca_grab` callbacks are idempotent per session. The first click sets `downloading_idx` on the pending entry and edits the message to `⬇️ Avvio download…` BEFORE pushing onto the queue. Subsequent clicks for the same result while the worker is still busy answer with `⏳ Download già in corso` and do nothing else. The guard is released on download success (entry removed) or failure (`downloading_idx` reset, picker reopened via inline `↩️ Cambia fonte`).
- **Retries**: archive.org downloads retry on transient 5xx and transport errors with exponential backoff (2 s / 5 s / 10 s — four attempts total). Hard failures (404, 403) fail immediately. YouTube downloads do not retry — yt-dlp already handles its own internal retries.
- **Errors**: any failure (network, yt-dlp missing, ffmpeg failure, disk full, library not writable) surfaces as a friendly Telegram message — no stack traces. The temp dir is wiped in all paths. After a failed `cerca_grab`, the error card includes an inline `↩️ Cambia fonte` button that reopens the picker so the user can fall back to the other source.

### Language-targeted download (`--lang` flag)

`/scarica` accepts an optional `--lang CODE` flag that filters the Radarr Interactive Search release list and triggers post-grab audio verification once the file lands on disk.

- **Syntax**: `/scarica <title> [year] [--lang CODE]`. The flag can appear anywhere in the message (`/scarica film --lang ITA`, `/scarica --lang JPN film`, `/scarica --lang=fra film`). Accepts 2-letter ISO 639-1 (`it`, `en`, `ja`) or 3-letter ISO 639-2 (`ITA`, `ENG`, `JPN`) or full names (`italian`); internally normalized to a 3-letter code from the LANGUAGE_REGISTRY.
- **Supported codes**: ITA, ENG, JPN, KOR, FRA, GER, SPA, CHI, RUS. Plus the post-grab-verify markers MULTI and DUAL detected on releases (the user does NOT pass these as `--lang`; they are inferred from the release title and trigger ffprobe verification).
- **Alias `/ita`**: shortcut equivalent to `/scarica ... --lang ITA`. Wired into the command alias registry, exposed in `/aiuto`, and surfaced in the "did you mean?" reply for close typos.
- **Default = VO**: without `--lang`, the bot queries TMDb for the picked film's `original_language` and maps it (`ja → JPN`, `it → ITA`, `en → ENG`, …) to a registry code. If TMDb returns a language we don't track, a banner is shown and no filter is applied. If `TMDB_API_KEY` is missing, the bot tells the user and proceeds without filter.
- **Pre-filter**: after `RadarrClient.releases()` returns, the list is filtered to keep only releases whose `detect_release_languages` set includes the target. MULTI/DUAL releases pass through unconditionally (they may contain the target track; verified post-grab). The existing `rank_releases` ordering then applies to the filtered list. Empty filter → friendly suggestion to retry without `--lang`.
- **Source banner**: the release picker and the grab-confirmation card both show `🔤 Filtro: 🇮🇹 ITA (esplicito)` or `🔤 Filtro: 🇯🇵 JPN (VO da TMDb)` so the user knows where the filter came from.
- **Post-grab verification**: each grabbed release stores `required_lang` in `requests.json` (`null` when no filter was applied or when the target was MULTI/DUAL). When the file lands and `_save_sub_and_update_state` matches a pending request, `verify_audio_language(video_path, required_lang)` inspects ffprobe `language` and `title` tags. On match → normal `📬 Pronto` notification. On mismatch → warning with `[✅ Tieni]` / `[🔁 Ri-richiedi]` inline buttons. The redo path skips TMDb / Radarr re-add and shows a fresh release list filtered on the same language, excluding the release the user just rejected.
- **False-positive safety**: release-title detection splits on non-alpha characters so 2-letter codes like `IT`, `EN`, `JP` never match inside common English words (HOBBIT, LIMIT, WHITE, iTunes).

## Configuration (Environment Variables)
| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot API token |
| `TELEGRAM_CHAT_ID` | Yes | Target chat ID |
| `SUBDL_API_KEY` | Yes | Subdl.com API key (free) |
| `OPENSUBTITLES_API_KEY` | Recommended | OpenSubtitles.com REST v1 API consumer key (free, enable `dev_mode` for 100 dl/day) |
| `TMDB_API_KEY` | Recommended | TMDb v3 API key (free, unlimited). Enables IMDb ID resolution from title+year |
| `CLAUDE_API_KEY` | No | Enables EN→IT translation fallback |
| `CLAUDE_MODEL` | No | Default: `claude-sonnet-4-20250514` |
| `OS_USERNAME` | No | Legacy, unused (kept for backwards compat) |
| `OS_PASSWORD` | No | Legacy, unused (kept for backwards compat) |
| `YT_COOKIES_FILE` | No | Path inside the container to a Netscape cookies.txt used by yt-dlp for `/cerca` YouTube downloads. Default `/config/secrets/yt_cookies.txt`. Missing file = no cookies (fewer YouTube results, no hard error). |
| `CERCA_DOWNLOAD_DIR` | No | Library root where `/cerca` installs the final MKV. Default `/media/films`. |
| `CERCA_MIN_RESOLUTION` | No | Minimum acceptable YouTube resolution (height) for `/cerca`. Default `720`. Lower-res results are still shown but tagged ⚠️ bassa qualità. |
| `CERCA_FALLBACK_RESOLUTION` | No | Hard floor used by yt-dlp's format selector when no `min` candidate exists. Default `480`. |

## Dependencies
| Dependency | Purpose | Install |
|---|---|---|
| Python 3.11 | Runtime | Docker base image |
| ffmpeg/ffprobe | Audio language detection | `apt-get install ffmpeg` |
| ffsubsync | Subtitle sync to audio | `pip install ffsubsync` |
| gcc + libc6-dev | Build webrtcvad (ffsubsync dep) | `apt-get install gcc libc6-dev` |
| Subdl.com API | Primary subtitle provider | API key via env var |
| OpenSubtitles XML-RPC | Fallback subtitle provider | Built-in (stdlib xmlrpc) |
| Claude API | EN→IT translation | API key via env var |

## File Structure
```
/app/sub_fetcher.py          # Main application
/config/state.json           # Persistent state (asked, downloaded, last_offset, claude_costs)
/config/batches.json         # Pending download/translate batches (separate file to avoid race conditions)
/config/exclude_folders.txt  # Excluded folders list
/config/sub_fetcher.log      # Application log
/media/series/               # Series (read-only mount)
/media/films/                # Films (read-only mount)
```

## Thread Safety

The main loop and the queue worker run in separate threads and both read/write persistent state. To avoid race conditions:

- `state.json` is owned by the main thread. The queue worker loads it locally at job start, makes changes, and saves it. The main thread also saves it every 5 seconds (for `last_offset` persistence).
- `batches.json` is owned independently: any thread reads/writes it via `load_batches()`/`save_batches()`. Since batches are only written at discrete moments (job complete, scan notify) and read only on button press, there is no write contention.
- The previous bug: batches were stored inside `state["batches"]`. The main thread's unconditional `save_state()` call at the end of `process_callbacks` used a stale in-memory state without the queue worker's newly-added batches, silently wiping them. Moving batches to a separate file eliminates this entirely.

## Testing
Run: `python3 -m unittest test_sub_fetcher -v`

### Pre-commit hook
The repo ships a tracked hook at `.githooks/pre-commit` that runs the full test suite before every commit. On first clone, activate it once:
```sh
git config core.hooksPath .githooks
```

## Future Ideas

Non-committed brainstorm of features that would be easy to add on top of the current architecture. Pick whichever becomes painful first.

### TMDb fallback lookup
When all providers return zero results, query TMDb by cleaned title + year to obtain the canonical title and IMDb ID, then retry the search by IMDb ID. Would rescue cases where the filename contains typos, alternate titles, or localized names. Requires a free TMDb API key.

### `/retitle <video> <new title>` command
Allow the user to manually override the parsed title for a specific file from Telegram. The override is stored in `state.json` and reused on next search. Useful when `parse_video` misfires on edge cases (anime with fansub tags, documentaries with unusual naming).

### Provider success-rate stats
Track per-provider hits/misses in `state.json` and expose via `/stats`. Helps decide when to reorder the provider cascade or drop one.

### Notification when a previously-failed item is finally found
Failures are retried after 24h. Today the user only sees the retry if they open Telegram at the right moment. A one-shot "📬 Finalmente trovato: X" notification on success would close the loop.

### Multi-language support
Currently hardcoded to Italian. Generalizing to a `TARGET_LANGS` env var (e.g. `it,es`) would let the same bot serve mixed-language households. Scoring, regex, and Claude translation prompt already parameterize cleanly on language code.

### inotify / watchdog instead of polling
Main loop scans the filesystem on a timer. Switching to `watchdog` would cut idle CPU and make new releases appear on Telegram within seconds instead of minutes. Keep the periodic scan as a safety net for missed events.

### `/undo` last download
Keep the last N downloaded subtitle paths in a ring buffer. `/undo` deletes the most recent `.it.srt` and re-marks the video as pending. Useful when a wrong subtitle slipped through the placeholder detector.

### Subtitle quality score in notifications
When multiple candidates exist, show the chosen one's score and provider in the success message (e.g. `✅ Punch-Drunk Love — Subdl (score: 780)`). Makes it easier to spot low-confidence matches that may need `/sync` or manual review.


---

## Proposal: request a new film from Telegram (`/scarica`)

### Goal
Today the bot only reacts to videos that **already exist** under `/media/films` and `/media/series`. The user wants a Telegram-driven way to **request a film the library does not yet have**, kicking off the standard *arr download flow without leaving the chat. The subtitle pipeline then triggers automatically once the file lands on disk.

### User flow
Two-step manual selection: first the **film**, then the **release**. We never let Radarr auto-grab — the user decides.

```
User:  /scarica Punch-Drunk Love
        ↓
Bot:   Search TMDb for title (+ optional year)
        ↓
Bot:   Step 1 — film disambiguation (inline buttons, top 5)
       [ 🎬 Punch-Drunk Love (2002) — EN · Paul Thomas Anderson ]
       [ 🎬 Punch-Drunk Knuckle (2015) — EN ]
       [ ❌ Annulla ]
        ↓
User:  Taps the right one
        ↓
Bot:   POST /api/v3/movie with monitored=true, searchForMovie=false
       (we add it to Radarr but do NOT auto-grab — we want the release list)
        ↓
Bot:   GET /api/v3/release?movieId=<id>   ← "Interactive Search"
        ↓
Bot:   Step 2 — release list (top 8, sorted by quality+seeders, paged if needed)
       Each row: quality · size · language · indexer · seeders
       
       🇮🇹 2160p · 18.4 GB · ITA+ENG · BHD · 142↑
       🇮🇹 1080p · 9.1 GB · ITA      · iTorrents · 88↑
       🇬🇧 1080p · 7.8 GB · ENG      · BHD · 220↑
       🇬🇧 720p  · 3.2 GB · ENG      · TorrentDay · 41↑
       […8 rows + Avanti/Indietro buttons]
       [ ❌ Annulla ]
        ↓
User:  Taps a release
        ↓
Bot:   Confirmation card (poster + full release name + size + indexer + grab button)
       [ ⬇️ Conferma grab ]  [ ↩️ Cambia release ]
        ↓
User:  Conferma
        ↓
Bot:   POST /api/v3/release  { guid, indexerId }
        ↓
Bot:   "📥 In download via <indexer>. Ti avviso quando arriva il file + sub ITA."
        ↓
        (existing sub-fetcher scan picks up the .mkv when the *arr stack finishes,
         the normal subtitle download / translation flow runs)
        ↓
Bot:   "✅ Pronto: Punch-Drunk Love (2002) — sub ITA scaricato"
```

### Scope
**In scope**
- Films only (Radarr). Series via Sonarr can be a second iteration.
- TMDb-driven disambiguation (already integrated for IMDb-id resolution).
- Inline-button selection of the match — no free-text disambiguation.
- One Radarr profile / root folder, configured via env vars.
- No quality / release-group choices: trust Radarr's default profile.

**Out of scope**
- Listing / cancelling pending Radarr requests from Telegram (use Radarr UI).
- Authentication / multi-user. The bot already trusts `TELEGRAM_CHAT_ID`.
- Mapping the eventual download back to a specific Telegram message ID (the sub-fetcher's existing scan-and-notify is good enough).

### New components
1. **`RadarrClient`** — minimal wrapper around Radarr v3 REST API:
   - `lookup(tmdb_id) -> dict` (`/movie/lookup/tmdb?tmdbId=...`) — validates the film and checks if it's already added.
   - `add(tmdb_id, quality_profile_id, root_folder_path) -> int` — POST `/movie` with `monitored=true, addOptions.searchForMovie=false` (we want the release list, not auto-grab). Returns the new `movieId`. Handles 409 (already exists) by re-fetching the existing movieId.
   - `releases(movie_id) -> list[Release]` — GET `/release?movieId=<id>`. Triggers Radarr's Interactive Search across all configured indexers and returns the deduplicated list. Each entry exposes: `guid`, `indexerId`, `indexer` (display name), `title` (release name), `size` (bytes), `seeders`, `leechers`, `quality.quality.name` (e.g. "Bluray-1080p"), `languages` (list of `{id, name}`), `rejections` (list — Radarr's reasons not to grab, e.g. "Already imported"). Surface rejections to the user as warning badges, don't filter them out.
   - `grab(guid, indexer_id) -> dict` — POST `/release` with the chosen guid + indexerId. Radarr forwards to the download client.

2. **Release ranking & formatting** (sub-fetcher side):
   - Sort by: (a) preferred-language matches first, (b) quality tier (2160p > 1080p > 720p), (c) seeders desc.
   - Detect language from `release.languages` first; fall back to substring match on the release title (`ITA`, `ITALIAN`, `iTALiAN`, `ENG`, `MULTI`). Show flag emoji per inferred language.
   - Truncate the release name to ~80 chars for the inline-button label (Telegram limits button text length); the **full** release name is shown in the confirmation card on the next step.
   - Paginate with Avanti / Indietro buttons (8 per page).

3. **TMDb search** — extend the existing TMDb helper with `search_movies(query) -> [{tmdb_id, title, year, original_language, overview, poster_url}]`. We already call `/search/movie` for IMDb resolution; just expose the multi-result form.

4. **New command `/scarica <title>`** (alias `/download`, `/req`) wired into the existing command registry.

5. **New callback handlers**:
   - `radarr_pick:<film_hash>` — step-1 selection (which TMDb film).
   - `radarr_rel:<release_hash>` — step-2 selection (which release).
   - `radarr_page:<film_hash>:<n>` — release list pagination.
   - `radarr_grab:<release_hash>` — final confirmation.
   - `radarr_cancel:<hash>` — abort at any step.

6. **Optional v2**: Radarr webhook → small HTTP listener so we can notify "📥 download partito / completato" without waiting for the next filesystem scan.

### State changes
- New file `requests.json` (separate from `state.json` to keep concerns clean — survives `/reset` since the user wouldn't expect a "Pronto" notification to be lost):
  ```json
  {
    "pending_radarr": {
      "<tmdb_id>": {
        "title": "Punch-Drunk Love",
        "year": 2002,
        "requested_at": "2026-05-13T17:00:00",
        "telegram_msg_id": 12345,
        "movie_id": 4231,
        "release": {"title": "...", "indexer": "BHD", "size_gb": 18.4, "language": "ITA"}
      }
    }
  }
  ```
- Short-lived `batches.json` entries hold the release list between step-1 and step-2 (keyed by `film_hash`), so the user can paginate / change release without re-querying Radarr.
- When `do_download` finds a new file under `/media/films`, after a successful subtitle save it checks `pending_radarr` for a matching title+year and clears the entry with a "📬 Pronto" notification.

### Configuration (new env vars)
| Variable | Required | Default | Description |
|---|---|---|---|
| `RADARR_URL` | Yes | — | e.g. `http://radarr:7878` |
| `RADARR_API_KEY` | Yes | — | from Radarr → Settings → General |
| `RADARR_ROOT_FOLDER` | No | first folder from `/rootFolder` | absolute path where Radarr writes films |
| `RADARR_QUALITY_PROFILE` | No | first profile from `/qualityprofile` | quality profile id or name — note: profile gates only **what shows up** in the release list. With Interactive Search the user can still grab outside the profile. |
| `RADARR_PREFERRED_LANGUAGES` | No | `ITA,ENG` | sort hint for the release picker |
| `TMDB_API_KEY` | Yes | — | already used; required for search |

### Error handling
- Radarr unreachable → "❌ Radarr non raggiungibile (RADARR_URL=...)". No retry; user re-tries.
- TMDb returns no results → "❌ Nessun film trovato per '<query>'. Prova con titolo + anno."
- Film already in Radarr (409) → re-use the existing `movieId` and proceed straight to the release list.
- Interactive Search returns 0 releases → "⚠️ Nessun rilascio trovato sugli indexer configurati. Riprova più tardi o controlla la profile in Radarr."
- All releases have `rejections` → still show them (with a 🚫 badge per row), but warn at the top.
- Radarr root folder / profile missing → log + Telegram error with the exact field. Fail loud.

### Why this fits the current architecture
- The bot already wraps a few HTTP APIs (Subdl, OpenSubtitles, TMDb, Claude, DeepL) — Radarr is just another. Same `urllib.request` style.
- The post-download trigger needs **no new logic**: the existing `scan_missing` loop already picks up new files. We just decorate the existing success notification with the "previously-requested via Telegram" context.
- The Telegram command registry just gained an alias system — `/scarica` slots in as one more entry.

### Open questions (decide before implementing)
1. **Sonarr (series) in the same iteration, or split?** Sonarr's add-series payload is more complex (seasons, monitoring profile). I'd ship films first.
2. **Interactive Search timeout.** Radarr queries every indexer sequentially — can take 10-30s on a sparse search. Show a "🔎 Sto chiedendo agli indexer…" placeholder before the release list, and edit-in-place when results arrive.
3. **Release list page size.** 8 per page (Telegram inline-button rows render well, message stays readable). Bigger pages risk the 4096-char limit + 100-button limit.
4. **What if Radarr already has a downloaded file for that film?** `lookup` exposes `hasFile=true`. Skip the flow and tell the user "ℹ️ Già scaricato in <path>". Optionally offer `/cancella` to wipe subs + re-queue.
5. **Where does the "📬 ready" notification fire?** From `do_download`'s success path checking `pending_radarr`, vs. a Radarr webhook listener. Webhook is cleaner but adds an HTTP server. State-file polling is dumber but zero ops.

### Estimated complexity
- ~400 lines: `RadarrClient` with release support (~140), TMDb multi-search (~30), command + 5 callbacks + pagination (~140), state plumbing (~50), tests (~40 cases).
- ~3-4 hours of focused work, mostly Radarr-side configuration and end-to-end testing on a real install with at least one configured indexer.
