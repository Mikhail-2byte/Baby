"""Агрегация событий, и прежде всего — границы суток.

Ночной сон пересекает полночь почти каждый день, и любая ошибка в обрезке даёт
правдоподобные, но неверные числа: «спала 14 часов» или «спала 4 часа» вместо
честных восьми. Заметить это на глаз невозможно — только сложив неделю.
"""

from datetime import timedelta

import pytest

from bot import texts
from bot.db import repo
from bot.db.models import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    FEED_BOTTLE,
    FEED_BREAST,
    SLEEP,
)
from bot.services import stats
from bot.services import timeutil as tu


@pytest.mark.asyncio
async def test_sleep_across_midnight_is_split(session, child, mother, family):
    """Сон с 23:40 до 06:10 делится между сутками, а не достаётся одним.

    Главный тест файла. Проверяем обе стороны сразу: сумма кусков обязана
    сойтись с полной длительностью, иначе часы где-то потерялись или удвоились.
    """
    tz = family.tz
    evening = tu.to_utc(tu.to_local(tu.now_utc(), tz).replace(
        year=2026, month=9, day=6, hour=23, minute=40, second=0, microsecond=0
    ), tz)
    morning = evening + timedelta(hours=6, minutes=30)
    await repo.add_event(
        session, child.id, mother.id, SLEEP, started_at=evening, ended_at=morning
    )

    first = await stats.day(session, child, tz, tu.to_local(evening, tz).date(), morning)
    second = await stats.day(session, child, tz, tu.to_local(morning, tz).date(), morning)

    assert first.sleep_time == timedelta(minutes=20)
    assert second.sleep_time == timedelta(hours=6, minutes=10)
    assert first.sleep_time + second.sleep_time == morning - evening


@pytest.mark.asyncio
async def test_sleep_counted_on_the_day_it_started(session, child, mother, family, now):
    """Сон считается «одним разом» в тех сутках, где он начался.

    Иначе ночной сон попал бы в счётчик дважды, и «7 снов за сутки»
    превратилось бы в 8 без единого лишнего засыпания.
    """
    tz = family.tz
    evening = tu.to_utc(tu.to_local(now, tz).replace(hour=23, minute=40), tz)
    morning = evening + timedelta(hours=6)
    await repo.add_event(
        session, child.id, mother.id, SLEEP, started_at=evening, ended_at=morning
    )

    first = await stats.day(session, child, tz, tu.to_local(evening, tz).date(), morning)
    second = await stats.day(session, child, tz, tu.to_local(morning, tz).date(), morning)

    assert first.sleep_count == 1
    assert second.sleep_count == 0
    assert second.sleep_time > timedelta()  # часы засчитаны, «раз» — нет


@pytest.mark.asyncio
async def test_open_timer_counts_only_until_now(session, child, mother, family, now):
    """Идущий таймер тянется до текущего момента, а не до конца суток.

    Иначе сон, начатый в 22:00 и не закрытый, добавил бы к суткам два часа,
    которых ещё не было, — и панель показала бы будущее.
    """
    tz = family.tz
    started = now - timedelta(hours=2)
    await repo.start_event(session, child.id, mother.id, SLEEP, started_at=started)

    span = await stats.day(session, child, tz, now=now)
    assert span.sleep_time == timedelta(hours=2)


@pytest.mark.asyncio
async def test_feeds_counted_by_start(session, child, mother, family, now):
    """Кормление принадлежит тем суткам, в которых его помнят родители."""
    tz = family.tz
    late = tu.to_utc(tu.to_local(now, tz).replace(hour=23, minute=50), tz)
    await repo.add_event(
        session, child.id, mother.id, FEED_BREAST,
        started_at=late, ended_at=late + timedelta(minutes=25),
    )

    today = await stats.day(session, child, tz, tu.to_local(late, tz).date(), late)
    assert today.feeds == 1
    # Двадцать минут из двадцати пяти пришлись на эти сутки
    assert today.feed_time == timedelta(minutes=10)


@pytest.mark.asyncio
async def test_bottle_volume_sums_up(session, child, mother, family, now):
    """Объёмы из бутылочки складываются, пустые не мешают."""
    for volume in (60.0, 40.0, None):
        await repo.add_event(
            session, child.id, mother.id, FEED_BOTTLE,
            started_at=now, value_num=volume,
        )

    span = await stats.day(session, child, family.tz, now=now)
    assert span.feeds == 3
    assert span.bottle_ml == 100.0


@pytest.mark.asyncio
async def test_mixed_diaper_counts_in_both(session, child, mother, family, now):
    """Смешанный подгузник — это и мокрый, и со стулом одновременно.

    Иначе родителям пришлось бы жать две кнопки на один подгузник, а счётчик
    мокрых — тот, по которому проверяется достаточность питья, — занижался бы.
    """
    await repo.add_event(session, child.id, mother.id, DIAPER_WET, started_at=now)
    await repo.add_event(session, child.id, mother.id, DIAPER_DIRTY, started_at=now)
    await repo.add_event(session, child.id, mother.id, DIAPER_MIXED, started_at=now)

    span = await stats.day(session, child, family.tz, now=now)
    assert span.wet == 2
    assert span.dirty == 2


@pytest.mark.asyncio
async def test_longest_sleep_is_tracked(session, child, mother, family, now):
    """Самый долгий сон — то, что родители спрашивают первым делом."""
    for hours, offset in ((1, 10), (3, 6), (2, 2)):
        started = now - timedelta(hours=offset)
        await repo.add_event(
            session, child.id, mother.id, SLEEP,
            started_at=started, ended_at=started + timedelta(hours=hours),
        )

    span = await stats.day(session, child, family.tz, now=now)
    assert span.longest_sleep == timedelta(hours=3)


@pytest.mark.asyncio
async def test_empty_day_is_all_zeros(session, child, family, now):
    """Сутки без записей не должны падать — первый день дома выглядит так."""
    span = await stats.day(session, child, family.tz, now=now)
    assert span.feeds == 0
    assert span.sleep_time == timedelta()
    assert span.longest_sleep is None
    assert span.feed_minutes == 0


@pytest.mark.asyncio
async def test_week_covers_monday_to_today(session, child, family, now):
    """Неделя считается от понедельника и кончается сегодня, а не воскресеньем."""
    total, days = await stats.week(session, child, family.tz, now=now)
    local_today = tu.local_today(family.tz, now)
    assert days[0][0].weekday() == 0
    assert days[-1][0] == local_today
    assert len(days) == local_today.weekday() + 1
    assert total.feeds == sum(span.feeds for _, span in days)


@pytest.mark.asyncio
async def test_panel_shows_running_timer(session, child, mother, family, now):
    """Панель с идущим таймером говорит о нём первой строкой."""
    await repo.start_event(
        session, child.id, mother.id, FEED_BREAST,
        started_at=now - timedelta(minutes=12), side="L",
    )
    text = await stats.panel_text(session, family, child, now)
    assert "идёт 12 мин" in text
    assert "левая" in text


@pytest.mark.asyncio
async def test_panel_without_who_tables_shows_birth_delta(session, child, family, now):
    """Без таблиц ВОЗ панель показывает динамику от рождения, а не молчит.

    В первые дни именно потеря массы от рождения и есть главный показатель —
    центиль тут вторичен.
    """
    mother = await repo.get_or_create_member(session, family.id, 222, "Аня")
    await repo.add_measurement(
        session, child.id, mother.id, "weight", taken_at=now, value=3250.0
    )
    text = await stats.panel_text(session, family, child, now)
    assert f"3{texts.THIN_SPACE}250 г" in text
    assert "от рождения" in text


@pytest.mark.asyncio
async def test_instant_events_do_not_leak_across_windows(
    session, child, mother, family, now
):
    """Подгузник из прошлого года не должен попадать в сегодняшнюю выборку.

    У мгновенных событий `ended_at` пуст всегда — это момент, а не отрезок.
    Пока условие «либо не закончилось» применялось ко всем видам, такое
    событие выглядело вечно идущим таймером и затягивалось в любую выборку:
    окно выгрузки переставало ограничивать, а запрос за сутки тащил архив.
    """
    ancient = now - timedelta(days=300)
    await repo.add_event(session, child.id, mother.id, DIAPER_WET, started_at=ancient)

    start, end = tu.day_bounds(tu.local_today(family.tz, now), family.tz)
    assert await repo.events_between(session, child.id, start, end) == []


@pytest.mark.asyncio
async def test_running_timer_still_leaks_in_on_purpose(
    session, child, mother, family, now
):
    """А вот незакрытый сон из прошлых суток в выборку попасть обязан.

    Это обратная сторона того же условия: таймер, идущий с вечера, и есть
    событие, пересекающее границу суток.
    """
    await repo.start_event(
        session, child.id, mother.id, SLEEP, started_at=now - timedelta(hours=10)
    )
    start, end = tu.day_bounds(tu.local_today(family.tz, now), family.tz)
    assert len(await repo.events_between(session, child.id, start, end)) == 1
