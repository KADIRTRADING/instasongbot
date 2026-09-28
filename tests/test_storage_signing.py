"""Tests for the HMAC signed-URL scheme used by the local storage backend."""

from __future__ import annotations

import time

from app.services.storage.signing import sign, verify


def test_sign_then_verify_succeeds() -> None:
    signature, expires_at = sign("song.mp3", secret="topsecret", ttl_seconds=3600)
    assert verify("song.mp3", expires_at, signature, secret="topsecret") is True


def test_verify_fails_with_wrong_secret() -> None:
    signature, expires_at = sign("song.mp3", secret="topsecret", ttl_seconds=3600)
    assert verify("song.mp3", expires_at, signature, secret="wrong-secret") is False


def test_verify_fails_with_tampered_filename() -> None:
    signature, expires_at = sign("song.mp3", secret="topsecret", ttl_seconds=3600)
    assert verify("different.mp3", expires_at, signature, secret="topsecret") is False


def test_verify_fails_with_tampered_expiry() -> None:
    signature, expires_at = sign("song.mp3", secret="topsecret", ttl_seconds=3600)
    assert verify("song.mp3", expires_at + 100000, signature, secret="topsecret") is False


def test_verify_fails_when_expired() -> None:
    signature, expires_at = sign("song.mp3", secret="topsecret", ttl_seconds=1)
    past_expiry = int(time.time()) - 10
    # Simulate an already-expired token by signing with a timestamp in the past.
    from app.services.storage.signing import _compute

    tampered_signature = _compute("song.mp3", past_expiry, "topsecret")
    assert verify("song.mp3", past_expiry, tampered_signature, secret="topsecret") is False


def test_verify_fails_with_tampered_signature() -> None:
    _, expires_at = sign("song.mp3", secret="topsecret", ttl_seconds=3600)
    assert verify("song.mp3", expires_at, "0" * 64, secret="topsecret") is False
