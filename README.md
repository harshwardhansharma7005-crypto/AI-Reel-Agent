# AI Reel Agent

Automatically builds (and, once you enable it, publishes) **two Instagram Reels a day – 10:00 AM and 8:00 PM IST** – from your own technology videos and your own phonk music.

**Safe by default:** nothing is posted until you deliberately switch publishing on. Anything the agent cannot verify is parked for your review instead of published.

> **Honest status**
> * Rendering, scanning, duplicate detection, music rotation, headline/caption rules, fact-check policy, validation, and the full publishing *flow* are implemented and covered by automated tests (see [Testing](#testing)).
> * Instagram publishing was tested **only against a simulated Instagram API** (no real account/token was available). Run your first real post manually and watch it.
> * Real AI vision analysis (OpenAI / Anthropic / Gemini) was **not run against the live APIs** (no API keys were available during development). The provider code, request formats, retries, error handling, caching, review rules and headline checks are covered by tests using **fake providers and mocked HTTP**. Your first real run with a key is the real test: start with `analyze` (section 4) and read what the AI says about your videos.
> * The offline `heuristic` mode does **not** understand videos. It is a test mode only: headlines come from the file name, or the video goes to review. It is clearly marked `HEURISTIC MODE` in logs, `metadata.json` and the run result.
> * Developed and tested on Linux. The Windows scripts are included but were not executed on Windows.
> * The design screenshots you mentioned were not visible to me, so the look is built from your written description (black 9:16 canvas, bold white headline with `#FF5733` highlights, centered landscape video, glow border, username). Tweak `reel_agent/templates.py` / `settings.yaml` to match your references.

---
## 1. Windows quick start (VS Code)

1. Install **Python 3.10+** (python.org, tick *Add to PATH*) and **VS Code**. Install **FFmpeg**:
   ```powershell
   winget install Gyan.FFmpeg
   ```
   Close and reopen VS Code afterwards.
2. Unzip the project, open the folder in VS Code (*File → Open Folder*), open a terminal (*Terminal → New Terminal*, PowerShell).
3. Install everything:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
   ```
   (or double-click `setup.bat`). Manual equivalent:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   copy .env.example .env
   ```
4. Make a **sample Reel** (synthetic test media, never posts):
   ```powershell
   .\.venv\Scripts\python.exe -m reel_agent demo
   ```
   Result: `media_demo\output\<date>-manual-<time>\reel.mp4` + `metadata.json`.
5. Run the **tests**:
   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
   ```
6. In VS Code pick the `.venv` interpreter (*Ctrl+Shift+P → Python: Select Interpreter*). *Run and Debug* has ready-made launch entries.

## 2. Use your own media

```
media/videos/   <- your 20-30+ technology videos (.mp4 .mov .mkv .webm ...)
media/music/    <- your phonk tracks (.mp3 .wav .m4a ...)
```
(Or point `paths.video_dir` / `paths.music_dir` in `settings.yaml` anywhere, e.g. `D:/Videos/Tech`.)

```powershell
.\.venv\Scripts\python.exe -m reel_agent check      # settings, FFmpeg, folders, publishing readiness
.\.venv\Scripts\python.exe -m reel_agent scan       # find videos, detect duplicates
.\.venv\Scripts\python.exe -m reel_agent analyze    # topic + OK/REVIEW decision for every video
.\.venv\Scripts\python.exe -m reel_agent run        # ONE reel, dry-run -> output\<slot>\reel.mp4
.\.venv\Scripts\python.exe -m reel_agent review list          # videos held back, with reasons
.\.venv\Scripts\python.exe -m reel_agent review approve <hash> # you checked it yourself: allow it
.\.venv\Scripts\python.exe -m reel_agent history --csv history\publication_history.csv
```
Set `reel.username` in `settings.yaml` (or `REEL_USERNAME` in `.env`) to your handle.

## 3. How it decides what to post

1. **Scan** – hashes files; exact copies and re-encoded near-duplicates (perceptual hash + colour signature) are ignored.
2. **Understand** – 6 frames + optional OCR (Tesseract) + optional transcript (faster-whisper or OpenAI Whisper) go to the configured AI provider, which returns topic, entity, claim type (`announcement / tutorial / demo / review / opinion`), claims, confidence.
3. **Verify** – *announcements* must match an **official feed** (OpenAI, Google, Android, Unity – `factcheck.feeds`). Tutorials/demos make no news claim, so they need no feed. If confidence < `review.min_confidence`, the claim type is unknown, a feed cannot be read, or nothing matches → status **review**, never published. Feed URLs are my best knowledge: **check them** and edit as needed.
4. **Headline** – written from the AI analysis (never from canned templates), then validated: word/char limits, banned + generic phrases, grounding in the video evidence, no emoji, no all-caps shouting, **numbers must appear in the video evidence**, not similar to recent hooks. Highlight words get `#FF5733`.
5. **Music** – never the previous track, avoids the last N, prefers least-used; random start offset; fades; optional loudness normalisation (−14 LUFS); optional low-volume original audio.
6. **Render** – Pillow draws the 1080×1920 layout, FFmpeg composites video (aspect preserved, letterboxed in a 16:9 frame) and mixes audio, H.264 High / AAC / yuv420p / +faststart.
7. **Validate** – codecs, size, pixel format, duration, not silent, safe margins, headline visible, video area not blank. Failed reels are never marked ready.
8. **Publish** – only if every gate passes (below). Every attempt is recorded in `data/history.db` and `history/publication_history.csv`.

**Without an AI key** the `heuristic` provider uses file names/OCR/transcript keywords. It works for testing but is *not* real video understanding – use a vision provider for production.

## 4. Real video understanding (vision AI) - setup

**How it works.** For every new video the agent picks up to `analysis.frame_count` (default 6) *visually different* frames (it samples ~3x as many, drops black/blank frames, keeps the most distinct), adds OCR text (optional) and the audio transcript (optional), and asks the vision model for: topic, entity, specific subject, claim type, factual summary, claims, keywords, confidence. The headline is then written **only from that evidence**, and rejected if it:
* is generic ("Technology in action", "A closer look…", "worth knowing" - see `GENERIC_PHRASES` in `headline.py` and `headline.generic_phrases`),
* contains a product name/number the evidence does not contain (invented GPT-6, "10x faster"…),
* uses fewer than half (`headline.min_grounded_ratio`) of its key words from the evidence,
* breaks the length/emoji/punctuation rules or is too similar to a recent hook.

If the model is unsure (`confidence < review.min_confidence`, unknown claim type, no concrete summary) the video goes to **review** and *no headline is generated*. Announcements still need an official-feed match. If the AI call itself fails (network, quota, bad key) nothing is saved or marked: that video is simply retried on the next run.

**Step 1 - get ONE key** (you pay the provider per request; ~6 images + text per new video, once):
* OpenAI: platform.openai.com → API keys → `OPENAI_API_KEY`
* Anthropic (Claude): console.anthropic.com → API keys → `ANTHROPIC_API_KEY`
* Google Gemini: aistudio.google.com → API key → `GEMINI_API_KEY`

**Step 2 - put it in `.env`** (never in `settings.yaml` or code; `.env` is git-ignored):
```powershell
copy .env.example .env
notepad .env        # e.g. ANTHROPIC_API_KEY=sk-ant-...   (no quotes, no spaces)
```
**Step 3 - choose the provider** in `settings.yaml`:
```yaml
ai:
  provider: anthropic        # openai | anthropic | gemini   (heuristic = offline test mode)
  models:
    anthropic: claude-sonnet-5-5   # check your provider's CURRENT vision-capable model names
```
**Step 4 - try it on your library, then inspect:**
```powershell
.\.venv\Scripts\python.exe -m reel_agent check            # shows provider, model and whether the key is set
.\.venv\Scripts\python.exe -m reel_agent analyze          # per video: topic, subject, confidence, OK/REVIEW
.\.venv\Scripts\python.exe -m reel_agent run              # one dry-run Reel with an AI headline
```
Open `output\<slot>\metadata.json`: `analysis_mode` must be `ai:<provider>` and `headline_source` `ai`. `heuristic` / `filename` means the AI was not used.

**Cache.** Each successful analysis is stored in `data\analysis_cache\` keyed by the video's content hash + provider + model + prompt version + frame/transcribe settings. Reruns make **no** analysis API calls; changing the model or provider re-analyses. Force a refresh with `analyze --refresh`, or disable with `analysis.cache: false`. Failed analyses are never cached.

**Transcripts.** `analysis.transcribe: auto` uses local `faster-whisper` if installed (`pip install faster-whisper`), otherwise OpenAI Whisper when `ai.provider: openai`; with Anthropic/Gemini and no faster-whisper, only frames + OCR are used. OCR: install Tesseract + `pip install pytesseract`.

**Heuristic (offline) mode.** `ai.provider: heuristic`: no understanding. The headline is the cleaned file name (`unity_game_engine_workflow.mp4` → "Unity game engine workflow") if it has ≥3 meaningful words, otherwise the video goes to review. Switch off the file-name headline with `headline.filename_fallback: false` (then everything without an AI headline goes to review).

## 5. Instagram publishing (official Graph API)

Requirements (Meta's rules – verify in their docs, they change): an **Instagram Professional (Business/Creator) account**, a Meta developer app, and an access token with the content-publishing permission (`instagram_content_publish` for Facebook-Login flows, with the account linked to a Facebook Page as Meta requires; or the *Instagram API with Instagram Login* variant – then set `publishing.api_host: graph.instagram.com`). Put `IG_USER_ID` and `IG_ACCESS_TOKEN` in `.env`. Long-lived tokens **expire (~60 days) and must be refreshed** – set a reminder.

Publishing stays OFF until **all** are true:
* `publishing.enabled: true` in `settings.yaml`
* `publishing.dry_run: false` **and** you run with `--publish`
* `IG_USER_ID` + `IG_ACCESS_TOKEN` set
* `music.i_have_rights_to_publish_all_tracks: true` (your confirmation that you may publish every track)

`upload_mode: url` (default) needs the video at a public HTTPS URL → configure cloud storage (section 6). `upload_mode: resumable` uploads the file bytes to Meta directly (no storage needed for upload).

Safety built in: status polling until `FINISHED`; retries with backoff for transient errors; the final `media_publish` call is **never auto-retried** – if its outcome is unclear the post is marked `unknown` and the slot is blocked so you can check Instagram manually (prevents double posts); one post per slot; same video not republished within `dedupe.video_cooldown_days`; quota check; tokens are redacted from logs.

First real test: `.\.venv\Scripts\python.exe -m reel_agent run --slot manual --publish` and look at your profile.

## 6. GitHub Actions automation

GitHub runners are **temporary cloud machines – they cannot see your PC's folders.** So videos, music and the history database live in S3-compatible storage (Cloudflare R2, Backblaze B2, AWS S3...). Upload with the provider's web UI/CLI to:
```
videos/...   music/...          (prefixes configurable in settings.yaml -> storage.s3)
```
In `settings.yaml` set `storage.provider: s3`. The agent downloads media, restores `state/history.db` before each run and saves it afterwards.

**Push to GitHub** (replace the URL):
```powershell
git init
git add .
git status          # make sure .env and media are NOT listed
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR-USER/ai-reel-agent.git
git push -u origin main
```
**Repo → Settings → Secrets and variables → Actions**
* Secrets: `IG_USER_ID`, `IG_ACCESS_TOKEN`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION`, `S3_ENDPOINT_URL` (R2/B2 only), and the key for your AI provider (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY`).
* Variables: `REEL_USERNAME` (e.g. `@har.*shhh`), and `PUBLISH_ENABLED` = `true` **only when you want scheduled runs to really post** (default: scheduled runs are dry-runs).
* Commit `settings.yaml` with your choices (provider, `storage.provider: s3`, `publishing.enabled: true`, `publishing.dry_run: false`, rights confirmation).

Schedule: `.github/workflows/reels.yml` – cron `30 4 * * *` (10:00 IST) and `30 14 * * *` (20:00 IST), because IST = UTC+5:30. Manual run: *Actions → Publish Reels → Run workflow* (choose publish true/false). Each run uploads logs + output as an artifact, writes a job summary, commits `history/publication_history.csv`, and opens an issue if it fails.

**Be realistic about GitHub:** scheduled runs may start late or be skipped under load (the agent maps a late run to the nearest slot within ±3 h); schedules in public repos pause after 60 days without repo activity; free-plan minutes are limited (check GitHub's current pricing/limits – each run needs a few minutes of FFmpeg time); don't assume it is unlimited. Cloud storage and AI APIs may also cost money.

## 7. Settings cheat-sheet (`settings.yaml`)
`reel.templates` (which layouts rotate) · `reel.max_duration` · `reel.frame_mode` · `audio.music_volume` / `audio.original_audio_volume` · `headline.*` limits and banned phrases · `review.*` strictness · `dedupe.*` cooldowns · `schedule.slots`. Put your own `.ttf` fonts (e.g. Montserrat ExtraBold, Anton) in `assets/fonts/` – templates pick them up automatically; otherwise Windows/Linux system bold fonts are used.

## Testing
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```
Covers: AI analysis with fake providers (frame selection, topic-specific headlines, generic/invented-claim rejection, API errors 401/429/bad JSON, caching, low-confidence review, heuristic marking), config validation & defaults, workflow cron ↔ IST maths, slot resolution, scanning (exact + re-encoded duplicates, short/corrupt files), music selection (no consecutive repeats, rotation), headline/caption validation, fact-check policy (verified / unverified / feed outage / stale entries), rendering of every template (margins, centering, aspect ratio), real FFmpeg renders + validation (incl. silent-audio detection), publisher flow with a simulated Instagram API (success, retries, errors, duplicate prevention, ambiguous-publish handling, token redaction, dry-run makes zero network calls), S3 storage with a fake client, and end-to-end pipeline runs.

## Troubleshooting
* *"ffmpeg not found"* → `winget install Gyan.FFmpeg`, reopen terminal; or set `FFMPEG_PATH` in `.env`.
* *PowerShell blocks scripts* → use the `powershell -ExecutionPolicy Bypass -File ...` form or the `.bat` files.
* *Everything lands in "review"* → expected with `heuristic` on unclear file names; use a vision provider, or `review approve <hash>` after checking yourself.
* *OCR not available* → optional: install Tesseract for Windows and `pip install pytesseract`, set `analysis.tesseract_cmd`.
* Logs: `logs/reel_agent.log`.
