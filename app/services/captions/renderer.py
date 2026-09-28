"""Caption templating engine.

Renders the admin-configured template for a media type (video/audio/image)
against a `CaptionContext`, substituting `{title}`, `{artist}`, `{source}`,
and `{bot_username}`. Deliberately NOT `str.format(**kwargs)`: an admin-edited
template is user-controlled input, and `str.format` on arbitrary text can
raise (`KeyError` on an unknown placeholder, `IndexError`/`ValueError` on a
stray `{}` or `{0}`) or, with a crafted template, reach attributes on the
context object (`{title.__class__}`). We instead do a narrow, whitelisted
substitution of exactly four known placeholders and leave everything else —
including any other `{...}` the admin typed — untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.constants import MediaType

# Telegram's current Bot API caption limit for photos/videos/audio/documents.
TELEGRAM_CAPTION_MAX_LENGTH = 1024

# Only these four placeholders are ever substituted — anything else in an
# admin's template (other `{...}` text, stray braces) is left as literal text
# rather than raising, so a typo in the admin panel never breaks delivery.
_PLACEHOLDER_RE = re.compile(r"\{(title|artist|source|bot_username)\}")


@dataclass(frozen=True)
class CaptionContext:
    title: str = ""
    artist: str = ""
    source: str = ""
    bot_username: str = ""


@dataclass(frozen=True)
class RenderedCaption:
    text: str
    reply_markup: InlineKeyboardMarkup | None


class CaptionRenderer:
    @staticmethod
    def render_text(template: str, context: CaptionContext) -> str:
        values = {
            "title": context.title,
            "artist": context.artist,
            "source": context.source,
            "bot_username": context.bot_username,
        }
        rendered = _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)

        if len(rendered) > TELEGRAM_CAPTION_MAX_LENGTH:
            rendered = rendered[: TELEGRAM_CAPTION_MAX_LENGTH - 1].rstrip() + "…"

        return rendered

    @staticmethod
    def build_keyboard(buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
        """`buttons` is a list of (label, url) pairs, already filtered/ordered
        by the caller (see CaptionRepository.list_buttons). One button per
        row keeps this readable on narrow mobile screens; admins configuring
        many buttons should keep labels short.
        """
        if not buttons:
            return None
        rows = [[InlineKeyboardButton(text=label, url=url)] for label, url in buttons if url]
        if not rows:
            return None
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @classmethod
    def render(
        cls, template: str, context: CaptionContext, buttons: list[tuple[str, str]]
    ) -> RenderedCaption:
        return RenderedCaption(
            text=cls.render_text(template, context),
            reply_markup=cls.build_keyboard(buttons),
        )


def media_type_for_caption(media_type: MediaType) -> str:
    """Small indirection so callers can pass the enum and get the exact string
    key CaptionRepository stores rows under ('video'/'audio'/'image')."""
    return media_type.value
