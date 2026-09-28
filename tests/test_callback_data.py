"""Tests for callback_data packing/unpacking, including the 64-byte ceiling."""

from __future__ import annotations

import uuid

import pytest

from app.bot.callback_data import (
    CallbackDataError,
    DownloadFormatCallback,
    LanguageCallback,
    ResultActionCallback,
    SearchCancelCallback,
    SearchPageCallback,
    SearchSelectCallback,
    matches_prefix,
)


def test_download_format_callback_roundtrip() -> None:
    job_id = str(uuid.uuid4())
    original = DownloadFormatCallback(probe_job_id=job_id, format_index=3)

    packed = original.pack()
    unpacked = DownloadFormatCallback.unpack(packed)

    assert unpacked == original


def test_download_format_callback_all_index() -> None:
    job_id = str(uuid.uuid4())
    original = DownloadFormatCallback(probe_job_id=job_id, format_index="all")

    unpacked = DownloadFormatCallback.unpack(original.pack())

    assert unpacked.format_index == "all"


def test_download_format_callback_fits_within_64_bytes_with_real_uuid() -> None:
    # A real UUID is the longest realistic probe_job_id; format_index up to a
    # 2-digit carousel index is realistic (Pinterest carousels rarely exceed
    # single digits, but be generous).
    job_id = str(uuid.uuid4())
    packed = DownloadFormatCallback(probe_job_id=job_id, format_index=99).pack()

    assert len(packed.encode("utf-8")) <= 64


def test_search_select_callback_roundtrip_and_fits() -> None:
    original = SearchSelectCallback(token="abcdef123456", index=7)
    packed = original.pack()
    assert SearchSelectCallback.unpack(packed) == original
    assert len(packed.encode("utf-8")) <= 64
    assert matches_prefix(packed, "ss")


def test_search_page_callback_roundtrip() -> None:
    original = SearchPageCallback(token="abcdef123456", page=3)
    assert SearchPageCallback.unpack(original.pack()) == original
    assert matches_prefix(original.pack(), "sp")


def test_search_cancel_callback_roundtrip() -> None:
    original = SearchCancelCallback(token="abcdef123456")
    assert SearchCancelCallback.unpack(original.pack()) == original
    assert matches_prefix(original.pack(), "sx")


def test_result_action_callback_roundtrip_and_fits() -> None:
    for action in ("find", "mp3", "other"):
        original = ResultActionCallback(token="abcdef123456", action=action)
        packed = original.pack()
        assert ResultActionCallback.unpack(packed) == original
        assert len(packed.encode("utf-8")) <= 64
        assert matches_prefix(packed, "ra")


def test_language_callback_roundtrip() -> None:
    for lang in ("uz", "ru", "en"):
        original = LanguageCallback(language=lang)
        unpacked = LanguageCallback.unpack(original.pack())
        assert unpacked.language == lang


def test_unpack_rejects_wrong_prefix() -> None:
    with pytest.raises(CallbackDataError):
        DownloadFormatCallback.unpack("va:some-job-id:identify")


def test_unpack_rejects_malformed_data() -> None:
    with pytest.raises(CallbackDataError):
        DownloadFormatCallback.unpack("not-even-close-to-valid")


def test_unpack_rejects_wrong_part_count() -> None:
    with pytest.raises(CallbackDataError):
        DownloadFormatCallback.unpack("dl:only-one-part")


def test_unpack_rejects_non_integer_format_index() -> None:
    with pytest.raises(ValueError):
        DownloadFormatCallback.unpack("dl:some-job-id:not-a-number")


def test_pack_raises_if_result_would_exceed_64_bytes() -> None:
    absurdly_long_id = "x" * 100
    with pytest.raises(CallbackDataError, match="exceeds 64 bytes"):
        DownloadFormatCallback(probe_job_id=absurdly_long_id, format_index=1).pack()


def test_matches_prefix() -> None:
    packed = DownloadFormatCallback(probe_job_id="abc", format_index=1).pack()
    assert matches_prefix(packed, "dl") is True
    assert matches_prefix(packed, "ra") is False
