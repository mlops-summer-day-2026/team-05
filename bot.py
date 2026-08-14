"""Точка входа: сборка Dispatcher, регистрация роутеров, запуск polling."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import ConfigError, load_settings
from db import Database
from handlers import channels, mood, period, reload, start

log = logging.getLogger(__name__)
_DB_PATH = Path("bot.db")


async def main() -> None:
    """Инициализирует бота и запускает polling."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # Сторонние библиотеки шумят DEBUG-сообщениями — глушим, чтобы лог читался.
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("Конфигурация не загрузилась: %s", exc)
        sys.exit(1)
    if not settings.bot_token:
        log.error("TELEGRAM_BOT_TOKEN не задан в .env")
        sys.exit(1)
    if not settings.llm_api_key:
        log.warning(
            "Ключ LLM не задан (%s в .env пуст) — сводки будут работать через фолбэк "
            "по ключевым словам",
            settings.llm.api_key_env,
        )

    db = Database(_DB_PATH)
    await db.init()

    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["settings"] = settings
    dispatcher["db"] = db
    dispatcher.include_routers(
        reload.router,
        start.router,
        channels.router,
        period.router,
        mood.router,
    )

    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот «%s» запущен", settings.bot.name)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен")
