"""Tests for platform detection and SSRF-safe URL validation."""

from __future__ import annotations

import pytest

from app.constants import Platform
from app.services.downloader.errors import UnsafeURLError
from app.services.downloader.url_utils import (
    assert_public_http_url,
    detect_platform,
    extract_first_url,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=abc123", Platform.YOUTUBE),
        ("https://youtu.be/abc123", Platform.YOUTUBE),
        ("https://m.youtube.com/watch?v=abc123", Platform.YOUTUBE),
        ("https://www.tiktok.com/@user/video/123", Platform.TIKTOK),
        ("https://vt.tiktok.com/abc/", Platform.TIKTOK),
        ("https://www.instagram.com/p/abc123/", Platform.INSTAGRAM),
        ("https://instagram.com/reel/abc123/", Platform.INSTAGRAM),
        ("https://www.facebook.com/watch/?v=123", Platform.FACEBOOK),
        ("https://fb.watch/abc123/", Platform.FACEBOOK),
        ("https://twitter.com/user/status/123", Platform.TWITTER),
        ("https://x.com/user/status/123", Platform.TWITTER),
        ("https://www.pinterest.com/pin/123456789/", Platform.PINTEREST),
        ("https://pinterest.co.uk/pin/123456789/", Platform.PINTEREST),
        ("https://pin.it/abc123", Platform.PINTEREST),
    ],
)
def test_detect_platform_supported(url: str, expected: Platform) -> None:
    assert detect_platform(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/video.mp4",
        "https://vimeo.com/12345",
        "not a url at all",
        "",
        "https://evil-youtube.com.attacker.net/watch?v=1",
    ],
)
def test_detect_platform_unsupported(url: str) -> None:
    assert detect_platform(url) is None


def test_detect_platform_rejects_lookalike_domain() -> None:
    # "notyoutube.com" must NOT match youtube.com just because it contains it.
    assert detect_platform("https://notyoutube.com/watch?v=1") is None
    # subdomain of an attacker-controlled domain that merely *ends* differently
    assert detect_platform("https://youtube.com.evil.net/watch") is None


def test_extract_first_url_from_free_text() -> None:
    text = "check this out https://www.tiktok.com/@user/video/123 nice right?"
    assert extract_first_url(text) == "https://www.tiktok.com/@user/video/123"


def test_extract_first_url_none_when_absent() -> None:
    assert extract_first_url("just some text, no links here") is None


def test_extract_first_url_empty_string() -> None:
    assert extract_first_url("") is None


# --- SSRF protection ---------------------------------------------------


def test_rejects_non_http_scheme() -> None:
    with pytest.raises(UnsafeURLError, match="scheme"):
        assert_public_http_url("ftp://example.com/file")


def test_rejects_file_scheme() -> None:
    with pytest.raises(UnsafeURLError, match="scheme"):
        assert_public_http_url("file:///etc/passwd")


def test_rejects_loopback_by_literal_ip() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_http_url("http://127.0.0.1:6379/")


def test_rejects_localhost_hostname() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_http_url("http://localhost:8080/admin")


def test_rejects_link_local_metadata_endpoint() -> None:
    # The canonical cloud metadata SSRF target (AWS/GCP/Azure all use this).
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_http_url("http://169.254.169.254/latest/meta-data/")


def test_rejects_private_range_10() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_http_url("http://10.0.0.5/internal")


def test_rejects_private_range_192_168() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_http_url("http://192.168.1.1/router")


def test_rejects_url_with_no_host() -> None:
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https:///no-host-here")


def test_accepts_known_public_host() -> None:
    # api.telegram.org is a stable, always-public host — resolving it is a
    # real DNS lookup, which is exactly what this guard needs to exercise.
    assert_public_http_url("https://api.telegram.org/")
