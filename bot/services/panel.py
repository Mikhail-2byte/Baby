"""Живая панель: одно закреплённое сообщение на семью.

Панель — главный и почти единственный экран бота. Всё, что нужно знать ночью,
и все кнопки записи в один тап. Бот её редактирует, а не шлёт новую, поэтому
лента чата не засоряется, а закреплённое сообщение всегда под рукой.

Сессию хендлера сюда передавать нельзя. `schedule` откладывает работу на
`panel_debounce_seconds`, а `FamilyMiddleware` закроет свою сессию сразу по
выходу из хендлера — то есть за секунды до пробуждения задачи. Плюс
`AsyncSession` не рассчитан на конкурентное использование двумя задачами.
Поэтому `refresh` открывает сессию сам, как и остальные фоновые сервисы.

`Session` импортируется по имени на уровне модуля намеренно: иначе тест не
сможет подменить фабрику через `monkeypatch.setattr(panel, "Session", ...)`.
"""

import asyncio
import logging
from contextlib import suppress
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import settings
from bot.db import repo
from bot.db.models import Family
from bot.db.session import Session
from bot.services import revisions, sending, stats, tracker_guard
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

# Ключ везде — family_id. Словари растут ровно по числу семей, чистить незачем
_locks: dict[int, asyncio.Lock] = {}
_tasks: dict[int, asyncio.Task] = {}


def schedule(bot: Bot, family_id: int, last_message_id: int | None = None) -> None:
    """Поставить перерисовку в очередь и сразу вернуть управление.

    Синхронная намеренно: хендлер не должен ждать панель — ответ человеку
    важнее. Предыдущая невыполненная задача отменяется, в этом и состоит
    дебаунс: из серии правок подряд до Telegram доживает только последняя.
    Это не оптимизация — Telegram ограничивает частоту правок одного
    сообщения, и без дебаунса серия быстрых тапов упёрлась бы во флуд-контроль.

    Словарь `_tasks` заодно держит сильную ссылку на задачу — без неё сборщик
    мусора вправе собрать её раньше, чем она отработает.
    """
    previous = _tasks.get(family_id)
    if previous is not None:
        previous.cancel()
    _tasks[family_id] = asyncio.create_task(
        _debounced(bot, family_id, last_message_id), name=f"panel:{family_id}"
    )


async def _debounced(bot: Bot, family_id: int, last_message_id: int | None) -> None:
    await asyncio.sleep(settings.panel_debounce_seconds)
    # Дальше отменять нечего: работа началась, и обрыв посреди правки оставил бы
    # панель рассинхронизированной с базой. У следующего `schedule` будет своя
    # задача, а лок выстроит их в очередь.
    #
    # Снимаем **только себя**, а не ключ вслепую. Отмена доставляется не в
    # момент `cancel()`, а на ближайшем `await`: задача, уже досыпавшая `sleep`,
    # но ещё не получившая управление, успевает дойти сюда после того, как
    # `schedule` положил под тот же ключ новую. Безусловный `pop` вычёркивал бы
    # из словаря её — а вместе с ней и сильную ссылку, ради которой словарь
    # заведён, и возможность отменить её следующим `schedule`, то есть сам дебаунс
    if _tasks.get(family_id) is asyncio.current_task():
        del _tasks[family_id]
    try:
        await refresh(bot, family_id, last_message_id)
    except Exception:
        # Задача fire-and-forget: без перехвата исключение всплыло бы как
        # «Task exception was never retrieved» уже после её смерти
        log.exception("Панель семьи #%s не обновлена", family_id)


async def refresh(
    bot: Bot,
    family_id: int,
    last_message_id: int | None = None,
    now: datetime | None = None,
    *,
    force_new: bool = False,
) -> None:
    """Пересобрать панель семьи: отредактировать или выпустить новую.

    `last_message_id` сохранён для совместимости вызывающих. Переписка больше
    не вызывает автоматический перенос панели.

    `force_new` — отключить старые кнопки и выпустить панель внизу по явному
    запросу. Обычные обновления только редактируют существующее сообщение.
    """
    # Сначала лок, потом сессия: стоять в очереди, держа соединение SQLite,
    # незачем — тикер пишет в ту же базу
    async with _locks.setdefault(family_id, asyncio.Lock()), Session() as session:
        family = await repo.get_family_by_id(session, family_id)
        if family is None:
            return
        child = await repo.active_child(session, family.id)
        if child is None:
            await _redraw(bot, session, family, last_message_id, now or tu.now_utc(),
                          force_new=force_new)
        else:
            async with tracker_guard.for_child(child.id):
                await session.refresh(family)
                await _redraw(bot, session, family, last_message_id, now or tu.now_utc(),
                              force_new=force_new)


async def refresh_stale(
    bot: Bot, session: AsyncSession, now: datetime | None = None
) -> None:
    """Раз в тик обновить активные таймеры и восстановить завершённые карточки."""
    moment = now or tu.now_utc()
    for family in await repo.all_families(session):
        child = await repo.active_child(session, family.id)
        running = await repo.any_open_event(session, child.id) if child else None
        if running is None and family.panel_message_id is None:
            continue
        if (running is None and family.panel_message_id is not None
                and family.panel_day == tu.local_today(family.tz, moment)):
            owner = (
                await repo.event_for_card(session, child.id, family.panel_message_id)
                if child else None
            )
            if owner is None or owner.ended_at is None:
                continue
        # Именно schedule, а не await refresh: у тикера открыта своя сессия, и
        # вложенная транзакция к SQLite — прямой путь к «database is locked»
        schedule(bot, family.id)


async def shutdown() -> None:
    """Погасить незавершённые дебаунсы при остановке бота.

    Без этого задача проснётся уже после `bot.session.close()` и на каждой
    остановке будет писать в лог сетевую ошибку.
    """
    tasks = list(_tasks.values())
    _tasks.clear()
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


async def _build(
    session: AsyncSession, family: Family, now: datetime
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст и клавиатура панели. Без ребёнка — приглашение завести карточку."""
    child = await repo.active_child(session, family.id)
    if child is None:
        return texts.NO_CHILD, None

    text = await stats.panel_text(session, family, child, now)
    running = await repo.any_open_event(session, child.id)
    return text, keyboards.panel(
        running_kind=running.kind if running else None,
        running_id=running.id if running else None,
        side=running.side if running else None,
        stamp=revisions.timer_stamp(running) if running else "",
    )


async def _redraw(
    bot: Bot,
    session: AsyncSession,
    family: Family,
    last_message_id: int | None,
    now: datetime,
    *,
    force_new: bool = False,
) -> None:
    child = await repo.active_child(session, family.id)
    running = await repo.any_open_event(session, child.id) if child else None
    if child is not None and family.panel_message_id is not None:
        owner = await repo.event_for_card(session, child.id, family.panel_message_id)
        if owner is not None and owner.ended_at is not None:
            status = await sending.edit(
                bot, family, family.panel_message_id, texts.finished(owner, family.tz),
                keyboards.stopped(owner.id), session=session,
            )
            if status not in (sending.OK, sending.NOT_FOUND):
                return  # ссылка остаётся в БД: следующий тик повторит завершение
            await _pin(bot, family, family.panel_message_id, pin=False)
            await repo.set_panel(session, family, None, None)

    text, markup = await _build(session, family, now)
    # У старой версии была отдельная карточка старта с дублирующей остановкой.
    if running is not None and running.card_message_id not in (None, family.panel_message_id):
        status = await sending.edit(
            bot, family, running.card_message_id, texts.started(running, family.tz),
            session=session,
        )
        if status not in (sending.OK, sending.NOT_FOUND):
            return
        running.card_message_id = None
        await session.commit()

    if force_new and family.panel_message_id is not None:
        status = await sending.edit(
            bot, family, family.panel_message_id, text, session=session,
        )
        if status not in (sending.OK, sending.NOT_FOUND):
            return  # не создаём второй набор управляющих кнопок при сбое уборки

    if family.panel_message_id is not None and not force_new:
        status = await sending.edit(
            bot, family, family.panel_message_id, text, markup, session=session
        )
        if status in (sending.OK, sending.RETRY, sending.BROKEN):
            # Единственный внешний признак, что дебаунс сработал: на серию
            # тапов подряд в логе обязана быть одна строка, а не по строке
            # на каждый
            log.info("Панель #%s: правка, %s", family.panel_message_id, status)
            if status == sending.OK:
                if running is not None:
                    running.card_message_id = family.panel_message_id
                await repo.set_panel(
                    session, family, family.panel_message_id, tu.local_today(family.tz, now)
                )
            return
        # Осталось NOT_FOUND (панель удалили руками), FORBIDDEN (выгнали) и
        # MOVED (группа стала супергруппой, а переехать не вышло) — все трое
        # разбирает `_publish`. Переехавшая группа даёт именно NOT_FOUND:
        # правили сообщение старого чата, в новом его нет

    await _publish(bot, session, family, text, markup, now)
    if running is not None and family.panel_message_id is not None:
        running.card_message_id = family.panel_message_id
        await session.commit()


async def _publish(
    bot: Bot,
    session: AsyncSession,
    family: Family,
    text: str,
    markup: InlineKeyboardMarkup | None,
    now: datetime,
) -> None:
    previous = family.panel_message_id
    status, message_id = await sending.send(
        bot, family, text, silent=True, reply_markup=markup, session=session
    )
    if status != sending.OK or message_id is None:
        if status == sending.FORBIDDEN and previous is not None:
            # Бота выгнали: старый id стал мусором. Вернут в чат — заведём новую
            await repo.set_panel(session, family, None, None)
        elif status in (sending.BROKEN, sending.MOVED) and previous is not None:
            # Текст не принимают — повтор даст ровно ту же ошибку. Помечаем день
            # отработанным, иначе тикер, видя вчерашний `panel_day`, будет слать
            # заведомо битое каждую минуту до полуночи. `MOVED` сюда же: чат
            # уехал, а перевезти семью не вышло — до вмешательства человека
            # писать в него бессмысленно
            await repo.set_panel(
                session, family, previous, tu.local_today(family.tz, now)
            )
        # RETRY здесь не помечаем: сеть и флуд-контроль проходят, и следующий
        # тик обязан попробовать снова
        return

    # Пишем в базу ДО закрепления. Если прав админа нет, панель обязана остаться
    # незакреплённой, а не выпускаться заново на каждой правке
    await repo.set_panel(session, family, message_id, tu.local_today(family.tz, now))
    log.info("Панель выпущена заново: #%s вместо #%s", message_id, previous)
    if previous is not None:
        await _pin(bot, family, previous, pin=False)
    await _pin(bot, family, message_id, pin=True)


async def _pin(bot: Bot, family: Family, message_id: int, *, pin: bool) -> None:
    """Закрепление — украшение: без прав админа панель просто живёт внизу чата.

    Ловим всё подряд намеренно. «not enough rights to pin a message»,
    «message to unpin not found», сетевой сбой — различать их здесь незачем,
    реакция одна: записать в лог и работать дальше.
    """
    try:
        if pin:
            await bot.pin_chat_message(
                family.chat_id, message_id, disable_notification=True
            )
        else:
            # message_id только по ключу: позиционно он уйдёт в
            # business_connection_id (сигнатура aiogram 3.31)
            await bot.unpin_chat_message(family.chat_id, message_id=message_id)
    except Exception:
        log.info(
            "Панель в чате %s осталась незакреплённой", family.chat_id, exc_info=True
        )
