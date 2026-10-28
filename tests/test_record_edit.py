"""Правка записи: список, карточка, сдвиг времени, смена вида, удаление.

Экраны гоняются через настоящий `Dispatcher` с боевым списком роутеров — по
той же причине, что карточка опроса и экран `/remind`: свой диспетчер из двух
роутеров доказал бы работу механизма при правильном порядке и промолчал бы о
том, каков порядок на самом деле.

Машинерия здесь своя, а не общая с `test_home.py`, и это решение, а не долг.
Собственный `RecordingSession` живёт уже в шести файлах, и они **разные**: у
одного `markups` хранит объекты (чтобы писать `== keyboards.home()`), у
другого — словари; один ловит `DeleteMessage`, другой держит второй диспетчер
без ребёнка. Общая фикстура в `conftest.py` была бы перекрыта локальной в
каждом файле, то есть стала бы фикстурой на одного пользователя.

Вердикт почти везде выносится **по базе**, а не по тексту в протоколе. Текст
говорит, что бот что-то сказал; вопрос же в том, что он записал.

Времени не подменяют: всё считается сдвигом данных от настоящего `now_utc()`.
Границы правки зависят от дня рождения ребёнка и от текущего момента, и
подменённые часы прятали бы ровно те ошибки, ради которых границы заведены.
"""

from datetime import timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import handlers, keyboards, texts
from bot.db import repo
from bot.db.models import (
    DIAPER_DIRTY,
    DIAPER_WET,
    FEED_BOTTLE,
    FEED_BREAST,
    INSTANT_KINDS,
    LEFT,
    PUMP,
    RIGHT,
    SLEEP,
    TIMED_KINDS,
    WEIGHT,
)
from bot.keyboards import RecordCB, StatsCB
from bot.services import pending, records, ticker
from bot.services import timeutil as tu

CHAT = Chat(id=-1001, type="supergroup", title="Малышка")
AUTHOR = User(id=222, is_bot=False, first_name="Аня")


class RecordingSession(BaseSession):
    """Сеть не трогаем. Всплывашки отделены от сообщений намеренно.

    `AnswerCallbackQuery` несёт поле `text` наравне с `SendMessage`, и свалив
    их в один список, мы получили бы тест, который зелен от отказа во
    всплывашке ровно так же, как от записи в ленту.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []
        self.edited: list[str] = []
        self.markups: list[Any] = []
        self.alerts: list[str] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Any, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if isinstance(method, AnswerCallbackQuery):
            if method.text:
                self.alerts.append(method.text)
            return True
        payload = method.model_dump(exclude_none=True)
        if "text" in payload:
            target = self.edited if "message_id" in payload else self.sent
            target.append(payload["text"])
            self.markups.append(getattr(method, "reply_markup", None))
        return Message(
            message_id=999, date=tu.now_utc(), chat=CHAT, from_user=AUTHOR, text=""
        )

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def wired(session, family, child, mother, monkeypatch):
    """Диспетчер с боевыми роутерами и подстановкой, как у `FamilyMiddleware`.

    Подменяется `panel.schedule`, а не `panel.refresh`, и разница
    существенна: `schedule` только ставит задачу, а рисует она себя через
    `panel_debounce_seconds` — то есть уже после конца теста. Сторож на
    `refresh` был бы зелёным всегда и молчал бы о том, зовут ли панель вообще.

    Подмена нужна и без сторожа: настоящая `refresh` открывает **свою** сессию
    к боевому файлу базы, а не к той, что подставил `inject`.
    """
    from bot.services import panel

    bot = Bot(token="42:TEST", session=RecordingSession())
    dp = Dispatcher()

    async def inject(handler, event, data):
        data.update(session=session, family=family, child=child, member=mother)
        return await handler(event, data)

    dp.update.outer_middleware(inject)
    order = tuple(handlers.routers)
    for router in order:
        dp.include_router(router)

    drawn: list[int] = []

    def schedule(bot_, family_id, last_message_id=None):
        drawn.append(family_id)

    monkeypatch.setattr(panel, "schedule", schedule)

    yield dp, bot, bot.session, drawn

    # Роутеры — синглтоны уровня модуля: без отвязки второй тест в файле падает
    # на «Router is already attached»
    for router in order:
        router._parent_router = None


_counter = [600]


async def _tap(dp, bot, data: RecordCB, user: User = AUTHOR) -> None:
    _counter[0] += 1
    call = CallbackQuery(
        id=str(_counter[0]),
        from_user=user,
        chat_instance="x",
        data=data.pack(),
        message=Message(
            message_id=5, date=tu.now_utc(), chat=CHAT, from_user=AUTHOR, text="📋"
        ),
    )
    await dp.feed_update(bot, Update(update_id=_counter[0], callback_query=call))


async def _feed(dp, bot, text: str) -> None:
    _counter[0] += 1
    message = Message(
        message_id=_counter[0], date=tu.now_utc(), chat=CHAT, from_user=AUTHOR, text=text
    )
    await dp.feed_update(bot, Update(update_id=_counter[0], message=message))


async def _sleep(session, child, mother, *, hours_ago: float, lasted: float, guess=False):
    """Закрытый сон, начавшийся `hours_ago` часов назад.

    `guess=True` — тот, который закрыл тикер: событие заводится открытым и
    закрывается через `repo.close_event`, ровно как в `ticker`. Поставить
    `estimated` сразу в `add_event` было бы короче и проверяло бы не тот путь.
    """
    started = tu.now_utc() - timedelta(hours=hours_ago)
    if not guess:
        return await repo.add_event(
            session,
            child.id,
            mother.id,
            SLEEP,
            started_at=started,
            ended_at=started + timedelta(hours=lasted),
        )

    event = await repo.add_event(session, child.id, mother.id, SLEEP, started_at=started)
    return await repo.close_event(
        session, event, started + timedelta(hours=lasted), estimated=True
    )


# --- Заглавный сценарий ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_forgotten_timer_can_be_shortened_by_hand(wired, session, child, mother):
    """Ради этого теста всё и делалось.

    «Забыла выключить» выглядит в базе так: таймер висел, тикер закрыл его на
    отметке «плюс четыре часа» и пометил длительность приблизительной. Человек
    знает настоящий конец и обязан уметь его назвать.
    """
    dp, bot, talk, drawn = wired
    event = await _sleep(session, child, mother, hours_ago=5, lasted=4, guess=True)
    was = event.ended_at

    await _tap(dp, bot, RecordCB(action="save", row_id=event.id, field="e", mins=-60))

    await session.refresh(event)
    assert event.ended_at == was - timedelta(hours=1), "конец обязан сдвинуться на час"
    assert event.started_at is not None
    assert drawn, "после правки панель обязана перерисоваться"


@pytest.mark.asyncio
async def test_a_hand_set_end_drops_the_guess_mark(wired, session, child, mother):
    """Пометка означает «конец назначил тикер». Названный человеком — не назначен.

    Сторож поведенческий намеренно: структурная проверка `repo.set_event_time`
    осталась бы зелёной, даже если бы хендлер передал ей не то.
    """
    dp, bot, talk, drawn = wired
    event = await _sleep(session, child, mother, hours_ago=5, lasted=4, guess=True)
    assert event.estimated is True

    await _tap(dp, bot, RecordCB(action="save", row_id=event.id, field="e", mins=-60))

    await session.refresh(event)
    assert event.estimated is False
    assert any("приблизительно" in line for line in talk.sent), (
        "снятую пометку надо назвать вслух: молча исчезнувшая выглядит как сбой"
    )


@pytest.mark.asyncio
async def test_moving_only_the_start_keeps_the_guess_marked(wired, session, child, mother):
    """Обратная половина предыдущего.

    Без неё мутация «снимать пометку при любой правке» остаётся зелёной, а она
    и есть враньё: сдвинутое начало не делает выдуманный конец измеренным.
    """
    dp, bot, talk, drawn = wired
    event = await _sleep(session, child, mother, hours_ago=5, lasted=4, guess=True)

    await _tap(dp, bot, RecordCB(action="save", row_id=event.id, field="s", mins=-15))

    await session.refresh(event)
    assert event.estimated is True, "конец остался догадкой тикера"


@pytest.mark.asyncio
async def test_a_time_is_not_written_until_it_is_confirmed(wired, session, child, mother):
    """Тап по шагу — черновик, а не запись. Пишет только «Готово».

    Главный тест доктрины экрана. Брошенный посреди экран не меняет ничего, а в
    общий чат уходит одно подтверждение на одно намерение, а не по строке на
    каждый тап.
    """
    dp, bot, talk, drawn = wired
    event = await _sleep(session, child, mother, hours_ago=5, lasted=4)
    was = event.ended_at

    await _tap(dp, bot, RecordCB(action="shift", row_id=event.id, field="e", mins=-60))
    await _tap(dp, bot, RecordCB(action="shift", row_id=event.id, field="e", mins=-120))
    await session.refresh(event)
    assert event.ended_at == was, "до «Готово» в базе не меняется ничего"
    assert not drawn, "и панель дёргать незачем"

    await _tap(dp, bot, RecordCB(action="save", row_id=event.id, field="e", mins=-120))
    await session.refresh(event)
    assert event.ended_at == was - timedelta(hours=2), "сдвиг применяется целиком"


# --- Границы ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_forged_shift_never_lands_in_the_future(wired, session, child, mother):
    """Подделанный `mins` мимо клавиатуры отбивается на «Готово».

    Кнопки такого шага не рисуют, но `callback_data` приходит с клиента, и
    доверять ему нельзя. Конец в будущем ломает `stats.clip` и всю сводку
    разом: в сегодняшние сутки засчиталось бы время, которого не было.
    """
    dp, bot, talk, drawn = wired
    event = await _sleep(session, child, mother, hours_ago=5, lasted=4)
    was = event.ended_at

    await _tap(dp, bot, RecordCB(action="save", row_id=event.id, field="e", mins=100000))

    await session.refresh(event)
    assert event.ended_at == was, "запись не сдвинулась"
    assert texts.RECORD_STALE in talk.alerts, "и человеку сказали, почему"


@pytest.mark.asyncio
async def test_a_record_cannot_be_moved_before_the_birth(wired, session, child, mother):
    """Нижняя граница — день рождения, и она не украшение.

    `export._csv` печатает врачу возраст на момент записи; уехавшая за
    рождение дала бы отрицательный.
    """
    dp, bot, talk, drawn = wired
    event = await _sleep(session, child, mother, hours_ago=5, lasted=4)
    was = event.started_at

    await _tap(dp, bot, RecordCB(action="save", row_id=event.id, field="s", mins=-1000000))

    await session.refresh(event)
    assert event.started_at == was


@pytest.mark.asyncio
async def test_an_end_is_never_dragged_before_its_start(wired, session, child, mother):
    """Отрицательная длительность выглядит правдоподобно и портит всю сумму."""
    dp, bot, talk, drawn = wired
    event = await _sleep(session, child, mother, hours_ago=5, lasted=1)
    was = event.ended_at

    await _tap(dp, bot, RecordCB(action="save", row_id=event.id, field="e", mins=-120))

    await session.refresh(event)
    assert event.ended_at == was
    assert event.ended_at > event.started_at


def test_a_step_that_breaks_the_segment_is_not_offered():
    """Фильтр честнее клампинга: невозможный шаг не становится кнопкой.

    Чистая половина той же границы — без базы и без диспетчера, поэтому
    проверяется сразу на краю: до начала записи осталось меньше, чем самый
    мелкий шаг назад.
    """
    from types import SimpleNamespace

    now = tu.now_utc()
    born = now - timedelta(days=10)
    event = SimpleNamespace(
        started_at=now - timedelta(hours=2),
        ended_at=now - timedelta(hours=2) + timedelta(minutes=4),
        kind=SLEEP,
    )

    steps = records.steps_for(event, records.END, 0, born_at=born, now=now)
    assert -5 not in steps, "конец уехал бы раньше начала"
    assert -15 not in steps and -60 not in steps
    assert 5 in steps and 60 in steps


def test_no_step_is_offered_that_would_reach_into_the_future():
    from types import SimpleNamespace

    now = tu.now_utc()
    born = now - timedelta(days=10)
    event = SimpleNamespace(
        started_at=now - timedelta(hours=1),
        ended_at=now - timedelta(minutes=2),
        kind=SLEEP,
    )

    steps = records.steps_for(event, records.END, 0, born_at=born, now=now)
    assert 5 not in steps and 60 not in steps
    assert -5 in steps


# --- Вид -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_forged_kind_never_crosses_the_timer_line(wired, session, child, mother):
    """Подгузник не может стать сном.

    Мгновенная запись с видом из `TIMED_KINDS` и пустым `ended_at` — это идущий
    таймер задним числом. Тикер закрыл бы его на отметке «плюс четыре часа» с
    пометкой «приблизительная», и в отчёте для педиатра это выглядело бы как
    измеренный многочасовой сон.
    """
    dp, bot, talk, drawn = wired
    event = await repo.add_event(
        session, child.id, mother.id, DIAPER_WET, started_at=tu.now_utc() - timedelta(hours=1)
    )

    await _tap(dp, bot, RecordCB(action="setkind", row_id=event.id, kind=SLEEP))

    await session.refresh(event)
    assert event.kind == DIAPER_WET
    assert texts.RECORD_WRONG_KIND in talk.alerts, (
        "«кнопка устарела» здесь врёт: никто ничего не двигал, вид невозможен"
    )
    assert texts.RECORD_STALE not in talk.alerts


@pytest.mark.asyncio
async def test_changing_the_kind_drops_a_number_that_lost_its_meaning(
    wired, session, child, mother
):
    """«😴 Сон, 90 мл» печатает только выгрузка для педиатра.

    Ни один экран бота число у чужого вида не показывает, то есть заметить его
    было бы нечем. Поэтому оно снимается — и об этом говорится вслух.
    """
    dp, bot, talk, drawn = wired
    event = await repo.add_event(
        session,
        child.id,
        mother.id,
        FEED_BOTTLE,
        started_at=tu.now_utc() - timedelta(hours=1),
        value_num=60,
    )

    await _tap(dp, bot, RecordCB(action="setkind", row_id=event.id, kind=DIAPER_DIRTY))

    await session.refresh(event)
    assert event.kind == DIAPER_DIRTY
    assert event.value_num is None
    assert any("60" in line and "убрал" in line for line in talk.sent), (
        "молча стёртое число — потеря без единого слова"
    )


@pytest.mark.asyncio
async def test_a_mistaken_side_is_fixed_by_pressing_the_right_one(
    wired, session, child, mother
):
    """«🤱 Левая» и «🤱 Правая» — соседние кнопки панели.

    Другого пути исправить сторону в боте нет вовсе: ни кнопка, ни голос её
    задним числом не меняют.
    """
    dp, bot, talk, drawn = wired
    started = tu.now_utc() - timedelta(hours=2)
    event = await repo.add_event(
        session,
        child.id,
        mother.id,
        FEED_BREAST,
        started_at=started,
        ended_at=started + timedelta(minutes=20),
        side=LEFT,
    )

    await _tap(
        dp, bot, RecordCB(action="setkind", row_id=event.id, kind=FEED_BREAST, side=RIGHT)
    )

    await session.refresh(event)
    assert event.kind == FEED_BREAST
    assert event.side == RIGHT


def test_no_choice_crosses_the_shape_boundary():
    """Структурный близнец поведенческого сторожа выше.

    Экраны собираются из двух списков, и ни один вариант не имеет права
    оказаться по обе стороны границы формы. Проверяется поимённо, а не через
    `_check`: тот судит множества, а этот — каждую кнопку.
    """
    timed = {choice.kind for choice in records.TIMED_CHOICES}
    instant = {choice.kind for choice in records.INSTANT_CHOICES}

    assert timed <= set(TIMED_KINDS)
    assert instant <= set(INSTANT_KINDS)
    assert not (timed & instant)
    for choice in records.INSTANT_CHOICES:
        assert choice.side == "", "у мгновенной записи стороны не бывает"


def test_the_side_survives_only_where_it_means_something():
    assert records.after_kind_change(FEED_BREAST, LEFT) == (LEFT, None)
    assert records.after_kind_change(SLEEP, LEFT) == (None, None)
    assert records.after_kind_change(PUMP, "") == (None, None)


# --- Объём -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_volume_question_reaches_the_number_handler(
    wired, session, family, child, mother
):
    """Правка объёма переиспользует ожидание числа, а не заводит свой разбор.

    Сторож заодно проверяет порядок роутеров: голое число обязано дойти до
    `measure`, который стоит последним.
    """
    dp, bot, talk, drawn = wired
    event = await repo.add_event(
        session,
        child.id,
        mother.id,
        FEED_BOTTLE,
        started_at=tu.now_utc() - timedelta(hours=1),
        value_num=60,
    )

    await _tap(dp, bot, RecordCB(action="ml", row_id=event.id))
    assert pending.peek(family.chat_id, AUTHOR.id) is not None

    await _feed(dp, bot, "95")

    await session.refresh(event)
    assert event.value_num == 95


# --- Удаление --------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_asks_first(wired, session, child, mother):
    """Единственное необратимое действие правки спрашивает.

    Под свежим подтверждением спрашивать незачем — промах лечится повторным
    тапом по панели. До записи трёхдневной давности так не дотянуться, а номер
    в списке легко перепутать с соседним.
    """
    dp, bot, talk, drawn = wired
    event = await repo.add_event(
        session, child.id, mother.id, DIAPER_WET, started_at=tu.now_utc() - timedelta(hours=1)
    )

    await _tap(dp, bot, RecordCB(action="ask", row_id=event.id))

    assert await repo.get_event(session, event.id) is not None, "вопрос не удаляет"
    assert any(texts.RECORD_ASK_DELETE in line for line in talk.edited)

    await _tap(dp, bot, RecordCB(action="del", row_id=event.id))
    assert await repo.get_event(session, event.id) is None


@pytest.mark.asyncio
async def test_deleting_names_what_was_deleted(wired, session, child, mother):
    """Вместо отмены — возможность завести заново, не вспоминая.

    Вернуть удалённое нечем: черновиков в памяти здесь нет по доктрине, а
    возить все поля события в `callback_data` не даёт потолок в 64 байта.
    """
    dp, bot, talk, drawn = wired
    event = await repo.add_event(
        session,
        child.id,
        mother.id,
        FEED_BOTTLE,
        started_at=tu.now_utc() - timedelta(hours=1),
        value_num=60,
    )

    await _tap(dp, bot, RecordCB(action="del", row_id=event.id))

    farewell = [line for line in talk.sent if "Удалил" in line]
    assert farewell, "удаление обязано отчитаться в ленту"
    assert "60" in farewell[0], "и назвать запись целиком, а не «запись удалена»"


@pytest.mark.asyncio
async def test_a_dead_card_answers_instead_of_going_quiet(wired, session, child, mother):
    """Двое могут открыть одну карточку, и один из них уже удалил запись.

    Telegram крутит «часики» до ответа, поэтому промолчать нельзя даже там,
    где делать нечего.
    """
    dp, bot, talk, drawn = wired
    event = await repo.add_event(
        session, child.id, mother.id, DIAPER_WET, started_at=tu.now_utc() - timedelta(hours=1)
    )
    row_id = event.id
    await repo.delete_event(session, event)

    await _tap(dp, bot, RecordCB(action="open", row_id=row_id))

    assert texts.RECORD_GONE in talk.alerts


@pytest.mark.asyncio
async def test_a_record_of_another_child_is_not_editable(wired, session, family, mother):
    """`row_id` приходит с клиента, а id сквозные по всей базе.

    Долго это было почти теорией — пока экран правки не начал раздавать номера
    записей в чат пачками.
    """
    dp, bot, talk, drawn = wired
    other_family = await repo.get_or_create_family(session, -1002, "Соседи")
    other_member = await repo.get_or_create_member(session, other_family.id, 444, "Оля")
    other_child = await repo.create_child(
        session,
        other_family.id,
        name="Гриша",
        sex="m",
        born_at=tu.now_utc() - timedelta(days=20),
        birth_weight_g=3100,
    )
    stranger = await repo.add_event(
        session,
        other_child.id,
        other_member.id,
        DIAPER_WET,
        started_at=tu.now_utc() - timedelta(hours=1),
    )

    await _tap(dp, bot, RecordCB(action="del", row_id=stranger.id))

    assert await repo.get_event(session, stranger.id) is not None, "чужая запись цела"
    assert texts.RECORD_GONE in talk.alerts


@pytest.mark.asyncio
async def test_the_birth_weight_point_cannot_be_edited_here(
    wired, session, family, child, mother
):
    """Вес при рождении лежит в двух местах, и править надо оба.

    Поправив точку отсюда, мы развели бы карточку ребёнка с графиком, и
    заметить это можно было бы только глазами. Поэтому здесь её показывают и
    отсылают в `/baby`.
    """
    dp, bot, talk, drawn = wired
    point = await repo.add_measurement(
        session, child.id, mother.id, WEIGHT, taken_at=child.born_at, value=3420
    )

    await _tap(dp, bot, RecordCB(action="del", what="m", row_id=point.id))
    assert await repo.get_measurement(session, point.id) is not None

    await _tap(dp, bot, RecordCB(action="open", what="m", row_id=point.id))
    card = talk.edited[-1]
    assert "/baby" in card, "карточка обязана сказать, где он правится"


# --- Список ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_night_sleep_shows_up_in_both_days_of_the_list(
    wired, session, family, child, mother
):
    """Сон через полночь принадлежит обеим суткам — как и в сводке.

    Искать его наутро человек будет в утренних сутках, а не во вчерашних: он
    проснулся сегодня. `events_between` отдаёт **пересекающиеся** записи, и
    список обязан наследовать это правило, а не заводить своё.
    """
    dp, bot, talk, drawn = wired
    today = tu.local_today(family.tz)
    start, _ = tu.day_bounds(today, family.tz)
    night = await repo.add_event(
        session,
        child.id,
        mother.id,
        SLEEP,
        started_at=start - timedelta(minutes=20),
        ended_at=start + timedelta(hours=6),
    )

    await _tap(dp, bot, RecordCB(action="list", day=0))
    assert str(night.id) in talk.markups[-1].model_dump_json(), "сон виден сегодня"

    await _tap(dp, bot, RecordCB(action="list", day=1))
    assert str(night.id) in talk.markups[-1].model_dump_json(), "и вчера тоже"


@pytest.mark.asyncio
async def test_the_list_is_two_taps_from_the_summary(wired, session, child, mother):
    """«📊 Сутки» → «📋 Записи». Единственный путь к записи, уехавшей из ленты."""
    dp, bot, talk, drawn = wired
    await repo.add_event(
        session, child.id, mother.id, DIAPER_WET, started_at=tu.now_utc() - timedelta(hours=1)
    )

    await _tap(dp, bot, RecordCB(action="list"))

    assert "Записи" in talk.edited[-1]
    assert texts.RECORD_PICK in talk.edited[-1]


def test_the_summary_keyboard_leads_to_the_list():
    """Структурная половина: кнопка на экране сводки существует и ведёт сюда."""
    labels = [b.text for row in keyboards.stats().inline_keyboard for b in row]
    assert "📋 Записи" in labels

    rows = keyboards.stats().inline_keyboard
    assert len(rows[0]) == 3, "первый ряд остался переключателем периода"
    assert len(rows[-1]) == 1, "а «Записи» — своим рядом: четвёртая подпись не влезет"


def test_the_list_returns_to_the_summary_through_the_existing_handler():
    """«К сводке» — это `StatsCB`, а не свой экран.

    Второй путь к той же сводке разошёлся бы с первым молча — например, потеряв
    переключатель периода.
    """
    markup = keyboards.records([], day=0, page=0, earlier=False, later=False,
                               back_day=True, forward_day=False)
    last = markup.inline_keyboard[-1][0]
    assert StatsCB.unpack(last.callback_data).period == "today"


def test_today_is_never_offered_a_tomorrow():
    """Кнопка, ведущая в пустоту, — это клампинг молчанием."""
    markup = keyboards.records([], day=0, page=0, earlier=False, later=False,
                               back_day=True, forward_day=False)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert not any("позже" in label.lower() for label in labels)


def test_the_page_window_never_loses_a_record():
    """Склейка страниц равна исходному списку, и нумерация сквозная."""
    rows = list(range(30))
    seen: list[int] = []
    numbers: list[int] = []
    page = 0
    while True:
        window, first, _, later = records.page_of(rows, page)
        seen.extend(window)
        numbers.extend(range(first, first + len(window)))
        if not later:
            break
        page += 1

    assert seen == rows
    assert numbers == list(range(1, 31)), "номера сквозные, а не по странице"


# --- Забытый таймер --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_auto_closed_timer_is_fixed_from_its_own_message(
    session, family, child, mother, bot
):
    """Сторож обещания, которое долго было ложным.

    `AUTO_CLOSED` звал «поправьте: /today», а обработчика у этого слова не
    было ни одного, да и отдельных записей в сводке не видно вовсе. Теперь под
    сообщением висит кнопка на **ту самую** запись, которую тикер закрыл.
    """
    from bot.config import settings

    family.sleep_nudge_minutes = 180
    await session.commit()
    started = tu.now_utc() - timedelta(hours=settings.open_timer_max_hours + 1)
    event = await repo.start_event(
        session, child.id, mother.id, SLEEP, started_at=started
    )

    await ticker.close_stale_timers(bot, session, tu.now_utc())

    await session.refresh(event)
    assert event.ended_at is None
    assert event.estimated is False
    markup = bot.kwargs[-1].get("reply_markup")
    assert markup is not None
    from bot.keyboards import TimeCB
    values = [TimeCB.unpack(button.callback_data)
              for row in markup.inline_keyboard for button in row
              if button.callback_data.startswith("tm:")]
    assert {value.row_id for value in values} == {event.id}
    assert {value.action for value in values} == {"ask"}


def test_the_promise_in_the_auto_closed_text_is_keepable():
    """Текст больше не отправляет в команду, которой это не под силу."""
    assert "/today" not in texts.AUTO_CLOSED
    assert "кнопк" in texts.AUTO_CLOSED


# --- Структурные -----------------------------------------------------------


def test_every_record_action_has_a_handler():
    """Каждое `action`, которое умеют порождать клавиатуры, есть в таблице.

    Близнец `test_every_home_button_has_an_action`, и поломка той же породы:
    кнопка есть, бот молчит. Действия собираются из **самих клавиатур**, а не
    из отдельного списка, — отдельный список сошёлся бы сам с собой.
    """
    from bot.handlers import record

    markups = [
        keyboards.records([(1, "e", 7)], day=1, page=1, earlier=True, later=True,
                          back_day=True, forward_day=True),
        keyboards.record_card(what="e", row_id=7, day=0, page=0,
                              fields=("s", "e", "t"), value=True, kinds=True),
        keyboards.record_time([(-5, "−5")], what="e", row_id=7, field="e",
                              mins=-5, day=0, page=0),
        keyboards.record_kinds([(SLEEP, "", "😴 Сон")], row_id=7, day=0, page=0),
        keyboards.record_delete(what="e", row_id=7, day=0, page=0),
        keyboards.record_fix(7, field="e"),
        keyboards.stats(),
        keyboards.stopped(7),
        keyboards.undo("event", 7),
        keyboards.running(SLEEP, row_id=7),
    ]

    actions = {
        RecordCB.unpack(button.callback_data).action
        for markup in markups
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data.startswith("rec:")
    }

    assert actions, "фильтр не должен молча найти ноль кнопок"
    assert actions <= set(record._ACTIONS), f"без обработчика: {actions - set(record._ACTIONS)}"


def test_record_buttons_fit_into_telegram():
    """`pack()` ограничен 64 байтами, а полей у класса девять."""
    worst = RecordCB(
        action="setkind",
        what="m",
        row_id=9_999_999,
        field="e",
        kind="feed_breast",
        side="L",
        mins=-1440,
        day=365,
        page=9,
    ).pack()
    assert len(worst.encode()) <= 64, worst


def test_the_editing_router_stands_below_the_triage():
    """Порядок берётся из боевого списка, а не из собранного тестом."""
    order = [router.name for router in handlers.routers]
    assert order.index("alarm") < order.index("record")
    assert order.index("record") < order.index("measure")


def test_a_confirmation_offers_to_correct_what_it_reports():
    """Второй вход в правку — под подтверждением, для того, кто здесь сейчас.

    Список нужен утром, когда подтверждение уехало вверх; эта кнопка — тому,
    кто заметил промах в ту же секунду.
    """
    for markup in (keyboards.stopped(7), keyboards.undo("event", 7)):
        buttons = [b for row in markup.inline_keyboard for b in row]
        fix = [b for b in buttons if b.callback_data.startswith("rec:")]
        assert fix, "подтверждение обязано предлагать правку"
        assert RecordCB.unpack(fix[0].callback_data).row_id == 7

    rows = keyboards.stopped(7).inline_keyboard
    assert all(len(row) <= 2 for row in rows), "ряд из трёх Telegram режет"
