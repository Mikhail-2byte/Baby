"""Кнопки записи на панели — главный путь ввода данных.

Всё здесь подчинено одному требованию: один тап, ночью, одной рукой, не глядя.
Отсюда несколько решений, которые иначе выглядели бы странно:

* тап по «начали», когда таймер уже идёт, не ругается, а закрывает предыдущий
  и открывает новый — потому что чаще всего это и имелось в виду;
* у каждой записи есть кнопка отмены: промах по соседней кнопке в темноте —
  обычное дело, а лишнее кормление заметно искажает неделю;
* ответ человеку уходит раньше, чем перерисовывается панель.
"""

import logging
from datetime import datetime

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import (
    DIAPER_KINDS,
    FEED_BOTTLE,
    PUMP,
    TIMED_KINDS,
    Child,
    Family,
    Member,
)
from bot.filters import IN_GROUP_CB
from bot.keyboards import PanelCB, TrackCB, UndoCB
from bot.services import panel, pending
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

router = Router(name="track")

# Что спросить после мгновенной записи. Пусто — значит ничего не спрашиваем,
# и тап так и остаётся одним тапом
ASK_AFTER = {FEED_BOTTLE: pending.BOTTLE_ML}

ASK_TEXT = {
    pending.BOTTLE_ML: "Сколько мл? Можно просто числом. Или не отвечайте — запишу без объёма.",
    pending.PUMP_ML: "Сколько мл сцедили? Можно просто числом.",
}


def _last_id(call: CallbackQuery) -> int | None:
    """message_id, по которому панель судит, далеко ли она уехала."""
    return call.message.message_id if isinstance(call.message, Message) else None


async def _say(
    call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Ответить в чат отдельным сообщением.

    Именно отдельным, а не всплывашкой: подтверждение должно остаться в ленте,
    чтобы второй родитель видел, что происходило ночью, не открывая статистику.

    `markup or keyboards.home()`: подтверждение записи несёт свою кнопку отмены,
    а вопрос «Сколько мл?» — ничего, и именно он становится носителем
    клавиатуры. Одно сообщение обе разметки не носит, поэтому выбор, а не сумма.
    """
    if isinstance(call.message, Message):
        await call.message.answer(text, reply_markup=markup or keyboards.home())


@router.callback_query(IN_GROUP_CB, TrackCB.filter())
async def track(
    call: CallbackQuery,
    callback_data: TrackCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child | None,
) -> None:
    if child is None:
        await call.answer(texts.NO_CHILD, show_alert=True)
        return

    now = tu.now_utc()
    handlers = {"start": _start, "stop": _stop, "add": _add}
    handler = handlers.get(callback_data.action)
    if handler is None:
        # Кнопка из будущей версии бота или испорченный callback_data.
        # Молчать нельзя: Telegram крутит «часики» до ответа
        await call.answer()
        return

    await handler(call, callback_data, session, family, member, child, now)
    panel.schedule(bot, family.id, _last_id(call))


async def _start(
    call: CallbackQuery,
    data: TrackCB,
    session: AsyncSession,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    if data.kind not in TIMED_KINDS:
        await call.answer()
        return

    # Идущий таймер закрываем, а не отказываем. Отказ («сначала закончите
    # предыдущее») в три часа ночи означает, что человек нажмёт ещё раз,
    # не поймёт, почему ничего не происходит, и бросит запись вовсе.
    #
    # И говорим об этом вслух. Раньше кнопка закрывала молча, и в докстроке
    # стояло почему: «панель меняется под пальцем, закрытие видно сразу». Довод
    # сломался вместе с интерфейсом — с клавиатурой под полем ввода панель чаще
    # всего выше по чату. Молчание превратилось в тихую потерю: промахнувшись по
    # «начали» при идущем таймере, человек получал закрытый «сейчас» предыдущий
    # и отменить мог только новый
    closed = await repo.close_running(session, child.id, now)

    event = await repo.start_event(
        session,
        child.id,
        member.id,
        data.kind,
        started_at=now,
        side=data.side or None,
        source_chat_id=family.chat_id,
        source_message_id=_last_id(call),
    )
    await call.answer("Пошло время")
    if closed is not None:
        await _say(call, texts.closed_previous(closed, family.tz))
    # «Закончить» рядом с «Отменить», а не только в панели: в эту секунду
    # человек смотрит на подтверждение, а панель уже выше по чату. И оно идёт
    # последним, чтобы кнопка окончания осталась под рукой
    await _say(
        call, texts.started(event, family.tz), keyboards.running(data.kind, row_id=event.id)
    )


async def _stop(
    call: CallbackQuery,
    data: TrackCB,
    session: AsyncSession,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    running = await repo.any_open_event(session, child.id)
    if running is None:
        # Обычно это двойной тап или нажатие по устаревшей панели: таймер уже
        # закрыт, и говорить об этом громко незачем
        await call.answer("Таймер уже закрыт")
        return

    await repo.close_event(session, running, now)
    await call.answer("Записал")
    await _say(
        call, texts.finished(running, family.tz), keyboards.stopped(running.id)
    )

    if running.kind == PUMP:
        pending.remember(
            family.chat_id, call.from_user.id, pending.PUMP_ML, running.id
        )
        await _say(call, ASK_TEXT[pending.PUMP_ML])


async def _add(
    call: CallbackQuery,
    data: TrackCB,
    session: AsyncSession,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    if data.kind not in DIAPER_KINDS and data.kind != FEED_BOTTLE:
        await call.answer()
        return

    event = await repo.add_event(
        session,
        child.id,
        member.id,
        data.kind,
        started_at=now,
        source_chat_id=family.chat_id,
        source_message_id=_last_id(call),
    )
    await call.answer("Записал")
    await _say(call, texts.logged(event, family.tz), keyboards.undo("event", event.id))

    what = ASK_AFTER.get(data.kind)
    if what is not None:
        # Запись уже сделана и без числа. Вопрос — предложение уточнить, а не
        # обязательный шаг: промолчать здесь совершенно нормально
        pending.remember(family.chat_id, call.from_user.id, what, event.id)
        await _say(call, ASK_TEXT[what])


@router.callback_query(IN_GROUP_CB, UndoCB.filter())
async def undo(
    call: CallbackQuery,
    callback_data: UndoCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    child: Child | None,
) -> None:
    """Отменить только что сделанное: удалить запись или вернуть таймеру ход.

    Ограничения по времени нет намеренно: кнопка живёт под своим сообщением, и
    нажать её можно только у той записи, о которой бот отчитался.

    `reopen` — отдельная ветка, а не частный случай удаления, и разница тут не
    косметическая. Промах по «Закончить» означает «сон продолжается», а не
    «сна не было»; до этой ветки под ответом об окончании стояла одна кнопка
    отмены, и она стирала весь сон целиком — то есть лечила промах потерей.
    """
    if child is None:
        await call.answer(texts.NO_CHILD, show_alert=True)
        return

    if callback_data.what == "reopen":
        await _reopen(call, callback_data.row_id, session, bot, family, child)
        return

    # `row_id` приходит с клиента, а id сквозные по всей базе: без сверки с
    # ребёнком чат, знающий номер, стирал бы чужой дневник. Долго это было
    # почти теорией — пока экран правки не начал раздавать номера записей в
    # чат пачками
    if callback_data.what == "event":
        event = await repo.get_event(session, callback_data.row_id)
        found = event is not None and event.child_id == child.id
        if found and event is not None:
            await repo.delete_event(session, event)
    else:
        measurement = await repo.get_measurement(session, callback_data.row_id)
        found = measurement is not None and measurement.child_id == child.id
        if found and measurement is not None:
            await repo.delete_measurement(session, measurement)

    if not found:
        # Запись уже удалили — скорее всего вторым тапом по той же кнопке
        await call.answer(texts.NOTHING_TO_UNDO, show_alert=True)
        return

    await call.answer(texts.UNDONE)
    if isinstance(call.message, Message):
        # Убираем кнопку, чтобы второй тап не спрашивал про уже удалённое
        await call.message.edit_reply_markup(reply_markup=None)
    panel.schedule(bot, family.id, _last_id(call))


async def _reopen(
    call: CallbackQuery,
    row_id: int,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    child: Child,
) -> None:
    """Вернуть ход закрытому таймеру.

    Инвариант «ровно один открытый таймер» проверяется здесь, а не в `repo`:
    молча закрыть чужой таймер ради этого нельзя — человек просил вернуть один,
    а не оборвать другой. Случай редкий, но достижимый: закончили сон, начали
    кормление, спохватились про сон.
    """
    event = await repo.get_event(session, row_id)
    if event is None or event.child_id != child.id or event.ended_at is None:
        # Запись уже удалили или таймер уже вернули — второй тап по той же кнопке
        await call.answer(texts.NOTHING_TO_UNDO, show_alert=True)
        return

    if await repo.any_open_event(session, event.child_id) is not None:
        await call.answer(texts.ANOTHER_TIMER_RUNS, show_alert=True)
        return

    await repo.reopen_event(session, event)
    await call.answer(texts.REOPENED)
    if isinstance(call.message, Message):
        await call.message.edit_reply_markup(reply_markup=None)
    panel.schedule(bot, family.id, _last_id(call))


@router.callback_query(IN_GROUP_CB, PanelCB.filter())
async def refresh(
    call: CallbackQuery, callback_data: PanelCB, bot: Bot, family: Family
) -> None:
    await call.answer()
    panel.schedule(bot, family.id, _last_id(call))
