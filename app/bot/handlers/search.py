"""Music text-search handler (song name / artist -> numbered results).

This is the LAST-resort content route: any plain-text message that is NOT a
command and does NOT contain a supported-platform URL is treated as a music
search query (the brief's "type a song name and get a numbered list"). Because
this router is registered LAST (see app/bot/dispatcher.py), commands, admin
FSM input, and download links have all already had their chance to match.

The search itself runs in a background `search_job` (network call to the
provider); the numbered-list navigation (pick a number / Next / Previous /
Cancel) is driven by the inline callbacks below, resolved against a cached,
user-bound `SearchSession` (see app/bot/search_cache.py).

Honesty (brief): on selecting a track we send a clearly-labeled 30-second
preview + an official-link button when a preview exists, or the official link
alone when it doesn't — never the full track presented as downloadable.
"""

from __future__ import annotations

from uuid import uuid4

from aiogram import F, Router, flags
from aiogram.types import CallbackQuery, Message
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callback_data import (
    CallbackDataError,
    SearchCancelCallback,
    SearchPageCallback,
    SearchSelectCallback,
    matches_prefix,
)
from app.bot.keyboards.search import build_search_results_keyboard
from app.bot.search_cache import load_search_session
from app.constants import JobType
from app.db.repositories import JobRepository
from app.i18n.translator import Translator
from app.services.downloader.url_utils import extract_first_url

router = Router(name="search")


def _is_search_query(message: Message) -> bool:
    """True for a plain-text message that should be treated as a music search:
    non-empty text, not a slash-command, and containing NO http(s) URL at all
    (any URL — supported or not — is the download flow's or an error's
    concern, never a search term)."""
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    url = extract_first_url(text)
    if url is not None:
        # Any URL disqualifies it as a search query. A supported one is handled
        # by the download router (registered earlier); an unsupported one falls
        # through to nothing here and the user gets no false "no results".
        return False
    return True


@router.message(F.text, _is_search_query)
@flags.rate_limit("recognize")
async def handle_search_query(
    message: Message,
    session: AsyncSession,
    translator: Translator,
    arq_pool: ArqRedis,
) -> None:
    query = (message.text or "").strip()

    progress_message = await message.answer(translator.t("search_searching", query=query))

    job_id = str(uuid4())
    await JobRepository.create(session, job_id=job_id, user_id=message.from_user.id, job_type=JobType.RECOGNIZE.value)

    await arq_pool.enqueue_job(
        "search_job",
        job_id=job_id,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        message_id=progress_message.message_id,
        query=query,
    )


@router.callback_query(lambda c: c.data is not None and matches_prefix(c.data, SearchPageCallback.PREFIX))
async def on_search_page(callback: CallbackQuery, translator: Translator, arq_pool: ArqRedis) -> None:
    try:
        data = SearchPageCallback.unpack(callback.data)
    except CallbackDataError:
        await callback.answer()
        return

    sess = await load_search_session(arq_pool, data.token)
    if sess is None:
        await callback.answer(translator.t("search_expired"), show_alert=True)
        return
    if not sess.belongs_to(callback.from_user.id):
        await callback.answer()
        return

    await callback.answer()
    if callback.message is None:
        return

    page_index = max(0, min(data.page, sess.total_pages - 1))
    text = _render_page_text(sess, page_index, translator)
    keyboard = build_search_results_keyboard(data.token, sess, page_index, translator)
    await callback.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)


@router.callback_query(lambda c: c.data is not None and matches_prefix(c.data, SearchCancelCallback.PREFIX))
async def on_search_cancel(callback: CallbackQuery, translator: Translator, arq_pool: ArqRedis) -> None:
    try:
        data = SearchCancelCallback.unpack(callback.data)
    except CallbackDataError:
        await callback.answer()
        return

    sess = await load_search_session(arq_pool, data.token)
    if sess is not None and not sess.belongs_to(callback.from_user.id):
        await callback.answer()
        return

    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(translator.t("search_cancelled"))


@router.callback_query(lambda c: c.data is not None and matches_prefix(c.data, SearchSelectCallback.PREFIX))
@flags.rate_limit("recognize")
async def on_search_select(
    callback: CallbackQuery,
    session: AsyncSession,
    translator: Translator,
    arq_pool: ArqRedis,
) -> None:
    try:
        data = SearchSelectCallback.unpack(callback.data)
    except CallbackDataError:
        await callback.answer()
        return

    sess = await load_search_session(arq_pool, data.token)
    if sess is None:
        await callback.answer(translator.t("search_expired"), show_alert=True)
        return
    if not sess.belongs_to(callback.from_user.id):
        await callback.answer()
        return

    result = sess.result_at(data.index)
    if result is None:
        await callback.answer(translator.t("search_expired"), show_alert=True)
        return

    await callback.answer()
    if callback.message is None:
        return

    progress = await callback.message.answer(translator.t("downloading_in_progress"))

    job_id = str(uuid4())
    await JobRepository.create(session, job_id=job_id, user_id=callback.from_user.id, job_type=JobType.RECOGNIZE.value)

    # Deliver the chosen track's preview/official link in a background job (it
    # fetches the ~30s preview audio, if any). Pass the result fields directly
    # — small and JSON-safe — so the worker needn't re-resolve the session.
    await arq_pool.enqueue_job(
        "search_deliver_job",
        job_id=job_id,
        user_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        message_id=progress.message_id,
        title=result.title,
        artist=result.artist,
        preview_url=result.preview_url or "",
        official_url=result.official_url or "",
        artwork_url=result.artwork_url or "",
    )


def _render_page_text(sess, page_index: int, translator: Translator) -> str:
    """Header + numbered lines for one results page. Shared by search_job's
    first render (in the worker) and paging here — kept identical by both
    importing this helper."""
    header = translator.t(
        "search_results_header",
        query=sess.query,
        page=page_index + 1,
        total_pages=sess.total_pages,
    )
    offset = page_index * sess.per_page
    lines = [header, ""]
    for i, result in enumerate(sess.page(page_index)):
        lines.append(_format_result_line(offset + i + 1, result, translator))
    return "\n".join(lines)


def _format_result_line(number: int, result, translator: Translator) -> str:
    version = translator.t(f"search_version_{result.version}") if result.version else ""
    if result.duration_seconds:
        mins, secs = divmod(int(result.duration_seconds), 60)
        duration = f" · {mins}:{secs:02d}"
    else:
        duration = ""
    return translator.t(
        "search_result_line",
        index=number,
        artist=result.artist,
        title=result.title,
        version=version,
        duration=duration,
    )
