"""Точка входа: long polling, логи в консоль и data/bot.log, фоновая очистка."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BotCommand, ErrorEvent

import handlers
from config import Config, ConfigError, load_config
from db import Database

logger = logging.getLogger("dialogspy")


def setup_logging(cfg: Config) -> None:
    log_file = cfg.db_path.parent / "bot.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    level = getattr(logging, cfg.log_level, logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)


async def retention_loop(db: Database, days: int) -> None:
    """Раз в сутки удаляет записи старше N дней."""
    while True:
        try:
            removed = await db.cleanup(days)
            if removed:
                logger.info("Очистка по RETENTION_DAYS=%s: удалено %s сообщений", days, removed)
        except Exception:
            logger.exception("Ошибка очистки старых записей")
        await asyncio.sleep(24 * 60 * 60)


async def error_handler(event: ErrorEvent) -> bool:
    """Любая ошибка апдейта логируется, бот продолжает работу."""
    if isinstance(event.exception, TelegramRetryAfter):
        logger.warning("Flood control: ждём %s с", event.exception.retry_after)
        await asyncio.sleep(event.exception.retry_after)
        return True
    logger.error(
        "Ошибка при обработке апдейта: %s",
        event.exception,
        exc_info=event.exception,
    )
    return True


async def main() -> None:
    cfg = load_config()
    setup_logging(cfg)
    logger.info("Запуск DialogSpy (long polling)")

    db = Database(cfg.db_path)
    await db.connect()
    await handlers.configure(cfg, db)

    session = AiohttpSession(proxy=cfg.telegram_proxy) if cfg.telegram_proxy else None
    bot = Bot(cfg.bot_token, session=session)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота и открыть инструкцию"),
            BotCommand(command="help", description="Показать инструкцию по подключению"),
        ]
    )
    dp = Dispatcher()
    dp.include_router(handlers.router)
    dp.errors.register(error_handler)

    retention_task: asyncio.Task[None] | None = None
    if cfg.retention_days > 0:
        retention_task = asyncio.create_task(retention_loop(db, cfg.retention_days))

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("Остановка DialogSpy")
        if retention_task is not None:
            retention_task.cancel()
            with suppress(asyncio.CancelledError):
                await retention_task
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}")
        raise SystemExit(1)
    except (KeyboardInterrupt, SystemExit):
        pass
