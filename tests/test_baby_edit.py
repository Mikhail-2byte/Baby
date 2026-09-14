"""Правка карточки ребёнка.

Карточку заполняют один раз, в первые часы после родов и не выспавшись, а до
появления этого кода поправить её было нельзя вовсе: в `repo` не было ни
`update_child`, ни `delete_child`, и повторный `/baby` просто показывал
карточку. Опечатка в моменте рождения чинилась только руками в SQLite.

Цена опечатки не косметическая. От `born_at` считаются все центили ВОЗ, порог
«младше трёх месяцев» в триаже и даты календаря прививок — то есть ровно те
числа, ради которых проект принципиально не подпускает к ним модель.

Два инварианта проверяются здесь подробно, потому что оба нарушаются молча:

1. вес при рождении живёт в двух местах — полем карточки и точкой на графике
   с `taken_at = born_at`. Поправив одно и забыв другое, мы получили бы
   карточку и график, которые расходятся между собой;
2. даты подписок на прививки и осмотры заморожены в момент включения. После
   правки даты рождения они посчитаны от несуществующей даты, и увидеть это
   можно только в поликлинике, придя не в тот день.

Объекты aiogram здесь настоящие, а не заглушки. Так вышло не из любви к
чистоте: хендлеры выбора поля и пола проверяют `isinstance(call.message,
Message)` — у `CallbackQuery.message` тип допускает `InaccessibleMessage`, —
и заглушка прошла бы мимо этой ветки, ничего не проверив.
"""

from datetime import datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, User

from bot import texts
from bot.db import repo
from bot.db.models import WEIGHT
from bot.handlers import baby
from bot.keyboards import EditCB, SexCB
from bot.services import schedule

CHAT = Chat(id=-1001, type="supergroup", title="Малышка")
AUTHOR = User(id=222, is_bot=False, first_name="Аня")
NOW = datetime(2026, 9, 13, 12, 0)


class RecordingSession(BaseSession):
    """Сети нет: складываем отправленное и всплывашки по отдельности."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []
        self.alerts: list[dict[str, Any]] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Any, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        payload = method.model_dump(exclude_none=True)
        if isinstance(method, AnswerCallbackQuery):
            self.alerts.append(payload)
            return True
        if "text" in payload:
            self.sent.append(payload["text"])
        return Message(
            message_id=999, date=NOW, chat=CHAT, from_user=AUTHOR,
            text=payload.get("text", ""),
        )

    async def stream_content(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class FakeState:
    """FSM-контекст ровно в том объёме, в каком его трогает правка."""

    def __init__(self) -> None:
        self.state: Any = None
        self.cleared = False

    async def set_state(self, value: Any) -> None:
        self.state = value

    async def clear(self) -> None:
        self.cleared = True
        self.state = None


@pytest.fixture
def wired():
    session = RecordingSession()
    return Bot(token="42:TEST", session=session), session


def _message(bot: Bot, text: str) -> Message:
    return Message(
        message_id=1, date=NOW, chat=CHAT, from_user=AUTHOR, text=text
    ).as_(bot)


def _call(bot: Bot, data: str = "x") -> CallbackQuery:
    return CallbackQuery(
        id="1", from_user=AUTHOR, chat_instance="ci",
        message=_message(bot, "карточка"), data=data,
    ).as_(bot)


async def _birth_point(session, child, mother, grams: int = 3420):
    """Точка веса при рождении — её заводит `_finish`, а не `create_child`."""
    return await repo.add_measurement(
        session, child.id, mother.id, WEIGHT,
        taken_at=child.born_at, value=float(grams),
    )


async def _vaccine_reminders(session, family, mother, count: int) -> None:
    """Подписка на прививки — пачка разовых строк, по одной на возраст."""
    for i in range(count):
        await repo.create_reminder(
            session, family.id, mother.id,
            text=f"Прививка #{i}",
            fire_at=datetime(2026, 12, 1, 9, 0) + timedelta(days=i),
            preset="vaccine",
        )


# --- Вес при рождении: два места, одна правда ------------------------------


@pytest.mark.asyncio
async def test_editing_weight_moves_the_point_on_the_chart(
    session, child, mother, wired
):
    """Поправленный вес обязан доехать и до графика, а не только до карточки."""
    bot, recorded = wired
    before = await _birth_point(session, child, mother)

    await baby.edit_weight(_message(bot, "3650"), FakeState(), session, mother, child)

    assert child.birth_weight_g == 3650
    after = await repo.measurement_at(session, child.id, WEIGHT, child.born_at)
    assert after is not None
    assert after.value == 3650.0
    assert after.id == before.id, "точку надо править, а не заводить вторую"


@pytest.mark.asyncio
async def test_weight_after_skip_is_added_not_lost(
    session, family, mother, born, wired
):
    """`/skip` в мастере не должен делать вес недостижимым навсегда.

    Текст `NO_CHILD` обещает, что вес «можно пропустить и добавить позже».
    До появления правки это обещание было ложью: точка на графике заводилась
    только внутри `_finish`, и пропустивший её терял потерю массы первых дней.
    """
    bot, _ = wired
    skipped = await repo.create_child(
        session, family.id, name="Аня", sex="f", born_at=born, birth_weight_g=None
    )
    assert await repo.measurement_at(session, skipped.id, WEIGHT, born) is None

    await baby.edit_weight(_message(bot, "3420"), FakeState(), session, mother, skipped)

    assert skipped.birth_weight_g == 3420
    point = await repo.measurement_at(session, skipped.id, WEIGHT, born)
    assert point is not None and point.value == 3420.0


@pytest.mark.asyncio
async def test_nonsense_weight_keeps_the_question_open(
    session, child, mother, wired
):
    """На «35600» переспрашиваем, а не записываем и не бросаем правку."""
    bot, recorded = wired
    state = FakeState()

    await baby.edit_weight(_message(bot, "35600"), state, session, mother, child)

    assert child.birth_weight_g == 3420, "дикое число не должно доехать до карточки"
    assert recorded.sent == [texts.BABY_BAD_WEIGHT]
    assert not state.cleared, "иначе повторный ответ числом уедет мимо правки"


# --- Момент рождения -------------------------------------------------------


@pytest.mark.asyncio
async def test_editing_born_moves_the_birth_point(
    session, child, family, mother, wired
):
    """Точка веса привязана к моменту рождения и обязана переехать с ним."""
    bot, _ = wired
    await _birth_point(session, child, mother)
    old_born = child.born_at

    await baby.edit_born(
        _message(bot, "02.09.2026 06:15"), FakeState(), session, family, child
    )

    assert child.born_at != old_born
    assert await repo.measurement_at(session, child.id, WEIGHT, old_born) is None
    moved = await repo.measurement_at(session, child.id, WEIGHT, child.born_at)
    assert moved is not None and moved.value == 3420.0


@pytest.mark.asyncio
async def test_editing_born_drops_calendar_subscriptions(
    session, child, family, mother, wired
):
    """Даты прививок посчитаны от старой даты — гасим их и говорим вслух."""
    bot, recorded = wired
    await _vaccine_reminders(session, family, mother, 14)

    await baby.edit_born(
        _message(bot, "02.09.2026 06:15"), FakeState(), session, family, child
    )

    left = await repo.active_reminders(session, family.id)
    assert [r for r in left if r.preset == "vaccine"] == []

    said = recorded.sent[0]
    assert "14" in said, "сколько именно погасили — должно быть названо"
    assert "/remind" in said, "и что с этим делать дальше"


@pytest.mark.asyncio
async def test_editing_born_leaves_repeating_presets_alone(
    session, child, family, mother, wired
):
    """Витамин D не зависит от даты рождения — гасить его незачем.

    Календарные пресеты отличаются не списком в коде, а тем, что у них есть
    раскрываемая секция. Список и здесь, и в `edit_born` выводится из
    `schedule.PRESETS`, поэтому шестой пресет не разъедет их молча.
    """
    bot, _ = wired
    await repo.create_reminder(
        session, family.id, mother.id,
        text="Витамин D", fire_at=datetime(2026, 12, 1, 9, 0),
        rrule="FREQ=DAILY", preset="vitd",
    )

    await baby.edit_born(
        _message(bot, "02.09.2026"), FakeState(), session, family, child
    )

    left = await repo.active_reminders(session, family.id)
    assert [r.preset for r in left] == ["vitd"]


def test_calendar_presets_are_derived_not_hardcoded():
    """Пара, которую гасит правка, берётся из расписания, а не из строк."""
    calendar = [p.id for p in schedule.PRESETS if schedule.section_of(p)]
    assert set(calendar) == {"vaccine", "checkup"}


@pytest.mark.asyncio
async def test_quiet_when_nothing_was_subscribed(session, child, family, wired):
    """Ничего не гасили — не пугаем предупреждением."""
    bot, recorded = wired

    await baby.edit_born(
        _message(bot, "02.09.2026"), FakeState(), session, family, child
    )

    assert "⚠️" not in recorded.sent[0]


@pytest.mark.asyncio
async def test_future_birth_date_is_refused(
    session, child, family, mother, wired
):
    """Перепутанный год не должен превратить ребёнка в нерождённого.

    Заодно проверяем, что подписки при отказе остаются живыми: гасить их из-за
    неудачной попытки правки — значит наказать за опечатку.
    """
    bot, recorded = wired
    await _vaccine_reminders(session, family, mother, 3)
    old_born = child.born_at
    state = FakeState()

    await baby.edit_born(_message(bot, "02.09.2027"), state, session, family, child)

    assert child.born_at == old_born
    assert recorded.sent == [texts.BABY_FUTURE_DATE]
    assert not state.cleared
    assert len(await repo.active_reminders(session, family.id)) == 3


@pytest.mark.asyncio
async def test_unparsable_date_keeps_the_question_open(
    session, child, family, wired
):
    bot, recorded = wired
    state = FakeState()

    await baby.edit_born(
        _message(bot, "позавчера вечером"), state, session, family, child
    )

    assert recorded.sent == [texts.BABY_BAD_DATE]
    assert not state.cleared


# --- Имя и пол -------------------------------------------------------------


@pytest.mark.asyncio
async def test_editing_name(session, child, wired):
    bot, recorded = wired

    await baby.edit_name(
        _message(bot, "  Анна   Михайловна "), FakeState(), session, child
    )

    assert child.name == "Анна Михайловна"
    assert "Анна Михайловна" in recorded.sent[0]


@pytest.mark.asyncio
async def test_blank_name_is_not_saved(session, child, wired):
    bot, _ = wired
    state = FakeState()

    await baby.edit_name(_message(bot, "   "), state, session, child)

    assert child.name == "Аня"
    assert not state.cleared


@pytest.mark.asyncio
async def test_editing_sex_does_not_touch_the_calendar(
    session, child, family, mother, wired
):
    """Пол не влияет ни на одну дату календаря — подписки обязаны выжить."""
    bot, recorded = wired
    await _vaccine_reminders(session, family, mother, 5)

    await baby.edit_sex(
        _call(bot), SexCB(value="m"), FakeState(), session, child
    )

    assert child.sex == "m"
    assert "мальчик" in recorded.sent[0]
    assert len(await repo.active_reminders(session, family.id)) == 5


# --- Выбор поля ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("name", baby.Edit.name),
        ("sex", baby.Edit.sex),
        ("born", baby.Edit.born),
        ("weight", baby.Edit.weight),
    ],
)
async def test_each_button_asks_its_own_question(field, expected, child, wired):
    bot, recorded = wired
    state = FakeState()

    await baby.edit_pick(_call(bot), EditCB(field=field), state, child)

    assert state.state is expected
    assert recorded.sent == [baby._EDIT_PROMPT[field]]


@pytest.mark.asyncio
async def test_stale_button_without_a_child_says_so(wired):
    """Кнопка из старого сообщения не должна ронять хендлер."""
    bot, recorded = wired
    state = FakeState()

    await baby.edit_pick(_call(bot), EditCB(field="born"), state, None)

    assert state.state is None
    assert recorded.sent == []
    assert recorded.alerts[0]["text"] == texts.NO_CHILD
