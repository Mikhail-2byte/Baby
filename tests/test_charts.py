"""Графики, дайджест и выгрузка.

Картинку тест «на глаз» не проверит, поэтому проверяем то, что действительно
ломается: не падает ли рендер, попали ли в него нужные данные, и — главное —
не рисуется ли пустой график там, где данных нет. Пустая картинка в чате хуже
честного «пока не из чего строить».
"""

from datetime import datetime, time, timedelta

import pytest
import pytest_asyncio

from bot.db import repo
from bot.db.models import FEED_BREAST, SLEEP, WEIGHT, WhoLms
from bot.services import charts, digest
from bot.services import timeutil as tu

# Настоящая строка таблицы ВОЗ: девочки, вес-к-возрасту, нулевой день
LMS = (0.3809, 3.2322, 0.14171)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest_asyncio.fixture
async def who_table(session):
    """Кусок таблицы ВОЗ на первый месяц — больше графику и не нужно."""
    session.add_all(
        [
            WhoLms(sex="f", indicator=WEIGHT, age_days=day, l=LMS[0], m=LMS[1], s=LMS[2])
            for day in range(0, 60)
        ]
    )
    await session.commit()


# --- Вес ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weight_chart_without_data_is_none(session, child, now):
    """Нет взвешиваний — нет картинки. Пустые оси в чат не отправляем."""
    assert await charts.weight_png(session, child, now) is None


@pytest.mark.asyncio
async def test_weight_chart_renders_png(session, child, mother, born, now, who_table):
    """Рендер доходит до конца и отдаёт настоящий PNG."""
    for day, grams in ((0, 3420.0), (3, 3170.0), (6, 3300.0)):
        await repo.add_measurement(
            session, child.id, mother.id, WEIGHT,
            taken_at=born + timedelta(days=day), value=grams,
        )

    payload = await charts.weight_png(session, child, now)
    assert payload is not None
    assert payload.startswith(PNG_MAGIC)
    assert len(payload) > 5000  # не заглушка в несколько байт


@pytest.mark.asyncio
async def test_weight_points_use_corrected_age(session, child, mother, born, who_table):
    """Точки ложатся на скорректированный возраст, тот же, что у центиля.

    Иначе у недоношенного ребёнка точка и её коридор считались бы по разным
    осям, и график показывал бы отставание, которого нет.
    """
    child.gestation_weeks = 34  # три недели поправки
    await session.commit()
    await repo.add_measurement(
        session, child.id, mother.id, WEIGHT,
        taken_at=born + timedelta(days=30), value=3600.0,
    )

    points, _, _ = await charts.collect_weight(session, child)
    assert points[0][0] == 30 - 21


@pytest.mark.asyncio
async def test_bands_extend_past_the_last_point(session, child, mother, born, who_table):
    """Коридор тянется дальше последней точки.

    Коридор, обрывающийся ровно на последнем взвешивании, читается так, будто
    ребёнок дошёл до края нормы, — а он просто дошёл до края данных.
    """
    await repo.add_measurement(
        session, child.id, mother.id, WEIGHT, taken_at=born, value=3420.0
    )
    points, bands, _ = await charts.collect_weight(session, child)
    assert bands[-1].day > points[-1][0]


@pytest.mark.asyncio
async def test_bands_are_ordered(session, child, mother, born, who_table):
    """−2 SD ниже медианы, +2 SD выше. Перепутать знак легко, заметить — нет."""
    await repo.add_measurement(
        session, child.id, mother.id, WEIGHT, taken_at=born, value=3420.0
    )
    _, bands, _ = await charts.collect_weight(session, child)
    for band in bands:
        assert band.sd2neg < band.sd1neg < band.median < band.sd1 < band.sd2


# --- Ритм суток -----------------------------------------------------------


@pytest.mark.asyncio
async def test_rhythm_without_data_is_none(session, child, family, now):
    assert await charts.rhythm_png(session, child, family, now) is None


@pytest.mark.asyncio
async def test_rhythm_renders_png(session, child, mother, family, now):
    await repo.add_event(
        session, child.id, mother.id, SLEEP,
        started_at=now - timedelta(hours=3), ended_at=now - timedelta(hours=1),
    )
    await repo.add_event(
        session, child.id, mother.id, FEED_BREAST,
        started_at=now - timedelta(minutes=40), ended_at=now - timedelta(minutes=20),
    )
    payload = await charts.rhythm_png(session, child, family, now)
    assert payload is not None
    assert payload.startswith(PNG_MAGIC)


@pytest.mark.asyncio
async def test_rhythm_splits_night_sleep_between_days(
    session, child, mother, family, now
):
    """Сон через полночь попадает в оба дня, каждый раз обрезанным по границе.

    На графике это и есть главное: отрезок обязан упираться в 24:00 одного дня
    и начинаться от 00:00 следующего, а не рисоваться поперёк всей строки.
    """
    tz = family.tz
    evening = tu.to_utc(tu.to_local(now, tz).replace(hour=23, minute=0), tz)
    morning = evening + timedelta(hours=6)
    await repo.add_event(
        session, child.id, mother.id, SLEEP, started_at=evening, ended_at=morning
    )

    rhythm = await charts.collect_rhythm(session, child, tz, morning, days=7)
    by_day = {entry.day: entry for entry in rhythm}
    first = by_day[tu.to_local(evening, tz).date()]
    second = by_day[tu.to_local(morning, tz).date()]

    assert first.sleeps == [(23.0, 24.0)]
    assert second.sleeps == [(0.0, 5.0)]


@pytest.mark.asyncio
async def test_rhythm_counts_the_requested_days(session, child, family, now):
    rhythm = await charts.collect_rhythm(session, child, family.tz, now, days=7)
    assert len(rhythm) == 7
    assert rhythm[-1].day == tu.local_today(family.tz, now)


# --- Дайджест -------------------------------------------------------------


def _monday_at(family, hour: int, minute: int = 0) -> datetime:
    """Момент UTC, который в часовом поясе семьи — понедельник этой недели.

    Считаем от понедельника, а не от «сегодня минус N»: дайджест привязан к дню
    недели, и тест, запущенный в воскресенье, иначе проверял бы не то.
    """
    today = tu.local_today(family.tz)
    monday = today - timedelta(days=today.weekday())
    return tu.to_utc(datetime.combine(monday, time(hour, minute)), family.tz)


def test_digest_not_due_on_other_weekdays(family):
    """Дайджест по понедельникам — во вторник он молчит."""
    family.digest_dow = 0
    family.digest_time = "10:00"
    tuesday = _monday_at(family, 12) + timedelta(days=1)
    assert digest.due_on(family, tuesday) is None


def test_digest_not_due_before_its_hour(family):
    family.digest_dow = 0
    family.digest_time = "10:00"
    assert digest.due_on(family, _monday_at(family, 9, 30)) is None


def test_digest_due_after_its_hour(family):
    family.digest_dow = 0
    family.digest_time = "10:00"
    assert digest.due_on(family, _monday_at(family, 10, 1)) is not None


def test_digest_not_due_twice_the_same_day(family):
    """Отметка в БД, а не в памяти: домашний ПК перезапускается часто."""
    family.digest_dow = 0
    family.digest_time = "10:00"
    moment = _monday_at(family, 12)
    family.last_digest_on = tu.local_today(family.tz, moment)
    assert digest.due_on(family, moment) is None


def test_broken_digest_time_falls_back(family):
    """Мусор в настройке не должен выключить дайджест навсегда."""
    family.digest_dow = 0
    family.digest_time = "не время"
    assert digest.due_on(family, _monday_at(family, 12)) is not None


@pytest.mark.asyncio
async def test_digest_marks_the_day_even_without_a_child(session, family, bot):
    """Семья без ребёнка: отметить и молчать.

    Без отметки бот каждую минуту до полуночи заново решал бы, что дайджест
    созрел, — и каждую минуту заново ничего не делал.
    """
    family.digest_dow = 0
    family.digest_time = "10:00"
    moment = _monday_at(family, 12)
    await digest.send_pending(bot, session, moment)

    assert bot.sent == []
    assert family.last_digest_on == tu.local_today(family.tz, moment)


@pytest.mark.asyncio
async def test_digest_reports_an_empty_week(session, family, child, bot):
    family.digest_dow = 0
    family.digest_time = "10:00"
    moment = _monday_at(family, 12)
    await digest.send_pending(bot, session, moment)

    assert "записей не было" in bot.texts[0]
    # Про этап рассказываем даже в пустую неделю: записей нет, а развитие идёт,
    # и это ровно то, ради чего дайджест читают
    assert "Что делать на этой неделе" in bot.texts[1]


@pytest.mark.asyncio
async def test_digest_sends_the_week(session, family, child, mother, bot):
    family.digest_dow = 0
    family.digest_time = "10:00"
    moment = _monday_at(family, 12)
    # Кормление в неделе, которая закончилась, — именно его дайджест и считает
    await repo.add_event(
        session, child.id, mother.id, FEED_BREAST,
        started_at=moment - timedelta(days=2),
        ended_at=moment - timedelta(days=2) + timedelta(minutes=20),
    )

    await digest.send_pending(bot, session, moment)
    assert "Итоги недели" in bot.texts[0]
    assert family.last_digest_on == tu.local_today(family.tz, moment)


@pytest.mark.asyncio
async def test_digest_counts_the_week_that_ended_not_today(
    session, family, child, mother, bot
):
    """Дайджест уходит в понедельник в 10:00 — и раньше считал эти десять часов.

    Неделя бралась календарная, то есть начиналась в тот же понедельник.
    «Итоги недели» подводили итог утру, текст при этом говорил «за неделю», а
    числа выглядели правдоподобно — просто как маленький день. Нашла живая
    проверка: в дайджесте стояло «По дням: пн 14 сен», одна строка.
    """
    family.digest_dow = 0
    family.digest_time = "10:00"
    moment = _monday_at(family, 12)

    # Единственная запись — сегодняшняя, за два часа до отправки
    await repo.add_event(
        session, child.id, mother.id, FEED_BREAST,
        started_at=moment - timedelta(hours=2),
        ended_at=moment - timedelta(hours=1, minutes=40),
    )

    await digest.send_pending(bot, session, moment)
    body = bot.texts[0]
    assert "записей не было" in body, (
        "сегодняшнее кормление попало в итоги недели, которая закончилась вчера"
    )
