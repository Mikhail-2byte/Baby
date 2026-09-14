"""Напоминания: `/remind`, пресеты и календарь прививок и осмотров.

Весь ввод здесь кнопочный, поэтому роутер ничего не перехватывает у соседей:
`F.text` в нём нет, ожидание из `pending` не заводится, и граница
`alarm → qa → measure` его не замечает.

Действие лежит в самой кнопке (`RemindCB.action`), а не выводится из текущего
состояния. Кнопки живут в истории чата неограниченно долго, и переключатель со
вчерашнего экрана, нажатый сегодня, выключил бы то, что вчера включили. С явным
действием повторный тап идемпотентен.

Известная гонка, которую здесь сознательно не лечим: два родителя, нажавшие
«включить» в одну секунду, получат две подписки — `reminder_by_preset` и
`create_reminder` не атомарны. Вероятность мала, цена — выключить и включить,
а блокировка ради этого стоила бы дороже самой болезни.
"""

import logging
from datetime import datetime

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import Child, Family, Member, Reminder
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.keyboards import RemindCB
from bot.services import schedule
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

router = Router(name="remind")

# Пресеты, у которых перед включением надо спросить интервал
PICKABLE = frozenset({preset.id for preset in schedule.PRESETS if preset.choices})


async def _state(
    session: AsyncSession, family: Family
) -> list[tuple[schedule.Preset, Reminder | None, int]]:
    """Что включено прямо сейчас, по одной строке на пресет.

    Один запрос на всё: `active_reminders` уже отсортирован по `fire_at`, так
    что первое найденное в группе и есть ближайшее срабатывание. Строки без
    пресета — ручные напоминания этапа 7 — пропускаются, а не падают ключом
    `None` и не рисуются пресетом с именем «None».
    """
    live = await repo.active_reminders(session, family.id)
    groups: dict[str, list[Reminder]] = {}
    for reminder in live:
        if reminder.preset:
            groups.setdefault(reminder.preset, []).append(reminder)

    rows = []
    for preset in schedule.PRESETS:
        found = groups.get(preset.id, [])
        rows.append((preset, found[0] if found else None, len(found)))
    return rows


async def _screen(
    session: AsyncSession, family: Family, now: datetime
) -> tuple[str, InlineKeyboardMarkup]:
    rows = await _state(session, family)
    buttons = [
        (preset.id, f"{preset.icon} {preset.title}", reminder is not None)
        for preset, reminder, _ in rows
    ]
    return texts.remind_screen(rows, family.tz, now), keyboards.remind(
        buttons, pickable=PICKABLE
    )


@router.message(IN_GROUP, Command("remind"))
async def show(message: Message, session: AsyncSession, family: Family) -> None:
    text, markup = await _screen(session, family, tu.now_utc())
    await message.answer(text, reply_markup=markup)


async def _turn_on(
    session: AsyncSession,
    family: Family,
    member: Member,
    child: Child | None,
    preset: schedule.Preset,
    hours: int,
    now: datetime,
) -> tuple[int, datetime | None]:
    """Завести напоминания пресета. Возвращает «сколько» и «когда ближайшее».

    Разовые строки календаря и повторяющееся правило — два разных пути, и
    ветвление стоит только здесь. Тикер про эту разницу не знает: он умеет
    ровно два случая, разовое и `rrule`, и оба уже умел до этого этапа.
    """
    section = schedule.section_of(preset)
    if section is None:
        moment = schedule.first_fire_at(preset, family.tz, now, hours or None)
        await repo.create_reminder(
            session,
            family.id,
            member.id,
            text=preset.text,
            fire_at=moment,
            rrule=schedule.rrule_of(preset, hours or None),
            child_id=child.id if child else None,
            preset=preset.id,
        )
        return 1, moment

    if child is None:
        return 0, None

    ahead = schedule.upcoming(section, child, family.tz, now)
    for entry, moment in ahead:
        day = schedule.entry_day(child, entry, family.tz)
        await repo.create_reminder(
            session,
            family.id,
            member.id,
            text=texts.schedule_reminder(preset, entry, day),
            fire_at=moment,
            child_id=child.id,
            preset=preset.id,
        )
    return len(ahead), ahead[0][1] if ahead else None


async def _redraw(call: CallbackQuery, session: AsyncSession, family: Family,
                  now: datetime) -> None:
    text, markup = await _screen(session, family, now)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        # «message is not modified» при повторном тапе — самый частый случай,
        # и он не стоит строчки в логе выше DEBUG
        log.debug("Экран напоминаний не перерисовали", exc_info=True)


@router.callback_query(IN_GROUP_CB, RemindCB.filter())
async def tap(
    call: CallbackQuery,
    callback_data: RemindCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child | None,
) -> None:
    now = tu.now_utc()
    action = callback_data.action

    if action == "back":
        await call.answer()
        await _redraw(call, session, family, now)
        return

    if action == "cal":
        await _calendar(call, family, child, callback_data.preset, now)
        return

    preset = schedule.BY_ID.get(callback_data.preset)
    if preset is None:
        # Кнопка из истории чата, пресет из файла убрали. Ответить надо всё
        # равно, иначе Telegram будет крутить «часики» до таймаута
        await call.answer()
        return

    if action == "pick":
        await call.answer()
        await call.message.answer(
            texts.REMIND_PICK,
            reply_markup=keyboards.remind_hours(preset.id, preset.choices),
        )
        return

    if action == "off":
        count = await repo.deactivate_preset(session, family.id, preset.id)
        await call.answer()
        await call.message.answer(texts.preset_turned_off(preset, count))
        await _redraw(call, session, family, now)
        return

    if await repo.reminder_by_preset(session, family.id, preset.id):
        await call.answer()  # уже включено, дублей не плодим
        return

    if schedule.section_of(preset) is not None and child is None:
        await call.answer(texts.REMIND_NEEDS_CHILD, show_alert=True)
        return

    count, moment = await _turn_on(
        session, family, member, child, preset, callback_data.hours, now
    )
    await call.answer()
    if count == 0 or moment is None:
        await call.message.answer(texts.REMIND_ALL_PAST)
        return
    await call.message.answer(texts.preset_turned_on(preset, count, moment, family.tz, now))
    await _redraw(call, session, family, now)


async def _calendar(
    call: CallbackQuery, family: Family, child: Child | None, preset_id: str,
    now: datetime,
) -> None:
    preset = schedule.BY_ID.get(preset_id)
    section = schedule.section_of(preset) if preset else None
    if child is None or preset is None or section is None:
        await call.answer(texts.REMIND_NEEDS_CHILD, show_alert=True)
        return

    rows = list(schedule.timeline(section, child, family.tz, now))
    text = texts.calendar_screen(child, section, schedule.SOURCES[preset.source], rows)
    await call.answer()
    try:
        await call.message.edit_text(text, reply_markup=keyboards.remind_calendar(preset_id))
    except Exception:
        log.debug("Календарь не перерисовали", exc_info=True)
