"""Сводки, графики и этап развития: `/today`, `/week`, `/chart`, `/stage`."""

import logging
from datetime import timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db.models import Child, Family
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.keyboards import StatsCB
from bot.services import charts, stage, stats
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

router = Router(name="views")


async def _today(session: AsyncSession, family: Family, child: Child) -> str:
    span = await stats.day(session, child, family.tz, now=tu.now_utc())
    return texts.day_summary_from_span(child, span, "сегодня")


async def _week(session: AsyncSession, family: Family, child: Child) -> str:
    now = tu.now_utc()
    total, days = await stats.week(session, child, family.tz, now=now)
    return texts.week_summary(child, total, days)


async def _month(session: AsyncSession, family: Family, child: Child) -> str:
    """Последние 30 дней. У ребёнка младше месяца — столько, сколько он прожил.

    Иначе среднее делилось бы на 30 при неделе данных и занижалось вчетверо —
    ровно то место, где родители решили бы, что кормят слишком редко.
    """
    now = tu.now_utc()
    today = tu.local_today(family.tz, now)
    days = min(30, max(1, (now - child.born_at).days + 1))
    start, _ = tu.day_bounds(today - timedelta(days=days - 1), family.tz)
    _, end = tu.day_bounds(today, family.tz)
    span = await stats.collect(session, child, start, end, now)
    return texts.average_summary(child, span, days)


PERIODS = {"today": _today, "week": _week, "month": _month}


async def send_today(
    message: Message, session: AsyncSession, family: Family, child: Child | None
) -> None:
    """Сводка за сутки одним сообщением, с переключателем периода.

    Вызывающих двое: команда `/today` и кнопка «📊 Сутки» на постоянной
    клавиатуре (`handlers/home.py`). Вторая копия отличалась бы от первой
    молча — например, потеряв `keyboards.stats()`, и «Неделя» с «Месяцем»
    перестали бы существовать для того, кто пришёл кнопкой.
    """
    if child is None:
        await message.answer(texts.NO_CHILD)
        return
    await message.answer(
        await _today(session, family, child), reply_markup=keyboards.stats()
    )


@router.message(IN_GROUP, Command("today"))
async def today(
    message: Message, session: AsyncSession, family: Family, child: Child | None
) -> None:
    await send_today(message, session, family, child)


@router.message(IN_GROUP, Command("week"))
async def week(
    message: Message, session: AsyncSession, family: Family, child: Child | None
) -> None:
    if child is None:
        await message.answer(texts.NO_CHILD)
        return
    await message.answer(
        await _week(session, family, child), reply_markup=keyboards.stats()
    )


@router.message(IN_GROUP, Command("chart"))
async def chart(
    message: Message, session: AsyncSession, family: Family, child: Child | None
) -> None:
    """Оба графика по требованию.

    Отправляем по одному, а не альбомом: у альбома одна подпись на всю группу,
    а подписать надо каждый — иначе непонятно, что на картинке.
    """
    if child is None:
        await message.answer(texts.NO_CHILD)
        return

    sent = await send_charts(message, session, family, child)
    if not sent:
        await message.answer(texts.CHART_EMPTY)


async def send_charts(
    message: Message, session: AsyncSession, family: Family, child: Child
) -> bool:
    """Отправить графики, которые есть. `False` — не было ни одного."""
    now = tu.now_utc()
    sent = False

    weight = await charts.weight_png(session, child, now)
    if weight is not None:
        await message.answer_photo(
            BufferedInputFile(weight, "weight.png"),
            caption=f"Вес · {child.name} · коридоры ВОЗ",
        )
        sent = True

    rhythm = await charts.rhythm_png(session, child, family, now)
    if rhythm is not None:
        await message.answer_photo(
            BufferedInputFile(rhythm, "rhythm.png"),
            caption="Ритм суток за неделю",
        )
        sent = True

    return sent


@router.callback_query(IN_GROUP_CB, StatsCB.filter())
async def switch_period(
    call: CallbackQuery,
    callback_data: StatsCB,
    session: AsyncSession,
    family: Family,
    child: Child | None,
) -> None:
    """Переключение периода правит то же сообщение, а не шлёт новое.

    Иначе три тапа по «Сутки / Неделя / Месяц» оставляли бы в чате три почти
    одинаковых простыни, между которыми теряется панель.
    """
    if child is None:
        await call.answer(texts.NO_CHILD, show_alert=True)
        return

    builder = PERIODS.get(callback_data.period)
    if builder is None:
        await call.answer()
        return

    text = await builder(session, family, child)
    await call.answer()
    if not isinstance(call.message, Message):
        return
    try:
        await call.message.edit_text(text, reply_markup=keyboards.stats())
    except Exception:
        # «message is not modified» при повторном тапе по тому же периоду —
        # самый частый случай, и он не стоит ни строчки в логе выше DEBUG
        log.debug("Сводку не перерисовали", exc_info=True)


@router.message(IN_GROUP, Command("stage"))
async def stage_now(message: Message, child: Child | None) -> None:
    """Что происходит на этой неделе: развитие, занятия, вехи.

    Отдельной командой, а не внутри `/week`: там статистика за прошедшее, а
    здесь — про то, что сейчас и дальше. Смешивать их значит, что читать не
    станут ни то, ни другое.
    """
    if child is None:
        await message.answer(texts.NO_CHILD)
        return
    await message.answer(stage.report(child, tu.now_utc()) or texts.STAGE_OVER)
