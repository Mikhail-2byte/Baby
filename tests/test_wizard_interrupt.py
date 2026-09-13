"""Команда посреди мастера обрывает мастер, и обрыв виден человеку.

Тест заведён по факту: `middlewares.drop_wizard_state` был написан ровно для
этого, снабжён докстрингом на семь строк — и не зарегистрирован нигде. Поломка,
от которой он придуман, была живой:

    /baby → «Как зовут?» → человек пишет /today → получает сводку →
    следующая его фраза в чате молча становится именем ребёнка.

У FSM нет таймаута, поэтому «следующая фраза» — это хоть через три дня.

Гоняем через настоящий `Dispatcher`, а не вызовом middleware напрямую. Иначе
тест не проверил бы главное допущение: что `state` вообще попадает в `data`
к моменту, когда outer-middleware на `dp.message` получает управление.
"""

from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.base import BaseSession
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, Message, Update, User

from bot import texts
from bot.middlewares import drop_wizard_state

CHAT = Chat(id=-1001, type="supergroup", title="Малышка")
AUTHOR = User(id=222, is_bot=False, first_name="Аня")
# Фиксированный момент: в этом тесте время ни на что не влияет, а плавающее
# «сейчас» делает разбор падения труднее, чем он есть
NOW = datetime(2026, 9, 13, 12, 0)


class Wizard(StatesGroup):
    """Заглушка вместо `baby.New` — тест про механику, а не про мастер."""

    name = State()


class RecordingSession(BaseSession):
    """Сеть не трогаем: складываем отправленное в список."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Any, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        payload = method.model_dump(exclude_none=True)
        if "text" in payload:
            self.sent.append(payload["text"])
        return Message(
            message_id=999, date=NOW, chat=CHAT, from_user=AUTHOR,
            text=payload.get("text", ""),
        )

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _message(text: str, message_id: int = 1) -> Message:
    return Message(
        message_id=message_id, date=NOW, chat=CHAT, from_user=AUTHOR, text=text
    )


@pytest.fixture
def wired():
    """Диспетчер с зарегистрированным middleware и двумя хендлерами."""
    session = RecordingSession()
    bot = Bot(token="42:TEST", session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(drop_wizard_state)

    router = Router(name="probe")
    seen: dict[str, Any] = {"names": []}

    @router.message(Command("today"))
    async def today(message: Message) -> None:
        await message.answer("сводка")

    # Порядок как в `handlers/baby.py`: команда шага объявлена раньше, чем
    # ловушка `F.text`, иначе «/skip» ушёл бы в неё как обычный текст
    @router.message(Wizard.name, Command("skip"))
    async def skip(message: Message) -> None:
        await message.answer("пропустил")

    @router.message(Wizard.name, F.text)
    async def got_name(message: Message, state: FSMContext) -> None:
        seen["names"].append(message.text)
        await state.clear()

    dp.include_router(router)
    return dp, bot, session, seen


async def _state_of(dp: Dispatcher, bot: Bot) -> str | None:
    context = dp.fsm.resolve_context(
        bot=bot, chat_id=CHAT.id, user_id=AUTHOR.id
    )
    return await context.get_state()


async def _feed(dp: Dispatcher, bot: Bot, text: str, message_id: int) -> None:
    await dp.feed_update(bot, Update(update_id=message_id, message=_message(text, message_id)))


@pytest.mark.asyncio
async def test_command_breaks_the_wizard_and_says_so(wired):
    dp, bot, session, seen = wired
    context = dp.fsm.resolve_context(bot=bot, chat_id=CHAT.id, user_id=AUTHOR.id)
    await context.set_state(Wizard.name)

    await _feed(dp, bot, "/today", 1)

    assert await _state_of(dp, bot) is None, "мастер обязан быть сброшен"
    assert "сводка" in session.sent, "ответ на саму команду важнее и идёт первым"
    assert texts.WIZARD_DROPPED in session.sent, "обрыв обязан быть виден"
    assert session.sent.index("сводка") < session.sent.index(texts.WIZARD_DROPPED)

    # Главное: следующая фраза человека НЕ становится именем ребёнка
    await _feed(dp, bot, "купи молока", 2)
    assert seen["names"] == []


@pytest.mark.asyncio
async def test_wizard_step_survives_its_own_command(wired):
    """`/skip` — шаг самого мастера, обрывать его нельзя."""
    dp, bot, session, seen = wired
    context = dp.fsm.resolve_context(bot=bot, chat_id=CHAT.id, user_id=AUTHOR.id)
    await context.set_state(Wizard.name)

    await _feed(dp, bot, "/skip", 1)

    assert await _state_of(dp, bot) == Wizard.name.state
    assert texts.WIZARD_DROPPED not in session.sent


@pytest.mark.asyncio
async def test_plain_text_reaches_the_wizard(wired):
    """Обычная реплика — это ответ мастеру, а не помеха ему."""
    dp, bot, session, seen = wired
    context = dp.fsm.resolve_context(bot=bot, chat_id=CHAT.id, user_id=AUTHOR.id)
    await context.set_state(Wizard.name)

    await _feed(dp, bot, "Аня", 1)

    assert seen["names"] == ["Аня"]
    assert texts.WIZARD_DROPPED not in session.sent


@pytest.mark.asyncio
async def test_command_without_a_wizard_is_untouched(wired):
    """Без мастера middleware обязан молчать — иначе он заметен всегда."""
    dp, bot, session, seen = wired

    await _feed(dp, bot, "/today", 1)

    assert session.sent == ["сводка"]
