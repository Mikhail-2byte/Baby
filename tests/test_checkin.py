"""Периодический опрос: созревание, молчание и три экрана карточки.

Карточка гоняется через настоящий `Dispatcher` с боевым списком роутеров — по
той же причине, что и экран `/remind`: свой диспетчер из двух роутеров доказал
бы работу механизма при правильном порядке и промолчал бы о том, каков порядок
на самом деле.

Времени здесь не подменяют вовсе. Всё, что судит о сроках, принимает `now`
явным аргументом, а сдвигают данные — отметку в семье и время событий. Там, где
момент задаёт не тест, а сам хендлер (`tu.now_utc()` внутри), проверяется не
равенство, а расстояние: «началось около часа назад».
"""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import handlers, keyboards, texts
from bot.db import repo
from bot.db.models import (
    DIAPER_WET,
    FEED_BREAST,
    SLEEP,
    TIMED_KINDS,
)
from bot.keyboards import CheckinCB
from bot.services import checkin, sending, ticker
from bot.services import timeutil as tu

CHAT = Chat(id=-1001, type="supergroup", title="Малышка")
AUTHOR = User(id=222, is_bot=False, first_name="Аня")
NOW = datetime(2026, 9, 13, 12, 0)

# Полдень по Москве: и не ночь, и не граница окна. Всё, что про созревание,
# считается от этого момента сдвигом данных
DAY = datetime(2026, 9, 8, 9, 0)


class RecordingSession(BaseSession):
    """Сеть не трогаем: складываем отправленное и его разметку в списки."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []
        self.markups: list[dict | None] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Any, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        payload = method.model_dump(exclude_none=True)
        if "text" in payload:
            self.sent.append(payload["text"])
            self.markups.append(payload.get("reply_markup"))
        return Message(message_id=999, date=NOW, chat=CHAT, from_user=AUTHOR, text="")

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def wired(session, family, child, mother):
    """Диспетчер с боевыми роутерами и подстановкой, как у `FamilyMiddleware`."""
    bot = Bot(token="42:TEST", session=RecordingSession())
    dp = Dispatcher()

    async def inject(handler, event, data):
        data.update(session=session, family=family, child=child, member=mother)
        return await handler(event, data)

    dp.update.outer_middleware(inject)
    order = tuple(handlers.routers)
    for router in order:
        dp.include_router(router)

    yield dp, bot, bot.session

    # Роутеры — синглтоны уровня модуля: без отвязки второй тест в файле падает
    # на «Router is already attached»
    for router in order:
        router._parent_router = None


@pytest.fixture
def orphan(session, family, mother):
    """То же, но `child` подставляется пустым — семья без карточки ребёнка."""
    bot = Bot(token="42:TEST", session=RecordingSession())
    dp = Dispatcher()

    async def inject(handler, event, data):
        data.update(session=session, family=family, child=None, member=mother)
        return await handler(event, data)

    dp.update.outer_middleware(inject)
    order = tuple(handlers.routers)
    for router in order:
        dp.include_router(router)

    yield dp, bot, bot.session

    for router in order:
        router._parent_router = None


async def _tap(dp, bot, action, kind="", back=0, mins=0, hours=0, update_id=50):
    call = CallbackQuery(
        id=str(update_id),
        from_user=AUTHOR,
        chat_instance="x",
        data=CheckinCB(
            action=action, kind=kind, back=back, mins=mins, hours=hours
        ).pack(),
        message=Message(message_id=5, date=NOW, chat=CHAT, from_user=AUTHOR, text="🕒"),
    )
    await dp.feed_update(bot, Update(update_id=update_id, callback_query=call))


async def _events(session, child):
    """Все события ребёнка, свежие первыми."""
    return await repo.events_between(
        session, child.id, tu.now_utc() - timedelta(days=2), tu.now_utc() + timedelta(days=1)
    )


def _minutes_ago(moment: datetime) -> float:
    return (tu.now_utc() - moment).total_seconds() / 60


# --- Созревание -------------------------------------------------------------


def test_the_checkin_is_off_until_asked(family):
    """Бот молчит, пока его не попросили. Особенно — сам задавая вопросы.

    Значение по умолчанию у колонки, а не «включено с интервалом по
    умолчанию»: одна правка default превратила бы бота в того, кто заговорил
    с семьёй первым.
    """
    assert family.checkin_hours == 0
    assert checkin.due(family, DAY) is False


def test_a_matured_checkin_is_due(family):
    family.checkin_hours = 3
    family.checkin_last_at = DAY - timedelta(hours=3)
    assert checkin.due(family, DAY) is True


def test_a_young_checkin_is_not_due(family):
    """Без этой ветки вопрос приходил бы на каждом тике — 1440 раз в сутки.

    Ровно та болезнь, ради которой заведена таблица `alerts`.
    """
    family.checkin_hours = 3
    family.checkin_last_at = DAY - timedelta(hours=2, minutes=59)
    assert checkin.due(family, DAY) is False


def test_an_unstamped_checkin_asks_at_once(family):
    """Пустая отметка — правка руками в SQLite, и молчать из-за неё нельзя."""
    family.checkin_hours = 3
    family.checkin_last_at = None
    assert checkin.due(family, DAY) is True


def test_the_night_window_wraps_over_midnight(family):
    """Окно 23:00 → 07:00 переваливает через полночь.

    Форма `start <= t < end` дала бы пустое множество: ночная проверка не
    срабатывала бы ни разу, и узнать об этом можно было бы только по
    разбуженной в три часа семье.
    """
    # Москва = UTC+3
    assert checkin.in_quiet(family.tz, datetime(2026, 9, 8, 21, 30)) is True  # 00:30
    assert checkin.in_quiet(family.tz, datetime(2026, 9, 8, 20, 30)) is True  # 23:30
    assert checkin.in_quiet(family.tz, datetime(2026, 9, 8, 3, 30)) is True  # 06:30
    assert checkin.in_quiet(family.tz, datetime(2026, 9, 8, 5, 0)) is False  # 08:00
    assert checkin.in_quiet(family.tz, datetime(2026, 9, 8, 19, 0)) is False  # 22:00


def test_the_night_window_is_local_not_utc(family):
    """Один и тот же момент — ночь в одной зоне и день в другой.

    Сравнение в UTC сдвинуло бы тишину на три часа: у московской семьи бот
    замолкал бы в 02:00 и будил её в 04:00.
    """
    moment = datetime(2026, 9, 8, 21, 30)  # 00:30 в Москве, 16:30 в Денвере
    assert checkin.in_quiet("Europe/Moscow", moment) is True
    assert checkin.in_quiet("America/Denver", moment) is False


def test_a_matured_checkin_stays_silent_at_night(family):
    """Созрел, но ночь — значит молчим.

    Проверка ночи стоит в `due`, то есть ДО всякого обращения к базе и до
    отметки. Поставленная после, она молчала бы правильно и съедала бы при
    этом утренний вопрос.
    """
    family.checkin_hours = 3
    family.checkin_last_at = datetime(2026, 9, 8, 15, 0)
    assert checkin.due(family, datetime(2026, 9, 8, 21, 30)) is False


# --- Молчание при записях ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_checkin_says_nothing_when_there_are_records(
    session, family, child, mother, bot
):
    """Записали десять минут назад — спрашивать нечего.

    Без этой проверки бот спрашивал бы «что было с прошлого раза?» сразу после
    тапа по панели, то есть ровно у тех, кто и так всё записывает.
    """
    now = tu.now_utc()
    family.checkin_hours = 3
    family.checkin_last_at = now - timedelta(hours=3)
    await repo.add_event(
        session, child.id, mother.id, DIAPER_WET, started_at=now - timedelta(minutes=10)
    )

    await checkin.send_pending(bot, session, now)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_a_silent_checkin_leaves_the_clock_alone(
    session, family, child, mother, bot
):
    """Промолчали по записям — отметку не двигаем.

    Двинутая отметка отложила бы следующий вопрос на два интервала вместо
    одного, и заметить это можно было бы только сверкой часов с логом.
    """
    now = tu.now_utc()
    stamp = now - timedelta(hours=3)
    family.checkin_hours = 3
    family.checkin_last_at = stamp
    await repo.add_event(
        session, child.id, mother.id, DIAPER_WET, started_at=now - timedelta(minutes=10)
    )

    await checkin.send_pending(bot, session, now)

    assert family.checkin_last_at == stamp


@pytest.mark.asyncio
async def test_the_window_is_sliding_not_since_the_last_question(
    session, family, child, mother, bot
):
    """Окно равно интервалу, а не «всему, что прошло с прошлого вопроса».

    ПК проспал девять часов, кормление было пять часов назад. Окно в три часа
    пусто — значит спрашиваем. Окно «с прошлого вопроса» захватило бы то
    кормление и промолчало бы, хотя последние три часа в дневнике тишина.
    """
    now = tu.now_utc()
    family.checkin_hours = 3
    family.checkin_last_at = now - timedelta(hours=9)
    await repo.add_event(
        session, child.id, mother.id, DIAPER_WET, started_at=now - timedelta(hours=5)
    )

    await checkin.send_pending(bot, session, now)

    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_a_missed_checkin_is_never_caught_up(session, family, child, bot):
    """ПК спал двадцать часов — ровно одна карточка, а не семь.

    Это и есть структурная причина, по которой опрос не пресет напоминаний:
    те догоняются, и сводка «пропустил 7 напоминаний: • Что было с прошлого
    раза?» была бы бессмыслицей.
    """
    now = tu.now_utc()
    family.checkin_hours = 3
    family.checkin_last_at = now - timedelta(hours=20)

    await checkin.send_pending(bot, session, now)

    assert len(bot.sent) == 1
    assert "Что было с прошлого раза" in bot.texts[0]


@pytest.mark.asyncio
async def test_the_card_comes_with_buttons(session, family, child, bot):
    """Вопрос без кнопок — это не опрос, а укор."""
    now = tu.now_utc()
    family.checkin_hours = 3
    family.checkin_last_at = now - timedelta(hours=4)

    await checkin.send_pending(bot, session, now)

    markup = bot.kwargs[0]["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert checkin.CARD.nothing_label in labels
    assert checkin.CARD.mute_label in labels
    for kind in checkin.CARD.kinds:
        assert kind.title in labels


@pytest.mark.asyncio
async def test_the_stamp_is_not_set_when_telegram_says_retry(
    session, family, child, bot, monkeypatch
):
    """Сеть подвела — вопрос обязан прийти следующим тиком, а не пропасть."""
    now = tu.now_utc()
    stamp = now - timedelta(hours=4)
    family.checkin_hours = 3
    family.checkin_last_at = stamp

    async def flaky(*args, **kwargs):
        return sending.RETRY

    monkeypatch.setattr(checkin.sending, "deliver", flaky)
    await checkin.send_pending(bot, session, now)

    assert family.checkin_last_at == stamp


@pytest.mark.asyncio
async def test_a_broken_card_is_not_resent_every_tick(
    session, family, child, bot, monkeypatch
):
    """`BROKEN` отметку ставит: повтор даст ту же ошибку.

    Без отметки битая карточка лезла бы в Telegram каждую минуту до полуночи —
    та же доктрина, что у напоминаний в `ticker._handle`.
    """
    now = tu.now_utc()
    family.checkin_hours = 3
    family.checkin_last_at = now - timedelta(hours=4)

    async def broken(*args, **kwargs):
        return sending.BROKEN

    monkeypatch.setattr(checkin.sending, "deliver", broken)
    await checkin.send_pending(bot, session, now)

    assert family.checkin_last_at == now


@pytest.mark.asyncio
async def test_a_family_without_a_child_gets_no_question(session, family, bot):
    """Спрашивать «что было с ребёнком» не о ком — и в Telegram не ходим вовсе."""
    now = tu.now_utc()
    family.checkin_hours = 3
    family.checkin_last_at = now - timedelta(hours=4)

    await checkin.send_pending(bot, session, now)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_one_broken_family_does_not_stop_the_others(
    session, family, child, bot, monkeypatch
):
    """Контракт проекта: сбой одной семьи не срывает обход остальных."""
    now = tu.now_utc()
    family.checkin_hours = 3
    family.checkin_last_at = now - timedelta(hours=4)

    async def boom(*args, **kwargs):
        raise RuntimeError("Telegram отвалился")

    monkeypatch.setattr(checkin, "send_for", boom)
    await checkin.send_pending(bot, session, now)  # не должно упасть


@pytest.mark.asyncio
async def test_the_ticker_actually_runs_the_checkin(session, family, child, bot):
    """Модуль написан, тестами покрыт и не подключён — буквально урок 15.09.

    У фичи, доезжающей одним необязательным путём, нет ни одного пути; здесь
    путь один и он обязательный, поэтому и проверяется целиком — через
    `tick_once`, а не через `send_pending`.
    """
    now = tu.now_utc()
    family.checkin_hours = 3
    family.checkin_last_at = now - timedelta(hours=4)
    # Дайджест в этот же тик не нужен: он про другое и отправил бы два графика
    family.last_digest_on = tu.local_today(family.tz, now)
    await session.commit()

    await ticker.tick_once(bot, session, now)

    assert any("Что было с прошлого раза" in text for text in bot.texts)


# --- Содержание -------------------------------------------------------------


def test_the_card_offers_only_kinds_that_exist():
    """Опечатка в файле уедет в `events` видом, которого не знает ни сводка,
    ни выгрузка для педиатра, — и лежать там будет вечно."""
    for kind in checkin.CARD.kinds:
        assert kind.kind in texts.KIND_NAMES


def test_every_timed_kind_on_the_card_has_durations():
    """Длящийся вид без длительностей открыл бы таймер задним числом.

    Тикер закрыл бы его на отметке «плюс четыре часа» с пометкой
    «приблизительная», и в отчёте для педиатра это выглядит как честно
    засечённый четырёхчасовой сон. Дыра возвращается правкой файла, а не кода,
    поэтому и сторож стоит на файле.
    """
    for kind in checkin.CARD.kinds:
        if kind.kind in TIMED_KINDS:
            assert kind.durations


def test_an_instant_kind_has_no_durations():
    """«Сколько длился мокрый подгузник» — вопрос без смысла."""
    for kind in checkin.CARD.kinds:
        if kind.kind not in TIMED_KINDS:
            assert kind.durations == ()


def test_default_hours_is_one_of_the_choices():
    assert checkin.CARD.default_hours in checkin.CARD.choices


def test_the_shifts_start_at_zero_and_grow():
    minutes = [shift.minutes for shift in checkin.CARD.shifts]
    assert minutes[0] == 0
    assert minutes == sorted(set(minutes))


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"default_hours": 5}, id="интервал вне списка"),
        pytest.param({"shifts": ()}, id="нет сдвигов"),
        pytest.param({"kinds": ()}, id="нет видов"),
        pytest.param(
            {"quiet_to": checkin.CARD.quiet_from}, id="границы ночи совпали"
        ),
    ],
)
def test_broken_content_kills_the_bot_at_import(broken):
    """Бот, молча потерявший карточку, хуже бота, который не запустился.

    Второе видно сразу, первое — в первую же ночь, когда никто ничего не
    спросил и все решили, что так и надо.
    """
    with pytest.raises(ValueError):
        checkin._check(replace(checkin.CARD, **broken))


def test_a_timed_kind_without_durations_is_rejected():
    kinds = tuple(
        replace(kind, durations=()) if kind.kind in TIMED_KINDS else kind
        for kind in checkin.CARD.kinds
    )
    with pytest.raises(ValueError):
        checkin._check(replace(checkin.CARD, kinds=kinds))


def test_an_unknown_kind_in_the_file_is_rejected():
    kinds = (replace(checkin.CARD.kinds[0], kind="feed_by_telepathy"),)
    with pytest.raises(ValueError):
        checkin._check(replace(checkin.CARD, kinds=kinds))


def test_text_from_the_file_is_escaped():
    """`<1>` в подписи — незакрытый тег: Telegram не ругается, а просто не
    отправляет сообщение. Сноски такого вида встречаются в документах, и
    сторож дешевле поставить до, а не после."""
    card = replace(checkin.CARD, title="Что было<1>", question="Отметьте & всё")
    text = texts.checkin_card(card, 3)
    assert "<1>" not in text
    assert "&amp;" in text


def test_the_duration_row_never_offers_more_than_the_shift():
    """`ended_at` в будущем ломает обрезку событий по границам суток.

    Фильтр честнее клампинга: клампинг молча поменял бы число, которое человек
    только что назвал, а отсутствующей кнопки просто нет на экране.
    """
    assert checkin.durations_for(SLEEP, 60) == (30, 60)
    assert checkin.durations_for(SLEEP, 180) == (30, 60, 120, 180)
    # «Началось сейчас и уже закончилось» не бывает — остаётся «ещё идёт»
    assert checkin.durations_for(SLEEP, 0) == ()


def test_a_forged_shift_is_not_in_the_file():
    assert checkin.shift_by(99999) is None
    assert checkin.shift_by(60) is not None


# --- Карточка через боевой диспетчер ----------------------------------------


@pytest.mark.asyncio
async def test_the_card_reaches_its_handler_through_the_real_dispatcher(wired):
    """Несущий тест: роутер в боевом списке и его никто не перехватывает выше."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "kind", kind=SLEEP)
    assert any("Когда началось" in text for text in session_bot.sent)


@pytest.mark.asyncio
async def test_an_instant_kind_is_written_in_two_taps(wired, session, child):
    """Подгузник — момент, а не отрезок: третий экран у него не выскакивает."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "kind", kind=DIAPER_WET, update_id=51)
    await _tap(dp, bot, "when", kind=DIAPER_WET, back=60, update_id=52)

    events = await _events(session, child)
    assert len(events) == 1
    assert events[0].kind == DIAPER_WET
    assert events[0].ended_at is None


@pytest.mark.asyncio
async def test_choosing_a_shift_writes_the_event_backdated(wired, session, child):
    """То единственное, ради чего опрос заведён: запись не «сейчас».

    Проигнорированный сдвиг дал бы бота, который умеет ровно то же, что кнопка
    панели, — и все три тапа были бы потрачены зря.
    """
    dp, bot, session_bot = wired
    await _tap(dp, bot, "when", kind=DIAPER_WET, back=120, update_id=53)

    events = await _events(session, child)
    assert len(events) == 1
    # Момент задаёт хендлер, поэтому проверяем расстояние, а не равенство
    assert 119 < _minutes_ago(events[0].started_at) < 121


@pytest.mark.asyncio
async def test_a_timed_kind_writes_nothing_until_the_duration_is_named(
    wired, session, child
):
    """Событие, созданное на втором экране, стало бы идущим таймером.

    Открытый час назад, он дождался бы тикера и получил пометку
    «приблизительная» на отметке «плюс четыре часа» — в отчёте для педиатра это
    измеренный четырёхчасовой сон.
    """
    dp, bot, session_bot = wired
    await _tap(dp, bot, "when", kind=SLEEP, back=120, update_id=54)

    assert await _events(session, child) == []
    assert any("Сколько длилось" in text for text in session_bot.sent)


@pytest.mark.asyncio
async def test_a_finished_sleep_from_the_card_is_not_estimated(wired, session, child):
    """Длительность назвал человек — значит это не догадка тикера.

    Пометка `estimated` означает ровно одно: закрыл не человек. Третьего смысла
    у неё нет, и поставленная здесь она врала бы в обратную сторону.
    """
    dp, bot, session_bot = wired
    await _tap(dp, bot, "dur", kind=SLEEP, back=120, mins=60, update_id=55)

    events = await _events(session, child)
    assert len(events) == 1
    event = events[0]
    assert event.ended_at is not None
    assert event.ended_at - event.started_at == timedelta(minutes=60)
    assert event.estimated is False
    assert 119 < _minutes_ago(event.started_at) < 121


@pytest.mark.asyncio
async def test_a_forged_duration_writes_nothing(wired, session, child):
    """Три часа сна, начавшегося полчаса назад, дали бы конец в будущем."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "dur", kind=SLEEP, back=30, mins=180, update_id=56)

    assert await _events(session, child) == []


@pytest.mark.asyncio
async def test_a_forged_shift_writes_nothing(wired, session, child):
    """Подделанный `back` записал бы кормление недельной давности."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "when", kind=DIAPER_WET, back=99999, update_id=57)

    assert await _events(session, child) == []


@pytest.mark.asyncio
async def test_an_unknown_kind_from_an_old_card_answers_anyway(wired, session, child):
    """Вид убрали из файла — Telegram всё равно ждёт ответа.

    Без ответа он крутит «часики» до таймаута, и бот выглядит зависшим.
    """
    dp, bot, session_bot = wired
    await _tap(dp, bot, "kind", kind="feed_by_telepathy", update_id=58)

    assert await _events(session, child) == []


@pytest.mark.asyncio
async def test_still_going_opens_a_timer_and_closes_the_previous_one(
    wired, session, child, mother
):
    """«Уснула час назад и спит до сих пор» — единственный ответ без конца.

    Порядок существен: `any_open_event` сортирует по `started_at desc`, и вызов
    `close_running` после создания закрыл бы только что открытый таймер, а
    прежний дождался бы тикера и получил пометку «приблизительная».
    """
    dp, bot, session_bot = wired
    old = await repo.start_event(
        session,
        child.id,
        mother.id,
        FEED_BREAST,
        started_at=tu.now_utc() - timedelta(hours=5),
    )

    await _tap(dp, bot, "run", kind=SLEEP, back=60, update_id=59)

    await session.refresh(old)
    assert old.ended_at is not None
    assert 59 < _minutes_ago(old.ended_at) < 61
    assert old.estimated is False

    running = await repo.any_open_event(session, child.id)
    assert running is not None
    assert running.kind == SLEEP
    assert 59 < _minutes_ago(running.started_at) < 61


@pytest.mark.asyncio
async def test_still_going_says_the_previous_timer_was_closed(
    wired, session, child, mother
):
    """У кнопки панели закрытие видно сразу, здесь — нет.

    Панель перерисовывается с дебаунсом, а сказали всего одно слово: то же
    правило, что у закрытого тикером таймера и у погашенных подписок.
    """
    dp, bot, session_bot = wired
    await repo.start_event(
        session,
        child.id,
        mother.id,
        FEED_BREAST,
        started_at=tu.now_utc() - timedelta(hours=5),
    )

    await _tap(dp, bot, "run", kind=SLEEP, back=60, update_id=60)

    assert any("Закрыл предыдущее" in text for text in session_bot.sent)


@pytest.mark.asyncio
async def test_every_confirmation_carries_an_undo_button(wired, session, child):
    """«Ничего не сохраняется молча» — запись задним числом не исключение.

    Промах по соседней кнопке в темноте — обычное дело, а лишний сон заметно
    искажает неделю.
    """
    dp, bot, session_bot = wired
    await _tap(dp, bot, "when", kind=DIAPER_WET, back=60, update_id=61)

    buttons = [
        button["text"]
        for markup in session_bot.markups
        if markup and "inline_keyboard" in markup
        for row in markup["inline_keyboard"]
        for button in row
    ]
    assert any("Отменить" in text for text in buttons)


@pytest.mark.asyncio
async def test_nothing_happened_writes_nothing_and_says_so(wired, session, child):
    """Тихий отказ выглядит как зависший бот: тапнули, кнопки пропали, пусто."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "none", update_id=62)

    assert await _events(session, child) == []
    assert any("ничего не записал" in text for text in session_bot.sent)


@pytest.mark.asyncio
async def test_a_plain_answer_carries_the_home_keyboard(wired):
    """Ответ без inline-разметки — носитель постоянной клавиатуры.

    Забытое место не видно ничем: кнопки у семьи просто не появляются, а бот
    выглядит исправным. Так и случилось 15.09.
    """
    dp, bot, session_bot = wired
    await _tap(dp, bot, "none", update_id=63)

    assert any(
        markup and "keyboard" in markup for markup in session_bot.markups
    )


@pytest.mark.asyncio
async def test_a_tap_without_a_child_card_does_not_crash(orphan, session):
    """Упавший хендлер — это потерянный навсегда апдейт: offset Telegram
    сдвигается независимо от исхода обработки."""
    dp, bot, session_bot = orphan
    await _tap(dp, bot, "when", kind=DIAPER_WET, back=60, update_id=64)


# --- Настройка --------------------------------------------------------------


@pytest.mark.asyncio
async def test_turning_the_checkin_on_asks_for_the_interval_first(
    wired, session, family
):
    """Включение одним тапом поставило бы интервал, которого никто не выбирал."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "pick", update_id=65)

    assert family.checkin_hours == 0
    assert any("Как часто спрашивать" in text for text in session_bot.sent)


@pytest.mark.asyncio
async def test_the_chosen_interval_survives_the_button(wired, session, family):
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", hours=6, update_id=66)

    assert family.checkin_hours == 6


@pytest.mark.asyncio
async def test_turning_the_checkin_on_does_not_ask_within_a_minute(
    wired, session, family, bot
):
    """Первый вопрос обязан прийти через интервал, а не через минуту.

    Отметка ставится в момент включения. Оставленная пустой, она означала бы
    «спросить на ближайшем тике» — то есть бот заговорил бы сразу после того,
    как его включили, и это читалось бы как сбой.
    """
    dp, wired_bot, session_bot = wired
    await _tap(dp, wired_bot, "on", hours=3, update_id=67)

    assert family.checkin_last_at is not None
    assert checkin.due(family, tu.now_utc()) is False


@pytest.mark.asyncio
async def test_a_forged_interval_turns_nothing_on(wired, session, family):
    """`hours=1000` завёл бы опрос, который не придёт никогда."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", hours=1000, update_id=68)

    assert family.checkin_hours == 0


@pytest.mark.asyncio
async def test_the_mute_button_turns_it_off_and_says_aloud(wired, session, family):
    """Молча выключенная функция читается как сломавшаяся.

    И выключать её надо с самой карточки: отказаться от навязанного должно
    быть не труднее, чем включить нужное, а карточка приходит сама.
    """
    dp, bot, session_bot = wired
    family.checkin_hours = 3
    await _tap(dp, bot, "mute", update_id=69)

    assert family.checkin_hours == 0
    assert any("спрашивать не буду" in text for text in session_bot.sent)


@pytest.mark.asyncio
async def test_a_stale_on_button_never_turns_it_off(wired, session, family):
    """Кнопка со вчерашнего экрана живёт вечно.

    Будь действие переключателем, вчерашний тап «включить» сегодня выключил бы
    то, что вчера включили, — и узнать об этом можно было бы только не получив
    вопроса.
    """
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", hours=3, update_id=70)
    await _tap(dp, bot, "on", hours=3, update_id=71)

    assert family.checkin_hours == 3


@pytest.mark.asyncio
async def test_the_checkin_row_does_not_appear_in_the_reminders_table(
    wired, session, family
):
    """Строка в `reminders` означала бы, что опрос начнут догонять.

    Тикер разослал бы его текстом, без единой кнопки, а после суток простоя —
    сводкой пропущенного.
    """
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", hours=3, update_id=72)

    assert await repo.active_reminders(session, family.id) == []


@pytest.mark.asyncio
async def test_the_remind_screen_shows_the_state_of_the_checkin(
    wired, session, family
):
    """Шестая строка обязана показывать интервал, а не просто галочку."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", hours=4, update_id=73)

    row = texts.checkin_row(checkin.CARD, family.checkin_hours)
    assert row.startswith("✅")
    assert "каждые 4 ч" in row
    assert texts.checkin_row(checkin.CARD, 0).startswith("⬜")


def test_the_settings_row_and_the_card_use_different_actions():
    """«Не спрашивать» с карточки и тап по ряду `/remind` — разные действия.

    Делают они с настройкой одно и то же, но после `off` надо перерисовать
    экран настроек, а после `mute` — погасить карточку. Одно действие на оба
    случая означало бы хендлер, угадывающий, откуда пришёл тап.
    """
    card = keyboards.checkin_card(
        [(SLEEP, "😴 Сон")], nothing="нет", mute=checkin.CARD.mute_label
    )
    packed = [
        button.callback_data
        for row in card.inline_keyboard
        for button in row
        if button.text == checkin.CARD.mute_label
    ]
    assert packed == [CheckinCB(action="mute").pack()]

    screen = keyboards.remind([], checkin=("🕒 Опрос", True))
    assert any(
        button.callback_data == CheckinCB(action="off").pack()
        for row in screen.inline_keyboard
        for button in row
    )
