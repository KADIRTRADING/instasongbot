"""Error taxonomy for the download pipeline.

Handlers catch these specific exception types (never a bare `Exception`) so
every failure mode the spec calls out — private posts, deleted content,
unsupported links, rate limits, download failures — gets its own translated,
user-facing message instead of one generic "something went wrong."
"""

from __future__ import annotations


class DownloaderError(Exception):
    """Base class for all downloader failures."""


class UnsupportedURLError(DownloaderError):
    """The URL doesn't match any supported platform, or that platform is
    currently disabled by an admin."""


class UnsafeURLError(DownloaderError):
    """The URL failed validation (not http(s), resolves to a private/loopback/
    link-local address, etc.) — see app/services/downloader/url_utils.py."""


class PrivateContentError(DownloaderError):
    """The content exists but requires login/is private (e.g. a private
    Instagram account, a friends-only Facebook post)."""


class ContentNotFoundError(DownloaderError):
    """The content was deleted or the link/ID is invalid."""


class RateLimitedError(DownloaderError):
    """The upstream platform is throttling us; retry later."""


class LoginRequiredError(DownloaderError):
    """The platform requires an authenticated session (operator-supplied
    cookies) to fetch this content anonymously — distinct from
    PrivateContentError, which means the content itself is private/friends-only.

    This exists specifically because platforms like Instagram now return a
    single combined "not available, rate-limited, or login required" message
    for anonymous requests from datacenter IPs, which is NOT the same as the
    post being private. Mapping that to PrivateContentError (as the code used
    to) told users a public reel was "private", which was wrong. We surface
    this as its own honest state: "couldn't fetch anonymously right now; the
    operator can supply a cookies file to enable it" — see
    app/services/downloader/ytdlp_client.py's Instagram classification and
    ARCHITECTURE/README's cookie-mount docs.
    """


class FileTooLargeError(DownloaderError):
    """The resolved media exceeds MAX_DOWNLOAD_MB."""

    def __init__(self, size_mb: float, limit_mb: int) -> None:
        super().__init__(f"File is {size_mb:.1f}MB, exceeds the {limit_mb}MB limit")
        self.size_mb = size_mb
        self.limit_mb = limit_mb


class DownloadTimeoutError(DownloaderError):
    """The download or probe exceeded its configured timeout."""


class DownloadFailedError(DownloaderError):
    """A catch-all for genuine, unexpected download failures (network error,
    extractor crash, corrupted output) that don't fit a more specific category
    above. Always logged with the underlying cause for debugging.
    """
