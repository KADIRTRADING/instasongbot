"""Callback data packing/unpacking.

Telegram caps `callback_data` at 64 BYTES (see ARCHITECTURE.md research —
confirmed via Bot API docs). A probe job id is a 36-character UUID, so we
can't afford to also embed a raw format_id (which for yt-dlp can be an
arbitrary-length string) — instead callbacks reference a format by its
*index* into the probed `formats` tuple, resolved back to the real format_id
by re-reading the cached ProbeResult (see app/bot/probe_cache.py) when the
callback fires. This keeps every callback_data string short and constant-ish
in size regardless of how verbose a platform's real format_id happens to be.
"""

from __future__ import annotations

from dataclasses import dataclass

_SEP = ":"
_MAX_CALLBACK_BYTES = 64


class CallbackDataError(ValueError):
    pass


@dataclass(frozen=True)
class DownloadFormatCallback:
    probe_job_id: str
    format_index: int | str  # int index, or the literal "all" for "download all images"

    PREFIX = "dl"

    def pack(self) -> str:
        packed = _SEP.join([self.PREFIX, self.probe_job_id, str(self.format_index)])
        _assert_fits(packed)
        return packed

    @classmethod
    def unpack(cls, data: str) -> DownloadFormatCallback:
        prefix, probe_job_id, index_raw = _split(data, expected_parts=3, expected_prefix=cls.PREFIX)
        format_index: int | str = index_raw if index_raw == "all" else int(index_raw)
        return cls(probe_job_id=probe_job_id, format_index=format_index)


@dataclass(frozen=True)
class SearchSelectCallback:
    """User tapped a numbered result in a search's result list. `token` is a
    short search-session key (see app/bot/search_cache.py), `index` is the
    ABSOLUTE index into the full ordered result list (not the page-relative
    one), so it stays valid regardless of which page it was tapped from.
    """

    token: str
    index: int

    PREFIX = "ss"

    def pack(self) -> str:
        packed = _SEP.join([self.PREFIX, self.token, str(self.index)])
        _assert_fits(packed)
        return packed

    @classmethod
    def unpack(cls, data: str) -> SearchSelectCallback:
        _, token, index_raw = _split(data, expected_parts=3, expected_prefix=cls.PREFIX)
        return cls(token=token, index=int(index_raw))


@dataclass(frozen=True)
class SearchPageCallback:
    """Previous/Next navigation within a search's result list. `page` is the
    zero-based page index to show next.
    """

    token: str
    page: int

    PREFIX = "sp"

    def pack(self) -> str:
        packed = _SEP.join([self.PREFIX, self.token, str(self.page)])
        _assert_fits(packed)
        return packed

    @classmethod
    def unpack(cls, data: str) -> SearchPageCallback:
        _, token, page_raw = _split(data, expected_parts=3, expected_prefix=cls.PREFIX)
        return cls(token=token, page=int(page_raw))


@dataclass(frozen=True)
class SearchCancelCallback:
    """User dismissed a search result list."""

    token: str

    PREFIX = "sx"

    def pack(self) -> str:
        packed = _SEP.join([self.PREFIX, self.token])
        _assert_fits(packed)
        return packed

    @classmethod
    def unpack(cls, data: str) -> SearchCancelCallback:
        _, token = _split(data, expected_parts=2, expected_prefix=cls.PREFIX)
        return cls(token=token)


@dataclass(frozen=True)
class ResultActionCallback:
    """A follow-up action offered under an auto-downloaded social video:
    "Find this song" / "Extract MP3" / "Other quality/options". `token`
    references a ResultActionContext (see app/bot/result_cache.py) that holds
    the already-delivered video's file_id + source URL, so the action reuses
    the media rather than refetching it.
    """

    token: str
    action: str  # "find" | "mp3" | "other"

    PREFIX = "ra"

    def pack(self) -> str:
        packed = _SEP.join([self.PREFIX, self.token, self.action])
        _assert_fits(packed)
        return packed

    @classmethod
    def unpack(cls, data: str) -> ResultActionCallback:
        _, token, action = _split(data, expected_parts=3, expected_prefix=cls.PREFIX)
        return cls(token=token, action=action)


@dataclass(frozen=True)
class LanguageCallback:
    language: str

    PREFIX = "lang"

    def pack(self) -> str:
        return _SEP.join([self.PREFIX, self.language])

    @classmethod
    def unpack(cls, data: str) -> LanguageCallback:
        _, language = _split(data, expected_parts=2, expected_prefix=cls.PREFIX)
        return cls(language=language)


def _assert_fits(packed: str) -> None:
    if len(packed.encode("utf-8")) > _MAX_CALLBACK_BYTES:
        raise CallbackDataError(f"callback_data exceeds {_MAX_CALLBACK_BYTES} bytes: {packed!r}")


def _split(data: str, *, expected_parts: int, expected_prefix: str) -> list[str]:
    parts = data.split(_SEP, maxsplit=expected_parts - 1)
    if len(parts) != expected_parts or parts[0] != expected_prefix:
        raise CallbackDataError(f"Malformed callback_data for prefix {expected_prefix!r}: {data!r}")
    return parts


def matches_prefix(data: str, prefix: str) -> bool:
    return data.split(_SEP, maxsplit=1)[0] == prefix
