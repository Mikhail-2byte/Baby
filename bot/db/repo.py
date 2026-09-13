"""Слой доступа к данным: функции, а не Active Record.

Правило проекта: SQL живёт только здесь. Хендлеры и сервисы получают готовые
объекты и не знают ни про `select`, ни про сессию дальше её передачи. Это то,
что позволяет тестам подменять данные, не поднимая aiogram.

Все функции, меняющие данные, коммитят сами. Причина простая: вызывающих много
(хендлеры, тикер, панель), и «кто-то закоммитит потом» — это способ однажды
потерять запись о кормлении.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import (
    TIMED_KINDS,
    Alert,
    Child,
    Event,
    Family,
    Measurement,
    Member,
    Reminder,
)

# --- Семья и участники ----------------------------------------------------


async def get_or_create_family(
    session: AsyncSession, chat_id: int, title: str | None = None
) -> Family:
    found = await session.scalar(select(Family).where(Family.chat_id == chat_id))
    if found is not None:
        # Название чата меняется, и «Малышка» вместо «Новая группа» приятнее
        # видеть в логах и в выгрузке
        if title and found.title != title:
            found.title = title
            await session.commit()
        return found

    family = Family(chat_id=chat_id, title=title, tz=settings.tz_default)
    session.add(family)
    await session.commit()
    return family


async def get_family_by_id(session: AsyncSession, family_id: int) -> Family | None:
    return await session.get(Family, family_id)


async def get_or_create_member(
    session: AsyncSession, family_id: int, tg_user_id: int, display_name: str
) -> Member:
    found = await session.scalar(
        select(Member).where(
            Member.family_id == family_id, Member.tg_user_id == tg_user_id
        )
    )
    if found is not None:
        if found.display_name != display_name:
            found.display_name = display_name
            await session.commit()
        return found

    member = Member(
        family_id=family_id, tg_user_id=tg_user_id, display_name=display_name
    )
    session.add(member)
    await session.commit()
    return member


async def migrate_family_chat_id(
    session: AsyncSession, old_chat_id: int, new_chat_id: int
) -> bool:
    """Переезд группы в супергруппу. `False` — переезжать было нечего.

    Панель обнуляется: `message_id` принадлежат конкретному чату, и в новом
    чате старый id указывает в пустоту. Это боевой баг Family_planer, здесь он
    учтён заранее.
    """
    family = await session.scalar(select(Family).where(Family.chat_id == old_chat_id))
    if family is None:
        return False
    family.chat_id = new_chat_id
    family.panel_message_id = None
    family.panel_day = None
    await session.commit()
    return True


async def set_panel(
    session: AsyncSession, family: Family, message_id: int | None, day: date | None
) -> None:
    """Запомнить выпущенную панель. Оба поля пишутся всегда.

    «Не менять день, если не передали» было бы удобнее в одном месте вызова и
    ядовито в другом: сброс панели после изгнания бота обязан обнулить и день,
    иначе вернувшийся в чат бот сочтёт вчерашнюю панель сегодняшней.
    """
    family.panel_message_id = message_id
    family.panel_day = day
    await session.commit()


async def all_families(session: AsyncSession) -> list[Family]:
    """Все семьи — для фоновых обходов тикера."""
    return list(await session.scalars(select(Family)))


# --- Ребёнок --------------------------------------------------------------


async def active_child(session: AsyncSession, family_id: int) -> Child | None:
    """Текущий ребёнок семьи. Несколько детей схема допускает, UI пока нет."""
    return await session.scalar(
        select(Child)
        .where(Child.family_id == family_id, Child.archived.is_(False))
        .order_by(Child.born_at.desc())
        .limit(1)
    )


async def create_child(
    session: AsyncSession,
    family_id: int,
    *,
    name: str,
    sex: str,
    born_at: datetime,
    birth_weight_g: int | None = None,
    birth_length_mm: int | None = None,
    birth_head_mm: int | None = None,
    gestation_weeks: int | None = None,
) -> Child:
    child = Child(
        family_id=family_id,
        name=name,
        sex=sex,
        born_at=born_at,
        birth_weight_g=birth_weight_g,
        birth_length_mm=birth_length_mm,
        birth_head_mm=birth_head_mm,
        gestation_weeks=gestation_weeks,
    )
    session.add(child)
    await session.commit()
    return child


# --- События --------------------------------------------------------------


async def open_event(
    session: AsyncSession, child_id: int, kind: str
) -> Event | None:
    """Идущий таймер данного вида, если он есть.

    Берём самый свежий, а не «единственный»: инвариант «один открытый таймер»
    держим мы сами, и полагаться на его нерушимость при чтении незачем —
    достаточно того, что кнопка «стоп» закроет тот, что человек и видел.
    """
    return await session.scalar(
        select(Event)
        .where(
            Event.child_id == child_id,
            Event.kind == kind,
            Event.ended_at.is_(None),
        )
        .order_by(Event.started_at.desc())
        .limit(1)
    )


async def any_open_event(session: AsyncSession, child_id: int) -> Event | None:
    """Любой идущий таймер — панель показывает его первой строкой."""
    return await session.scalar(
        select(Event)
        .where(
            Event.child_id == child_id,
            Event.kind.in_(TIMED_KINDS),
            Event.ended_at.is_(None),
        )
        .order_by(Event.started_at.desc())
        .limit(1)
    )


async def start_event(
    session: AsyncSession,
    child_id: int,
    author_id: int,
    kind: str,
    *,
    started_at: datetime,
    side: str | None = None,
    subtype: str | None = None,
    source_chat_id: int | None = None,
    source_message_id: int | None = None,
) -> Event:
    event = Event(
        child_id=child_id,
        author_id=author_id,
        kind=kind,
        started_at=started_at,
        side=side,
        subtype=subtype,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
    )
    session.add(event)
    await session.commit()
    return event


async def close_event(
    session: AsyncSession,
    event: Event,
    ended_at: datetime,
    *,
    estimated: bool = False,
) -> Event:
    event.ended_at = ended_at
    event.estimated = estimated
    await session.commit()
    return event


async def add_event(
    session: AsyncSession,
    child_id: int,
    author_id: int,
    kind: str,
    *,
    started_at: datetime,
    ended_at: datetime | None = None,
    value_num: float | None = None,
    side: str | None = None,
    subtype: str | None = None,
    note: str | None = None,
    source_chat_id: int | None = None,
    source_message_id: int | None = None,
) -> Event:
    """Готовое событие целиком — подгузник, бутылочка, температура."""
    event = Event(
        child_id=child_id,
        author_id=author_id,
        kind=kind,
        started_at=started_at,
        ended_at=ended_at,
        value_num=value_num,
        side=side,
        subtype=subtype,
        note=note,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
    )
    session.add(event)
    await session.commit()
    return event


async def delete_event(session: AsyncSession, event: Event) -> None:
    await session.delete(event)
    await session.commit()


async def get_event(session: AsyncSession, event_id: int) -> Event | None:
    return await session.get(Event, event_id)


async def events_between(
    session: AsyncSession,
    child_id: int,
    start: datetime,
    end: datetime,
    kinds: frozenset[str] | set[str] | None = None,
) -> list[Event]:
    """События, пересекающиеся с полуинтервалом [start, end).

    Именно пересекающиеся, а не начавшиеся внутри: сон с 23:40 до 06:10
    принадлежит обеим суткам, и сутки, которые его не видят, показывают
    неправдоподобные «спала 4 часа».

    Пустой `ended_at` означает идущий таймер **только** у событий из
    `TIMED_KINDS`. У подгузника и бутылочки он пуст всегда: это моменты, а не
    отрезки. Без этой оговорки условие «либо не закончилось» затягивало в
    любую выборку все мгновенные события за всю историю — окно выгрузки
    переставало что-либо ограничивать, а запрос за сутки тащил годовой архив.
    """
    overlaps = and_(
        Event.started_at < end,
        or_(
            Event.started_at >= start,
            Event.ended_at > start,
            and_(Event.ended_at.is_(None), Event.kind.in_(TIMED_KINDS)),
        ),
    )
    query = select(Event).where(Event.child_id == child_id, overlaps)
    if kinds:
        query = query.where(Event.kind.in_(kinds))
    result = await session.scalars(query.order_by(Event.started_at))
    return list(result)


async def last_event(
    session: AsyncSession, child_id: int, kinds: frozenset[str] | set[str]
) -> Event | None:
    return await session.scalar(
        select(Event)
        .where(Event.child_id == child_id, Event.kind.in_(kinds))
        .order_by(Event.started_at.desc())
        .limit(1)
    )


async def count_events_since(
    session: AsyncSession,
    child_id: int,
    since: datetime,
    kinds: frozenset[str] | set[str],
) -> int:
    """Сколько событий начиналось после `since`. Верхней границы нет намеренно.

    «За последние сутки» — окно без правого края: событий в будущем не бывает,
    все записи создаются моментом `now`. Полуинтервал `[since, now)`, который
    стоял здесь раньше, выбрасывал событие, отмеченное ровно в момент тика, —
    в бою это микросекундное совпадение, а в тестах оно наступает всегда, и
    отличить «окно посчиталось правильно» от «окно не посчиталось» становилось
    нельзя.
    """
    return (
        await session.scalar(
            select(func.count(Event.id)).where(
                Event.child_id == child_id,
                Event.kind.in_(kinds),
                Event.started_at >= since,
            )
        )
        or 0
    )


async def stale_open_events(session: AsyncSession, now: datetime) -> list[Event]:
    """Таймеры, висящие дольше `open_timer_max_hours` — забытые.

    Тикер закрывает их с пометкой `estimated`. Не закрывать нельзя: открытый
    таймер ломает статистику суток целиком, а забыть вторую кнопку ночью —
    норма, а не исключение.
    """
    cutoff = now - timedelta(hours=settings.open_timer_max_hours)
    result = await session.scalars(
        select(Event).where(
            Event.kind.in_(TIMED_KINDS),
            Event.ended_at.is_(None),
            Event.started_at < cutoff,
        )
    )
    return list(result)


# --- Измерения ------------------------------------------------------------


async def add_measurement(
    session: AsyncSession,
    child_id: int,
    author_id: int,
    kind: str,
    *,
    taken_at: datetime,
    value: float,
    note: str | None = None,
) -> Measurement:
    measurement = Measurement(
        child_id=child_id,
        author_id=author_id,
        kind=kind,
        taken_at=taken_at,
        value=value,
        note=note,
    )
    session.add(measurement)
    await session.commit()
    return measurement


async def measurements(
    session: AsyncSession, child_id: int, kind: str, limit: int | None = None
) -> list[Measurement]:
    """Все измерения одного вида, от старых к новым — как на графике."""
    query = (
        select(Measurement)
        .where(Measurement.child_id == child_id, Measurement.kind == kind)
        .order_by(Measurement.taken_at)
    )
    if limit:
        # Нужны последние `limit`, а порядок всё равно хронологический:
        # берём с конца и разворачиваем уже в Python
        query = (
            select(Measurement)
            .where(Measurement.child_id == child_id, Measurement.kind == kind)
            .order_by(Measurement.taken_at.desc())
            .limit(limit)
        )
        rows = list(await session.scalars(query))
        return list(reversed(rows))
    return list(await session.scalars(query))


async def last_measurement(
    session: AsyncSession, child_id: int, kind: str
) -> Measurement | None:
    return await session.scalar(
        select(Measurement)
        .where(Measurement.child_id == child_id, Measurement.kind == kind)
        .order_by(Measurement.taken_at.desc())
        .limit(1)
    )


async def delete_measurement(session: AsyncSession, measurement: Measurement) -> None:
    await session.delete(measurement)
    await session.commit()


async def get_measurement(
    session: AsyncSession, measurement_id: int
) -> Measurement | None:
    return await session.get(Measurement, measurement_id)


# --- Напоминания ----------------------------------------------------------


async def create_reminder(
    session: AsyncSession,
    family_id: int,
    created_by: int,
    *,
    text: str,
    fire_at: datetime,
    rrule: str | None = None,
    child_id: int | None = None,
    preset: str | None = None,
) -> Reminder:
    reminder = Reminder(
        family_id=family_id,
        child_id=child_id,
        created_by=created_by,
        text=text,
        fire_at=fire_at,
        rrule=rrule,
        preset=preset,
    )
    session.add(reminder)
    await session.commit()
    return reminder


async def reminder_by_preset(
    session: AsyncSession, family_id: int, preset: str
) -> Reminder | None:
    """Живой пресет, если он уже заведён — чтобы не плодить дубли."""
    return await session.scalar(
        select(Reminder).where(
            Reminder.family_id == family_id,
            Reminder.preset == preset,
            Reminder.active.is_(True),
        )
    )


async def active_reminders(session: AsyncSession, family_id: int) -> list[Reminder]:
    result = await session.scalars(
        select(Reminder)
        .where(Reminder.family_id == family_id, Reminder.active.is_(True))
        .order_by(Reminder.fire_at)
    )
    return list(result)


async def get_reminder(session: AsyncSession, reminder_id: int) -> Reminder | None:
    return await session.get(Reminder, reminder_id)


async def due_reminders(session: AsyncSession, now: datetime) -> list[Reminder]:
    result = await session.scalars(
        select(Reminder)
        .where(
            Reminder.active.is_(True),
            Reminder.sent_at.is_(None),
            Reminder.fire_at <= now,
        )
        .order_by(Reminder.fire_at)
    )
    return list(result)


async def mark_reminder_sent(
    session: AsyncSession, reminder: Reminder, now: datetime
) -> None:
    reminder.sent_at = now
    reminder.active = False
    await session.commit()


async def deactivate_reminder(session: AsyncSession, reminder: Reminder) -> None:
    reminder.active = False
    await session.commit()


async def reschedule_reminder(
    session: AsyncSession, reminder: Reminder, following: datetime
) -> None:
    reminder.fire_at = following
    reminder.sent_at = None
    await session.commit()


# --- Тревоги по данным ----------------------------------------------------


async def alert_raised(
    session: AsyncSession, child_id: int, flag_id: str, day: date
) -> bool:
    """Говорили ли уже про этот признак сегодня."""
    found = await session.scalar(
        select(Alert.id).where(
            Alert.child_id == child_id,
            Alert.flag_id == flag_id,
            Alert.raised_on == day,
        )
    )
    return found is not None


async def raise_alert(
    session: AsyncSession, child_id: int, flag_id: str, day: date
) -> None:
    session.add(Alert(child_id=child_id, flag_id=flag_id, raised_on=day))
    await session.commit()
