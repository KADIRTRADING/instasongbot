"""Inline keyboard builders for the download/convert flows."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.callback_data import DownloadFormatCallback
from app.constants import MediaType
from app.i18n.translator import Translator
from app.services.downloader.models import ProbeResult


def _human_size(size_bytes: int | None) -> str:
    if not size_bytes:
        return ""
    mb = size_bytes / (1024 * 1024)
    return f" ({mb:.1f} MB)" if mb >= 1 else f" ({size_bytes // 1024} KB)"


def build_format_keyboard(probe_job_id: str, probe: ProbeResult, translator: Translator) -> InlineKeyboardMarkup:
    """One button per available format, in the order the backend already
    ranked them (best quality / most relevant first — see ytdlp_client.py and
    pinterest_client.py). A multi-image carousel also gets a "download all"
    button at the bottom.
    """
    rows: list[list[InlineKeyboardButton]] = []

    for index, fmt in enumerate(probe.formats):
        label = f"{fmt.label}{_human_size(fmt.filesize_bytes)}"
        callback = DownloadFormatCallback(probe_job_id=probe_job_id, format_index=index)
        rows.append([InlineKeyboardButton(text=label, callback_data=callback.pack())])

    is_multi_image = len(probe.formats) > 1 and all(f.media_type == MediaType.IMAGE for f in probe.formats)
    if is_multi_image:
        all_callback = DownloadFormatCallback(probe_job_id=probe_job_id, format_index="all")
        rows.append(
            [
                InlineKeyboardButton(
                    text=translator.t("download_all_images", count=len(probe.formats)),
                    callback_data=all_callback.pack(),
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)
