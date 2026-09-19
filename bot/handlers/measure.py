"""Взвешивание и ответы числом на заданный ботом вопрос.

Здесь же живёт единственный в проекте обработчик «голого числа». Он срабатывает
только тогда, когда бот сам только что спросил, — см. `services/pending.py`.
Без этого условия любое число в семейном чате («купи 2 пачки») превращалось бы
в вес ребёнка.
"""

import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import WEIGHT, Child, Family, Measurement, Member
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.keyboards import MeasureCB
from bot.services import growth, panel, pending
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

router = Router(name="measure")

# Границы правдоподобия для веса младенца, в граммах. Верхняя взята с запасом
# на весь первый год с лишним: это защита от опечатки, а не от ожирения
MIN_G = 400.0
MAX_G = 20000.0


def to_grams(value: float) -> float | None:
    """Число из реплики → граммы, с разбором «это килограммы или граммы».

    Порог 100 безопасен в обе стороны: младенец не весит 100 грамм, и никто не
    вводит вес в килограммах числом больше сотни.
    """
    grams = value * 1000 if value < 100 else value
    return grams if MIN_G <= grams <= MAX_G else None


@router.callback_query(IN_GROUP_CB, MeasureCB.filter())
async def ask_weight(
    call: CallbackQuery, callback_data: MeasureCB, family: Family, child: Child | None
) -> None:
    if child is None:
        await call.answer(texts.NO_CHILD, show_alert=True)
        return
    pending.remember(family.chat_id, call.from_user.id, pending.WEIGHT_G)
    await call.answer()
    if isinstance(call.message, Message):
        await call.message.answer(texts.ASK_WEIGHT, reply_markup=keyboards.home())


@router.message(IN_GROUP, Command("weight"))
async def weight_command(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child | None,
) -> None:
    """`/weight 3560` — записать сразу, `/weight` — спросить.

    Форма с аргументом существует ради одного сценария: вернулись от педиатра,
    цифра в голове, панель где-то выше по чату. Одна команда — и готово.
    """
    if child is None:
        await message.answer(texts.NO_CHILD, reply_markup=keyboards.home())
        return

    if not command.args:
        pending.remember(family.chat_id, message.from_user.id, pending.WEIGHT_G)
        await message.answer(texts.ASK_WEIGHT, reply_markup=keyboards.home())
        return

    value = pending.parse_number(command.args)
    if value is None:
        await message.answer(texts.BAD_WEIGHT, reply_markup=keyboards.home())
        return
    await _save_weight(message, session, bot, family, member, child, value)


@router.message(IN_GROUP, F.text)
async def number_reply(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child | None,
) -> None:
    """Голое число в ответ на вопрос бота.

    Роутер стоит последним в списке, поэтому команды и мастера до сюда не
    доходят. Если ожидания нет — молчим и не мешаем семье переписываться.
    """
    if child is None or message.from_user is None:
        return

    ask = pending.peek(family.chat_id, message.from_user.id)
    if ask is None:
        return

    value = pending.parse_number(message.text or "")
    if value is None:
        # Человек ответил словами, а не числом. Ожидание не снимаем: он мог
        # написать что-то своё, а к весам пойти через минуту
        return

    pending.take(family.chat_id, message.from_user.id)

    if ask.what == pending.WEIGHT_G:
        await _save_weight(message, session, bot, family, member, child, value)
        return

    if ask.row_id is None:
        return
    event = await repo.get_event(session, ask.row_id)
    if event is None or event.child_id != child.id:
        # Запись успели отменить кнопкой, пока человек набирал число, — или
        # номер пришёл не от этого ребёнка: id сквозные по всей базе
        return
    await repo.set_event_value(session, event, value)
    await message.answer(f"Записал: {value:g} мл", reply_markup=keyboards.home())
    panel.schedule(bot, family.id, message.message_id)


async def record_weight(
    session: AsyncSession,
    family: Family,
    member: Member,
    child: Child,
    grams: float,
    taken_at: datetime,
) -> tuple[Measurement, str]:
    """Записать взвешивание и собрать текст о нём. Ничего не отправляет.

    Вызывающих двое: ответ числом (`_save_weight` ниже) и диктовка «вес три
    двести» (`handlers/voice`). Возвращает текст, а не отправляет его, ровно
    поэтому: голосовой путь кладёт его в свою карточку, а не во второе
    сообщение.

    Собирать этот текст у голоса заново нельзя: центиль и прибавка с прошлого
    взвешивания — то, ради чего вес вообще записывают, и вторая, более бедная
    версия того же объявления разошлась бы с первой молча.

    `taken_at` параметром: сказанное вслух взвешивание могло быть утром, а не
    «сейчас». Проверку правдоподобия делает `to_grams`, и делает её один — это
    единственный вход веса в базу.
    """
    previous = await repo.last_measurement(session, child.id, WEIGHT)
    measurement = await repo.add_measurement(
        session, child.id, member.id, WEIGHT, taken_at=taken_at, value=grams
    )
    text = texts.weight_saved(
        measurement,
        family.tz,
        zscore_text=await _zscore_text(session, child, grams, taken_at),
        delta_text=_delta_text(previous, grams, taken_at),
    )
    return measurement, text


async def _save_weight(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    raw: float,
) -> None:
    grams = to_grams(raw)
    if grams is None:
        await message.answer(texts.WEIGHT_OUT_OF_RANGE, reply_markup=keyboards.home())
        return

    measurement, text = await record_weight(
        session, family, member, child, grams, tu.now_utc()
    )
    await message.answer(
        text, reply_markup=keyboards.undo("measure", measurement.id)
    )
    panel.schedule(bot, family.id, message.message_id)


async def _zscore_text(
    session: AsyncSession, child: Child, grams: float, now: datetime
) -> str | None:
    """Центиль по таблицам ВОЗ, или динамика от рождения, если таблиц ещё нет.

    Второй вариант — не заглушка: в первые дни жизни важна именно потеря массы
    от рождения (порог 10% из клинрекомендаций), а не место в центильном
    коридоре.
    """
    evaluated = await growth.evaluate(session, child, WEIGHT, grams, now)
    if evaluated is not None:
        z, _ = evaluated
        if growth.implausible(z, WEIGHT):
            return texts.WEIGHT_IMPLAUSIBLE
        return texts.percentile_str(*evaluated)

    loss = growth.loss_from_birth(child, grams)
    if loss is None:
        return None
    if loss > 0:
        return f"−{loss:.1f}% от веса при рождении"
    return f"+{-loss:.1f}% к весу при рождении"


def _delta_text(
    previous: Measurement | None, grams: float, now: datetime
) -> str | None:
    """Прибавка с прошлого взвешивания — то, что спрашивает педиатр."""
    if previous is None:
        return None
    days = (now - previous.taken_at).total_seconds() / 86400
    if days < 0.5:
        # Перевзвесили в тот же день: «+20 г за 0 дней» не значит ничего
        return None
    return texts.weight_delta(grams - previous.value, days)
