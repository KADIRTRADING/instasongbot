"""Minimal aiohttp file server for the local storage backend's signed download
links (see app/services/storage/local_backend.py).

Mounted at /files/{filename} in two places:
  - The webhook entrypoint (app/webhook_app.py) calls `register_file_routes()`
    to add this exact route to its existing aiohttp Application, alongside
    the Telegram webhook route — same process, same port, one aiohttp app.
    (aiohttp's `add_subapp()` was deliberately not used for this: it requires
    a non-empty path *prefix*, which would force the route to live at
    something like /files-app/files/{filename} instead of the intended
    /files/{filename}, breaking every link this module signs.)
  - The long-polling entrypoint (app/main.py) runs `create_file_server_app()`
    as a small standalone aiohttp server on WEB_SERVER_PORT, since long
    polling itself has no HTTP server otherwise, and locally-stored large
    files still need to be reachable by a URL sent to the user in Telegram.

Every request's signature+expiry is verified before any bytes are served —
an unsigned or expired link returns 403/404, never a directory listing or an
arbitrary path off `storage_dir` (filenames are also restricted to a safe
pattern to prevent path traversal, e.g. `../../etc/passwd`).
"""

from __future__ import annotations

import re
from pathlib import Path

from aiohttp import web

from app.config import Settings
from app.logging_conf import get_logger
from app.services.storage.signing import verify

logger = get_logger(__name__)

_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

SETTINGS_KEY = web.AppKey("settings", Settings)


def register_file_routes(app: web.Application, settings: Settings) -> None:
    """Add the `/files/{filename}` route (and its settings dependency) to an
    EXISTING aiohttp Application — used by app/webhook_app.py so the webhook
    route and the file-serving route share one process/port. Safe to call on
    an app that also has other routes/middlewares already registered.
    """
    app[SETTINGS_KEY] = settings
    app.router.add_get("/files/{filename}", _handle_download)


def create_file_server_app(settings: Settings) -> web.Application:
    """Build a standalone aiohttp app with only the file-serving route —
    used by app/main.py (long-polling mode), which has no other aiohttp app
    of its own to attach this route to.
    """
    app = web.Application()
    register_file_routes(app, settings)
    return app


async def _handle_download(request: web.Request) -> web.StreamResponse:
    settings = request.app[SETTINGS_KEY]
    filename = request.match_info["filename"]

    if not _SAFE_FILENAME_RE.match(filename):
        raise web.HTTPBadRequest(text="Invalid filename")

    signature = request.query.get("sig", "")
    expires_at_raw = request.query.get("exp", "")
    if not signature or not expires_at_raw.isdigit():
        raise web.HTTPForbidden(text="Missing or malformed signature")

    if not verify(filename, int(expires_at_raw), signature, settings.BOT_TOKEN):
        raise web.HTTPForbidden(text="Link is invalid or has expired")

    storage_dir = Path(settings.WORKDIR) / "large_files"
    file_path = (storage_dir / filename).resolve()

    # Belt-and-suspenders: even though _SAFE_FILENAME_RE already blocks path
    # separators, confirm the resolved path is still inside storage_dir before
    # serving it.
    if storage_dir.resolve() not in file_path.parents and file_path != storage_dir.resolve():
        raise web.HTTPForbidden(text="Invalid path")

    if not file_path.is_file():
        raise web.HTTPNotFound(text="File not found or already expired/cleaned up")

    logger.info("file_server_serving", filename=filename)
    return web.FileResponse(file_path)
