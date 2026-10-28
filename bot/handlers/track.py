"""Старт и смена таймера. Одна карточка, переход с подтверждением.

Смена груди привязана к снимку записи; переход между видами таймера требует
подтверждения. После сохранения обновление единственной карточки делает panel.
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
    FEED_BREAST,
    LEFT,
    PUMP,
    RIGHT,
    TIMED_KINDS,
    Child,
    Event,
    Family,
    Member,
)
from bot.filters import IN_GROUP_CB
from bot.keyboards import PanelCB, TimerCB, TrackCB, UndoCB
from bot.services import panel, pending, revisions, tracker_guard
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
) -> Message | None:
    """Ответить в чат отдельным сообщением.

    Именно отдельным, а не всплывашкой: подтверждение должно остаться в ленте,
    чтобы второй родитель видел, что происходило ночью, не открывая статистику.

    `markup or keyboards.home()`: подтверждение записи несёт свою кнопку отмены,
    а вопрос «Сколько мл?» — ничего, и именно он становится носителем
    клавиатуры. Одно сообщение обе разметки не носит, поэтому выбор, а не сумма.
    """
    if isinstance(call.message, Message):
        return await call.message.answer(text, reply_markup=markup or keyboards.home())
    return None


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

    family_id = family.id
    try:
        with revisions.actor(member.id):
            async with tracker_guard.for_child(child.id):
                await handler(call, callback_data, session, family, member, child, now)
    finally:
        panel.schedule(bot, family_id, _last_id(call))


async def _start(
    call: CallbackQuery,
    data: TrackCB,
    session: AsyncSession,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    if data.kind not in TIMED_KINDS or data.side not in {"", LEFT, RIGHT}:
        await call.answer()
        return

    current = await repo.any_open_event(session, child.id)
    if current is not None and current.kind == data.kind:
        if current.side != (data.side or None) and data.kind == FEED_BREAST:
            await call.answer("Смените грудь на актуальной карточке таймера", show_alert=True)
            return
        await call.answer("Этот таймер уже идёт")
        return
    if current is not None:
        await call.answer()
        await _say(call, f"Завершить текущий таймер и начать {texts.KIND_NAMES[data.kind]}?",
                   keyboards.timer_transition(current.id, data.kind, data.side,
                                              revisions.timer_stamp(current)))
        return
    await _begin(call, data.kind, data.side, session, family, member, child, now)


async def _begin(
    call: CallbackQuery, kind: str, side: str, session: AsyncSession,
    family: Family, member: Member, child: Child, now: datetime,
) -> None:
    async with repo.atomic(session):
        await repo.close_running(session, child.id, now)
        await repo.start_event(
            session, child.id, member.id, kind, started_at=now,
            side=(side or None) if kind in {FEED_BREAST, PUMP} else None,
            source_chat_id=family.chat_id, source_message_id=_last_id(call),
        )
    await call.answer("Таймер начался")


@router.callback_query(IN_GROUP_CB, TimerCB.filter())
async def timer(
    call: CallbackQuery, callback_data: TimerCB, session: AsyncSession,
    bot: Bot, family: Family, member: Member, child: Child | None,
) -> None:
    if child is None:
        await call.answer(texts.NO_CHILD, show_alert=True)
        return
    data = callback_data
    family_id = family.id
    try:
        async with tracker_guard.for_child(child.id):
            row = await session.get(Event, data.row_id, populate_existing=True)
            current = await repo.any_open_event(session, child.id)
            if (row is None or row.child_id != child.id or row.ended_at is not None
                    or current is None or current.id != row.id
                    or row.kind not in TIMED_KINDS
                    or (data.stamp and data.stamp != revisions.timer_stamp(row))):
                await call.answer("Таймер изменился. Откройте актуальную панель.", show_alert=True)
                return
            if data.action == "menu":
                await call.answer()
                await _say(call, "Действия с таймером", keyboards.timer_menu(
                    row.id, row.kind, revisions.timer_stamp(row),
                ))
                return
            if data.action == "cancel":
                await call.answer("Таймер продолжается")
                if isinstance(call.message, Message):
                    await call.message.edit_text("Таймер продолжается", reply_markup=None)
                return
            if (data.action not in {"switch", "confirm"} or not data.stamp
                    or data.kind not in TIMED_KINDS
                    or data.side not in (
                        {LEFT, RIGHT} if data.kind == FEED_BREAST else
                        {"", LEFT, RIGHT} if data.kind == PUMP else {""}
                    )):
                await call.answer("Откройте актуальную панель", show_alert=True)
                return
            if row.kind == data.kind and row.side == (data.side or None):
                await call.answer("Этот таймер уже идёт")
                return
            if data.action == "switch" and row.kind != data.kind:
                await call.answer()
                await _say(
                    call, f"Завершить текущий таймер и начать {texts.KIND_NAMES[data.kind]}?",
                    keyboards.timer_transition(row.id, data.kind, data.side, data.stamp),
                )
                return
            now = tu.now_utc()
            if now <= row.started_at:
                await call.answer("Подождите секунду после старта таймера")
                return
            with revisions.actor(member.id):
                await _begin(call, data.kind, data.side, session, family, member, child, now)
            if data.action == "confirm" and isinstance(call.message, Message):
                await call.message.edit_text("Переход выполнен", reply_markup=None)
    finally:
        panel.schedule(bot, family_id)


async def _stop(
    call: CallbackQuery,
    data: TrackCB,
    session: AsyncSession,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    # Старый TrackCB не содержит id. Не даём вчерашней кнопке закрыть новый таймер.
    await call.answer(
        "Эта старая кнопка не привязана к записи. Откройте актуальную панель.", show_alert=True
    )


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

    async with tracker_guard.for_child(child.id):
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
    from bot.handlers.timing import render_card

    await render_card(bot, family, event, session=session)
    await call.answer(texts.REOPENED)
    if isinstance(call.message, Message):
        await call.message.edit_reply_markup(reply_markup=None)
    panel.schedule(bot, family.id, _last_id(call))


@router.callback_query(IN_GROUP_CB, PanelCB.filter())
async def refresh(
    call: CallbackQuery,
    callback_data: PanelCB,
    bot: Bot,
    family: Family,
    session: AsyncSession,
) -> None:
    await call.answer()
    if callback_data.action == "more":
        await _say(call, "Редкие действия", keyboards.more())
    elif callback_data.action == "quiet":
        family.diapers_tracked = False
        await repo.set_checkin(session, family, 0, tu.now_utc())
        await _say(
            call, "Опросы отключены, подгузники не учитываются в этом чате. "
            "Вернуть опросы можно через /remind."
        )
    elif callback_data.action == "show":
        await panel.refresh(bot, family.id, force_new=True)
    else:
        panel.schedule(bot, family.id, _last_id(call))
