"""Периодический опрос: карточка «Что было с прошлого раза?» и её настройка.

Весь ввод здесь кнопочный. Текстовых обработчиков в роутере нет ни одного, и
границу `alarm → qa → measure` он не двигает и двигать не может: aiogram
разводит колбэки по префиксу `callback_data`, а префикс `ci` не занят никем.

**Состояние диалога целиком едет в `callback_data`.** Черновиков в памяти нет —
ни словаря «кто на каком экране», ни отметки «эта карточка уже отвечена».
Сначала это было удобством, потом стало решением: модульное состояние
переживает тест, требует autouse-фикстуры и отдельного ответа на вопрос «что
после перезапуска». У карточки, вся память которой — её собственные кнопки,
такого вопроса нет вовсе: вчерашняя работает ровно так же, как сегодняшняя.

**Действие лежит в кнопке, а не выводится из того, что на экране.** Кнопки
живут в истории чата вечно, и хендлер, угадывающий «а это включить или
выключить», однажды угадает неправильно. Поэтому у «не спрашивать» с карточки
и у тапа по ряду на экране `/remind` разные действия, хотя с настройкой они
делают одно и то же.

Порядок проверок обратный `track`: настройка опроса работает и без карточки
ребёнка. Включить опрос заранее — законно, а `texts.NO_CHILD` в ответ на
попытку что-то настроить выглядит как поломка.
"""

import logging
from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import TIMED_KINDS, Child, Event, Family, Member
from bot.filters import IN_GROUP_CB
from bot.handlers import remind
from bot.keyboards import CheckinCB
from bot.services import checkin, panel
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

router = Router(name="checkin")


def _last_id(call: CallbackQuery) -> int | None:
    """message_id, по которому панель судит, далеко ли она уехала."""
    return call.message.message_id if isinstance(call.message, Message) else None


async def _say(
    call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Ответить в чат отдельным сообщением. Двойник `track._say`.

    `markup or keyboards.home()` — выбор, а не сумма: подтверждение записи
    несёт свою кнопку отмены, а «ничего не записал» не несёт ничего и именно
    поэтому становится носителем постоянной клавиатуры.
    """
    if isinstance(call.message, Message):
        await call.message.answer(text, reply_markup=markup or keyboards.home())


async def _card(call: CallbackQuery, family: Family) -> None:
    """Вернуть сообщение к первому экрану карточки.

    Зовётся и по «⬅️ Назад», и после каждой записи. После записи — намеренно:
    «что было с прошлого раза» редко значит одну вещь, и после сна обычно идёт
    подгузник. Карточка, погасшая от первого ответа, заставляла бы искать
    панель ради второго.

    Часы берутся из настройки, а если опрос успели выключить с другого
    устройства — из файла: в тексте они только называют окно.
    """
    hours = family.checkin_hours or checkin.CARD.default_hours
    await _edit(
        call,
        texts.checkin_card(checkin.CARD, hours),
        keyboards.checkin_card(
            [(kind.kind, kind.title) for kind in checkin.CARD.kinds],
            nothing=checkin.CARD.nothing_label,
            mute=checkin.CARD.mute_label,
        ),
    )


async def _edit(
    call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None
) -> None:
    """Перерисовать саму карточку. Ошибку правки глушим на DEBUG.

    Тот же случай, что на экране `/remind`: «message is not modified» при
    повторном тапе — самый частый исход и не стоит строчки в логе выше DEBUG.
    """
    if not isinstance(call.message, Message):
        return
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        log.debug("Карточку опроса не перерисовали", exc_info=True)


@router.callback_query(IN_GROUP_CB, CheckinCB.filter())
async def tap(
    call: CallbackQuery,
    callback_data: CheckinCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child | None,
) -> None:
    """Единственный вход. Диспетчеризация по `action`, как в `track.track`."""
    now = tu.now_utc()
    action = callback_data.action

    if action in _SETTINGS:
        await _SETTINGS[action](call, callback_data, session, family, now)
        return

    if child is None:
        await call.answer(texts.NO_CHILD, show_alert=True)
        return

    handler = _CARD.get(action)
    if handler is None:
        # Кнопка из будущей версии бота или испорченный callback_data.
        # Молчать нельзя: Telegram крутит «часики» до ответа
        await call.answer()
        return

    await handler(call, callback_data, session, bot, family, member, child, now)


# --- Настройка ------------------------------------------------------------


async def _pick(
    call: CallbackQuery,
    data: CheckinCB,
    session: AsyncSession,
    family: Family,
    now: datetime,
) -> None:
    """Спросить интервал перед включением. Близнец того же шага в `/remind`."""
    await call.answer()
    await _say(call, texts.CHECKIN_PICK, keyboards.checkin_hours(checkin.CARD.choices))


async def _on(
    call: CallbackQuery,
    data: CheckinCB,
    session: AsyncSession,
    family: Family,
    now: datetime,
) -> None:
    """Включить опрос с выбранным интервалом.

    Интервал сверяется со списком из файла: `callback_data` приходит с клиента,
    и `hours=1000` иначе завело бы опрос, который не придёт никогда.
    """
    hours = data.hours or checkin.CARD.default_hours
    if hours not in checkin.CARD.choices:
        await call.answer(texts.CHECKIN_STALE)
        return
    await repo.set_checkin(session, family, hours, now)
    await call.answer()
    await _say(call, texts.checkin_turned_on(checkin.CARD, hours))
    await remind.redraw(call, session, family, now)


async def _off(
    call: CallbackQuery,
    data: CheckinCB,
    session: AsyncSession,
    family: Family,
    now: datetime,
) -> None:
    """Выключить с экрана `/remind` — и перерисовать этот экран."""
    await repo.set_checkin(session, family, 0, now)
    await call.answer()
    await _say(call, texts.CHECKIN_TURNED_OFF)
    await remind.redraw(call, session, family, now)


async def _mute(
    call: CallbackQuery,
    data: CheckinCB,
    session: AsyncSession,
    family: Family,
    now: datetime,
) -> None:
    """«Не спрашивать» с самой карточки.

    Говорим вслух, а не гасим молча: молча выключенная функция читается как
    сломавшаяся. Разметку с карточки снимаем, чтобы второй тап не выключал уже
    выключенное по второму разу.
    """
    await repo.set_checkin(session, family, 0, now)
    await call.answer()
    if isinstance(call.message, Message):
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            log.debug("Кнопки карточки опроса не сняли", exc_info=True)
    await _say(call, texts.CHECKIN_MUTED)


# --- Карточка -------------------------------------------------------------


async def _ask_when(
    call: CallbackQuery,
    data: CheckinCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """Второй экран: как давно это было."""
    kind = checkin.BY_KIND.get(data.kind)
    if kind is None:
        await call.answer(texts.CHECKIN_STALE)
        return
    await call.answer()
    await _edit(
        call,
        texts.checkin_when(kind),
        keyboards.checkin_when(
            kind.kind, [(shift.minutes, shift.label) for shift in checkin.CARD.shifts]
        ),
    )


async def _back(
    call: CallbackQuery,
    data: CheckinCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    await call.answer()
    await _card(call, family)


async def _chose_when(
    call: CallbackQuery,
    data: CheckinCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """Сдвиг выбран. Мгновенный вид пишется сразу, длящийся спрашивает длительность.

    Здесь и проходит вся разница между двумя и тремя тапами. Длящийся вид,
    записанный на этом шаге, остался бы без `ended_at` — то есть стал бы идущим
    таймером, открытым час назад, и тикер закрыл бы его с пометкой
    «приблизительная» на отметке «плюс четыре часа». В отчёте для педиатра это
    выглядит как честно засечённый четырёхчасовой сон.
    """
    kind = checkin.BY_KIND.get(data.kind)
    shift = checkin.shift_by(data.back)
    if kind is None or shift is None:
        await call.answer(texts.CHECKIN_STALE)
        return

    if kind.kind not in TIMED_KINDS:
        event = await repo.add_event(
            session,
            child.id,
            member.id,
            kind.kind,
            started_at=now - timedelta(minutes=shift.minutes),
            source_chat_id=family.chat_id,
            source_message_id=_last_id(call),
        )
        await _written(call, bot, family, event)
        return

    durations = checkin.durations_for(kind.kind, shift.minutes)
    await call.answer()
    await _edit(
        call,
        texts.checkin_how_long(kind, shift, closed=bool(durations)),
        keyboards.checkin_how_long(
            kind.kind,
            shift.minutes,
            [
                (minutes, texts.duration(timedelta(minutes=minutes)))
                for minutes in durations
            ],
            still=checkin.CARD.still_label,
        ),
    )


async def _chose_duration(
    call: CallbackQuery,
    data: CheckinCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """Длительность названа — пишем закончившийся отрезок.

    `estimated` остаётся `False`: пометка означает догадку тикера, и третьего
    смысла у неё нет. Длительность здесь назвал человек, пусть и кнопкой из
    четырёх вариантов, — ровно как время закрытия у `repo.close_running`.

    Длительность сверяется с тем же фильтром, который рисовал кнопки: без
    сверки подделанное `mins` дало бы `ended_at` в будущем, а это ломает
    обрезку событий по границам суток и всю сводку разом.
    """
    kind = checkin.BY_KIND.get(data.kind)
    shift = checkin.shift_by(data.back)
    if kind is None or shift is None:
        await call.answer(texts.CHECKIN_STALE)
        return
    if data.mins not in checkin.durations_for(kind.kind, shift.minutes):
        await call.answer(texts.CHECKIN_STALE)
        return

    started_at = now - timedelta(minutes=shift.minutes)
    event = await repo.add_event(
        session,
        child.id,
        member.id,
        kind.kind,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=data.mins),
        source_chat_id=family.chat_id,
        source_message_id=_last_id(call),
    )
    await _written(call, bot, family, event)


async def _still_running(
    call: CallbackQuery,
    data: CheckinCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """«Ещё идёт» — открыть таймер задним числом.

    Единственный ответ, у которого конца нет вовсе: «уснула час назад и спит до
    сих пор». Дальше работает уже написанное: тикер закроет таймер на отметке
    «плюс четыре часа» с пометкой «приблизительная» и **скажет об этом вслух**.
    Догадка остаётся догадкой и объявляется — в отличие от длительности,
    подставленной из файла, которую никто бы не увидел.

    Предыдущий таймер закрывается моментом начала нового, и закрывается ДО
    создания: `any_open_event` сортирует по `started_at desc`, и вызов после
    создания закрыл бы только что открытый. Говорим об этом вслух — панель под
    пальцем не меняется, как у кнопки, а перерисовка отложена дебаунсом.
    """
    kind = checkin.BY_KIND.get(data.kind)
    shift = checkin.shift_by(data.back)
    if kind is None or shift is None or kind.kind not in TIMED_KINDS:
        await call.answer(texts.CHECKIN_STALE)
        return

    started_at = now - timedelta(minutes=shift.minutes)
    closed = await repo.close_running(session, child.id, started_at)
    event = await repo.start_event(
        session,
        child.id,
        member.id,
        kind.kind,
        started_at=started_at,
        source_chat_id=family.chat_id,
        source_message_id=_last_id(call),
    )
    if closed is not None:
        await _say(call, texts.closed_previous(closed, family.tz))
    await _written(call, bot, family, event)


async def _nothing(
    call: CallbackQuery,
    data: CheckinCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child,
    now: datetime,
) -> None:
    """«Ничего не было» — не пишем ничего и говорим об этом.

    Тихий отказ выглядел бы как зависший бот: человек тапнул, кнопки пропали,
    в ленте пусто.
    """
    await call.answer()
    if isinstance(call.message, Message):
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            log.debug("Кнопки карточки опроса не сняли", exc_info=True)
    await _say(call, texts.CHECKIN_NOTHING)


async def _written(
    call: CallbackQuery, bot: Bot, family: Family, event: Event
) -> None:
    """Общий хвост всех трёх записей: ответ, отмена, карточка заново, панель.

    Порядок как в `track`: всплывашка снимает «часики», подтверждение уходит в
    ленту отдельным сообщением со своей кнопкой отмены, и только последней
    планируется перерисовка панели — ответ человеку раньше панели.
    """
    await call.answer("Записал")
    await _say(
        call, texts.checkin_logged(event, family.tz), keyboards.undo("event", event.id)
    )
    await _card(call, family)
    panel.schedule(bot, family.id, _last_id(call))


_SETTINGS = {"pick": _pick, "on": _on, "off": _off, "mute": _mute}

_CARD = {
    "kind": _ask_when,
    "back": _back,
    "when": _chose_when,
    "dur": _chose_duration,
    "run": _still_running,
    "none": _nothing,
}
