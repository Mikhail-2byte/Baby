"""Правка уже сделанной записи: список за сутки, карточка, время, вид, удаление.

До этого роутера правки не существовало как класса операции. Запись можно было
удалить — и только кнопкой под собственным подтверждением, то есть пока оно не
уехало вверх по чату; полей `kind`, `started_at`, `side` не менял вообще
ничто. Жалоба, с которой всё началось, звучала так: «если проходит кормление
грудью, женщины забывают выключать; или покормила и забыла».

Устройство целиком по образцу `handlers/checkin.py`, и два его решения
перенесены дословно.

**Состояние диалога едет в `callback_data`.** Ни словаря «кто на каком экране»,
ни черновика в памяти. Вчерашняя карточка работает ровно как сегодняшняя, и
вопроса «что после перезапуска» не возникает вовсе. Сюда же относится
накопленный сдвиг времени: он лежит в `mins` самих кнопок, а не в переменной.

**Действие лежит в кнопке, а не выводится из того, что на экране.** Отсюда две
пары, которые иначе выглядели бы дублями: `list`/`open` правят сообщение, под
которым нажали, а `card` присылает новое. Кнопка «✏️ Поправить» висит под
подтверждением записи, а подтверждение — общий журнал семьи; превратив его в
экран настроек, мы стёрли бы то, ради чего подтверждения вообще уходят
отдельными сообщениями.

**Ничего не сохраняется молча, и сдвиг времени тут не исключение.** Экран
показывает «Было … Станет …», в базу не пишет ничего, и только «✅ Готово»
зовёт `repo` — ровно как карточка голосового ждёт «Записать». Цена названа
вслух: между первым тапом и «Готово» ту же запись мог подвинуть второй
родитель, поэтому на «Готово» границы проверяются **заново**, по свежему
значению из базы.

**Принадлежность записи ребёнку проверяется в каждой ветке.** `callback_data`
приходит с клиента, а id событий сквозные по всей базе: без сверки чат,
знающий номер, правил бы чужой дневник. Этот экран вдобавок раздаёт номера
записей в чат пачками, то есть переводит вопрос из теоретических в
достижимые.
"""

import logging
from datetime import date, datetime, timedelta

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import (
    FEED_BOTTLE,
    PUMP,
    WEIGHT,
    Child,
    Event,
    Family,
    Measurement,
    Member,
)
from bot.filters import IN_GROUP_CB
from bot.keyboards import RecordCB
from bot.services import panel, pending, records, revisions, tracker_guard
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

router = Router(name="record")

# Правка объёма переиспользует ожидание числа целиком, включая разбор в
# `measure.number_reply`. Свой у неё только текст вопроса: у `track.ASK_TEXT`
# хвост «Или не отвечайте — запишу без объёма», который здесь врёт — объём уже
# записан, и молчание оставит его прежним, а не сотрёт
ASK_ML = {FEED_BOTTLE: pending.BOTTLE_ML, PUMP: pending.PUMP_ML}

Row = Event | Measurement


def _last_id(call: CallbackQuery) -> int | None:
    """message_id, по которому панель судит, далеко ли она уехала."""
    return call.message.message_id if isinstance(call.message, Message) else None


async def _say(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Ответить в чат отдельным сообщением. Двойник `track._say`.

    Подтверждение правки обязано остаться в ленте по той же причине, по какой
    там остаётся подтверждение записи: второй родитель должен видеть, что
    происходило ночью, не открывая ни одного экрана.
    """
    if isinstance(call.message, Message):
        await call.message.answer(text, reply_markup=markup or keyboards.home())


async def _edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Перерисовать сам экран. Ошибку правки глушим на DEBUG.

    Тот же случай, что на экране `/remind` и у карточки опроса: «message is not
    modified» при повторном тапе — самый частый исход и не стоит строчки в логе
    выше DEBUG.
    """
    if not isinstance(call.message, Message):
        return
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        log.debug("Экран правки не перерисовали", exc_info=True)


@router.callback_query(IN_GROUP_CB, RecordCB.filter())
async def tap(
    call: CallbackQuery,
    callback_data: RecordCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child | None,
) -> None:
    """Единственный вход. Диспетчеризация по `action`, как в `checkin.tap`.

    Порядок проверок обычный для проекта, а не обратный, как у опроса: без
    карточки ребёнка нет и записей, поэтому править тут нечего в буквальном
    смысле.
    """
    if child is None:
        await call.answer(texts.NO_CHILD, show_alert=True)
        return

    handler = _ACTIONS.get(callback_data.action)
    if handler is None:
        # Кнопка из будущей версии бота или испорченный callback_data.
        # Молчать нельзя: Telegram крутит «часики» до ответа
        await call.answer()
        return

    try:
        with revisions.actor(member.id):
            async with tracker_guard.for_child(child.id):
                await handler(
                    call, callback_data, session, bot, family, member, child, tu.now_utc()
                )
    except Exception:
        panel.schedule(bot, family.id, _last_id(call))
        raise


# --- Список ---------------------------------------------------------------


async def _rows(
    session: AsyncSession, child: Child, family: Family, day: int, now: datetime
) -> tuple[list[Row], date]:
    """Все записи за локальные сутки `day` назад, в хронологическом порядке.

    События берутся `events_between`, то есть **пересекающиеся** с сутками:
    сон с 23:40 до 06:10 стоит в обоих днях, и искать его наутро человек будет
    в утренних сутках, а не во вчерашних.
    """
    target = tu.local_today(family.tz, now) - timedelta(days=day)
    start, end = tu.day_bounds(target, family.tz)
    events = await repo.events_between(session, child.id, start, end)
    measured = await repo.measurements_between(session, child.id, start, end)

    rows: list[Row] = [*events, *measured]
    rows.sort(key=_moment)
    return rows, target


def _moment(row: Row) -> datetime:
    return row.taken_at if isinstance(row, Measurement) else row.started_at


async def _list(
    call: CallbackQuery,
    data: RecordCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    await call.answer()
    await _draw_list(call, data.day, data.page, session, family, child, now)


async def _draw_list(
    call: CallbackQuery,
    day: int,
    page: int,
    session: AsyncSession,
    family: Family,
    child: Child,
    now: datetime,
) -> None:
    """Общий рисовальщик списка: зовут и кнопка, и возврат после удаления."""
    rows, target = await _rows(session, child, family, day, now)
    window, first, earlier, later = records.page_of(rows, page)

    born = tu.to_local(child.born_at, family.tz).date()
    await _edit(
        call,
        texts.records_screen(
            window,
            family.tz,
            target,
            tu.local_today(family.tz, now),
            first=first,
            total=len(rows),
        ),
        keyboards.records(
            [(first + offset, _what(row), row.id) for offset, row in enumerate(window)],
            day=day,
            page=page,
            earlier=earlier,
            later=later,
            back_day=target > born,
            forward_day=day > 0,
        ),
    )


def _what(row: Row) -> str:
    return "m" if isinstance(row, Measurement) else "e"


# --- Карточка -------------------------------------------------------------


async def _fetch(session: AsyncSession, data: RecordCB, child: Child) -> Row | None:
    """Запись по номеру из кнопки — и только своего ребёнка.

    Голый `session.get` по id, пришедшему с клиента, дал бы чужому чату доступ
    к чужому дневнику по одному угаданному числу. Проверка живёт здесь, а не в
    `repo`: `repo` не знает, чей это апдейт, и знать не должен.
    """
    row: Row | None
    if data.what == "m":
        row = await repo.get_measurement(session, data.row_id)
    else:
        row = await repo.get_event(session, data.row_id)
    if row is None or row.child_id != child.id:
        return None
    return row


def _is_birth_weight(row: Row, child: Child) -> bool:
    """Точка веса при рождении — вторая половина пары с `children.birth_weight_g`.

    Поправив её отсюда, мы развели бы карточку ребёнка с графиком, и заметить
    это можно было бы только глазами. Правится она в `/baby`, где правятся обе
    половины разом.
    """
    return isinstance(row, Measurement) and row.kind == WEIGHT and row.taken_at == child.born_at


def _card_markup(row: Row, data: RecordCB, child: Child) -> InlineKeyboardMarkup:
    if _is_birth_weight(row, child):
        return keyboards.record_card(
            what=data.what,
            row_id=row.id,
            day=data.day,
            page=data.page,
            delete=False,
        )
    if isinstance(row, Measurement):
        return keyboards.record_card(
            what="m", row_id=row.id, day=data.day, page=data.page, fields=("t",)
        )
    return keyboards.record_card(
        what="e",
        row_id=row.id,
        day=data.day,
        page=data.page,
        fields=records.fields_of(row),
        value=records.carries_value(row.kind),
        kinds=True,
    )


def _card_text(row: Row, family: Family, child: Child, now: datetime) -> str:
    text = texts.record_card(row, family.tz, now)
    if _is_birth_weight(row, child):
        text = f"{text}\n\n{texts.RECORD_BIRTH_WEIGHT}"
    return text


async def _open(
    call: CallbackQuery,
    data: RecordCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """Карточка на месте: правит то сообщение, под которым нажали."""
    row = await _fetch(session, data, child)
    if row is None:
        await call.answer(texts.RECORD_GONE, show_alert=True)
        return
    await call.answer()
    await _edit(call, _card_text(row, family, child, now), _card_markup(row, data, child))


async def _card(
    call: CallbackQuery,
    data: RecordCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """Карточка новым сообщением: тап пришёл из ленты, а не с экрана.

    Ленту затирать нельзя. Под подтверждением «🍼 Бутылочка в 03:20, 60 мл» и
    под сообщением тикера о закрытом таймере стоит та же кнопка, но оба этих
    сообщения — свидетельства, а не экраны.

    `field` в кнопке означает «открыть сразу на сдвиге этого времени»: под
    сообщением о забытом таймере ехать через карточку незачем, человеку уже
    сказали, что не так.
    """
    row = await _fetch(session, data, child)
    if row is None:
        await call.answer(texts.RECORD_GONE, show_alert=True)
        return
    if data.field and isinstance(row, Event) and data.field in records.fields_of(row):
        from bot.handlers.timing import ask_time

        await ask_time(call, session, family, child, row.id, data.field, "e")
        return
    await call.answer()
    await _say(call, _card_text(row, family, child, now), _card_markup(row, data, child))


# --- Время ----------------------------------------------------------------


def _current(row: Row, field: str) -> datetime:
    if isinstance(row, Measurement):
        return row.taken_at
    if field == records.END and row.ended_at is not None:
        return row.ended_at
    return row.started_at


def _time_screen(
    row: Row,
    data: RecordCB,
    family: Family,
    child: Child,
    now: datetime,
    field: str,
    mins: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопки экрана сдвига — на всех путях одни и те же.

    Собирается отдельной функцией, потому что вызывающих трое: вход с карточки,
    каждый тап по шагу и вход прямо из сообщения тикера. Три копии разошлись бы
    молча, и расхождение выглядело бы как работающий экран.
    """
    was = _current(row, field)
    if isinstance(row, Measurement):
        steps = _measure_steps(row, mins, child, now)
        becomes = was + timedelta(minutes=mins)
    else:
        steps = records.steps_for(row, field, mins, born_at=child.born_at, now=now)
        started, ended = records.preview(row, field, mins)
        becomes = ended if field == records.END and ended is not None else started

    return (
        texts.record_time(row, family.tz, field, was, becomes, now),
        keyboards.record_time(
            [(step, records.STEP_LABELS[step]) for step in steps],
            what=data.what,
            row_id=row.id,
            field=field,
            mins=mins,
            day=data.day,
            page=data.page,
        ),
    )


def _measure_steps(row: Measurement, mins: int, child: Child, now: datetime) -> tuple[int, ...]:
    """Те же границы, что у события, но точке конца не бывает.

    Взвешивание — момент, поэтому `fits` зовётся с пустым `ended_at`: судить
    надо только «не раньше рождения и не в будущем».
    """
    return tuple(
        step
        for step in records.STEPS
        if records.fits(
            row.taken_at + timedelta(minutes=mins + step),
            None,
            born_at=child.born_at,
            now=now,
        )
    )


async def _time(
    call: CallbackQuery,
    data: RecordCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """Вход на экран сдвига с карточки. Накопленного сдвига ещё нет."""
    row = await _fetch(session, data, child)
    if row is None or _is_birth_weight(row, child):
        await call.answer(texts.RECORD_GONE, show_alert=True)
        return
    await call.answer()
    await _edit(call, *_time_screen(row, data, family, child, now, data.field, 0))


async def _shift(
    call: CallbackQuery,
    data: RecordCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """Тап по шагу. В базу не пишет — перерисовывает экран с новым черновиком."""
    row = await _fetch(session, data, child)
    if row is None or _is_birth_weight(row, child):
        await call.answer(texts.RECORD_GONE, show_alert=True)
        return
    await call.answer()
    await _edit(call, *_time_screen(row, data, family, child, now, data.field, data.mins))


async def _save(
    call: CallbackQuery,
    data: RecordCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """«✅ Готово» — единственное место, которое пишет время в базу.

    Границы проверяются **заново**, а не на доверии к кнопке: `callback_data`
    приходит с клиента, и подделанный `mins` иначе уехал бы в будущее или за
    день рождения. Сверка с тем же фильтром, который рисовал кнопки, — то же
    решение, что у `checkin._chose_duration`.

    Пометку `estimated` снимает только правка **конца**. Она означает ровно
    одно — конец назначил тикер, — и сдвинутое начало этого не отменяет.
    """
    row = await _fetch(session, data, child)
    if row is None or _is_birth_weight(row, child):
        await call.answer(texts.RECORD_GONE, show_alert=True)
        return

    was = _current(row, data.field)
    dropped = False

    if isinstance(row, Measurement):
        moved = was + timedelta(minutes=data.mins)
        if not records.fits(moved, None, born_at=child.born_at, now=now):
            await call.answer(texts.RECORD_STALE, show_alert=True)
            return
        await repo.update_measurement(session, row, taken_at=moved)
    else:
        started, ended = records.preview(row, data.field, data.mins)
        if not records.fits(started, ended, born_at=child.born_at, now=now):
            await call.answer(texts.RECORD_STALE, show_alert=True)
            return
        dropped = row.estimated and data.field == records.END
        await repo.set_event_time(
            session,
            row,
            started_at=started,
            ended_at=ended,
            estimated=row.estimated and not dropped,
        )

    await call.answer("Поправил")
    await _say(call, texts.record_moved(row, family.tz, data.field, was))
    if dropped:
        await _say(call, texts.ESTIMATE_DROPPED)
    await _edit(call, _card_text(row, family, child, now), _card_markup(row, data, child))
    panel.schedule(bot, family.id, _last_id(call))


# --- Вид ------------------------------------------------------------------


async def _kinds(
    call: CallbackQuery,
    data: RecordCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    row = await _fetch(session, data, child)
    if row is None or not isinstance(row, Event):
        await call.answer(texts.RECORD_GONE, show_alert=True)
        return
    await call.answer()

    choices = records.kinds_for(row)
    await _edit(
        call,
        f"{texts.record_card(row, family.tz, now)}\n\n{texts.RECORD_KINDS_ASK}",
        keyboards.record_kinds(
            [
                (
                    choice.kind,
                    choice.side,
                    f"• {choice.label}" if records.same_choice(row, choice) else choice.label,
                )
                for choice in choices
            ],
            row_id=row.id,
            day=data.day,
            page=data.page,
        ),
    )


async def _setkind(
    call: CallbackQuery,
    data: RecordCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """Сменить вид, не пересекая границу формы записи.

    Сверка с `records.allows` обязательна, и не только против подделки.
    `row_id` в SQLite переиспользуется: удалив последнюю запись и заведя новую,
    получаем тот же номер у другого события — а кнопка вчерашнего экрана всё
    ещё на него указывает. Подделанный или устаревший `kind=sleep` у подгузника
    завёл бы идущий таймер задним числом, и тикер закрыл бы его на отметке
    «плюс четыре часа» с пометкой «приблизительная».
    """
    row = await _fetch(session, data, child)
    if row is None or not isinstance(row, Event):
        await call.answer(texts.RECORD_GONE, show_alert=True)
        return
    if not records.allows(row, data.kind):
        await call.answer(texts.RECORD_WRONG_KIND, show_alert=True)
        return

    was_name = (
        f"{texts.KIND_ICONS.get(row.kind, '📝')} "
        f"{texts.KIND_NAMES.get(row.kind, row.kind).capitalize()}"
    )
    if row.side:
        was_name += f" ({texts.SIDE_NAMES[row.side]})"
    lost = row.value_num

    side, value_num = records.after_kind_change(data.kind, "")
    await repo.set_event_kind(session, row, kind=data.kind, side=side, value_num=value_num)

    dropped = (
        texts.record_value_dropped(data.kind, lost)
        if lost is not None and value_num is None
        else None
    )
    await call.answer("Поправил")
    await _say(call, texts.record_kind_changed(row, family.tz, was_name, dropped))
    await _edit(call, _card_text(row, family, child, now), _card_markup(row, data, child))
    panel.schedule(bot, family.id, _last_id(call))


# --- Объём ----------------------------------------------------------------


async def _ml(
    call: CallbackQuery,
    data: RecordCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """Спросить число. Разбирает его уже написанный `measure.number_reply`.

    Своего парсера здесь нет и не будет: ожидание числа — один механизм на
    проект, и вторая его копия разошлась бы с первой на первой же правке.
    """
    row = await _fetch(session, data, child)
    if row is None or not isinstance(row, Event) or not records.carries_value(row.kind):
        await call.answer(texts.RECORD_GONE, show_alert=True)
        return
    if call.from_user is None:
        await call.answer()
        return

    pending.remember(family.chat_id, call.from_user.id, ASK_ML[row.kind], row.id)
    await call.answer()
    await _say(call, texts.RECORD_ASK_ML)


# --- Удаление -------------------------------------------------------------


async def _ask(
    call: CallbackQuery,
    data: RecordCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """Спросить перед удалением — единственное необратимое действие правки."""
    row = await _fetch(session, data, child)
    if row is None or _is_birth_weight(row, child):
        await call.answer(texts.RECORD_GONE, show_alert=True)
        return
    await call.answer()
    await _edit(
        call,
        f"{texts.record_card(row, family.tz, now)}\n\n{texts.RECORD_ASK_DELETE}",
        keyboards.record_delete(what=data.what, row_id=row.id, day=data.day, page=data.page),
    )


async def _delete(
    call: CallbackQuery,
    data: RecordCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """Удалить и вернуться к списку — пропажа обязана быть видна.

    Подтверждение называет запись целиком, а не «Запись удалена»: отмены у
    этого действия нет, и единственная страховка от промаха — возможность
    завести её заново, не вспоминая, что в ней было.
    """
    row = await _fetch(session, data, child)
    if row is None:
        await call.answer(texts.RECORD_GONE, show_alert=True)
        return
    if _is_birth_weight(row, child):
        await call.answer(texts.RECORD_BIRTH_WEIGHT, show_alert=True)
        return

    farewell = texts.record_deleted(row, family.tz)
    if isinstance(row, Measurement):
        await repo.delete_measurement(session, row)
    else:
        await repo.delete_event(session, row)

    await call.answer(texts.UNDONE)
    await _say(call, farewell)
    await _draw_list(call, data.day, data.page, session, family, child, now)
    panel.schedule(bot, family.id, _last_id(call))


_ACTIONS = {
    "list": _list,
    "open": _open,
    "card": _card,
    "time": _time,
    "shift": _shift,
    "save": _save,
    "kinds": _kinds,
    "setkind": _setkind,
    "ml": _ml,
    "ask": _ask,
    "del": _delete,
}
