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
    Usage,
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


async def family_by_chat(session: AsyncSession, chat_id: int) -> Family | None:
    """Найти семью, НЕ заводя её. Отдельно от `get_or_create_family`.

    Нужна там, где заводить нельзя: у апдейта, который никто не обработает,
    семьи быть не должно, но уже заведённую надо отдать хендлеру как обычно.
    Разница выражена именами, а не флагом `create=False`: флаг читается на месте
    вызова как «наверное, заведёт», и один такой вызов уже стоил лишней семьи в
    боевой базе.
    """
    return await session.scalar(select(Family).where(Family.chat_id == chat_id))


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


async def mark_greeted(session: AsyncSession, family: Family, moment: datetime) -> None:
    """Запомнить, что бот в этом чате уже здоровался.

    Момент приходит аргументом, а не берётся здесь: `repo` не знает, который
    час, — это правило всего файла, и оно же позволяет ревизии проставить
    отметку задним числом существующим семьям.
    """
    family.greeted_at = moment
    await session.commit()


async def set_checkin(
    session: AsyncSession, family: Family, hours: int, now: datetime
) -> None:
    """Включить или выключить периодический опрос. `hours = 0` — выключен.

    Отметка ставится в момент включения, а не оставляется пустой: пустая
    означает «спросить на ближайшем тике», то есть бот заговорил бы через
    минуту после того, как его включили. Первый вопрос обязан прийти через
    интервал — та же доктрина, что у `schedule.first_fire_at`.
    """
    family.checkin_hours = hours
    family.checkin_last_at = now if hours > 0 else None
    await session.commit()


async def mark_checkin(session: AsyncSession, family: Family, now: datetime) -> None:
    """Вопрос задан — засечь время. Двойник отметки дайджеста."""
    family.checkin_last_at = now
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


async def update_child(
    session: AsyncSession,
    child: Child,
    *,
    name: str | None = None,
    sex: str | None = None,
    born_at: datetime | None = None,
    birth_weight_g: int | None = None,
) -> Child:
    """Поправить поля карточки. `None` означает «не трогать», а не «стереть».

    Стирания здесь нет намеренно: ни один путь в интерфейсе не просит убрать
    уже введённый вес или дату, а различать «не передали» и «передали пусто»
    пришлось бы отдельным дозорным значением ради случая, которого нет.
    """
    if name is not None:
        child.name = name
    if sex is not None:
        child.sex = sex
    if born_at is not None:
        child.born_at = born_at
    if birth_weight_g is not None:
        child.birth_weight_g = birth_weight_g
    await session.commit()
    return child


async def measurement_at(
    session: AsyncSession, child_id: int, kind: str, moment: datetime
) -> Measurement | None:
    """Измерение данного вида, снятое ровно в этот момент.

    Нужно ровно для одного: вес при рождении живёт в двух местах — полем
    `children.birth_weight_g` и точкой на графике с `taken_at = born_at`.
    Поправив одно и забыв другое, мы получили бы карточку и график, которые
    расходятся между собой, причём заметить это можно было бы только глазами.
    """
    return await session.scalar(
        select(Measurement).where(
            Measurement.child_id == child_id,
            Measurement.kind == kind,
            Measurement.taken_at == moment,
        )
    )


async def update_measurement(
    session: AsyncSession,
    measurement: Measurement,
    *,
    taken_at: datetime | None = None,
    value: float | None = None,
) -> Measurement:
    """Поправить измерение. `None`, как и у `update_child`, — «не трогать»."""
    if taken_at is not None:
        measurement.taken_at = taken_at
    if value is not None:
        measurement.value = value
    await session.commit()
    return measurement


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


async def close_running(
    session: AsyncSession, child_id: int, now: datetime
) -> Event | None:
    """Закрыть идущий таймер, если он есть. Возвращает закрытый.

    Инвариант «ровно один открытый таймер на ребёнка» держит `repo`, а не база
    (см. докстроку `Event`), — значит и закрывать предыдущий должен `repo`, а
    не каждый вызывающий по-своему. Вызывающих двое: кнопка «начали»
    (`handlers/track`) и диктовка «уснула» (`handlers/voice`), и две пары голых
    вызовов `any_open_event` + `close_event` разошлись бы молча — например, в
    тот день, когда одному из них понадобится `estimated`.

    Порядок внутри существен: закрывать надо ДО создания новой записи.
    `any_open_event` сортирует по `started_at desc`, поэтому вызов после
    создания закрыл бы только что открытый таймер, а прежний остался бы висеть
    — и дождался бы тикера, который пометит его `estimated`. В отчёте для
    педиатра это выглядит как измеренный многочасовой сон.

    `estimated` не ставим: время закрытия известно, это «сейчас». Пометка
    означает догадку тикера, и ставить её здесь значило бы врать в обратную
    сторону.
    """
    running = await any_open_event(session, child_id)
    if running is None:
        return None
    return await close_event(session, running, now)


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


async def reopen_event(session: AsyncSession, event: Event) -> Event:
    """Вернуть закрытому таймеру ход — отмена случайного «Закончить».

    Не то же самое, что удалить запись, и разница здесь принципиальная. Промах
    по кнопке окончания означает «сон продолжается», а не «сна не было»; до
    этой функции единственной кнопкой под ответом была отмена, и она стирала
    весь сон целиком — то есть лечила промах потерей данных.

    `estimated` снимается вместе с `ended_at`: пометка означает догадку тикера
    о конце, а конца снова нет. Оставленная, она пережила бы следующее, уже
    честное закрытие и соврала бы в отчёте для педиатра.

    Инвариант «ровно один открытый таймер» проверяет вызывающий: здесь нет
    `child_id`, а тащить его сюда ради одной проверки значило бы дать функции
    знание о том, чего она не решает.
    """
    event.ended_at = None
    event.estimated = False
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


# --- Правка событий -------------------------------------------------------
#
# Три узкие функции по имени намерения, а не одна широкая с «`None` — не
# трогать», как у `update_child` и `update_measurement`. Причина не в чистоте:
# правке вида нужно уметь **стирать** `value_num` и `side`, а общее соглашение
# пришлось бы ломать sentinel-объектом ради одного вызывающего.
#
# Отсюда же второе: каждая пишет всё, чем владеет, — доктрина `set_panel`. У
# функции, которая пишет поле «иногда», появляется состояние «не тронули», и
# оно рано или поздно расходится с тем, что человек видел на экране.


async def set_event_time(
    session: AsyncSession,
    event: Event,
    *,
    started_at: datetime,
    ended_at: datetime | None,
    estimated: bool,
) -> Event:
    """Время правит человек. Все три поля пишутся всегда.

    Проверок правдоподобия здесь нет намеренно: границы зависят от дня
    рождения ребёнка и от текущего момента, а `repo` не знает ни того, ни
    другого. Судит `services/records.fits`, и судит дважды — рисуя кнопки и
    принимая обратно то, что пришло с клиента.

    `estimated` передаёт вызывающий, и это не лень. Пометка означает ровно
    одно — конец назначил тикер, — поэтому правка **конца** её снимает, а
    правка **начала** оставляет: конец от сдвинутого начала честнее не стал.
    Политика эта про смысл операции, а не про строку в таблице, и живёт там
    же, где инвариант «ровно один открытый таймер», — у вызывающего.
    """
    event.started_at = started_at
    event.ended_at = ended_at
    event.estimated = estimated
    await session.commit()
    return event


async def set_event_kind(
    session: AsyncSession,
    event: Event,
    *,
    kind: str,
    side: str | None,
    value_num: float | None,
) -> Event:
    """Вид и всё, что от него зависит.

    Сторона и число пишутся здесь же и всегда: оставленные от прежнего вида,
    они дали бы «😴 Сон, 90 мл» — число, которое печатает выгрузка для
    педиатра и не печатает ни один экран бота, то есть увидеть его было бы
    нечем. Что именно переживает смену, решает `records.after_kind_change`.

    `subtype` и `note` не трогаются: это слова человека из голосового разбора,
    и стереть их молча было бы потерей, а не уборкой.
    """
    event.kind = kind
    event.side = side
    event.value_num = value_num
    await session.commit()
    return event


async def set_event_value(
    session: AsyncSession, event: Event, value_num: float | None
) -> Event:
    """Число у записи: миллилитры у бутылочки и сцеживания.

    Заведена не только ради правки. До неё `handlers/measure.number_reply`
    писал `event.value_num` напрямую — единственное место проекта, где событие
    менялось в обход `repo`, и потому единственное, которого не видел ни один
    сторож этого файла.
    """
    event.value_num = value_num
    await session.commit()
    return event


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


async def has_events_since(
    session: AsyncSession, child_id: int, since: datetime
) -> bool:
    """Записывали ли хоть что-нибудь после `since`.

    Своя функция, а не `count_events_since` с полным набором видов: там фильтр
    по видам обязателен, а здесь его нет намеренно. Вопрос «что было с
    прошлого раза?» задаётся не про конкретный вид, а про тишину в дневнике:
    любая запись означает, что семья сейчас пользуется ботом, и спрашивать её
    незачем.

    Взвешивания при этом не считаются — они в другой таблице, они редкие, и
    карточка опроса про них не спрашивает. Семья, которая за три часа только
    взвесила ребёнка, вопрос получит, и это решение, а не недосмотр.
    """
    found = await session.scalar(
        select(Event.id)
        .where(Event.child_id == child_id, Event.started_at >= since)
        .limit(1)
    )
    return found is not None


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


async def measurements_between(
    session: AsyncSession, child_id: int, start: datetime, end: datetime
) -> list[Measurement]:
    """Все измерения ребёнка за полуинтервал [start, end), любого вида.

    Соседка `measurements` фильтрует по одному виду и окна не знает вовсе —
    она собирает график. Здесь нужен день целиком: список записей показывает
    взвешивание вперемешку с кормлениями, в том порядке, в каком они были.
    """
    result = await session.scalars(
        select(Measurement)
        .where(
            Measurement.child_id == child_id,
            Measurement.taken_at >= start,
            Measurement.taken_at < end,
        )
        .order_by(Measurement.taken_at)
    )
    return list(result)


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


async def deactivate_preset(session: AsyncSession, family_id: int, preset: str) -> int:
    """Погасить весь пресет и сказать, сколько напоминаний сняли.

    `reminder_by_preset` возвращает одно, и для «витамина D» этого хватает.
    Но подписка на прививки — это полтора десятка разовых строк, по одной на
    возраст; выключить первую значит оставить четырнадцать работать.
    """
    reminders = await session.scalars(
        select(Reminder).where(
            Reminder.family_id == family_id,
            Reminder.preset == preset,
            Reminder.active.is_(True),
        )
    )
    count = 0
    for reminder in reminders:
        reminder.active = False
        count += 1
    await session.commit()
    return count


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


# --- Учёт использования ---------------------------------------------------


async def add_usage(
    session: AsyncSession,
    *,
    at: datetime,
    family_id: int | None,
    member_id: int | None,
    kind: str,
    action: str,
    detail: str,
    handled: bool,
    ms: int,
) -> None:
    """Записать, что кто-то что-то нажал.

    Аргументы плоские, без объектов ORM, и это существенно: зовут эту функцию
    из `UsageMiddleware`, а та пишет в **свою** сессию, не в ту, которую
    держит `FamilyMiddleware`. Приняв `Family`, мы притащили бы объект чужой
    сессии — и первое же обращение к непрогруженному атрибуту дало бы
    `MissingGreenlet` посреди побочной записи.

    `detail` приходит строкой, а пустая едет в базу как `NULL`: отличать
    «вердикт без вопроса» от «действие, у которого подробности не бывает»
    отчёту придётся, и пустая строка эту разницу стёрла бы.
    """
    session.add(
        Usage(
            at=at,
            family_id=family_id,
            member_id=member_id,
            kind=kind,
            action=action[:64],
            detail=detail[:32] or None,
            handled=handled,
            ms=ms,
        )
    )
    await session.commit()
