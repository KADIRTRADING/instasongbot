"""Tests for the i18n translator, including a completeness check that all
three locale files define exactly the same set of keys — the most common way
an i18n system silently breaks is a key added to one language and forgotten
in the others.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.i18n.translator import Translator, available_languages, get_translator

LOCALES_DIR = Path(__file__).parent.parent / "app" / "i18n" / "locales"


def _load(language: str) -> dict:
    return json.loads((LOCALES_DIR / f"{language}.json").read_text(encoding="utf-8"))


def test_all_three_locale_files_exist_and_parse() -> None:
    for lang in ("uz", "ru", "en"):
        data = _load(lang)
        assert isinstance(data, dict)
        assert len(data) > 0


def test_all_locales_have_identical_key_sets() -> None:
    uz_keys = set(_load("uz").keys())
    ru_keys = set(_load("ru").keys())
    en_keys = set(_load("en").keys())

    assert uz_keys == ru_keys, f"uz/ru key mismatch: {uz_keys ^ ru_keys}"
    assert uz_keys == en_keys, f"uz/en key mismatch: {uz_keys ^ en_keys}"


def test_no_locale_has_empty_string_values() -> None:
    for lang in ("uz", "ru", "en"):
        for key, value in _load(lang).items():
            assert value.strip(), f"{lang}.json key {key!r} is empty"


@pytest.mark.parametrize("language", ["uz", "ru", "en"])
def test_translator_loads_each_language(language: str) -> None:
    translator = Translator(language)
    assert translator.language == language
    assert translator.t("menu_help")  # any key present in every locale


def test_uz_is_the_default_language() -> None:
    from app.constants import DEFAULT_LANGUAGE

    assert DEFAULT_LANGUAGE.value == "uz"


def test_get_translator_defaults_to_uzbek_when_none() -> None:
    translator = get_translator(None)
    assert translator.language == "uz"


def test_get_translator_falls_back_to_default_for_unknown_language() -> None:
    translator = get_translator("fr")  # unsupported language code
    assert translator.language == "uz"


def test_substitution_with_named_placeholder() -> None:
    translator = Translator("en")
    result = translator.t("welcome", name="Alice")
    assert "Alice" in result


def test_substitution_with_numeric_format_spec() -> None:
    translator = Translator("en")
    result = translator.t("error_file_too_large", size_mb=42.7, limit_mb=50)
    assert "43 MB" in result  # {size_mb:.0f} rounds
    assert "50" in result


def test_missing_placeholder_kwarg_does_not_raise() -> None:
    translator = Translator("en")
    # Deliberately omit "name" — must not raise, must return something sane.
    result = translator.t("welcome")
    assert result  # falls back to the raw (unsubstituted) template, not an exception


def test_unknown_key_returns_the_key_itself_rather_than_raising() -> None:
    translator = Translator("en")
    result = translator.t("this_key_does_not_exist_anywhere")
    assert result == "this_key_does_not_exist_anywhere"


def test_available_languages_matches_locale_files() -> None:
    langs = {lang.value for lang in available_languages()}
    assert langs == {"uz", "ru", "en"}


def test_all_translations_use_html_safe_bold_tags_consistently() -> None:
    # help_text uses <b> tags in every locale (parse_mode=HTML in the bot) —
    # verify no locale forgot to close a tag it opened.
    for lang in ("uz", "ru", "en"):
        help_text = _load(lang)["help_text"]
        assert help_text.count("<b>") == help_text.count("</b>")
