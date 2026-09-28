# InstaSongBot

A Telegram bot that:

- 🎵 **Identifies songs** from a voice message, audio file, or video clip.
- ⬇️ **Downloads media** from Pinterest, Instagram, TikTok, YouTube, Facebook, and X — including Pinterest multi-image carousels, one image (or all) at a time.
- 🎧 **Converts video to MP3** — from an uploaded file or a supported link.
- 🛠 Ships a Telegram-native **admin panel**: editable caption templates, per-media-type buttons, per-platform enable/disable toggles, live usage stats, configurable file-size limits, and broadcast announcements.
- 🌐 Speaks **Uzbek, Russian, and English**, switchable per-user.

Built with Python 3.12, [aiogram 3](https://docs.aiogram.dev/), PostgreSQL, Redis, [arq](https://arq-docs.helpmanual.io/) for background jobs, and `ffmpeg`/`yt-dlp` for media handling. See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the full design rationale, database schema, and — importantly — an honest per-platform support matrix (§8): which platforms are live-tested versus best-effort, and why.

## Contents

- [Quick start (Docker)](#quick-start-docker)
- [Getting your credentials](#getting-your-credentials)
- [Configuration reference](#configuration-reference)
- [Running without Docker](#running-without-docker)
- [Deploying to AWS](#deploying-to-aws)
- [Webhook mode vs. long polling](#webhook-mode-vs-long-polling)
- [Large files](#large-files)
- [Admin panel](#admin-panel)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## Quick start (Docker)

Requires Docker Engine with the Compose plugin (`docker compose version` should print something). See [Deploying to AWS](#deploying-to-aws) if you're starting from a bare server with neither installed.

```bash
git clone https://github.com/KADIRTRADING/instasongbot.git
cd instasongbot
cp .env.example .env
```

Edit `.env` and set at minimum:

- `BOT_TOKEN` — from [@BotFather](#1-bot_token--botfather) (see below).
- `ADMIN_IDS` — your own numeric Telegram user ID, so you can reach the admin panel.

Everything else in `.env.example` has a working default for local/single-box use (SQLite is *not* used — Postgres and Redis come from `docker-compose.yml` automatically; do not set `DATABASE_URL`/`REDIS_URL` yourself when running via Compose).

```bash
docker compose up --build
```

This starts Postgres, Redis, the bot (long polling), and one background worker. The bot runs `alembic upgrade head` automatically on every start, so the database schema is created for you — no separate migration step needed. Message your bot on Telegram; `/start` should respond within a couple of seconds.

To run in the background: `docker compose up --build -d`, then `docker compose logs -f` to follow logs, `docker compose down` to stop.

## Getting your credentials

### 1. `BOT_TOKEN` — BotFather

1. Open Telegram, message [**@BotFather**](https://t.me/BotFather).
2. Send `/newbot`, follow the prompts (choose a display name, then a unique `@username` ending in `bot`).
3. BotFather replies with a token like `123456789:AAH...`. Copy it into `.env` as `BOT_TOKEN`.
4. (Optional, recommended) Send `/setprivacy` → select your bot → **Disable**, so it can react to messages in groups too, if you plan to add it to any.

### 2. `ADMIN_IDS` — your numeric Telegram user ID

Message [**@userinfobot**](https://t.me/userinfobot) (or any similar "what's my ID" bot) — it replies with your numeric ID. Put it in `.env`:

```
ADMIN_IDS=111111111
```

Comma-separate multiple admins: `ADMIN_IDS=111111111,222222222`. Without this set, `/admin` and the "🛠 Admin Panel" menu button do nothing for anyone — set it before you need it.

### 3. `AUDD_API_TOKEN` — music recognition

The bot defaults to [AudD](https://audd.io/) for song identification (see [ARCHITECTURE.md §6](ARCHITECTURE.md#6-music-recognition-whats-tested-vs-whats-implemented) for why, and for the ACRCloud alternative). Sign up at audd.io for a free API token, or use the literal string `test` (AudD's own shared public test token) to try things out — **do not** rely on `test` in a real deployment; it's rate-limited and shared with everyone else trying the same thing.

### 4. Nothing else is required to start

Pinterest, TikTok, YouTube, Facebook, and X downloads work with no additional API keys or accounts — see [ARCHITECTURE.md §7/§8](ARCHITECTURE.md#7-pinterest-how-we-actually-support-it-video--images--multi-image-posts) for how. Instagram currently needs an optional cookies file to work reliably (see below); this is a known, honestly-documented platform limitation, not a bug.

## Configuration reference

Every environment variable is documented inline in **[`.env.example`](.env.example)** — copy it to `.env` and read the comments, they're the authoritative reference (one comment block per variable, organized into the same sections as `app/config.py`). A few worth calling out up front:

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | **Required.** From BotFather. |
| `ADMIN_IDS` | **Required for admin access.** Comma-separated numeric Telegram user IDs. |
| `DATABASE_URL` | **Required.** `postgresql+asyncpg://user:pass@host:5432/db`. Set for you by `docker-compose.yml`. |
| `USE_WEBHOOK` | `false` (default, long polling) or `true` (webhook — needs `WEBHOOK_BASE_URL`). See [below](#webhook-mode-vs-long-polling). |
| `STORAGE_BACKEND` | `local` (default, zero AWS setup) or `s3` (recommended past one box). See [Large files](#large-files). |
| `RECOGNITION_PROVIDER` | `audd` (default) or `acrcloud`. |

Admins can also tune rate limits and file-size limits **live, from the Telegram admin panel**, without touching `.env` or redeploying — those environment variables are only the cold, first-boot defaults.

## Running without Docker

Needs Python 3.12+, a running PostgreSQL 16 and Redis 7 (reachable at whatever you put in `DATABASE_URL`/`REDIS_URL`), and `ffmpeg` + [Deno](https://deno.com/) (yt-dlp's external JS runtime, needed for full YouTube support — see [ARCHITECTURE.md §8](ARCHITECTURE.md#8-platform-support-matrix-honesty-section)) on `PATH`.

```bash
make venv && source .venv/bin/activate
make install-dev          # runtime + test dependencies
cp .env.example .env       # then edit it — see above
make dev-services          # starts just Postgres+Redis via Docker, if you don't have your own
make migrate                # alembic upgrade head
make run                    # long polling — Ctrl+C to stop
```

In a second terminal, start a worker (the bot enqueues jobs but never executes them itself — see [ARCHITECTURE.md §3](ARCHITECTURE.md#3-why-the-bot-never-blocks)):

```bash
make worker
```

Run `make help` for every other available target (tests, lint, migrations, Docker shortcuts).

## Deploying to AWS

The bot is designed so **the simplest possible AWS deployment — one EC2 instance, long polling, no load balancer, no TLS certificate — is fully supported**, not a stripped-down fallback. This is the recommended starting point; see [Webhook mode](#webhook-mode-vs-long-polling) below for when you'd want more.

### 1. Launch an EC2 instance

- **AMI:** Ubuntu 22.04 or 24.04 LTS (or Amazon Linux 2023 — adjust package-manager commands accordingly).
- **Instance type:** `t3.small` (2 GiB RAM) is a reasonable starting point; `ffmpeg` and `yt-dlp` are the heaviest consumers. Scale up if you see OOM kills in `docker compose logs`.
- **Storage:** 20 GiB gp3 is plenty to start (media is deleted immediately after delivery — see [ARCHITECTURE.md §12](ARCHITECTURE.md#12-temp-files--data-retention) — this isn't a media archive).
- **Security group:**
  - `SSH (22)` from your own IP only.
  - **Long polling (recommended default): no other inbound ports needed.** The bot only makes *outbound* HTTPS calls to Telegram/AudD/social platforms.
  - If you'll also use the local storage backend's large-file links (`STORAGE_BACKEND=local`, the default) from *outside* the box, additionally open `Custom TCP 8080` (or whatever `WEB_SERVER_PORT` you set) from anywhere — those temporary download links need to be reachable by whoever received them in Telegram.
  - **Webhook mode only:** additionally open `HTTP (80)` and `HTTPS (443)` (for Caddy's automatic TLS — see below).

### 2. Install Docker

SSH in, then ([official Docker docs](https://docs.docker.com/engine/install/ubuntu/)):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker   # or log out and back in
docker compose version   # confirm the plugin is present
```

### 3. Clone and configure

```bash
git clone https://github.com/KADIRTRADING/instasongbot.git
cd instasongbot
cp .env.example .env
nano .env   # set BOT_TOKEN, ADMIN_IDS, AUDD_API_TOKEN at minimum
```

For a bare-EC2, long-polling, local-storage deployment, also set:

```
PUBLIC_BASE_URL=http://YOUR_EC2_PUBLIC_IP:8080
```

(This is only used to build large-file download links — see [Large files](#large-files). If every file your users request stays under `TELEGRAM_DIRECT_UPLOAD_MB` (50 MB by default), it's never actually used, but must still be set or `STORAGE_BACKEND=local` refuses to start.)

### 4. Start it

```bash
docker compose up --build -d
docker compose logs -f bot   # confirm it connected and started polling
```

That's a complete, working deployment. Message the bot on Telegram.

### 5. (Recommended) Run it as a system service / survive reboots

Docker's `restart: unless-stopped` (the base `docker-compose.yml`'s default) already brings containers back after a Docker daemon restart. For a genuine production posture — resource limits, log rotation, `restart: always` surviving a full host reboot — use the production overlay instead:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

See `docker-compose.prod.yml`'s own header comment for exactly what this changes and why. Docker itself restarts on boot once enabled (`sudo systemctl enable docker`), and `restart: always` containers come back with it.

### 6. Point a domain at it (optional)

Not required for long polling. If you'd like a friendly hostname anyway (e.g. for the large-file links above, or in preparation for webhook mode later), create an A record pointing your domain at the EC2 instance's Elastic IP, then set `PUBLIC_BASE_URL=https://your-domain.com` and run Caddy (see [Webhook mode](#webhook-mode-vs-long-polling)) purely for the free TLS certificate, even while `USE_WEBHOOK=false`.

## Webhook mode vs. long polling

| | Long polling (`USE_WEBHOOK=false`, default) | Webhook (`USE_WEBHOOK=true`) |
|---|---|---|
| Public URL / TLS needed | No | **Yes** — Telegram requires HTTPS |
| Inbound firewall ports | None (outbound-only) | 80/443 (or your reverse proxy's ports) |
| Setup complexity | Lowest — just works | Needs a domain + certificate |
| Scaling the bot process | One process holds the long-poll loop | Each update is an independent HTTP request — scales better across replicas |

**Recommendation: start with long polling.** Switch to webhook mode only once you have a real reason (horizontal scaling, an existing reverse-proxy/PaaS setup that expects webhooks).

To switch: set `USE_WEBHOOK=true`, `WEBHOOK_BASE_URL=https://your-domain.com`, and generate a `WEBHOOK_SECRET` (`openssl rand -hex 32`) in `.env`, then bring up the stack with the production overlay and the bundled `proxy` profile (Caddy, automatic Let's Encrypt TLS):

```bash
echo "BOT_DOMAIN=your-domain.com" >> .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile proxy up -d --build
```

Caddy (see `deploy/Caddyfile`) terminates TLS and forwards `/webhook` and `/files/*` to the bot container — no manual certificate handling. If you already run your own reverse proxy, skip the `proxy` profile and point your existing proxy at the bot container's `WEB_SERVER_PORT` instead.

## Large files

Telegram's Bot API caps direct uploads at 50 MB (`TELEGRAM_DIRECT_UPLOAD_MB`) and direct downloads-from-users at 20 MB (`MAX_TELEGRAM_FETCH_MB`) — see [ARCHITECTURE.md §9](ARCHITECTURE.md#9-telegram-file-size-limits-and-how-we-handle-them) for the full explanation, including the optional self-hosted [Local Bot API Server](https://github.com/tdlib/telegram-bot-api) path for raising those ceilings. Whenever a resolved download exceeds the upload ceiling, the bot instead:

1. Uploads the file to storage (`STORAGE_BACKEND`).
2. Sends the user a secure, time-limited download link instead of the file itself.

Two backends:

- **`local`** (default) — no AWS account needed. The bot signs its own HMAC-based temporary links (see `app/services/storage/local_backend.py`) and serves them itself via the small `app/file_server.py`. Requires `PUBLIC_BASE_URL` to be set to wherever that server is reachable from the internet.
- **`s3`** — uploads to a real S3 bucket (or any S3-compatible provider — MinIO, R2, Spaces, B2 — via `S3_ENDPOINT_URL`) and returns a real presigned URL. Recommended once you're running more than one box, or don't want to manage the local storage volume's lifecycle yourself.

## Admin panel

Available to every numeric ID listed in `ADMIN_IDS`, via the `/admin` command or the "🛠 Admin Panel" reply-keyboard button:

- **✏️ Edit Captions** — per-media-type (video/audio/image) caption templates. Supports `{title}`, `{artist}`, `{source}`, `{bot_username}` placeholders.
- **🔗 Manage Buttons** — inline buttons attached under every delivered video/audio/photo, scoped to one media type or all of them.
- **🎚 Platforms** — enable/disable each of the six supported platforms independently, live.
- **📊 Statistics** — total/new users, job counts by status/type/platform.
- **⚙️ File Size Limits** — override the download-size cap globally or per-platform, without editing `.env` or restarting anything.
- **📢 Announcement** — broadcast a message to every non-banned user; runs as its own background job so it can't block anything else, with a preview-and-confirm step before it actually sends.

## Development

```bash
make install-dev
make test           # fast suite, no network -- this is what CI should run
make test-live       # opt-in: hits real AudD/Pinterest/TikTok/Redis -- see each test file's docstring
make lint            # ruff check
make test-cov        # fast suite + coverage report
```

See `make help` for the complete list. Tests follow a deliberate "prefer the real thing over a mock" philosophy throughout this codebase: real aiogram `Dispatcher.feed_update()` end-to-end dispatch, a real in-memory SQLite database via the app's own session/repository layer, real `fakeredis` (with Lua `EVAL` support via `lupa`, needed for the rate limiter's atomic script) — only genuine external network calls (real Telegram, real AudD, real yt-dlp/Pinterest HTTP) are mocked in the default suite, and are instead covered by the opt-in `-m live` tests.

## Troubleshooting

**Bot doesn't respond to `/start`.**
Check `docker compose logs bot`. Most commonly: `BOT_TOKEN` is wrong/has a typo, or (webhook mode only) the webhook registration failed — check for a `webhook_registered` log line and that `WEBHOOK_BASE_URL` is a real, publicly-reachable HTTPS URL.

**"Admin Panel" button does nothing / `/admin` is silently ignored.**
Your Telegram user ID isn't in `ADMIN_IDS`. Double check via @userinfobot and that you restarted the bot after editing `.env` (admin allow-listing is intentionally an env-based, restart-required security boundary — see [ARCHITECTURE.md §11](ARCHITECTURE.md#11-deployment-topology)).

**Instagram links fail / X (Twitter) links sometimes fail.**
Documented, honest platform limitations, not bugs in this project — see the [platform support matrix](ARCHITECTURE.md#8-platform-support-matrix-honesty-section) in ARCHITECTURE.md §8 for exactly what's going on with each and the relevant upstream `yt-dlp` issue trackers. Instagram specifically can be improved by supplying your own `INSTAGRAM_COOKIES_FILE` (see `.env.example`) — at your own account's ToS risk.

**YouTube downloads fail with a format/extraction error.**
YouTube periodically requires yt-dlp to run a small JS challenge via an external JS runtime. The Docker image bundles [Deno](https://deno.com/) for exactly this; if you're running without Docker, make sure `deno` is on `PATH` (`deno --version` should work).

**A file never seems to arrive, and no error was shown either.**
Check `docker compose logs worker` — the bot's own process only ever enqueues jobs and shows a "⏳ processing" message ([ARCHITECTURE.md §3](ARCHITECTURE.md#3-why-the-bot-never-blocks)); all actual work, and any resulting error message, comes from the worker process. If the worker container isn't running (`docker compose ps`), nothing will ever complete.

**`docker compose up` fails at the `alembic upgrade head` step.**
Usually means Postgres wasn't ready yet — the compose file's `depends_on: condition: service_healthy` should prevent this, but if you're running against an external/non-Compose Postgres, confirm `DATABASE_URL` is reachable first (`docker compose exec bot python -c "import asyncpg, asyncio; asyncio.run(asyncpg.connect('...'))"`, adjusting the DSN).

---

For the full technical design — stack rationale, data flow per feature, database schema, and the complete platform-by-platform honesty section — see **[ARCHITECTURE.md](ARCHITECTURE.md)**.
