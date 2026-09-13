"""Фоновый цикл: забытые таймеры, напоминания, панель, бэкап.

Почему свой цикл, а не APScheduler. Наследство Family_planer и его довод,
который здесь работает даже сильнее: всё состояние лежит обычными строками в
`reminders`, поэтому рестарт переживается бесплатно, а догонка после
выключенного ПК получается сама собой. У APScheduler джобы сериализуются
pickle — их не посмотреть глазами и не починить руками, а `misfire_grace_time`
плохо ложится на домашнюю машину, которая уходит в сон непредсказуемо.

Тикер идёт мимо диспетчера, а значит и мимо `FamilyMiddleware`: сессию он
открывает сам. `Session` импортируется по имени, чтобы тест мог подменить
фабрику через `monkeypatch.setattr(ticker, "Session", session_maker)`.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from dateutil.rrule import rrulestr
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import settings
from bot.db import repo
from bot.db.models import Child, Family, Reminder
from bot.db.session import Session
from bot.services import backup, panel, sending
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

# Признак живого цикла для внешнего сторожа. Отдельный файл, а не `bot.log`:
# в спокойный тик бот не пишет в лог ни строки, и свежесть лога о жизни цикла
# ничего не говорит.
#
# Нужен он затем, что «тихая смерть» long polling — задокументированный класс
# багов: туннель моргнул, `getUpdates` завис, процесс жив, задача Планировщика
# «Running», логов нет, бот молчит. Заметит это семья, а не система, если не
# проверять heartbeat снаружи.
HEARTBEAT = settings.db_path.parent / "heartbeat"


def _beat() -> None:
    """Отметить, что цикл прошёл круг. Ошибки глотаем целиком.

    Тикер не должен умирать из-за полного диска: пропущенные напоминания хуже
    неточного healthcheck.
    """
    try:
        HEARTBEAT.write_text(str(tu.now_utc()), encoding="utf-8")
    except Exception:
        log.warning("Не удалось обновить heartbeat", exc_info=True)


# Насколько напоминание опоздало
SILENT = "silent"  # почти вовремя — отправляем как есть
LATE = "late"  # заметно позже — с пометкой, на какое время было запланировано
SUMMARY = "summary"  # ПК был выключен надолго — сворачиваем в одну сводку


def classify_lateness(
    fire_at: datetime, now: datetime, *, silent_min: int, summary_hours: int
) -> str:
    """Насколько сильно опоздало напоминание: молча, с пометкой или сводкой."""
    delay = now - fire_at
    if delay < timedelta(minutes=silent_min):
        return SILENT
    if delay < timedelta(hours=summary_hours):
        return LATE
    return SUMMARY


def _without_dtstart(rrule: str) -> str:
    """Выбросить из правила собственные DTSTART/DTEND.

    `rrulestr` при наличии `DTSTART` в строке **игнорирует** аргумент `dtstart`,
    и якорем серии молча становится дата из правила: «каждые три часа»
    превращается в серию от полуночи. Сами мы такие строки не пишем, но с
    появлением свободного ввода `rrule` начнёт приходить от LLM, а модели
    дописывают `DTSTART` охотно.
    """
    return "\n".join(
        line
        for line in rrule.splitlines()
        if not line.strip().upper().startswith(("DTSTART", "DTEND"))
    )


def next_fire_at(
    rrule: str, dtstart_utc: datetime, after_utc: datetime, tz: str
) -> datetime | None:
    """Следующее срабатывание строго после `after_utc`, или None.

    `dtstart_utc` — текущий `fire_at` напоминания: он задаёт и время суток, и
    фазу серии. Брать за точку отсчёта «сейчас» нельзя — «каждый день в 20:00»
    превратилось бы в «каждый день в момент отправки».

    Правило применяется к локальному времени семьи и только потом переводится
    обратно в UTC, иначе серия уедет на час при переводе часов.

    Пропущенные срабатывания намеренно перешагиваются: про них уже сказала
    сводка догонки, и повторять их по одному — ровно та очередь уведомлений,
    которой мы избегаем.
    """
    try:
        rule = rrulestr(_without_dtstart(rrule), dtstart=tu.to_local(dtstart_utc, tz))
        following = rule.after(tu.to_local(after_utc, tz))
    except (ValueError, TypeError, OverflowError):
        return None
    if following is None:  # серия кончилась по UNTIL или COUNT
        return None
    return tu.to_utc(following.replace(tzinfo=None), tz)


async def run(bot: Bot) -> None:
    """Цикл до отмены. Ошибка внутри итерации не должна убивать тикер."""
    log.info("Тикер запущен, период %s с", settings.tick_seconds)
    while True:
        try:
            async with Session() as session:
                await tick_once(bot, session)
        except asyncio.CancelledError:
            # Не глотаем: иначе остановка бота повиснет на этой задаче
            log.info("Тикер остановлен")
            raise
        except Exception:
            log.exception("Сбой в тике — цикл продолжается")
        # После перехвата, а не внутри `try`: сбой одного тика — это ещё живой
        # цикл, и гасить heartbeat на нём значит перезапускать бота из-за одной
        # неудачной отправки
        _beat()
        await asyncio.sleep(settings.tick_seconds)


async def tick_once(
    bot: Bot, session: AsyncSession, now: datetime | None = None
) -> None:
    """Одна итерация. Порядок шагов не случаен.

    Забытые таймеры закрываем первыми: пока они висят, любая сводка и любая
    панель показывают неправду. Бэкап последним — сорвавшийся снимок не должен
    задержать напоминание.
    """
    moment = now or tu.now_utc()
    await close_stale_timers(bot, session, moment)
    await send_due_reminders(bot, session, moment)
    await panel.refresh_stale(bot, session, moment)
    await backup.run_daily(moment)


async def close_stale_timers(
    bot: Bot, session: AsyncSession, now: datetime | None = None
) -> None:
    """Закрыть таймеры, висящие дольше разумного, и сказать об этом.

    Сказать обязательно. Молча закрытый таймер выглядит как честно измеренный
    четырёхчасовой сон, а это не то, что стоит показывать педиатру. Пометка
    `estimated` доезжает и до выгрузки.
    """
    moment = now or tu.now_utc()
    stale = await repo.stale_open_events(session, moment)
    if not stale:
        return

    for event in stale:
        cutoff = event.started_at + timedelta(hours=settings.open_timer_max_hours)
        await repo.close_event(session, event, cutoff, estimated=True)

        family = await _family_of(session, event.child_id)
        if family is None:
            continue
        await sending.deliver(
            bot,
            family,
            texts.AUTO_CLOSED.format(
                name=texts.KIND_NAMES.get(event.kind, event.kind),
                hours=settings.open_timer_max_hours,
            ),
        )
        log.info("Таймер #%s закрыт автоматически", event.id)


async def _family_of(session: AsyncSession, child_id: int) -> Family | None:
    """Чей это ребёнок. Событие знает про ребёнка, а писать надо в чат семьи."""
    child = await session.get(Child, child_id)
    if child is None:
        return None
    return await repo.get_family_by_id(session, child.family_id)


async def send_due_reminders(
    bot: Bot, session: AsyncSession, now: datetime | None = None
) -> None:
    moment = now or tu.now_utc()
    due = await repo.due_reminders(session, moment)
    if not due:
        return

    by_family: dict[int, list[Reminder]] = {}
    for reminder in due:
        by_family.setdefault(reminder.family_id, []).append(reminder)

    for family_id, reminders in by_family.items():
        family = await repo.get_family_by_id(session, family_id)
        if family is None:  # осиротевшие напоминания — данные из прошлой жизни
            log.warning("Напоминания без семьи #%s пропущены", family_id)
            continue
        try:
            await _send_for_family(bot, session, family, reminders, moment)
        except Exception:
            # Сбой одной семьи не должен срывать рассылку остальным
            log.exception("Напоминания семьи #%s не разосланы", family_id)


def plan(reminders: list[Reminder], now: datetime) -> dict[str, list[Reminder]]:
    """Разложить созревшие напоминания по степени опоздания. Чистая функция."""
    buckets: dict[str, list[Reminder]] = {SILENT: [], LATE: [], SUMMARY: []}
    for reminder in reminders:
        kind = classify_lateness(
            reminder.fire_at,
            now,
            silent_min=settings.late_silent_min,
            summary_hours=settings.late_summary_hours,
        )
        buckets[kind].append(reminder)
    return buckets


async def _send_for_family(
    bot: Bot,
    session: AsyncSession,
    family: Family,
    reminders: list[Reminder],
    now: datetime,
) -> None:
    buckets = plan(reminders, now)

    # Сводка идёт первой: она про прошлое, а следом — созревшее только что
    if buckets[SUMMARY]:
        text = texts.missed_summary(buckets[SUMMARY], family.tz, now)
        await _handle(bot, session, family, text, buckets[SUMMARY], now)

    for kind in (LATE, SILENT):
        for reminder in buckets[kind]:
            text = texts.reminder_message(reminder, family.tz, now, late=(kind == LATE))
            await _handle(bot, session, family, text, [reminder], now)


async def _handle(
    bot: Bot,
    session: AsyncSession,
    family: Family,
    text: str,
    covered: list[Reminder],
    now: datetime,
) -> None:
    """Отправить сообщение и решить судьбу напоминаний, которые оно закрывает."""
    result = await sending.deliver(bot, family, text)
    if result == sending.RETRY:
        return
    for reminder in covered:
        if result == sending.FORBIDDEN:
            await repo.deactivate_reminder(session, reminder)
        elif result == sending.BROKEN:
            # Повтор даст ту же ошибку — закрываем, чтобы не долбить каждый тик
            await repo.mark_reminder_sent(session, reminder, now)
        else:
            await _settle(session, reminder, family.tz, now)


async def _settle(
    session: AsyncSession, reminder: Reminder, tz: str, now: datetime
) -> None:
    """Закрыть разовое напоминание либо сдвинуть повторяющееся на будущее."""
    if not reminder.rrule:
        await repo.mark_reminder_sent(session, reminder, now)
        return

    following = next_fire_at(reminder.rrule, reminder.fire_at, now, tz)
    if following is None:
        # Битое правило или серия кончилась — иначе сработает на каждом тике
        log.warning("Напоминание #%s погашено: rrule %r", reminder.id, reminder.rrule)
        await repo.deactivate_reminder(session, reminder)
        return
    await repo.reschedule_reminder(session, reminder, following)
