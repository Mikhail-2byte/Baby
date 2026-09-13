import asyncio
import logging
import sys
from contextlib import suppress
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats

from bot import texts
from bot.config import settings
from bot.db.session import engine
from bot.handlers import routers
from bot.middlewares import FamilyMiddleware
from bot.services import lock, panel, ticker

# Файл пишем сами, а не редиректом shell, и это не удобство. При `>> bot.log`
# кодировку выбирает Python по локали Windows, а не по кодовой странице консоли:
# получается файл, где часть строк cp866, часть utf-8, и целиком его не читает
# ни один декодер. Здесь кодировка задана явно, а заодно появляется ротация,
# которой у редиректа нет вовсе.
LOG_PATH = settings.db_path.parent / "bot.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3


def _log_handlers() -> list[logging.Handler]:
    """Консоль всегда, файл — если получится его открыть.

    Сбой файла не должен мешать боту стартовать. Это не осторожность впрок:
    в Family_planer ровно так бот и не поднялся после перезапуска — старый
    `cmd.exe` держал лог открытым, новый процесс получил `PermissionError`
    **на импорте модуля** и умер, не дойдя до `main`. Причин хватает и без
    этого: полный диск, антивирус, права.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                LOG_PATH,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUPS,
                encoding="utf-8",
            )
        )
    except OSError as exc:
        # До `basicConfig` логгера ещё нет — пишем напрямую в stderr
        print(
            f"Лог-файл {LOG_PATH} недоступен ({exc}); пишу только в консоль",
            file=sys.stderr,
        )
    return handlers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=_log_handlers(),
)
log = logging.getLogger("bot")

# Прокси Karing отваливается по десятку раз в час, и aiogram честно пишет об
# этом строку на каждую попытку — в Family_planer это 5 МБ лога за полтора дня.
# Сам факт разрыва нам важен, поток одинаковых строк — нет
logging.getLogger("aiogram.dispatcher").setLevel(logging.WARNING)


def make_bot() -> Bot:
    # Пустой TELEGRAM_PROXY = ходим напрямую. Прокси нужен из-за недоступности
    # api.telegram.org из России
    session = (
        AiohttpSession(proxy=settings.telegram_proxy)
        if settings.telegram_proxy
        else None
    )
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )


async def _publish_commands(bot: Bot) -> None:
    """Меню команд для групп — в личке бот всё равно отвечает отказом.

    Сбой здесь не должен ронять старт: без меню бот работает, просто команды
    приходится помнить наизусть.
    """
    commands = [BotCommand(command=c, description=d) for c, d in texts.COMMANDS]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
    except Exception:
        log.warning("Не удалось обновить меню команд", exc_info=True)


async def _stop_ticker(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    # CancelledError здесь ожидаем — это ответ на нашу же отмену
    with suppress(asyncio.CancelledError):
        await task


async def main() -> None:
    if not settings.bot_token:
        log.error("BOT_TOKEN пуст. Заполните его в .env — токен даёт @BotFather.")
        sys.exit(1)

    # Замок берём до всего остального, и до `delete_webhook` в особенности:
    # второй экземпляр, дошедший до него, выбрасывает очередь Telegram, которую
    # первый ещё не забрал. Порядок здесь не стилистический
    guard = lock.SingleInstance()
    if not guard.acquire():
        # Не ошибка, а сработавшая защита, поэтому код возврата нулевой:
        # ненулевой заставил бы сторож Планировщика долбиться каждые пять минут
        log.warning(
            "Бот уже запущен — этот экземпляр не нужен (замок %s). "
            "Два long polling на одном токене теряют сообщения молча.",
            lock.PATH,
        )
        return

    bot = make_bot()
    dp = Dispatcher()
    dp.update.outer_middleware(FamilyMiddleware())
    for router in routers:
        dp.include_router(router)

    task: asyncio.Task | None = None
    try:
        me = await bot.get_me()
        log.info("Запуск @%s", me.username)
        await bot.delete_webhook(drop_pending_updates=True)
        await _publish_commands(bot)

        task = asyncio.create_task(ticker.run(bot), name="ticker")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        # Порядок обязателен: сначала перестаём порождать отправки, потом
        # закрываем сеть, последним — движок БД. Каждый шаг в своём try, чтобы
        # падение одного не съело остальные
        for step in (
            _stop_ticker(task),
            panel.shutdown(),
            bot.session.close(),
            engine.dispose(),
        ):
            try:
                await step
            except Exception:
                log.exception("Ошибка при остановке")
        # Последним, когда всё уже закрыто: пока замок наш, второй экземпляр не
        # начнёт поднимать то, что мы ещё не отпустили
        guard.release()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # SystemExit намеренно не ловим: код возврата должен дойти до
        # Планировщика задач, иначе ошибка конфигурации выглядит как успех
        log.info("Остановлен")
