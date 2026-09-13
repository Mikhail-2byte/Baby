"""Сводки: `/today`, `/week` и кнопки периодов на панели."""

import logging
from datetime import timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db.models import Child, Family
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.keyboards import StatsCB
from bot.services import stats
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

router = Router(name="views")


def _render(child: Child, span: stats.Span, label: str) -> str:
    return texts.day_summary(
        child,
        label,
        feeds=span.feeds,
        feed_minutes=span.feed_minutes,
        bottle_ml=span.bottle_ml,
        sleep_total=span.sleep_time,
        sleep_count=span.sleep_count,
        wet=span.wet,
        dirty=span.dirty,
        longest_sleep=span.longest_sleep,
    )


async def _today(session: AsyncSession, family: Family, child: Child) -> str:
    now = tu.now_utc()
    span = await stats.day(session, child, family.tz, now=now)
    return _render(child, span, "сегодня")


async def _week(session: AsyncSession, family: Family, child: Child) -> str:
    """Неделя: итог плюс построчная разбивка по дням.

    Построчно, а не графиком: графики — этап 3, а разбивка нужна уже сейчас,
    чтобы видеть, что вчерашняя бессонная ночь была именно вчерашней.
    """
    now = tu.now_utc()
    total, days = await stats.week(session, child, family.tz, now=now)
    lines = [_render(child, total, "неделя"), "", "<b>По дням:</b>"]
    for day, span in days:
        sleep = texts.duration(span.sleep_time) if span.sleep_time else "—"
        lines.append(
            f"{tu.WEEKDAYS_SHORT[day.weekday()]} {tu.day_stamp(day)}: "
            f"🍼 {span.feeds} · 😴 {sleep} · 💧 {span.wet}/{span.dirty}"
        )
    return "\n".join(lines)


async def _month(session: AsyncSession, family: Family, child: Child) -> str:
    """Последние 30 дней — среднее за сутки.

    Именно среднее, а не сумма: «812 кормлений за месяц» не говорит ни о чём,
    а «8 кормлений в сутки» сравнимо с нормой и с прошлым месяцем.
    """
    now = tu.now_utc()
    today = tu.local_today(family.tz, now)
    start, _ = tu.day_bounds(today - timedelta(days=29), family.tz)
    _, end = tu.day_bounds(today, family.tz)
    span = await stats.collect(session, child, start, end, now)

    days = max(1, (now - child.born_at).days + 1)
    days = min(days, 30)
    return (
        f"📊 <b>{child.name}, последние {days} "
        f"{texts.plural(days, 'день', 'дня', 'дней')}</b>\n\n"
        f"🍼 Кормления: {span.feeds / days:.1f} в сутки\n"
        f"😴 Сон: {texts.duration(span.sleep_time / days)} в сутки\n"
        f"💧 Подгузники: {span.wet / days:.1f} мокрых, "
        f"{span.dirty / days:.1f} со стулом в сутки"
    )


PERIODS = {"today": _today, "week": _week, "month": _month}


@router.message(IN_GROUP, Command("today"))
async def today(
    message: Message, session: AsyncSession, family: Family, child: Child | None
) -> None:
    if child is None:
        await message.answer(texts.NO_CHILD)
        return
    await message.answer(
        await _today(session, family, child), reply_markup=keyboards.stats()
    )


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
