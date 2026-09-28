"""Top-level error handler, registered on `dp.errors` (not a middleware — see
aiogram's ErrorsMiddleware/error-event model). Catches anything that escaped
every handler and middleware, logs it with a full traceback, and — best
effort — tells the user something went wrong instead of leaving them with a
bot that silently never responds.

This is a last-resort safety net, not the primary error-handling path: most
user-facing failures should already be caught and translated closer to their
source (see app/workers/tasks.py's per-exception-type handling). Reaching
this handler at all usually indicates a bug worth investigating in the logs.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import ErrorEvent

from app.i18n.translator import get_translator
from app.logging_conf import get_logger

logger = get_logger(__name__)


async def handle_unexpected_error(event: ErrorEvent, bot: Bot) -> None:
    """Registered on `dp.errors.register(...)`. aiogram's ErrorsMiddleware
    propagates the same `**data` every regular handler receives (see aiogram
    source: `self.router.propagate_event(update_type="error", event=..., **data)`),
    which is how `bot` ends up injected here via aiogram's own dependency
    injection — NOT via a `Bot.get_current()` class method, which does not
    exist in aiogram 3 (removed in the 2->3 migration; see aiogram's own
    migration guide: "if you want to get current bot instance inside
    handlers ... you should accept the argument bot: Bot").
    """
    logger.error(
        "unhandled_update_error",
        error=str(event.exception),
        update_id=event.update.update_id,
        exc_info=event.exception,
    )

    chat_id = None
    if event.update.message is not None:
        chat_id = event.update.message.chat.id
    elif event.update.callback_query is not None and event.update.callback_query.message is not None:
        chat_id = event.update.callback_query.message.chat.id

    if chat_id is None:
        return

    translator = get_translator(None)  # default language — we don't reliably know the user's here
    try:
        await bot.send_message(chat_id=chat_id, text=translator.t("error_generic"))
    except Exception:  # noqa: BLE001 - this IS the last-resort handler; nothing left to escalate to
        logger.warning("error_handler_could_not_notify_user", chat_id=chat_id)
