# HAL - Agent Guidelines

## Project Overview
Telegram-interactive Italian subtitle downloader. Scans media folders, finds videos missing Italian subs, asks via Telegram whether to download them, and saves them with the correct filename. Runs as a long-lived Docker container.

## Architecture
Single-file Python 3 application (`hal.py`). External deps: `ffmpeg` (audio detection via ffprobe), `ffsubsync` (subtitle-to-audio sync). Two subtitle providers: **Subdl.com** (primary, no VIP placeholders) and **OpenSubtitles.org** (fallback). Translation pipeline: **DeepL** (cue-by-cue, no truncation) → optional **Claude Haiku polish** pass that rewrites only unnatural lines → falls back to full **Claude Sonnet** translation if DeepL is unavailable. Both `.en.srt` and `.it.srt` are saved. Containerized via `Dockerfile` (python:3.11-slim + gcc for webrtcvad build).

### Key Components
- **SubdlClient**: primary provider, REST API, downloads ZIP archives. Episode matching (+500 correct, -1000 wrong) prevents downloading wrong episode subs.
- **OSClient**: fallback provider, XML-RPC, `_download_first_valid()` tries up to 5 results skipping VIP placeholders
- **Two-phase download**: Phase 1 downloads EN subs (free). Phase 2 translates to IT (paid, user-confirmed with cost estimate).
- **Translation pipeline** (`translate_srt`):
  - `translate_srt_with_deepl`: primary path. POSTs to DeepL `/v2/translate` with batches of 50 cues. Each cue is a separate string, so output cannot be truncated. Free tier autodetected via `:fx` key suffix.
  - `polish_translation_with_claude`: secondary pass with Claude Haiku 4.5 (`CLAUDE_POLISH_MODEL`). Receives both EN and DeepL-IT for each cue, returns only the cues it wants to rewrite. Sparse output → very low truncation risk. Cost tracked under `claude_polish_costs` in state.
  - `translate_srt_with_claude`: full Sonnet translation, used when `DEEPL_API_KEY` is missing or DeepL fails (e.g. monthly free credit exhausted). Internals: `_claude_translate_call` issues a single API call; `_claude_translate_bisect` wraps it with recursive halving — if Claude truncates (`stop_reason == "max_tokens"`) or skips any cue, the missing-only subset is re-translated in halved batches until every cue is resolved. Hallucinated indices Claude makes up are rejected at parse time. The only way a cue survives untranslated is a single-cue retry that *still* fails after API success — extremely rare; we log and keep EN.
- **Radarr integration (`/scarica` command)**: `RadarrClient` (v3 REST) adds films with `searchForMovie=false`, calls Interactive Search (`GET /release`), and lets the user pick a release via inline buttons. Release language is detected from Radarr's `languages` field with a fallback to word-boundary regex on the release title (`ITA`, `iTALiAN`, `MULTI`, etc. — no false positives on substrings like "engage"). Ranking: preferred language first, then quality tier (2160p > 1080p > 720p), then seeders. Pending requests live in `/config/requests.json` (separate from `state.json` so `/reset` doesn't drop them). When `_save_sub_and_update_state` saves a sub for a file matching a pending request, the user gets a "📬 Pronto" notification.
- **ffsubsync integration**: aligns subtitle timecodes to video audio via Voice Activity Detection. Uses `os.system()` (not `subprocess.run` with pipes — pipes interfere with ffsubsync's `rich` library). EN subs synced BEFORE translation so IT inherits correct timecodes.
- **ffprobe integration**: detects Italian audio tracks to skip Italian-language films
- **Telegram Bot**: user interaction (grouped notifications for series, individual for films, batch downloads, progress bar, download queue, translation confirmation with cost)
- **Download Queue**: thread-safe FIFO processing download/translate/single jobs sequentially
- **State management**: JSON file tracking asked/downloaded/failed/excluded/en_only state + Claude costs
- **Media scanner**: walks `/media/series` and `/media/films` for missing `.it.srt`

## How to Run Tests
```bash
python3 test_hal.py -v
```
Tests use only `unittest` (stdlib). The test harness patches `/config` paths to a temp directory. 55 tests covering parsing, search cascade, grouping, placeholder detection, Subdl client, ffprobe mocking, episode matching, two-phase download, manual search with dots, queue job types.

## Code Conventions
- Minimal external dependencies (stdlib + ffsubsync only)
- Logging via Python `logging` module to file + stdout
- All Telegram messages in Italian
- SRT files saved as `<video_name>.it.srt` (and `.en.srt` when translating)
- State persisted in `/config/state.json`
- No comments explaining *what* — use self-documenting code. Comments only for *why*.

## Configuration (Environment Variables)
| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot API token |
| `TELEGRAM_CHAT_ID` | Yes | Target chat ID for notifications |
| `SUBDL_API_KEY` | Yes | Subdl.com API key (free registration) |
| `DEEPL_API_KEY` | No | Primary EN→IT translator. Free-tier keys end with `:fx`. Without it, the system falls back to Claude. |
| `CLAUDE_API_KEY` | No | Used for the polish pass (Haiku) and for fallback full-translate (Sonnet). |
| `CLAUDE_MODEL` | No | Sonnet model for the fallback full-translate. Default: `claude-sonnet-4-20250514` |
| `CLAUDE_POLISH_MODEL` | No | Cheap model for the polish pass. Default: `claude-haiku-4-5-20251001` |
| `POLISH_TRANSLATION` | No | `true`/`false`. Run Claude polish over DeepL output. Default: `true`. |
| `RADARR_URL` | No | Radarr v3 base URL (e.g. `http://radarr:7878`). Enables the `/scarica` command. |
| `RADARR_API_KEY` | No | Radarr API key (Settings → General). |
| `RADARR_ROOT_FOLDER` | No | Override root folder for new films. Default: first folder returned by `/rootFolder`. |
| `RADARR_QUALITY_PROFILE` | No | Quality profile id or name. Default: first profile. |
| `RADARR_PREFERRED_LANGUAGES` | No | Comma-separated language tags used to sort the release picker. Default: `ITA,ENG`. |
| `OS_USERNAME` | No | OpenSubtitles.org username |
| `OS_PASSWORD` | No | OpenSubtitles.org password |

## Docker
```bash
docker compose up -d --build hal
```

## Telegram Commands
`/status`, `/scan`, `/costs`, `/sync [name]`, `/cleanup`, `/excludes`, `/reset`, `/help`
Free text searches the media library (handles dots/underscores in filenames).
