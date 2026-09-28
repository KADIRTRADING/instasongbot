"""Tests for the download/convert inline keyboard builders."""

from __future__ import annotations

from app.bot.callback_data import DownloadFormatCallback
from app.bot.keyboards.download import build_format_keyboard
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


def test_build_result_actions_keyboard_with_file_has_find_and_mp3() -> None:
    from app.bot.callback_data import ResultActionCallback
    from app.bot.keyboards.search import build_result_actions_keyboard

    keyboard = build_result_actions_keyboard("tok123", Translator("en"), has_file=True)
    actions = [
        ResultActionCallback.unpack(btn.callback_data).action
        for row in keyboard.inline_keyboard
        for btn in row
    ]
    assert set(actions) == {"find", "mp3", "other"}
    for row in keyboard.inline_keyboard:
        for btn in row:
            assert ResultActionCallback.unpack(btn.callback_data).token == "tok123"


def test_build_result_actions_keyboard_without_file_only_has_other() -> None:
    from app.bot.callback_data import ResultActionCallback
    from app.bot.keyboards.search import build_result_actions_keyboard

    keyboard = build_result_actions_keyboard("tok123", Translator("en"), has_file=False)
    actions = [
        ResultActionCallback.unpack(btn.callback_data).action
        for row in keyboard.inline_keyboard
        for btn in row
    ]
    assert actions == ["other"]


def test_build_official_link_keyboard_is_url_button() -> None:
    from app.bot.keyboards.search import build_official_link_keyboard

    keyboard = build_official_link_keyboard("https://music.apple.com/song/1", Translator("en"))
    btn = keyboard.inline_keyboard[0][0]
    assert btn.url == "https://music.apple.com/song/1"
    assert btn.callback_data is None


def test_human_size_formats_small_files_in_kb() -> None:
    from app.bot.keyboards.download import _human_size

    assert _human_size(500 * 1024) == " (500 KB)"


def test_human_size_formats_large_files_in_mb() -> None:
    from app.bot.keyboards.download import _human_size

    assert _human_size(10 * 1024 * 1024) == " (10.0 MB)"


def test_human_size_returns_empty_string_for_none() -> None:
    from app.bot.keyboards.download import _human_size

    assert _human_size(None) == ""


def _search_session(n: int, per_page: int = 10):
    from app.bot.search_cache import SearchSession
    from app.services.search.base import SearchResult

    results = tuple(
        SearchResult(
            title=f"Track {i}",
            artist="Artist",
            album="Album",
            duration_seconds=200,
            preview_url="https://p" if i % 2 == 0 else None,
            official_url="https://o",
            artwork_url="https://a",
            is_downloadable_preview=(i % 2 == 0),
        )
        for i in range(n)
    )
    return SearchSession(user_id=1, query="q", results=results, per_page=per_page)


def test_search_results_keyboard_page0_numbers_nav_cancel() -> None:
    from app.bot.callback_data import SearchPageCallback, SearchSelectCallback
    from app.bot.keyboards.search import build_search_results_keyboard

    sess = _search_session(25)  # 3 pages of 10/10/5
    kb = build_search_results_keyboard("tok", sess, 0, Translator("en"))

    # First two rows are number buttons (5 each on a full page).
    nums = [SearchSelectCallback.unpack(b.callback_data).index for b in kb.inline_keyboard[0] + kb.inline_keyboard[1]]
    assert nums == list(range(10))  # absolute indices 0..9 on page 0

    # Page 0 has Next but not Previous.
    nav = kb.inline_keyboard[2]
    nav_pages = [SearchPageCallback.unpack(b.callback_data).page for b in nav]
    assert nav_pages == [1]  # only Next -> page 1

    # Last row is Cancel.
    assert kb.inline_keyboard[-1][0].callback_data.startswith("sx:")


def test_search_results_keyboard_last_page_indices_and_prev_only() -> None:
    from app.bot.callback_data import SearchPageCallback, SearchSelectCallback
    from app.bot.keyboards.search import build_search_results_keyboard

    sess = _search_session(25)
    kb = build_search_results_keyboard("tok", sess, 2, Translator("en"))  # last page: 5 items

    nums = [SearchSelectCallback.unpack(b.callback_data).index for row in kb.inline_keyboard[:1] for b in row]
    assert nums == [20, 21, 22, 23, 24]  # absolute indices on page 2

    # find the nav row (has SearchPageCallback) — should be Previous only
    nav_pages = []
    for row in kb.inline_keyboard:
        for b in row:
            if b.callback_data and b.callback_data.startswith("sp:"):
                nav_pages.append(SearchPageCallback.unpack(b.callback_data).page)
    assert nav_pages == [1]  # only Previous -> page 1


def test_search_results_keyboard_single_page_has_no_nav() -> None:
    from app.bot.keyboards.search import build_search_results_keyboard

    sess = _search_session(4)
    kb = build_search_results_keyboard("tok", sess, 0, Translator("en"))
    # no sp: nav buttons at all
    has_nav = any(b.callback_data and b.callback_data.startswith("sp:") for row in kb.inline_keyboard for b in row)
    assert has_nav is False
