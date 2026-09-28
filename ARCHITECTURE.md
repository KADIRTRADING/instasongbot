# Architecture & Implementation Plan

This document is the design contract for the bot before any code is read. It explains
the stack choice, the shape of every component, how each user-facing feature flows
through the system, the database schema, deployment topology, and — importantly —
the honest limitations of third-party integrations that are outside our control.

## 1. Stack choice and why

| Concern | Choice | Why |
|---|---|---|
| Language/runtime | Python 3.12 | Best library support for `yt-dlp`, `ffmpeg` wrappers, and async Telegram frameworks. 3.12 for perf + modern typing. |
| Bot framework | **aiogram 3.x** | Fully async, first-class FSM, middlewares, both polling and webhook (aiohttp) built in, actively maintained. Requested by the user. |
| Database | **PostgreSQL 16** + SQLAlchemy 2.0 (async) + Alembic | Requested by the user; real concurrent writes (jobs, stats) need row-level locking and JSONB, which SQLite/JSON-file stores don't give safely. Alembic gives reviewable, versioned migrations. |
| Cache / queue transport | **Redis 7** | Backs both the rate limiter (sorted-set sliding window) and the job queue transport. One moving part instead of two. |
| Background jobs | **arq** | Native asyncio (matches aiogram's loop model), backed by Redis, minimal (~1 dependency), has retries, delayed jobs, and health checks out of the box. Chosen over Celery because Celery's multiprocessing/prefork model fights asyncio and adds an entire second paradigm (kombu, prefork pools) for no benefit here — every I/O-bound job we run (HTTP download, ffmpeg subprocess, HTTP recognition call) is naturally async or subprocess-based. |
| Media download | **yt-dlp** (YouTube/TikTok/Instagram/Facebook/X) + a **dedicated Pinterest client** | yt-dlp has no first-class, reliable Pinterest video+carousel extraction path for our needs and Pinterest has no download API, so we talk to Pinterest's own public unauthenticated JSON resource endpoint directly (verified working, see §7). |
| Audio/video processing | **ffmpeg** (subprocess via `asyncio.create_subprocess_exec`) | Industry standard, scriptable, no Python binding lock-in. |
| Object storage | **S3-compatible (boto3)**, local disk fallback for dev | Needed for "secure temporary download link" requirement via presigned URLs; AWS-native, matches the deployment target. |
| Music recognition | **AudD** (default) and **ACRCloud** (alternate), behind one interface | Provider is a config flag (`RECOGNITION_PROVIDER`), not a code fork. See §6 for which one is live-tested. |
| Config | `pydantic-settings` reading `.env` | Typed, fails fast on missing/invalid config. |
| Logging | `structlog` → JSON lines | Grep/parse-friendly on a server, works with `docker logs` / CloudWatch agent without extra plumbing. |
| Packaging/deploy | Docker + Docker Compose | Requested; also the most portable path onto a bare AWS EC2/Lightsail box. |

Explicitly **not** chosen: Celery (heavier, process model mismatch with asyncio),
python-telegram-bot (aiogram was requested and has a cleaner webhook story),
SQLite for production (fine for tests, not for concurrent job/stat writes),
a headless-browser scraper for Instagram/Pinterest (ToS risk, fragile, slow — see §7/§8
for what we do instead).

## 2. Component diagram

```
                                   ┌─────────────────────────┐
                                   │        Telegram          │
                                   └────────────┬─────────────┘
                                     polling or │ webhook (aiohttp)
                                                ▼
                       ┌────────────────────────────────────────────┐
                       │                Bot process                  │
                       │  aiogram Dispatcher                         │
                       │  ├─ middlewares: i18n, db-session,          │
                       │  │   throttling(rate-limiter), logging,     │
                       │  │   error handler                          │
                       │  ├─ handlers: start/menu/recognize/         │
                       │  │   download/convert/language/help/admin   │
                       │  └─ enqueues jobs, never blocks on I/O      │
                       └───────────────┬───────────────┬────────────┘
                                       │ enqueue        │ read/write
                                       ▼                ▼
                              ┌────────────────┐  ┌─────────────┐
                              │  Redis (arq)   │  │ PostgreSQL  │
                              │  queue + rate  │  │ users/jobs/ │
                              │  limiter keys  │  │ settings    │
                              └───────┬────────┘  └──────▲──────┘
                                      │ pop job                 │ status updates
                                      ▼                          │
                       ┌───────────────────────────────────────┴────┐
                       │              Worker process(es)              │
                       │  arq Worker running the same codebase        │
                       │  tasks: recognize_job / download_job /       │
                       │         convert_job / broadcast_job          │
                       │  uses: recognition providers, downloader     │
                       │  (yt-dlp + Pinterest client), ffmpeg,        │
                       │  storage (local/S3), caption renderer        │
                       │  sends results back via its own Bot instance │
                       └───────┬───────────────┬───────────┬─────────┘
                               ▼               ▼           ▼
                       AudD/ACRCloud     yt-dlp targets   S3 bucket
                       (music ID)        + Pinterest      (large files,
                                         public JSON API   presigned URLs)
```

Bot and worker are **the same Docker image**, started with different commands. This
keeps deployment simple (one image to build/push) while still letting them scale
independently (`docker compose up --scale worker=3`).

## 3. Why the bot never blocks

Every handler that would otherwise do slow I/O (HTTP download, ffmpeg, recognition
API call) instead:
1. Validates input synchronously (fast: regex/URL checks, file size from Telegram
   metadata, rate-limit check — all sub-millisecond Redis/regex ops).
2. Writes a `jobs` row (`status=pending`) and `enqueue_job(...)` into arq.
3. Immediately replies with a localized "⏳ processing" message and returns.
4. The worker picks up the job on its own event loop, does the slow work, and calls
   `bot.send_*` itself (editing the progress message) when done.

This is what makes "handle multiple users concurrently without blocking the bot" true
structurally, not just by accident of asyncio scheduling — the bot's event loop is
never occupied by ffmpeg or a multi-second download.

## 4. Data flow per feature

### 4.1 Find Music
`voice/audio/video/document message` → handler checks size ≤ `MAX_TELEGRAM_FETCH_MB`
(Bot API download ceiling, see §9) → downloads to a per-job temp dir → enqueues
`recognize_job(file_path, source="upload")` → worker: `ffmpeg` trims to the first
`RECOGNITION_CLIP_SECONDS` and transcodes to mp3 (small, fast upload to the provider)
→ calls the configured `MusicRecognitionProvider` → on match, formats title/artist/
album/cover art/links (Spotify/Apple Music/Deezer when the provider returns them) using
the **audio** caption template; on no-match or low confidence, sends a clear
"couldn't confidently identify this" message (never a fabricated guess) → deletes the
temp file → updates the `jobs` row.

### 4.2 Download Media
Any message containing a supported-platform URL (menu button optional, links work
anywhere) → `url_utils.detect_platform()` classifies + validates (SSRF-safe: rejects
non-http(s), private/loopback/link-local resolved IPs, unknown hosts) → enqueues a
`probe_job` → worker calls `yt-dlp` (or the Pinterest client) in "extract info only,
no download" mode → bot edits the progress message into an inline keyboard of real
available options (resolutions / audio-only / each image in a carousel) built from the
actual probe result, never a hard-coded list → user taps one → callback enqueues
`download_job(format_id | image_index)` → worker streams the file to disk with a hard
byte-cap (`MAX_DOWNLOAD_MB`), then:
- size ≤ `TELEGRAM_DIRECT_UPLOAD_MB` → send directly (video/audio/photo, with the
  admin's caption template + buttons for that media type);
- size > that limit → upload to S3, generate a presigned URL valid for
  `PRESIGNED_URL_TTL_SECONDS`, send that link instead (clearly labeled "secure,
  expires in …");
- always deletes the local temp file in a `finally` block, regardless of outcome.

Failure paths (private post, deleted content, unsupported link, provider rate-limited,
download failed) map to distinct, translated error messages — see `services/downloader/errors.py`.

### 4.3 Convert Video → Audio
Same probe step as §4.2. Once we know the source is a video (uploaded file or a link
that resolves to video), the bot presents three explicit choices as inline buttons:
**🎯 Identify song**, **🎧 Extract audio (MP3)**, **⬇️ Original video** — exactly the
three actions the spec asks for, on the same probed source so we never download twice.
"Extract audio" reuses `download_job` to fetch (or reuses the already-downloaded file
for an upload) then pipes it through the same ffmpeg extraction used in §4.1, and can
chain straight into "Identify song" on the extracted clip.

### 4.4 Captions & buttons
Every outgoing video/audio/photo goes through `services/captions/renderer.py`, which:
loads the admin-edited template for that media type from `caption_templates`
(falling back to a sane default if the admin hasn't customized it), substitutes
`{title} {artist} {source} {bot_username}` (missing variables render as empty string,
never a raw `{placeholder}` or a `KeyError`), and attaches an inline keyboard built
from `caption_buttons` rows (label + URL, admin-managed, ordered).

### 4.5 Admin
Gated by a middleware that checks `message.from_user.id in settings.ADMIN_IDS`
(env-sourced, hot-reloadable only by restart — see §11 on why this is intentional).
Admin actions (edit caption template, add/remove button, toggle a platform, edit file
size limits, view stats, broadcast) are plain FSM-driven handlers that write to the
`caption_templates` / `caption_buttons` / `platform_settings` / `bot_settings` tables;
the rest of the bot reads those tables (with a short in-process cache) so changes are
live without a redeploy. Broadcast is itself an arq job (`broadcast_job`) so sending to
thousands of users doesn't block anything and is resumable/observable via the
`broadcasts` table.

## 5. Database schema (see `migrations/versions/0001_initial.py` for the real DDL)

- **users** — `id` (Telegram user id, PK), `username`, `first_name`, `language_code`
  (`uz`/`ru`/`en`), `is_banned`, `created_at`, `last_seen_at`.
- **jobs** — `id` (UUID PK), `user_id` (FK), `job_type` (`recognize`/`download`/
  `convert`/`broadcast`), `status` (`pending`/`processing`/`completed`/`failed`),
  `platform`, `source_url`, `error_message`, `result_meta` (JSONB), timestamps.
  Indexed on `(user_id)`, `(job_type, status)`, `(created_at)` for stats queries.
- **caption_templates** — `media_type` (`video`/`audio`/`image`, unique), `template`,
  `updated_by`, `updated_at`.
- **caption_buttons** — `id`, `media_type` (nullable = applies to all), `label`,
  `url`, `position`, `is_active`.
- **platform_settings** — `platform` (PK), `enabled`, `max_file_size_mb` (nullable
  per-platform override), `updated_at`.
- **bot_settings** — generic KV (`key` PK, `value` JSONB, `updated_at`, `updated_by`)
  for runtime-tunable knobs (global file-size limit, rate-limit numbers, maintenance
  mode) so admins don't need a redeploy for every change.
- **broadcasts** — `id`, `admin_id`, `message_text`, `status`, `total_users`,
  `sent_count`, `failed_count`, `created_at`, `completed_at` — lets an admin watch a
  broadcast job's progress instead of it being a fire-and-forget black box.

Usage statistics are computed with aggregate queries over `jobs`/`users` rather than a
separate denormalized stats table — fewer moving parts, no double-writes to keep in
sync.

## 6. Music recognition: what's tested vs. what's implemented

- **AudD** (`RECOGNITION_PROVIDER=audd`, default): implemented **and live-tested**
  against the real API during development (public `test` token, real HTTP call,
  real structured response with artist/title/album/cover/Spotify/Apple Music data).
  Simple auth (one `api_token` form field), generous free tier, good fit as the
  default.
- **ACRCloud** (`RECOGNITION_PROVIDER=acrcloud`): implemented against ACRCloud's
  published HTTP protocol (HMAC-SHA1 request signing, `multipart/form-data` to
  `/v1/identify`) with unit tests covering signature generation and response parsing
  against recorded fixtures. It is **not** live-tested end-to-end in this project
  because that requires a paid/registered ACRCloud project's own `access_key`/
  `access_secret`/`host`, which we don't have. Treat it as implemented-per-spec, and
  verify it yourself against your own ACRCloud console before relying on it in
  production. Both providers share one interface (`MusicRecognitionProvider`), so
  switching is a config change, not a code change.

## 7. Pinterest: how we actually support it (video + images + multi-image posts)

Pinterest has no public download API and yt-dlp's own Pinterest support calls the same
internal endpoint we do. We talk directly to Pinterest's **public, unauthenticated**
JSON resource endpoint (`https://www.pinterest.com/resource/PinResource/get/`) — no
login, no API key. This was verified live during development against a real pin.
From the returned JSON we handle:
- **Single image pin** → `images.orig.url`.
- **Single video pin** → `videos.video_list`, preferring a direct progressive MP4
  (`V_720P`) and falling back to remuxing the HLS (`V_HLSV4`, `.m3u8`) with ffmpeg
  when no progressive format exists.
- **Carousel (multiple images in one post)** → `carousel_data.carousel_slots`, one
  entry per image, each offered to the user individually or as "download all."
- **`pin.it/...` short links** → resolved via HTTP redirect following before parsing.
- **Story/Idea pins** (Pinterest's multi-block story format) → best-effort: images and
  video blocks are extracted; this is documented as best-effort, not guaranteed, since
  the block schema is the least stable part of Pinterest's data model.
We only ever fetch what a logged-out browser could already see; private boards/pins
are not and cannot be accessed by this bot.

## 8. Platform support matrix (honesty section)

| Platform | Mechanism | Status |
|---|---|---|
| TikTok | yt-dlp | **Tested live** — real video downloaded and probed during development. |
| Facebook (public videos) | yt-dlp | **Tested live** on a public video. |
| YouTube | yt-dlp + bundled JS runtime (Deno) | **Tested live**; YouTube periodically requires a JS runtime for full format access, which the Docker image bundles. Age-restricted/private/members-only videos are out of scope (no login is performed). |
| Pinterest | dedicated client, §7 | **Tested live** for image and video pins. |
| Instagram | yt-dlp, optional cookies | **Implemented, currently degraded.** As of this writing, Instagram's anonymous (logged-out) access to its post API returns empty responses for yt-dlp (a live, currently-open upstream issue: `yt-dlp/yt-dlp#17275` — Instagram rolled out a new GraphQL API). We surface a clear "Instagram link couldn't be fetched right now" error rather than pretending it works. Operators can optionally supply `INSTAGRAM_COOKIES_FILE` (their own logged-in session, exported by the operator) to restore access at their own risk/ToS responsibility; we do not bundle or harvest credentials. |
| X / Twitter | yt-dlp | **Implemented, best-effort.** Twitter/X's video API is presently inconsistent for yt-dlp (multiple currently-open upstream issues, e.g. `yt-dlp/yt-dlp#17563`, `#17058`) — some tweets extract fine, others return "no video found" even though a video is visible in-browser. We surface the real error instead of silently failing. |

We will not bypass logins, paywalls, or DRM to "fix" the two degraded rows above —
that would violate the platforms' terms and the explicit constraint in the request.

## 9. Telegram file-size limits and how we handle them

- Bot API file **upload** ceiling is 50 MB via normal `sendDocument`/`sendVideo`/
  `sendAudio`. We use `TELEGRAM_DIRECT_UPLOAD_MB` (default 50) as the cutoff: at or
  under it we upload directly; above it we upload to S3 and send a presigned link.
- Bot API file **download** (fetching a user's uploaded file back, via `getFile`) is
  capped at 20 MB unless you run your own [Local Bot API
  Server](https://github.com/tdlib/telegram-bot-api) (supports up to 2000 MB). We
  default to the standard Bot API (`MAX_TELEGRAM_FETCH_MB=20`) and document the local
  Bot API server as an optional docker-compose profile for operators who need larger
  uploads accepted from users (see README "Large files" section). This is a genuine
  Telegram platform constraint, not a bug in this project.

## 10. Rate limiting & abuse prevention

Redis sorted-set sliding window per `(user_id, action)` (`recognize`/`download`/
`convert`), limits configurable in `bot_settings` (hot) with env fallbacks (cold
default). A separate stricter global per-user "burst" limit prevents one user from
saturating the worker pool. Banned users (`users.is_banned`) are rejected at the
middleware layer before any handler logic runs.

## 11. Deployment topology

Two supported modes, same codebase/image:
- **Long polling** (`python -m app.main`) — no public URL, no TLS, no inbound ports.
  This is the easiest path on a bare AWS EC2 instance: the box only needs outbound
  HTTPS. Recommended default for "just get it running on a server."
- **Webhook** (`python -m app.webhook_app`) — aiohttp server behind a reverse proxy
  (Caddy config included) with `X-Telegram-Bot-Api-Secret-Token` validation. Better
  for horizontal scaling of the bot process itself (the worker already scales
  independently either way).

`docker-compose.yml` runs Postgres, Redis, bot (polling by default), and worker.
`docker-compose.prod.yml` layers on restart policies, log rotation, resource limits,
and an optional Caddy reverse-proxy service for webhook mode with automatic TLS.

Admin allow-listing via `ADMIN_IDS` is env-based and requires a restart to change by
design: it is a security boundary, and boundaries that can be silently rewritten at
runtime by a compromised admin session are a weaker boundary.

## 12. Temp files & data retention

Every job gets its own `tempfile.mkdtemp()` under `WORKDIR/tmp/<job_id>/`, always
removed in a `finally` block (both success and failure paths), plus a periodic
`cleanup` arq cron job that sweeps any directory older than `TEMP_FILE_MAX_AGE_MINUTES`
as a backstop against process crashes leaking disk space. We do not persist user
media beyond the lifetime of the job; only metadata needed for stats/abuse-prevention
(`jobs` row: platform, status, timestamps, no raw file content) is kept, and it is
pruned by a configurable retention job (`JOBS_RETENTION_DAYS`).

## 13. Testing strategy

Unit tests (no network) cover: URL/platform detection + SSRF guards, caption template
rendering, the sliding-window rate limiter (via `fakeredis`), the ACRCloud signature
algorithm, both providers' response parsing against recorded JSON fixtures, the
Pinterest JSON parser (single image/video/carousel fixtures captured from real
responses), and the repository layer against an in-memory SQLite/aiosqlite engine.
These are the "core flows" the spec asks for; they run in CI without any external
credentials. A couple of opt-in, marker-gated tests hit the real AudD `test` token to
prove the end-to-end path still works, but are skipped by default so test runs never
depend on network access or burn API quota.
