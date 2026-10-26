"""Фоновый цикл: догонка после спящего ПК и забытые таймеры.

Здесь проверяется главное обещание проекта домашней машине: она уходит в сон
непредсказуемо, а бот обязан вести себя после пробуждения разумно — не молчать
и не выплёвывать очередь одинаковых уведомлений.
"""

from datetime import timedelta

import pytest

from bot.db import repo
from bot.db.models import FEED_BREAST, SLEEP
from bot.services import schedule, ticker
from bot.services import timeutil as tu

TZ = "Europe/Moscow"


# --- Ступени опоздания ----------------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (0, ticker.SILENT),
        (9, ticker.SILENT),
        (11, ticker.LATE),
        (60 * 11, ticker.LATE),
        (60 * 13, ticker.SUMMARY),
        (60 * 48, ticker.SUMMARY),
    ],
)
def test_lateness_steps(minutes, expected):
    now = tu.now_utc()
    fire_at = now - timedelta(minutes=minutes)
    assert (
        ticker.classify_lateness(fire_at, now, silent_min=10, summary_hours=12)
        == expected
    )


@pytest.mark.asyncio
async def test_long_outage_collapses_the_old_ones(session, family, mother, bot, child):
    """ПК проспал сутки: давнее — одной сводкой, недавнее — по отдельности.

    Это и есть смысл трёх ступеней, и «свернуть вообще всё» было бы неправильно.
    Напоминание о кормлении, опоздавшее на три часа, ещё имеет смысл выполнить;
    опоздавшее на сутки — уже нет, про него достаточно знать, что оно было.

    Проверяем именно границу: восемь напоминаний превращаются в одну сводку
    плюс три живых, а не в восемь уведомлений подряд.
    """
    now = tu.now_utc()
    for hours_ago in range(24, 0, -3):
        await repo.create_reminder(
            session, family.id, mother.id,
            text=f"Кормление {hours_ago}",
            fire_at=now - timedelta(hours=hours_ago),
        )

    await ticker.send_due_reminders(bot, session, now)

    # Одна сводка про давние (24, 21, 18, 15, 12 часов назад) и три отдельных
    # про недавние (9, 6, 3) — порог `late_summary_hours` равен 12
    assert len(bot.sent) == 4
    assert "пропустил 5 напоминаний" in bot.texts[0]
    assert bot.texts[0].startswith("⏰ Пока меня не было")
    for text in bot.texts[1:]:
        assert "Было запланировано" in text

    # Все погашены — иначе следующий тик пришлёт сводку снова
    assert await repo.due_reminders(session, now) == []


@pytest.mark.asyncio
async def test_fresh_reminder_goes_as_is(session, family, mother, bot):
    """Созревшее только что приходит без пометок об опоздании."""
    now = tu.now_utc()
    await repo.create_reminder(
        session, family.id, mother.id, text="Витамин D", fire_at=now
    )
    await ticker.send_due_reminders(bot, session, now)

    assert len(bot.sent) == 1
    assert "Витамин D" in bot.texts[0]
    assert "запланировано" not in bot.texts[0]


@pytest.mark.asyncio
async def test_slightly_late_reminder_says_when_it_was_due(session, family, mother, bot):
    """Опоздавшее на час честно говорит, на какое время было запланировано."""
    now = tu.now_utc()
    await repo.create_reminder(
        session, family.id, mother.id,
        text="Витамин D", fire_at=now - timedelta(hours=1),
    )
    await ticker.send_due_reminders(bot, session, now)
    assert "запланировано" in bot.texts[0]


# --- Повторяющиеся напоминания --------------------------------------------


def test_next_fire_keeps_time_of_day():
    """Серия держится за своё время суток, а не за момент отправки.

    Бот мог отправить напоминание в 20:47 — потому что до этого спал ПК, —
    но завтрашнее обязано прийти в 20:00.
    """
    fire_at = tu.to_utc(tu.to_local(tu.now_utc(), TZ).replace(hour=20, minute=0), TZ)
    late = fire_at + timedelta(minutes=47)

    following = ticker.next_fire_at("FREQ=DAILY", fire_at, late, TZ)
    assert following is not None
    assert tu.to_local(following, TZ).hour == 20
    assert tu.to_local(following, TZ).minute == 0


def test_dtstart_from_model_is_ignored():
    """`DTSTART` внутри правила не должен угонять якорь серии.

    `rrulestr` при наличии `DTSTART` в строке игнорирует одноимённый аргумент, и
    «каждый день в 20:00» молча превращается в серию от полуночи. Сами мы таких
    строк не пишем, но с появлением свободного ввода их начнёт дописывать LLM.
    """
    fire_at = tu.to_utc(tu.to_local(tu.now_utc(), TZ).replace(hour=20, minute=0), TZ)
    rule = "DTSTART:20200101T000000\nFREQ=DAILY"

    following = ticker.next_fire_at(rule, fire_at, fire_at, TZ)
    assert following is not None
    assert tu.to_local(following, TZ).hour == 20


def test_broken_rrule_gives_none():
    """Битое правило не должно ронять тик — напоминание просто гасится."""
    now = tu.now_utc()
    assert ticker.next_fire_at("не правило вовсе", now, now, TZ) is None


def test_finished_series_gives_none():
    """Серия с COUNT кончается — и это не ошибка."""
    now = tu.now_utc()
    assert ticker.next_fire_at("FREQ=DAILY;COUNT=1", now, now, TZ) is None


@pytest.mark.asyncio
async def test_repeating_reminder_is_rescheduled(session, family, mother, bot):
    """Повторяющееся не гасится, а уезжает на следующий раз."""
    now = tu.now_utc()
    reminder = await repo.create_reminder(
        session, family.id, mother.id,
        text="Витамин D", fire_at=now, rrule="FREQ=DAILY",
    )
    await ticker.send_due_reminders(bot, session, now)

    await session.refresh(reminder)
    assert reminder.active is True
    assert reminder.sent_at is None
    assert reminder.fire_at > now


@pytest.mark.asyncio
async def test_broken_rrule_deactivates_instead_of_looping(
    session, family, mother, bot
):
    """Напоминание с битым правилом гасится, а не срабатывает каждый тик."""
    now = tu.now_utc()
    reminder = await repo.create_reminder(
        session, family.id, mother.id,
        text="Мусор", fire_at=now, rrule="ерунда",
    )
    await ticker.send_due_reminders(bot, session, now)

    await session.refresh(reminder)
    assert reminder.active is False


# --- Забытые таймеры ------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_timer_is_nudged_once_not_closed(session, family, child, mother, bot):
    """Таймер, висящий дольше порога, закрывается с пометкой «приблизительно».

    Пометка важнее самого закрытия: молча закрытый таймер выглядит как честно
    измеренный четырёхчасовой сон, а это не то, что стоит показывать педиатру.
    """
    family.sleep_nudge_minutes = 180
    await session.commit()
    now = tu.now_utc()
    event = await repo.start_event(
        session, child.id, mother.id, SLEEP, started_at=now - timedelta(hours=9)
    )

    await ticker.close_stale_timers(bot, session, now)

    await session.refresh(event)
    assert event.ended_at is None
    assert event.estimated is False
    assert event.nudged_at == now
    assert len(bot.sent) == 1
    await ticker.close_stale_timers(bot, session, now + timedelta(hours=1))
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_young_timer_is_left_alone(session, child, mother, bot):
    """Час — это нормальный сон, а не забытый таймер."""
    now = tu.now_utc()
    await repo.start_event(
        session, child.id, mother.id, FEED_BREAST, started_at=now - timedelta(hours=1)
    )
    await ticker.close_stale_timers(bot, session, now)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_tick_survives_a_broken_family(session, family, mother, bot, monkeypatch):
    """Сбой одной семьи не должен срывать рассылку остальным.

    Семей здесь одна, но проверяется контракт: исключение внутри рассылки
    перехватывается и не всплывает в цикл, где убило бы весь тик вместе с
    бэкапом и панелью.
    """
    now = tu.now_utc()
    await repo.create_reminder(
        session, family.id, mother.id, text="Тест", fire_at=now
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("Telegram лёг")

    monkeypatch.setattr(ticker.sending, "deliver", boom)
    await ticker.send_due_reminders(bot, session, now)  # не должно упасть


# --- Стык с пресетами этапа 2 ---------------------------------------------
#
# Два теста ниже — единственная проверка того, что ради `/remind` тикер
# править не пришлось. Он и до этого этапа умел ровно два случая, разовое и
# `rrule`, и подписка на календарь легла на первый из них как есть.


@pytest.mark.asyncio
async def test_a_preset_reminder_makes_a_full_round_trip(session, family, mother, bot):
    """Пресет «кормление» проходит полный круг и остаётся жить.

    Текст приходит из `schedule_ru.yaml`, правило — из `schedule.rrule_of`,
    а сдвиг считает тикер. Ни одна из трёх сторон о двух других не знает.
    """
    feed = schedule.BY_ID["feed"]
    # Без микросекунд: правило считает `rrulestr`, а он работает с точностью до
    # секунды и молча их срезает. Для напоминаний это безразлично, для
    # сравнения «ровно три часа» — нет
    now = tu.now_utc().replace(microsecond=0)
    reminder = await repo.create_reminder(
        session, family.id, mother.id,
        text=feed.text, fire_at=now,
        rrule=schedule.rrule_of(feed), preset=feed.id,
    )

    await ticker.send_due_reminders(bot, session, now)
    await session.refresh(reminder)

    assert len(bot.texts) == 1
    assert "Пора кормить" in bot.texts[0]
    assert reminder.active is True
    assert reminder.fire_at == now + timedelta(hours=feed.default_hours)


@pytest.mark.asyncio
async def test_a_fired_calendar_reminder_dies_and_the_rest_live(
    session, family, mother, bot
):
    """Сработавшая строка подписки гаснет сама, остальные не трогаются.

    Именно поэтому подписка сделана пачкой разовых, а не одним напоминанием,
    которое кто-то перезаряжал бы: перезаряжать календарь тикер не умеет и
    уметь не должен.
    """
    now = tu.now_utc()
    due = await repo.create_reminder(
        session, family.id, mother.id,
        text="Прививки: 3 месяца", fire_at=now, preset="vaccine",
    )
    later = await repo.create_reminder(
        session, family.id, mother.id,
        text="Прививки: 6 месяцев", fire_at=now + timedelta(days=90), preset="vaccine",
    )

    await ticker.send_due_reminders(bot, session, now)
    await session.refresh(due)
    await session.refresh(later)

    assert due.active is False and due.sent_at is not None
    assert later.active is True and later.sent_at is None
