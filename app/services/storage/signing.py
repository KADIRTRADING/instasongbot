"""HMAC-based signed URL tokens for the local storage backend's file server.

Kept as small, pure, independently-testable functions (mirrors the same
"write the crypto once, test it directly" approach used for the ACRCloud
request signature in app/services/recognition/acrcloud_provider.py).
"""

from __future__ import annotations

import hashlib
import hmac
import time


def sign(filename: str, secret: str, ttl_seconds: int) -> tuple[str, int]:
    """Return (signature_hex, expires_at_unix_timestamp) for `filename`."""
    expires_at = int(time.time()) + ttl_seconds
    signature = _compute(filename, expires_at, secret)
    return signature, expires_at


def verify(filename: str, expires_at: int, signature: str, secret: str) -> bool:
    """True if `signature` is valid for `filename`/`expires_at` and not expired."""
    if expires_at < int(time.time()):
        return False
    expected = _compute(filename, expires_at, secret)
    return hmac.compare_digest(expected, signature)


def _compute(filename: str, expires_at: int, secret: str) -> str:
    message = f"{filename}:{expires_at}".encode()
    return hmac.new(secret.encode(), message, digestmod=hashlib.sha256).hexdigest()
