"""Прямой ввод времени и безопасное окончание конкретного таймера.

Роутер ниже alarm, мастера baby и явного вопроса qa, выше числового fallback.
Голое время подходит только после запроса тому же пользователю в том же чате.
"""

import logging
from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, ForceReply, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import (
    TIMED_KINDS,
    WEIGHT,
    Child,
    Event,
    Family,
    Measurement,
    Member,
    ProcessedMessage,
)
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.keyboards import TimeCB
from bot.services import panel, pending, records, replay, revisions, time_input, tracker_guard
from bot.services import timeutil as tu

log = logging.getLogger(__name__)
router = Router(name="timing")


def snapshot(row: Event | Measurement) -> str:
    return revisions.snapshot(row)


async def get_row(
    session: AsyncSession, child: Child, row_id: int, what: str = "e"
) -> Event | Measurement | None:
    row: Event | Measurement | None
    if what == "m":
        row = await session.get(Measurement, row_id, populate_existing=True)
    else:
        row = await session.get(Event, row_id, populate_existing=True)
    return row if row is not None and row.child_id == child.id else None


async def ask_time(
    call: CallbackQuery,
    session: AsyncSession,
    family: Family,
    child: Child,
    row_id: int,
    field: str,
    what: str = "e",
) -> None:
    row = await get_row(session, child, row_id, what)
    if row is None:
        await call.answer(texts.RECORD_GONE, show_alert=True)
        return
    if isinstance(row, Measurement):
        if field != "t" or (row.kind == WEIGHT and row.taken_at == child.born_at):
            await call.answer(texts.RECORD_BIRTH_WEIGHT, show_alert=True)
            return
        target, label = pending.TIME_POINT, "время измерения"
        current = row.taken_at
    else:
        if field not in {"s", "e"} or (field == "e" and row.kind not in TIMED_KINDS):
            await call.answer("Для этой записи время меняется через историю.", show_alert=True)
            return
        target = pending.TIME_START if field == "s" else pending.TIME_END
        label = "время начала" if field == "s" else "время окончания"
        current = row.started_at if field == "s" else row.ended_at
    if not isinstance(call.message, Message):
        await call.answer()
        return
    date_hint = ""
    if current is not None:
        local = tu.to_local(current, family.tz)
        date_hint = f" Дата записи: {local:%d.%m.%Y}."
    prompt = await call.message.answer(
        f"Напишите {label}: например, 14:35 или «20 минут назад».{date_hint} "
        "Другой день: «вчера в 23:50» или «2026-10-26 14:35». Для выхода — «отмена».",
        reply_markup=ForceReply(selective=False, input_field_placeholder="14:35"),
        reply_to_message_id=call.message.message_id,
    )
    pending.remember(
        family.chat_id,
        call.from_user.id,
        target,
        row.id,
        snapshot=snapshot(row),
        prompt_id=prompt.message_id,
    )
    await call.answer()


async def render_card(
    bot: Bot,
    family: Family,
    event: Event,
    *,
    fallback: Message | None = None,
    controls: bool = True,
    session: AsyncSession | None = None,
) -> bool:
    """Обновить прежнюю карточку. Сбой Telegram не отменяет сохранённую запись."""
    text = texts.finished(event, family.tz)
    if event.ended_at is None:
        text = texts.started(event, family.tz)
    markup = (
        keyboards.stopped(event.id)
        if event.ended_at
        else keyboards.running(event.kind, row_id=event.id)
    )
    if not controls:
        markup = None
    if event.card_message_id is not None:
        try:
            await bot.edit_message_text(
                text,
                chat_id=family.chat_id,
                message_id=event.card_message_id,
                reply_markup=markup,
            )
            return True
        except TelegramAPIError as exc:
            description = str(exc).lower()
            if "message is not modified" in description:
                return True
            if "not found" not in description and "can't be edited" not in description:
                log.info("Временный сбой обновления карточки #%s", event.card_message_id)
                return False
    if fallback is not None:
        receipt = await fallback.answer(text, reply_markup=markup)
        if session is not None:
            event.card_message_id = receipt.message_id
            await session.commit()
    return False


@router.callback_query(IN_GROUP_CB, TimeCB.filter())
async def tap(
    call: CallbackQuery,
    callback_data: TimeCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    child: Child | None,
    member: Member | None = None,
) -> None:
    if child is None:
        await call.answer(texts.NO_CHILD, show_alert=True)
        return
    async with tracker_guard.for_child(child.id):
        data = callback_data
        if data.what not in {"e", "m"}:
            await call.answer()
            return
        if data.action == "ask":
            await ask_time(call, session, family, child, data.row_id, data.field, data.what)
            return
        row = await get_row(session, child, data.row_id)
        if data.action != "stop" or not isinstance(row, Event) or row.kind not in TIMED_KINDS:
            await call.answer(texts.RECORD_GONE, show_alert=True)
            return
        if row.ended_at is not None:
            await call.answer("Эта запись уже завершена")
            return
        now = tu.now_utc()
        if row.started_at >= now:
            await call.answer("Проверьте время начала записи", show_alert=True)
            return
        with revisions.actor(member.id if member else None):
            await repo.close_event(session, row, now)
        panel.schedule(bot, family.id)
        try:
            await call.answer("Записал")
        except TelegramAPIError:
            log.info("Подтверждение кнопки недоступно, запись сохранена")
        fallback = call.message if isinstance(call.message, Message) else None
        await render_card(bot, family, row, fallback=fallback, session=session)


class IsTimeInput(BaseFilter):
    async def __call__(self, message: Message, family: Family | None = None) -> bool:
        if family is None or message.from_user is None or not message.text:
            return False
        value = time_input.clean(message.text)
        if value.startswith("/"):
            return False
        if time_input.finish_request(value) or time_input.INTERVAL.fullmatch(value):
            return True
        ask = pending.peek(family.chat_id, message.from_user.id)
        return bool(
            ask
            and ask.what in pending.TIME_KINDS
            and (time_input.looks_like_time(value) or value in {"отмена", "отменить"})
        )


async def save_time(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    family: Family,
    child: Child,
    ask: pending.Ask,
    value: str,
    now: datetime,
) -> None:
    what = "m" if ask.what == pending.TIME_POINT else "e"
    row = await get_row(session, child, ask.row_id or 0, what)
    if row is None:
        pending.forget(family.chat_id, message.from_user.id)
        raise time_input.TimeInputError("Запись уже удалена. Откройте актуальную историю.")
    if ask.snapshot is not None and snapshot(row) != ask.snapshot:
        pending.forget(family.chat_id, message.from_user.id)
        raise time_input.TimeInputError(
            "Запись уже изменена — возможно, вторым родителем. "
            "Откройте «Изменить» ещё раз; новые данные не перезаписаны."
        )
    if isinstance(row, Measurement):
        if row.kind == WEIGHT and row.taken_at == child.born_at:
            raise time_input.TimeInputError(texts.RECORD_BIRTH_WEIGHT)
        result = time_input.moment(
            value,
            now=now,
            tz=family.tz,
            lower=child.born_at - timedelta(microseconds=1),
            upper=now,
            reference=row.taken_at,
        )
        session.add(ProcessedMessage(chat_id=family.chat_id, message_id=message.message_id))
        await repo.update_measurement(session, row, taken_at=result)
        panel.schedule(bot, family.id, message.message_id)
        pending.forget(family.chat_id, message.from_user.id)
        await message.answer(texts.record_card(row, family.tz, now))
    else:
        if row.kind not in TIMED_KINDS and ask.what != pending.TIME_START:
            raise time_input.TimeInputError("Вид записи изменился. Откройте её ещё раз.")
        if ask.what == pending.TIME_START:
            result = time_input.moment(
                value,
                now=now,
                tz=family.tz,
                lower=child.born_at - timedelta(microseconds=1),
                upper=(row.ended_at - timedelta(microseconds=1)) if row.ended_at else now,
                reference=row.started_at,
            )
            start, end = result, row.ended_at
        else:
            result = time_input.moment(
                value,
                now=now,
                tz=family.tz,
                lower=row.started_at,
                upper=now,
                reference=row.ended_at,
            )
            start, end = row.started_at, result
        if not records.fits(start, end, born_at=child.born_at, now=now):
            raise time_input.TimeInputError("Проверьте начало и окончание записи.")
        session.add(ProcessedMessage(chat_id=family.chat_id, message_id=message.message_id))
        await repo.set_event_time(
            session,
            row,
            started_at=start,
            ended_at=end,
            estimated=False if ask.what == pending.TIME_END else row.estimated,
        )
        panel.schedule(bot, family.id, message.message_id)
        pending.forget(family.chat_id, message.from_user.id)
        if row.kind in TIMED_KINDS:
            await render_card(bot, family, row, controls=False)
            summary = texts.finished(row, family.tz) if end else texts.started(row, family.tz)
        else:
            summary = texts.logged(row, family.tz)
        markup = (
            keyboards.running(row.kind, row_id=row.id)
            if (row.kind in TIMED_KINDS and row.ended_at is None)
            else keyboards.stopped(row.id)
        )
        receipt = await message.answer(summary, reply_markup=markup)
        row.card_message_id = receipt.message_id
        await session.commit()


@router.message(IN_GROUP, IsTimeInput())
async def reply(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    family: Family,
    member: Member,
    child: Child | None,
) -> None:
    if child is None:
        await message.answer(texts.NO_CHILD)
        return
    with revisions.actor(member.id):
        async with tracker_guard.for_child(child.id):
            now = replay.message_moment(message, tu.now_utc())
            processed = await session.get(ProcessedMessage, (family.chat_id, message.message_id))
            if processed is not None:
                panel.schedule(bot, family.id, message.message_id)
                await message.answer("Это сообщение уже записано. Повторно ничего не менял.")
                return
            value = time_input.clean(message.text or "")
            ask = pending.peek(family.chat_id, message.from_user.id)
            finished = time_input.finish_request(value)
            try:
                if (
                    ask is not None
                    and ask.what in pending.TIME_KINDS
                    and (finished is None or ask.what == pending.TIME_END)
                ):
                    if (
                        message.reply_to_message
                        and ask.prompt_id is not None
                        and (message.reply_to_message.message_id != ask.prompt_id)
                    ):
                        await message.answer(
                            "Это ответ на другой вопрос. Ответьте на последний запрос времени."
                        )
                        return
                    if value in {"отмена", "отменить"}:
                        pending.forget(family.chat_id, message.from_user.id)
                        await message.answer("Отменено. Запись не менял.")
                        return
                    if finished:
                        target = await get_row(session, child, ask.row_id or 0)
                        if not isinstance(target, Event) or target.kind != finished[0]:
                            raise time_input.TimeInputError(
                                "Этот запрос относится к другому виду записи. "
                                "Для выхода напишите «отмена»."
                            )
                        value = finished[1]
                    await save_time(message, bot, session, family, child, ask, value, now)
                    return
                finished = time_input.finish_request(value)
                if finished:
                    kind, time_text = finished
                    event = await repo.any_open_event(session, child.id)
                    if event is None or event.kind != kind:
                        raise time_input.TimeInputError(
                            "Такого активного таймера нет. Если запись уже завершена, "
                            "нажмите под ней «Изменить» и выберите окончание."
                        )
                    await session.refresh(event)
                    request = pending.Ask(pending.TIME_END, event.id, now, snapshot(event))
                    # При неоднозначном времени сохраняем контекст для короткого уточнения.
                    pending.remember(
                        family.chat_id,
                        message.from_user.id,
                        pending.TIME_END,
                        event.id,
                        snapshot=snapshot(event),
                    )
                    await save_time(message, bot, session, family, child, request, time_text, now)
                    return
                parsed = time_input.interval(value, now=now, tz=family.tz, born_at=child.born_at)
                if parsed is None:
                    return
                # Открытый таймер не закрываем задним числом молча и не создаём дубль.
                if await repo.any_open_event(session, child.id) is not None:
                    raise time_input.TimeInputError(
                        "Сейчас идёт таймер. Сначала завершите его или исправьте через «Изменить»."
                    )
                duplicate = await session.scalar(
                    select(Event)
                    .where(
                        Event.child_id == child.id,
                        Event.kind == parsed.kind,
                        Event.side == parsed.side,
                        Event.started_at == parsed.started_at,
                        Event.ended_at == parsed.ended_at,
                    )
                    .limit(1)
                )
                if duplicate is not None:
                    await message.answer("Такой интервал уже записан — дубль не добавлял.")
                    return
                session.add(ProcessedMessage(chat_id=family.chat_id, message_id=message.message_id))
                event = await repo.add_event(
                    session,
                    child.id,
                    member.id,
                    parsed.kind,
                    side=parsed.side,
                    started_at=parsed.started_at,
                    ended_at=parsed.ended_at,
                    source_chat_id=family.chat_id,
                    source_message_id=message.message_id,
                )
                panel.schedule(bot, family.id, message.message_id)
                receipt = await message.answer(
                    texts.finished(event, family.tz), reply_markup=keyboards.stopped(event.id)
                )
                event.card_message_id = receipt.message_id
                await session.commit()
            except time_input.TimeInputError as exc:
                await message.answer(str(exc))
