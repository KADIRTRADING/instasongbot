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
    def unpack(cls, data: str) -> "DownloadFormatCallback":
        prefix, probe_job_id, index_raw = _split(data, expected_parts=3, expected_prefix=cls.PREFIX)
        format_index: int | str = index_raw if index_raw == "all" else int(index_raw)
        return cls(probe_job_id=probe_job_id, format_index=format_index)


@dataclass(frozen=True)
class VideoActionCallback:
    """`ref_id` is a lookup key, not the media itself (Telegram file_ids are
    80+ bytes, well over the 64-byte callback_data ceiling — see module
    docstring). What it references depends on `source`:
      - source="link": ref_id is a probe_job_id, resolved via
        app/bot/probe_cache.py against a previously-probed URL.
      - source="upload": ref_id is an upload_job_id, resolved via
        app/bot/upload_cache.py against a Telegram file_id the user sent.
    """

    ref_id: str
    action: str  # "identify" | "audio" | "video"
    source: str  # "link" | "upload"

    PREFIX = "va"

    def pack(self) -> str:
        packed = _SEP.join([self.PREFIX, self.ref_id, self.action, self.source])
        _assert_fits(packed)
        return packed

    @classmethod
    def unpack(cls, data: str) -> "VideoActionCallback":
        _, ref_id, action, source = _split(data, expected_parts=4, expected_prefix=cls.PREFIX)
        return cls(ref_id=ref_id, action=action, source=source)


@dataclass(frozen=True)
class LanguageCallback:
    language: str

    PREFIX = "lang"

    def pack(self) -> str:
        return _SEP.join([self.PREFIX, self.language])

    @classmethod
    def unpack(cls, data: str) -> "LanguageCallback":
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
