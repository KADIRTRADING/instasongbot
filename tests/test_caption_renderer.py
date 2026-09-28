"""Tests for the caption templating engine, including adversarial admin-
edited templates (the whole point of NOT using str.format directly)."""

from __future__ import annotations

from app.services.captions.renderer import (
    TELEGRAM_CAPTION_MAX_LENGTH,
    CaptionContext,
    CaptionRenderer,
)


def test_renders_all_four_placeholders() -> None:
    template = "{title} by {artist}\nSource: {source}\nvia @{bot_username}"
    context = CaptionContext(title="Song Name", artist="Artist Name", source="TikTok", bot_username="instasongbot")

    result = CaptionRenderer.render_text(template, context)

    assert result == "Song Name by Artist Name\nSource: TikTok\nvia @instasongbot"


def test_missing_context_value_renders_as_empty_string() -> None:
    template = "{title} — {artist}"
    context = CaptionContext(title="Only Title")  # artist defaults to ""

    result = CaptionRenderer.render_text(template, context)

    assert result == "Only Title — "


def test_unknown_placeholder_left_untouched() -> None:
    # An admin might type a typo'd or unsupported placeholder — must not raise,
    # and must not be substituted (left exactly as literal text).
    template = "{title} costs {price} dollars"
    context = CaptionContext(title="Song")

    result = CaptionRenderer.render_text(template, context)

    assert result == "Song costs {price} dollars"


def test_stray_single_brace_does_not_raise() -> None:
    # str.format would raise ValueError on a lone "{" — our regex-based
    # substitution must tolerate it since it's just literal text to us.
    template = "50% off { this is not a placeholder"
    context = CaptionContext()

    result = CaptionRenderer.render_text(template, context)

    assert result == template


def test_attribute_access_injection_is_not_possible() -> None:
    # str.format(**kwargs) would happily evaluate "{title.__class__}" against
    # a passed object. Our substitution only ever recognizes the exact four
    # whitelisted tokens, so this must render as literal text, unresolved.
    template = "{title.__class__} {artist.__init__}"
    context = CaptionContext(title="Song", artist="Artist")

    result = CaptionRenderer.render_text(template, context)

    assert result == "{title.__class__} {artist.__init__}"


def test_positional_format_injection_is_not_possible() -> None:
    template = "{0} {1}"
    context = CaptionContext()

    result = CaptionRenderer.render_text(template, context)

    assert result == "{0} {1}"  # left as literal text, not IndexError


def test_empty_template_renders_empty() -> None:
    assert CaptionRenderer.render_text("", CaptionContext(title="x")) == ""


def test_template_with_no_placeholders_passes_through_unchanged() -> None:
    template = "Thanks for using our bot! Subscribe for more."
    assert CaptionRenderer.render_text(template, CaptionContext()) == template


def test_long_rendered_caption_is_truncated_to_telegram_limit() -> None:
    template = "{title}"
    context = CaptionContext(title="x" * 2000)

    result = CaptionRenderer.render_text(template, context)

    assert len(result) == TELEGRAM_CAPTION_MAX_LENGTH
    assert result.endswith("…")


def test_caption_exactly_at_limit_is_not_truncated() -> None:
    template = "{title}"
    context = CaptionContext(title="x" * TELEGRAM_CAPTION_MAX_LENGTH)

    result = CaptionRenderer.render_text(template, context)

    assert result == "x" * TELEGRAM_CAPTION_MAX_LENGTH
    assert "…" not in result


def test_repeated_placeholder_substituted_every_occurrence() -> None:
    template = "{title}! Did we say {title}? Yes, {title}."
    context = CaptionContext(title="Wow")

    result = CaptionRenderer.render_text(template, context)

    assert result == "Wow! Did we say Wow? Yes, Wow."


# --- Buttons / keyboard -----------------------------------------------


def test_build_keyboard_with_buttons() -> None:
    markup = CaptionRenderer.build_keyboard([("Channel", "https://t.me/example"), ("Site", "https://example.com")])

    assert markup is not None
    assert len(markup.inline_keyboard) == 2
    assert markup.inline_keyboard[0][0].text == "Channel"
    assert markup.inline_keyboard[0][0].url == "https://t.me/example"
    assert markup.inline_keyboard[1][0].text == "Site"


def test_build_keyboard_empty_list_returns_none() -> None:
    assert CaptionRenderer.build_keyboard([]) is None


def test_build_keyboard_skips_buttons_with_no_url() -> None:
    markup = CaptionRenderer.build_keyboard([("No URL", ""), ("Has URL", "https://example.com")])

    assert markup is not None
    assert len(markup.inline_keyboard) == 1
    assert markup.inline_keyboard[0][0].text == "Has URL"


def test_build_keyboard_all_buttons_missing_url_returns_none() -> None:
    assert CaptionRenderer.build_keyboard([("No URL", "")]) is None


# --- Full render() ------------------------------------------------------


def test_render_combines_text_and_keyboard() -> None:
    result = CaptionRenderer.render(
        "{title} by {artist}",
        CaptionContext(title="Song", artist="Artist"),
        [("Listen", "https://open.spotify.com/track/x")],
    )

    assert result.text == "Song by Artist"
    assert result.reply_markup is not None
    assert result.reply_markup.inline_keyboard[0][0].text == "Listen"


def test_render_with_no_buttons_has_none_markup() -> None:
    result = CaptionRenderer.render("{title}", CaptionContext(title="Song"), [])
    assert result.reply_markup is None
