# Sub ITA Fetcher — CLAUDE.md

Project-level guidance for AI agents (Claude Code, etc.) working in this repo. Read this FIRST, before touching any file.

Other docs to consult, in order:

1. `README.md` — what the bot does and how to run it
2. `SPEC.md` — full technical specification (flow, providers, error handling, future ideas)
3. `AGENTS.md` — short agent guidelines (kept for backwards compat; this file supersedes it)

If a rule here contradicts those, this file wins.

---

## 1. What this project is

Telegram-interactive Italian subtitle downloader. Long-lived Docker container on a home NAS that:

1. Scans `/media/series` and `/media/films` on a timer
2. For every video missing `.it.srt`, runs a multi-provider search cascade (Subdl → OpenSubtitles), syncs subs to audio (`ffsubsync`), saves them
3. When only EN is found, asks the user via Telegram whether to translate (DeepL → optional Claude polish, or full Claude as fallback)
4. Exposes Telegram commands for manual search, sync, retranslation, deletion, status, costs, Radarr-driven new-film requests (`/scarica`)

Single-file Python 3.11 app: `sub_fetcher.py` (~177 KB). Single-file test suite: `test_sub_fetcher.py` (~92 KB, stdlib `unittest` only, 55+ cases).

---

## 2. Architecture in one screen

```
+------------------+        +-------------------+
|  Main thread     |        |  Queue worker     |
|  - scan loop     |        |  - download jobs  |
|  - tg polling    |  ←→    |  - translate jobs |
|  - callback hdlr |        |  - single jobs    |
+--------+---------+        +----------+--------+
         |                              |
         v                              v
  state.json     batches.json     requests.json     sub_fetcher.log
  (asked,        (pending         (Radarr pending   (rotating)
   downloaded,   batches +        film requests,
   failed,       release lists)   separate from
   en_only,                       state so /reset
   costs)                         doesn't drop them)
```

### Classes

- **`SubdlClient`** — primary provider. REST. ZIP-based downloads. Episode-match scoring (+500 / −1000) to prevent wrong-episode subs.
- **`OSClient`** — fallback provider. OpenSubtitles.com REST v1 (NOT legacy XML-RPC). `_to_legacy()` adapts response to legacy field names so downstream scoring code is unchanged. Tracks `downloads_remaining` for quota reporting.
- **`RadarrClient`** — minimal wrapper around Radarr v3 REST. Adds film with `searchForMovie=false`, calls Interactive Search (`GET /release`), grabs the user-picked release (`POST /release`). Language detection: `release.languages` first, then word-boundary regex fallback (`\bITA\b`, `\bITALIAN\b`, `\bMULTI\b`).

### Key functions (you will edit these often)

- `parse_video()` — extracts `{type, name, year|season+episode}` from basename. Strips scraper noise (`www.SceneTime.com -`, `[YTS.MX]`). Series regex first, movie regex second, fallback last. **Edit very carefully — every regex change risks breaking real-world filenames.**
- `find_imdb_id()` → `_find_imdb_id_from_nfo()` → `tmdb_find_imdb_id()` — two-step IMDb resolver. NFO first (free, local), TMDb fallback (1 API call). Both Subdl and OpenSubtitles search by IMDb ID when available.
- `has_italian_audio()` / `is_italian_original()` — two complementary skip checks. First inspects audio stream metadata via `ffprobe`. Second consults TMDb `original_language` for muxed-without-tags Italian originals. Result cached in `state["italian_original"]` so TMDb is hit at most once per file.
- `validate_sync()` — gate between download and save. Writes sub to temp file, runs `sync_subtitle(min_score=800)`, discards if score is too low. This is what keeps bad ITA subs from masking a perfectly good ENG alternative.
- `sync_subtitle()` — runs `ffsubsync` via **`os.system()`** (NOT `subprocess.run` with pipes — pipes break `ffsubsync`'s `rich` library). 5-minute timeout. Non-blocking on failure: returns unsynced sub.
- `translate_srt()` → `translate_srt_with_deepl()` → `polish_translation_with_claude()` → `translate_srt_with_claude()` fallback. **Three-stage translation pipeline**, see §5.
- `pick_best()` — ranks candidate subs from a provider. Penalises forced/signs-only (`-200`), VIP placeholders (rejected outright), wrong episode (`-1000`). Boosts correct episode (`+500`).

---

## 3. Hard rules — DO NOT BREAK

These are non-negotiable. If you are about to do any of these, stop and ask.

- **Never run `subprocess.run` with `stdout=PIPE` / `stderr=PIPE` for `ffsubsync`.** Use `os.system()`. `rich` (ffsubsync's progress lib) misbehaves under captured pipes. This bug already cost us hours.
- **Never short-circuit `validate_sync()`.** Every saved sub MUST pass the score gate. Skipping it for "trusted" providers is how bad subs got into the library.
- **Never store a Claude API key, Telegram token, DeepL key, or any credential in code, tests, or commits.** Everything goes through env vars. The `.gitignore` already excludes `state.json` / `requests.json` / `batches.json` because they can contain channel IDs and quota balances — keep it that way.
- **Never log full subtitle content, API responses, or user-facing Telegram messages with credentials.** Log structured events (`provider=subdl`, `result=ok`, `score=812`), not blobs.
- **Never write code in Italian.** Identifiers, function names, comments, log lines, internal error messages → English. Telegram user-facing copy → Italian (it's the product language). A function called `scaricaSottotitolo` is a hard NO.
- **Never bypass the cost gate on Claude translation.** Phase 1 (free EN+ITA search) is unconditional. Phase 2 (paid EN→IT) requires explicit Telegram confirmation. Skipping confirmation = burning user money.
- **Never call `state.json` mutations from the queue worker without copying the state first.** The main thread also writes `state.json` every 5s. See §6 for the locking convention.
- **Never put batches inside `state.json` again.** This was an actual bug: the main thread's `save_state()` wiped batches added by the queue worker because it serialized stale in-memory state. Batches live in `batches.json`. Requests live in `requests.json`. They MUST stay separate.
- **Never break the alias registry.** Every command MUST be reachable via its canonical Italian name AND English aliases. If you add a command, update the registry + add the alias map + add a typo "did you mean?" entry.
- **Never `print()` for user feedback.** Telegram-facing output via `tg_send` / `tg_edit_message`. Local diagnostics via the `logging` module.
- **Never re-introduce the legacy OpenSubtitles XML-RPC client.** REST v1 is the only path. Old `OS_USERNAME` / `OS_PASSWORD` env vars are kept as no-ops for backwards compat — do not wire them to anything.
- **Never trust filenames without `parse_video()` + `_strip_scraper_noise()`.** Real-world inputs contain `www.SceneTime.com - Title (2002).mkv`, `[YTS.MX] Title.2002.mkv`, etc.
- **Never depend on a non-stdlib library in tests.** Tests run with `python3 -m unittest` and nothing else.

---

## 4. Workflow — every change

1. **Read** `SPEC.md` for the feature you're touching. If the spec is silent, the implementation is authoritative.
2. **Read the relevant function(s)** in `sub_fetcher.py` end-to-end before editing. Functions are big; partial edits cause silent breakage.
3. **Write the test first** in `test_sub_fetcher.py` (mirror the existing style: stdlib only, mock HTTP via `unittest.mock`, patch `/config` paths to a temp dir).
4. **Implement the change.** Self-documenting names. Comments only for *why*, never for *what*.
5. **Run the full test suite** before considering the change done:
   ```bash
   python3 -m unittest test_sub_fetcher -v
   ```
6. **Update docs** if behaviour changes:
   - `SPEC.md` — if you changed a flow, a provider contract, a state field, or an env var
   - `README.md` — if you changed env vars or commands surfaced to the user
   - `CLAUDE.md` (this file) — if you discovered a new pitfall or a new hard rule
7. **Commit.** Follow the conventions in §8. The tracked pre-commit hook runs the test suite — do not skip it (`--no-verify` is forbidden).
8. **Rebuild + restart on the NAS:**
   ```bash
   docker compose up -d --build sub-fetcher
   docker logs -f sub-fetcher --tail 100
   ```

---

## 5. The translation pipeline (most error-prone subsystem)

Three stages, each with its own failure mode. Touch with care.

### Stage 1 — DeepL (`translate_srt_with_deepl`)

- POST to `/v2/translate` with batches of **50 cues per request** (each cue a separate string in the array).
- Per-cue request/response means the output cannot be truncated — DeepL returns exactly `len(input)` strings or errors.
- Free-tier key autodetected by `:fx` suffix → uses `api-free.deepl.com`. Paid → `api.deepl.com`.
- Character usage tracked under `state["deepl_chars"]`.
- **Failure modes**: 429 (rate limit), 456 (quota exhausted), 5xx. On any failure: fall through to Stage 3 (Claude full).

### Stage 2 — Claude polish (`polish_translation_with_claude`)

- Default ON (`POLISH_TRANSLATION=true`). Model: `CLAUDE_POLISH_MODEL` (default Haiku 4.5).
- Receives BOTH the EN cue and the DeepL IT cue for each line. Returns ONLY the cues it wants to rewrite (sparse output).
- Sparse → very low truncation risk. We never ask Claude to "rewrite all 800 lines" in one shot.
- Batches of **80 cues**. Cost tracked under `state["claude_polish_costs"]`.
- **Failure modes**: API down, malformed JSON. On failure: keep the DeepL output as-is, log warning.

### Stage 3 — Claude full translate (`translate_srt_with_claude`)

- Used ONLY when DeepL is unavailable (no key, or all retries failed).
- Two-layer call: `_claude_translate_call` makes one API call; `_claude_translate_bisect` wraps it with **recursive halving on truncation or missing cues**.
- If Claude returns `stop_reason == "max_tokens"` or skips any cue, the missing subset is re-translated in halved batches until every cue resolves.
- Claude can hallucinate cue indices — those are rejected at parse time.
- Batches start at 40 cues, halve down to 1. A single-cue retry failure leaves the EN line untranslated and is logged. This should essentially never happen.
- Cost tracked under `state["claude_costs"]` (distinct from `claude_polish_costs`).

### Cost surfacing

- `/costi` (`/costs`) splits DeepL chars, Claude polish cost, Claude full cost into three sections so you can see where the money goes.
- Pre-translate confirmation message always shows the **estimate**, not just the offer. Never charge without showing the number.

---

## 6. State files — locking convention

Three persistent JSON files, each with a specific owner:

| File | Owner | Reader | Write trigger |
|---|---|---|---|
| `state.json` | Main thread | Both threads | Every 5s (main, for `last_offset`); end of job (queue worker) |
| `batches.json` | Either thread | Either thread | Discrete moments (job complete, scan notify, button press) |
| `requests.json` | Either thread | Either thread | New `/scarica` request; "Pronto" notification clears entry |

### Rules

- **Queue worker** must call `load_state()` at the start of every job and `save_state()` at the end. Do NOT hold an in-memory reference across job boundaries — the main thread may have written in between.
- **Main thread** writes `state.json` every 5s for `last_offset` persistence. This is intentional and must not be removed.
- `batches.json` and `requests.json` are written at discrete moments only — no write contention has surfaced in production.
- If you find yourself wanting a global lock, you've probably designed wrong. Re-read the convention and find which thread should own the field.

---

## 7. Testing

### How to run

```bash
python3 -m unittest test_sub_fetcher -v          # full suite
python3 -m unittest test_sub_fetcher.TestParseVideo -v   # one class
python3 -m unittest test_sub_fetcher.TestParseVideo.test_movie_with_year_in_parens -v  # one case
```

### Conventions

- Stdlib `unittest` only. No `pytest`. No `mock` library — use `unittest.mock`.
- Tests patch `/config` paths to a temp directory in `setUp` and clean up in `tearDown`. Never leak state across tests.
- Mock HTTP at `urllib.request.urlopen` level. No real network calls.
- Mock `ffprobe`/`ffsubsync` subprocess calls. Real binaries do not run during tests.
- New feature → at least one happy-path test and one sad-path test. Five-case minimum on any pure function (happy, edge, boundary, error, default).

### Pre-commit hook

Already tracked at `.githooks/pre-commit`. Activate once after cloning:

```bash
git config core.hooksPath .githooks
```

Runs the full suite. Do not skip with `--no-verify`. If the hook fails, fix the issue; do not bypass.

---

## 8. Git

### Commit format

Lower-case imperative subject, no scope prefix:

```
Add DeepL free-tier autodetect
Fix episode-match score in series search
Skip Italian-original films via TMDb original_language
```

Body (optional) explains *why*. Reference an issue or a real-world bug case if relevant.

### Hard nos

- **No AI signatures** in commits (`Co-Authored-By: Claude` and similar). Hard rule.
- **No squashed mega-commits** for unrelated changes. One logical change per commit.
- **No `--no-verify`.** The pre-commit hook exists for a reason.
- **No commits with `state.json` / `requests.json` / `batches.json` / `*.log` staged.** They're in `.gitignore`; if you see them in `git status`, check the gitignore wasn't broken.

---

## 9. Configuration — quick reference

Required:

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — bot identity + target chat
- `SUBDL_API_KEY` — primary provider

Recommended (each one degrades cleanly if absent):

- `OPENSUBTITLES_API_KEY` — fallback provider; 100 dl/day with `dev_mode`
- `TMDB_API_KEY` — IMDb resolution + Italian-original detection
- `DEEPL_API_KEY` — primary translation (free tier ends with `:fx`)
- `CLAUDE_API_KEY` — polish pass + full-translate fallback

Translation tuning:

- `CLAUDE_MODEL` (default `claude-sonnet-4-20250514`) — full translate model
- `CLAUDE_POLISH_MODEL` (default `claude-haiku-4-5-20251001`) — polish model
- `POLISH_TRANSLATION` (default `true`) — toggle polish pass

Radarr (enables `/scarica`):

- `RADARR_URL`, `RADARR_API_KEY` — required for the command
- `RADARR_ROOT_FOLDER` — default: first folder from `/rootFolder`
- `RADARR_QUALITY_PROFILE` — default: first profile
- `RADARR_PREFERRED_LANGUAGES` — default `ITA,ENG`

Legacy (kept as no-ops, do not wire):

- `OS_USERNAME`, `OS_PASSWORD` — old XML-RPC, dead path

---

## 10. Telegram UX rules

- **Bot speaks Italian.** All user-facing strings in Italian. Internal logs in English.
- **Canonical Italian command + English alias(es).** `/scarica` ↔ `/download`, `/req`. `/sincronizza` ↔ `/sync`. The alias registry is the single source of truth — do not hardcode names in handlers.
- **Typo on a slash command → "did you mean?" reply.** Never silently fall through to free-text search.
- **Edit-in-place for progress.** One message per video / per series, updated in place. Do NOT spam a new message for each state change. The progress bar pattern (`[▓▓▓░░░░░░░] 40%`) is established — match it.
- **The cost gate is sacred.** Phase 1 (search + EN sync) auto-runs. Phase 2 (EN→IT translate via Claude) requires an explicit button press AND the estimate must be shown in the prompt. Never charge without showing the number.
- **`/scarica` is two-step.** Step 1: TMDb disambiguation (top 5 inline buttons). Step 2: Radarr Interactive Search release list (paged 8 per page, sorted by preferred language → quality tier → seeders). No auto-grab — the user picks the release. Always.

---

## 11. Where things live (file map)

```
sub_fetcher.py          # Single-file app. ~177 KB. Read it whole before editing.
test_sub_fetcher.py     # Single-file test suite. ~92 KB. Stdlib only.
Dockerfile              # python:3.11-slim + ffmpeg + ffsubsync. Multi-stage purge of gcc after build.
SPEC.md                 # Full technical spec. Authoritative on flow + provider contracts.
README.md               # User-facing setup + commands.
AGENTS.md               # Short guidelines (legacy, superseded by this file).
CLAUDE.md               # THIS FILE. Read first.
.githooks/pre-commit    # Runs full test suite. Activate via `git config core.hooksPath .githooks`.
.gitignore              # Excludes state/batches/requests JSON + logs + python caches.
exclude_folders.txt     # Folders to skip (Italian audio / Italian originals).
```

Runtime files (on NAS, not in repo):

```
/config/state.json        # Persistent state. Owned by main thread.
/config/batches.json      # Pending batches. Either thread.
/config/requests.json     # Pending Radarr requests. Either thread.
/config/sub_fetcher.log   # Rotating log.
/media/series/            # Read-only mount.
/media/films/             # Read-only mount.
```

---

## 12. When in doubt

- Read `SPEC.md` first.
- Re-read the relevant function end-to-end before editing.
- Write the test first.
- Ask the user before adding a third-party library, changing the public Telegram command surface, or breaking a state-file invariant.
