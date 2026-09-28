"""Tests for the download/convert inline keyboard builders."""

from __future__ import annotations

from app.bot.callback_data import DownloadFormatCallback, VideoActionCallback
from app.bot.keyboards.download import build_format_keyboard, build_video_action_keyboard
from app.constants import MediaType
from app.i18n.translator import Translator
from app.services.downloader.models import MediaFormat, ProbeResult


def _probe(formats: tuple[MediaFormat, ...]) -> ProbeResult:
    return ProbeResult(
        platform="pinterest",
        source_url="https://example.com",
        title="x",
        uploader=None,
        thumbnail_url=None,
        duration_seconds=None,
        formats=formats,
    )


def test_build_format_keyboard_one_button_per_format() -> None:
    formats = (
        MediaFormat(format_id="video:720p", media_type=MediaType.VIDEO, label="720p", ext="mp4", filesize_bytes=5_000_000),
        MediaFormat(format_id="audio:best", media_type=MediaType.AUDIO, label="Audio only", ext="mp3"),
    )
    probe = _probe(formats)
    translator = Translator("en")

    keyboard = build_format_keyboard("job-123", probe, translator)

    assert len(keyboard.inline_keyboard) == 2
    assert "720p" in keyboard.inline_keyboard[0][0].text
    assert "4.8 MB" in keyboard.inline_keyboard[0][0].text  # 5_000_000 bytes / 1024^2
    assert "Audio only" in keyboard.inline_keyboard[1][0].text


def test_build_format_keyboard_callback_data_round_trips_to_correct_index() -> None:
    formats = (
        MediaFormat(format_id="a", media_type=MediaType.IMAGE, label="Image 1", ext="jpg"),
        MediaFormat(format_id="b", media_type=MediaType.IMAGE, label="Image 2", ext="jpg"),
        MediaFormat(format_id="c", media_type=MediaType.IMAGE, label="Image 3", ext="jpg"),
    )
    probe = _probe(formats)
    translator = Translator("en")

    keyboard = build_format_keyboard("job-abc", probe, translator)

    for index, row in enumerate(keyboard.inline_keyboard[:3]):
        callback = DownloadFormatCallback.unpack(row[0].callback_data)
        assert callback.probe_job_id == "job-abc"
        assert callback.format_index == index
        assert probe.formats[callback.format_index].format_id == formats[index].format_id


def test_build_format_keyboard_adds_download_all_for_multi_image() -> None:
    formats = (
        MediaFormat(format_id="a", media_type=MediaType.IMAGE, label="Image 1 of 2", ext="jpg"),
        MediaFormat(format_id="b", media_type=MediaType.IMAGE, label="Image 2 of 2", ext="jpg"),
    )
    probe = _probe(formats)
    translator = Translator("en")

    keyboard = build_format_keyboard("job-xyz", probe, translator)

    assert len(keyboard.inline_keyboard) == 3  # 2 images + "download all"
    last_row = keyboard.inline_keyboard[-1]
    callback = DownloadFormatCallback.unpack(last_row[0].callback_data)
    assert callback.format_index == "all"


def test_build_format_keyboard_no_download_all_for_single_image() -> None:
    formats = (MediaFormat(format_id="a", media_type=MediaType.IMAGE, label="Image", ext="jpg"),)
    probe = _probe(formats)

    keyboard = build_format_keyboard("job-single", probe, Translator("en"))

    assert len(keyboard.inline_keyboard) == 1


def test_build_format_keyboard_no_download_all_for_video_formats() -> None:
    # Multiple VIDEO formats (different resolutions) should NOT get a
    # "download all" button — that's only meaningful for image carousels.
    formats = (
        MediaFormat(format_id="720p", media_type=MediaType.VIDEO, label="720p", ext="mp4"),
        MediaFormat(format_id="480p", media_type=MediaType.VIDEO, label="480p", ext="mp4"),
    )
    probe = _probe(formats)

    keyboard = build_format_keyboard("job-video", probe, Translator("en"))

    assert len(keyboard.inline_keyboard) == 2


def test_build_video_action_keyboard_has_three_options() -> None:
    keyboard = build_video_action_keyboard("job-999", Translator("en"))

    assert len(keyboard.inline_keyboard) == 3
    actions = [VideoActionCallback.unpack(row[0].callback_data).action for row in keyboard.inline_keyboard]
    assert actions == ["identify", "audio", "video"]


def test_human_size_formats_small_files_in_kb() -> None:
    from app.bot.keyboards.download import _human_size

    assert _human_size(500 * 1024) == " (500 KB)"


def test_human_size_formats_large_files_in_mb() -> None:
    from app.bot.keyboards.download import _human_size

    assert _human_size(10 * 1024 * 1024) == " (10.0 MB)"


def test_human_size_returns_empty_string_for_none() -> None:
    from app.bot.keyboards.download import _human_size

    assert _human_size(None) == ""
