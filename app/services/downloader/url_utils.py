"""URL extraction, platform detection, and SSRF-safe validation.

Any URL a user pastes ends up fed to yt-dlp or our own HTTP client, which will
happily fetch whatever address it resolves to. Without validation, a user
could paste `http://169.254.169.254/latest/meta-data/` (a cloud metadata
endpoint) or `http://localhost:6379/` (our own Redis) and turn the bot into an
SSRF proxy against its own AWS host. `assert_public_http_url()` closes that
off by resolving the hostname and rejecting anything that isn't a public
unicast address, on top of only ever accepting http(s) schemes.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

from app.constants import Platform
from app.services.downloader.errors import UnsafeURLError

_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)

# Ordered so more specific hosts (e.g. "m.youtube.com") match before broader
# checks would be needed; each entry is (Platform, compiled hostname regex).
_PLATFORM_HOST_PATTERNS: tuple[tuple[Platform, re.Pattern[str]], ...] = (
    (Platform.YOUTUBE, re.compile(r"(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$", re.IGNORECASE)),
    (Platform.TIKTOK, re.compile(r"(^|\.)(tiktok\.com|vt\.tiktok\.com|vm\.tiktok\.com)$", re.IGNORECASE)),
    (Platform.INSTAGRAM, re.compile(r"(^|\.)instagram\.com$", re.IGNORECASE)),
    (Platform.FACEBOOK, re.compile(r"(^|\.)(facebook\.com|fb\.watch)$", re.IGNORECASE)),
    (Platform.TWITTER, re.compile(r"(^|\.)(twitter\.com|x\.com)$", re.IGNORECASE)),
    (
        Platform.PINTEREST,
        re.compile(
            r"(^|\.)(pinterest\.[a-z.]+|pin\.it)$",
            re.IGNORECASE,
        ),
    ),
)


def extract_first_url(text: str) -> str | None:
    """Pull the first http(s) URL out of a free-text message, if any."""
    match = _URL_RE.search(text or "")
    return match.group(0) if match else None


def detect_platform(url: str) -> Platform | None:
    """Classify a URL by hostname. Returns None for unsupported/unknown hosts."""
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower()
    for platform, pattern in _PLATFORM_HOST_PATTERNS:
        if pattern.search(host):
            return platform
    return None


def _is_public_address(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_public_http_url(url: str) -> None:
    """Raise UnsafeURLError unless `url` is http(s) and resolves only to public,
    routable IP addresses. Call this before handing any user-supplied URL to
    yt-dlp, httpx, or any other outbound fetcher.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Only http/https URLs are supported, got scheme: {parsed.scheme!r}")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    try:
        addrinfos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Could not resolve host: {host}") from exc

    if not addrinfos:
        raise UnsafeURLError(f"Could not resolve host: {host}")

    resolved_ips = {info[4][0] for info in addrinfos}
    for ip_str in resolved_ips:
        try:
            if not _is_public_address(ip_str):
                raise UnsafeURLError(f"URL resolves to a non-public address ({ip_str}), refusing to fetch")
        except ValueError as exc:
            raise UnsafeURLError(f"Could not parse resolved address: {ip_str}") from exc
