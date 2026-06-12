import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from config import settings
from bot.db.database import AsyncSessionLocal, init_db, load_initial_synonyms
from bot.services.normalizer import normalizer
from bot.handlers.order_handler import handle_message, handle_edited_message
from bot.handlers.admin_handler import (
    cmd_order,
    cmd_synonyms,
    cmd_add_synonym,
    cmd_unknown,
    cmd_resolve,
    cmd_cache,
    cmd_cache_clear,
    cmd_cache_clear_all,
    cmd_stock_refresh,
    cmd_stock_status,
)

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    await init_db()
    await load_initial_synonyms()
    async with AsyncSessionLocal() as session:
        await normalizer.reload(session)
    application.bot_data["session_factory"] = AsyncSessionLocal

    if settings.STOCK_API_TOKEN:
        from bot.services.stock_service import stock_checker
        try:
            count = await stock_checker.refresh(
                settings.STOCK_API_URL_IPSH,
                settings.STOCK_API_URL_IPD,
                settings.STOCK_API_TOKEN,
            )
            logger.info("Stock preloaded: %d items", count)
        except Exception as exc:
            logger.warning("Stock preload failed (non-critical): %s", exc)

    logger.info("Bot ready. Admin IDs: %s", settings.admin_ids_list or "all users")


async def post_shutdown(application: Application) -> None:
    logger.info("Shutting down...")
    from bot.db.database import engine
    await engine.dispose()


def main() -> None:
    app = (
        Application.builder()
        .token(settings.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT, handle_edited_message))

    # Управление заказами
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("unknown", cmd_unknown))
    app.add_handler(CommandHandler("resolve", cmd_resolve))

    # Справочник
    app.add_handler(CommandHandler("synonyms", cmd_synonyms))
    app.add_handler(CommandHandler("add_synonym", cmd_add_synonym))
    app.add_handler(CommandHandler("cache", cmd_cache))
    app.add_handler(CommandHandler("cache_clear", cmd_cache_clear))
    app.add_handler(CommandHandler("cache_clear_all", cmd_cache_clear_all))

    # Остатки
    app.add_handler(CommandHandler("stock_refresh", cmd_stock_refresh))
    app.add_handler(CommandHandler("stock_status", cmd_stock_status))

    logger.info("Starting polling...")
    app.run_polling(
        allowed_updates=[
            Update.MESSAGE,
            Update.EDITED_MESSAGE,
        ]
    )


if __name__ == "__main__":
    main()
