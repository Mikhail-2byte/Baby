"""Недельный дайджест: итоги и графики раз в неделю, сам.

Почему раз в неделю, а не каждое утро. Ежедневная сводка в первые месяцы —
это уведомление о том, что и так было перед глазами всю ночь. Недельная
показывает то, чего в моменте не видно: набирает ли вес, съезжает ли режим,
стало ли ночных пробуждений меньше. Именно за этим и садятся смотреть.

Отметка «за какой день уже отправлено» — колонка `last_digest_on`, а не флаг в
памяти: домашний ПК перезапускается чаще, чем наступает понедельник, и
внутрипроцессная отметка гарантированно потерялась бы.

Дайджест не догоняется. Пропустили понедельник из-за выключенного ПК — итоги
недели придут в следующий раз; слать их в среду бессмысленно, а раздражает
не меньше пропущенных напоминаний.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time

from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.db import repo
from bot.db.models import Child, Family
from bot.services import charts, sending, stage, stats
from bot.services import timeutil as tu

log = logging.getLogger(__name__)


def due_on(family: Family, now: datetime) -> date | None:
    """Локальный день, за который пора выпустить дайджест, или `None`.

    Условий три: сегодня нужный день недели, локальное время уже перевалило за
    назначенное, и за сегодня ещё не отправляли.
    """
    today = tu.local_today(family.tz, now)
    if today.weekday() != family.digest_dow:
        return None
    if family.last_digest_on == today:
        return None

    try:
        moment = tu.parse_hhmm(family.digest_time)
    except ValueError:
        # Мусор в настройке не должен останавливать дайджест навсегда
        log.warning("Битое время дайджеста у семьи #%s: %r", family.id, family.digest_time)
        moment = time(10, 0)

    if tu.to_local(now, family.tz).time() < moment:
        return None
    return today


async def send_pending(
    bot: Bot, session: AsyncSession, now: datetime | None = None
) -> None:
    """Обойти семьи и разослать созревшие дайджесты. Зовёт тикер.

    В обычный тик не делает ни одного обращения к Telegram — только читает
    семьи и сравнивает дату.
    """
    moment = now or tu.now_utc()
    for family in await repo.all_families(session):
        target = due_on(family, moment)
        if target is None:
            continue
        try:
            await send_for(bot, session, family, moment, target)
        except Exception:
            # Сбой одной семьи не срывает остальных; отметку не ставим —
            # попробуем на следующем тике
            log.exception("Дайджест семьи #%s не отправлен", family.id)


async def send_for(
    bot: Bot, session: AsyncSession, family: Family, now: datetime, target: date
) -> None:
    """Собрать и отправить дайджест одной семье, затем отметить день."""
    child = await repo.active_child(session, family.id)
    if child is None:
        # Семья без ребёнка: отмечаем день и молчим, иначе каждую минуту до
        # полуночи будем заново решать, что дайджест созрел
        await _mark(session, family, target)
        return

    # Неделя, которая закончилась: дайджест уходит утром, и неполные
    # сегодняшние сутки в итоге недели не значат ничего
    total, days = await stats.week(
        session, child, family.tz, now=now, ending_yesterday=True
    )
    if total.feeds == 0 and not total.sleep_time:
        status = await sending.deliver(
            bot, family, texts.DIGEST_NO_DATA, session=session
        )
    else:
        status = await sending.deliver(
            bot,
            family,
            texts.DIGEST_HEAD + texts.week_summary(child, total, days),
            session=session,
        )

    if status == sending.RETRY:
        # Сеть или флуд-контроль: отметку не ставим, следующий тик повторит
        return

    if status == sending.OK:
        await _send_stage(bot, family, child, now)
        await _send_charts(bot, family, session, child, now)

    await _mark(session, family, target)
    log.info("Дайджест семьи #%s отправлен (%s)", family.id, status)


async def _send_stage(
    bot: Bot, family: Family, child: Child, now: datetime
) -> None:
    """Что происходит на этой неделе — отдельным сообщением после итогов.

    Отдельным, а не приклеенным к сводке: одно про прошедшее, другое про
    предстоящее, и склеенные они читаются хуже обоих. Сбой здесь дайджест не
    отменяет — итоги недели уже ушли.
    """
    try:
        text = stage.report(child, now)
    except Exception:
        log.exception("Этап развития для семьи #%s не собрался", family.id)
        return
    if text is not None:
        await sending.deliver(bot, family, text)


async def _send_charts(
    bot: Bot, family: Family, session: AsyncSession, child: Child, now: datetime
) -> None:
    """Графики следом за текстом. Их отсутствие не повод отменять дайджест.

    Построение каждого графика заворачивается в лямбду, а не вызывается сразу:
    иначе обе корутины создались бы до цикла, и сбой первой оставил бы вторую
    неожиданной — с предупреждением «coroutine was never awaited» в логе и
    неотправленным графиком без всякого объяснения.
    """
    builders: list[tuple[Callable[[], Awaitable[bytes | None]], str, str]] = [
        (lambda: charts.weight_png(session, child, now), "weight.png", "Вес · коридоры ВОЗ"),
        (lambda: charts.rhythm_png(session, child, family, now), "rhythm.png", "Ритм суток"),
    ]
    for build, name, caption in builders:
        try:
            payload = await build()
            if payload is None:
                continue
            await bot.send_photo(
                family.chat_id, BufferedInputFile(payload, name), caption=caption
            )
        except Exception:
            log.warning("График %s не ушёл семье #%s", name, family.id, exc_info=True)


async def _mark(session: AsyncSession, family: Family, target: date) -> None:
    family.last_digest_on = target
    await session.commit()
