"""Minimal JSON-backed translator for the bot's three supported languages.

Deliberately not a full i18n framework (no gettext/.po pipeline, no pluralization
rules engine) — the message set is small and fixed, and a plain dict lookup
with `str.format`-style substitution covers every string in app/i18n/locales/*.json.
Using `str.format` here (unlike the caption renderer) is safe because these
templates are our own shipped files, not admin/user-controlled input.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.constants import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, Language

_LOCALES_DIR = Path(__file__).parent / "locales"


@lru_cache
def _load_locale(language: str) -> dict[str, str]:
    path = _LOCALES_DIR / f"{language}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class Translator:
    """Bound to one language at construction; middlewares hand out a fresh
    instance per request based on the user's stored `language_code`.
    """

    def __init__(self, language: str) -> None:
        self.language = language if language in {lang.value for lang in SUPPORTED_LANGUAGES} else DEFAULT_LANGUAGE.value
        self._strings = _load_locale(self.language)
        self._fallback = _load_locale(DEFAULT_LANGUAGE.value)

    def t(self, key: str, **kwargs: object) -> str:
        """Translate `key`, substituting any `{placeholder}` in the string.
        Falls back to the default language, then to the raw key itself, so a
        missing translation is visibly wrong rather than a crash.
        """
        template = self._strings.get(key) or self._fallback.get(key) or key
        if not kwargs:
            return template
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return template


def get_translator(language_code: str | None) -> Translator:
    return Translator(language_code or DEFAULT_LANGUAGE.value)


def available_languages() -> tuple[Language, ...]:
    return SUPPORTED_LANGUAGES
