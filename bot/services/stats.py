"""Агрегация событий: сутки, неделя, тело панели.

Чистый слой между `repo` и текстами. Хендлеры не считают ничего сами — иначе
«сколько кормлений за сутки» посчиталось бы тремя разными способами в трёх
местах и разошлось бы на границе полуночи.

Главная тонкость здесь одна и стоит того, чтобы её назвать вслух: события
обрезаются по границам периода. Сон с 23:40 до 06:10 принадлежит обеим суткам,
и сутки, которые засчитывают его целиком, показывают «спала 14 часов», а
сутки, которые его не видят вовсе, — «спала 4 часа». Оба ответа неверны, и оба
выглядят правдоподобно, пока не сложишь неделю.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    FEED_BOTTLE,
    FEED_BREAST,
    FEED_KINDS,
    PUMP,
    SLEEP,
    WEIGHT,
    Child,
    Event,
    Family,
)
from bot.services import growth
from bot.services import timeutil as tu


@dataclass
class Span:
    """Итог по одному периоду. Все длительности — после обрезки по границам."""

    feeds: int = 0
    feed_time: timedelta = timedelta()
    bottle_ml: float = 0.0
    pump_ml: float = 0.0
    sleep_time: timedelta = timedelta()
    sleep_count: int = 0
    longest_sleep: timedelta | None = None
    wet: int = 0
    dirty: int = 0

    @property
    def feed_minutes(self) -> int:
        return int(self.feed_time.total_seconds() // 60)


def clip(event: Event, start: datetime, end: datetime, now: datetime) -> timedelta:
    """Сколько события попало в [start, end). Открытый таймер считаем до `now`.

    Открытый таймер тянется до текущего момента, а не до конца периода: иначе
    сон, начатый вчера и не закрытый, добавил бы к вчерашним суткам часы,
    которых ещё не было.
    """
    began = max(event.started_at, start)
    over = min(event.ended_at or now, end)
    return max(timedelta(), over - began)


async def collect(
    session: AsyncSession,
    child: Child,
    start: datetime,
    end: datetime,
    now: datetime | None = None,
) -> Span:
    """Свести все события ребёнка за период в один объект."""
    moment = now or tu.now_utc()
    events = await repo.events_between(session, child.id, start, end)
    span = Span()

    for event in events:
        if event.kind in FEED_KINDS:
            # Считаем по началу: кормление, начатое до полуночи, принадлежит
            # тем суткам, в которых родители его помнят
            if start <= event.started_at < end:
                span.feeds += 1
                if event.kind == FEED_BOTTLE and event.value_num:
                    span.bottle_ml += event.value_num
            if event.kind == FEED_BREAST:
                span.feed_time += clip(event, start, end, moment)

        elif event.kind == SLEEP:
            piece = clip(event, start, end, moment)
            if piece:
                span.sleep_time += piece
                if start <= event.started_at < end:
                    span.sleep_count += 1
                if span.longest_sleep is None or piece > span.longest_sleep:
                    span.longest_sleep = piece

        elif event.kind == PUMP:
            if start <= event.started_at < end and event.value_num:
                span.pump_ml += event.value_num

        elif event.kind in (DIAPER_WET, DIAPER_MIXED):
            if start <= event.started_at < end:
                span.wet += 1
                if event.kind == DIAPER_MIXED:
                    span.dirty += 1

        elif event.kind == DIAPER_DIRTY:
            if start <= event.started_at < end:
                span.dirty += 1

    return span


async def day(
    session: AsyncSession,
    child: Child,
    tz: str,
    on: date | None = None,
    now: datetime | None = None,
) -> Span:
    """Итог за локальные сутки."""
    moment = now or tu.now_utc()
    target = on or tu.local_today(tz, moment)
    start, end = tu.day_bounds(target, tz)
    return await collect(session, child, start, end, moment)


async def week(
    session: AsyncSession, child: Child, tz: str, now: datetime | None = None
) -> tuple[Span, list[tuple[date, Span]]]:
    """Итог за текущую неделю и разбивка по дням — для графика."""
    moment = now or tu.now_utc()
    today = tu.local_today(tz, moment)
    start, end = tu.week_bounds(today, tz)
    total = await collect(session, child, start, end, moment)

    days: list[tuple[date, Span]] = []
    cursor = today - timedelta(days=today.weekday())
    while cursor <= today:
        days.append((cursor, await day(session, child, tz, cursor, moment)))
        cursor += timedelta(days=1)
    return total, days


# --- Тело панели ----------------------------------------------------------


async def panel_text(
    session: AsyncSession, family: Family, child: Child, now: datetime
) -> str:
    """Собрать главный экран. Один вызов — один снимок состояния.

    Вес берём последний измеренный, а не пересчитываем на сегодня: вес — это
    факт взвешивания, и показывать его «на сейчас» значило бы выдумывать.
    """
    from bot import texts  # локальный импорт: texts знает про stats.Span

    today = await day(session, child, family.tz, now=now)
    running = await repo.any_open_event(session, child.id)
    last_feed = await repo.last_event(session, child.id, FEED_KINDS)
    last_sleep = await repo.last_event(session, child.id, frozenset({SLEEP}))
    weight = await repo.last_measurement(session, child.id, WEIGHT)

    note = None
    if weight is not None:
        evaluated = await growth.evaluate(
            session, child, WEIGHT, weight.value, weight.taken_at
        )
        if evaluated is not None:
            note = texts.percentile_str(*evaluated)
        else:
            # Таблицы ВОЗ ещё не залиты — покажем хотя бы динамику от рождения
            loss = growth.loss_from_birth(child, weight.value)
            if loss is not None and loss > 0:
                note = f"−{loss:.1f}% от рождения"
            elif loss is not None:
                note = f"+{-loss:.1f}% от рождения"

    return texts.panel(
        child,
        now,
        family.tz,
        running=running,
        last_feed=last_feed if last_feed is not running else None,
        last_sleep=last_sleep if last_sleep is not running else None,
        wet=today.wet,
        dirty=today.dirty,
        feeds_today=today.feeds,
        weight=weight,
        weight_note=note,
    )
