# syntax=docker/dockerfile:1
#
# Multi-stage build for the bot AND worker processes (ARCHITECTURE.md §2:
# "Bot and worker are the same Docker image, started with different
# commands" — see docker-compose.yml's `command:` overrides). Splitting the
# Python dependency install (builder stage) from the final runtime image
# means the final image never carries pip's download cache or any
# build-only tooling, and Docker can cache the (slow) dependency-install
# layer independently of application code changes (fast, frequent).
#
# System dependencies baked into the final image, and why each is needed:
#   - ffmpeg/ffprobe : audio/video probing, extraction, HLS remuxing
#     (app/services/media/ffmpeg_tools.py, DownloadManager._download_hls).
#     Installed from Debian's own apt repo — confirmed available there
#     (ffmpeg 7.x on the `python:3.12-slim` image's Debian base) — no need
#     for a separate static-binary download/PPA.
#   - deno           : yt-dlp's external JS runtime, required for full
#     YouTube format-extraction support (see ARCHITECTURE.md §8 and
#     yt-dlp's own "External JavaScript runtime" requirement). Installed via
#     Deno's official install script into a fixed, PATH-visible location.
#   - curl           : only used to fetch the Deno installer at build time;
#     NOT installed in the final runtime image (see builder/runtime split
#     below) to keep the attack surface and image size down.

FROM python:3.12-slim AS builder

WORKDIR /build

# curl+ca-certificates only needed to install Deno here in the builder stage
# — the resulting binary is copied into the final image; curl itself is not.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

ENV DENO_INSTALL=/opt/deno
RUN curl -fsSL https://deno.land/install.sh | sh -s -- --no-modify-path

# Build wheels for every dependency once, cached by Docker as long as
# requirements.txt itself doesn't change (independent of app/ code churn).
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /build/wheels -r requirements.txt


FROM python:3.12-slim AS runtime

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash --uid 1000 appuser

COPY --from=builder /opt/deno /opt/deno
ENV DENO_INSTALL=/opt/deno
ENV PATH="${DENO_INSTALL}/bin:${PATH}"

WORKDIR /app

COPY --from=builder /build/wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

# WORKDIR/tmp (per-job scratch space, app/services/media/tempfiles.py) and
# WORKDIR/large_files (local storage backend, app/services/storage/local_backend.py)
# both need to exist and be writable by the non-root user before the app
# ever tries to create per-job subdirectories under them.
RUN mkdir -p /data/tmp /data/large_files && chown -R appuser:appuser /data /app

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WORKDIR=/data

# No EXPOSE/CMD baked in as "the" way to run this image: docker-compose.yml
# overrides `command:` per-service (bot vs worker, polling vs webhook) — see
# that file and the README's "Running without Compose" section for the
# exact commands each mode uses. A sensible default (long polling) is still
# provided so `docker run` alone does something reasonable.
CMD ["python", "-m", "app.main"]
