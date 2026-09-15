"""Постоянная клавиатура под полем ввода: три кнопки и три границы.

Нажатие reply-кнопки Telegram присылает обычным сообщением от человека —
неотличимым от реплики в чат. Отсюда всё, что здесь проверяется: метка обязана
дойти до своего хендлера и не быть съеденной ни ожиданием вопроса, ни
обработчиком голого числа, ни шагом мастера, — и при этом не забрать себе
обычную переписку семьи.

Прогон идёт через настоящий `Dispatcher` с боевым `handlers.routers` целиком,
а не со своей тройкой роутеров в правильном порядке. Своя тройка доказывала бы,
что механизм работает при верном порядке, и молчала бы о том, каков порядок на
самом деле.
"""

from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage, TelegramMethod
from aiogram.types import Chat, Message, Update, User

from bot import handlers, keyboards, texts
from bot.handlers import baby, home
from bot.middlewares import drop_wizard_state
from bot.services import pending

CHAT = Chat(id=-1001, type="supergroup", title="Малышка")
AUTHOR = User(id=222, is_bot=False, first_name="Аня")
NOW = datetime(2026, 9, 13, 12, 0)


# --- Сторожа, которым диспетчер не нужен ----------------------------------


def test_every_home_button_has_an_action():
    """Метки клавиатуры и таблица действий обязаны совпадать.

    Тот же сторож, что `test_menu_promises_only_what_exists` у меню команд, и
    заведён по той же причине: кнопка без действия не отвечает ничего, а
    молчание бота на им же предложенную кнопку хуже отсутствия кнопки.

    Сравнение трёхстороннее и не тавтология: `keyboards.home()` рисует
    клавиатуру, `home.ACTIONS` решает, что делать, а `keyboards.HOME_LABELS`
    читает `middlewares`, решая, обрывать ли мастер. Разойдись любые два —
    и часть кнопок начнёт вести себя иначе, чем соседние.
    """
    shown = {button.text for row in keyboards.home().keyboard for button in row}
    assert shown == set(home.ACTIONS)
    assert shown == set(keyboards.HOME_LABELS)


def test_labels_carry_no_variation_selector():
    """Эмодзи метки — без U+FE0F, и это не придирка.

    Подпись reply-кнопки уходит на клиент и возвращается оттуда обычным
    текстом. Селектор вариаций клиент вправе нормализовать, и вернувшаяся
    метка перестаёт равняться своей же константе: кнопка есть, бот молчит, и
    видно это только глазами в чате.
    """
    for label in keyboards.HOME_LABELS:
        assert "️" not in label, label


def test_no_label_is_eaten_by_the_triage():
    """Слово из `redflags.yaml` в метке отняло бы кнопку навсегда.

    `alarm` стоит выше и забрал бы такую метку себе — молча. Проверяем тем же
    кодом, каким триаж и работает.
    """
    from bot.services import redflags

    for label in keyboards.HOME_LABELS:
        assert not redflags.triage(label), label


def test_the_router_stands_below_alarm_and_above_the_wizard():
    """Три границы места роутера `home`, и все три по боевому списку.

    Ниже `alarm` — граница безопасности. Выше `baby` — иначе тап посреди
    мастера станет именем ребёнка. Выше `qa` и `measure` — иначе метку съест
    фильтр ожидания вопроса или обработчик голого числа.
    """
    order = [router.name for router in handlers.routers]
    assert order.index("alarm") < order.index("home")
    assert order.index("home") < order.index("baby")
    assert order.index("home") < order.index("qa") < order.index("measure")


def test_the_keyboard_survives_a_tap():
    """`is_persistent` и `resize_keyboard` — не украшение.

    Без первого клавиатура сворачивается в иконку после нажатия, то есть
    перестаёт быть одним тапом, ради которого всё и затевалось. `selective` не
    ставим: показанная одному родителю клавиатура — это второй родитель,
    который не узнал, что кнопки есть.
    """
    markup = keyboards.home()
    assert markup.is_persistent is True
    assert markup.resize_keyboard is True
    assert markup.one_time_keyboard is not True
    assert markup.selective is not True


# --- Прогон через настоящий диспетчер -------------------------------------


class RecordingSession(BaseSession):
    """Сеть не трогаем: складываем отправленное и удалённое в списки.

    Имена методов записываются наравне с текстом: без этого `DeleteMessage` не
    оставлял бы следа вовсе, и тест на уборку был бы тавтологией в одежде
    проверки.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []
        self.markups: list[Any] = []
        self.deleted: list[int] = []
        self.refuse_delete = False

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Any, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if isinstance(method, DeleteMessage):
            if self.refuse_delete:
                raise TelegramBadRequest(
                    method=method, message="not enough rights to delete a message"
                )
            self.deleted.append(method.message_id)
            return True
        payload = method.model_dump(exclude_none=True)
        if "text" in payload:
            self.sent.append(payload["text"])
            self.markups.append(payload.get("reply_markup"))
        return Message(message_id=999, date=NOW, chat=CHAT, from_user=AUTHOR, text="")

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def wired(session, family, child, mother, monkeypatch):
    """Диспетчер с боевыми роутерами и боевыми middleware.

    `drop_wizard_state` регистрируется здесь не для полноты: без него тест про
    тап посреди мастера проверял бы не то, что происходит в бою.
    """
    talk = RecordingSession()
    bot = Bot(token="42:TEST", session=talk)
    dp = Dispatcher()

    async def inject(handler, event, data):
        data.update(session=session, family=family, child=child, member=mother)
        return await handler(event, data)

    dp.update.outer_middleware(inject)
    dp.message.outer_middleware(drop_wizard_state)

    order = tuple(handlers.routers)
    for router in order:
        dp.include_router(router)

    opened: list[dict] = []

    async def refresh(bot_, family_id, last_message_id=None, now=None, *, force_new=False):
        opened.append({"family_id": family_id, "force_new": force_new})

    # Настоящая `panel.refresh` открывает свою сессию — к боевому файлу базы, а
    # не к той, что подставил `inject`. См. её докстроку про то, почему сессию
    # хендлера туда не передают
    monkeypatch.setattr(home.panel, "refresh", refresh)

    yield dp, bot, talk, opened

    # Роутеры — синглтоны уровня модуля, и aiogram запоминает, к какому
    # диспетчеру их подключили. Без отвязки второй тест в файле падает на
    # «Router is already attached»
    for router in order:
        router._parent_router = None


async def _feed(dp, bot, text, message_id=1):
    message = Message(
        message_id=message_id, date=NOW, chat=CHAT, from_user=AUTHOR, text=text
    )
    await dp.feed_update(bot, Update(update_id=message_id, message=message))


@pytest.mark.asyncio
async def test_every_button_gets_an_answer(wired):
    """Поведенческая половина сторожа: на каждую кнопку что-то происходит.

    Структурное сравнение таблиц не поймало бы ни метку, съеденную роутером
    выше, ни забытый в `handlers.routers` роутер.
    """
    dp, bot, talk, opened = wired

    for number, label in enumerate(sorted(keyboards.HOME_LABELS), start=10):
        before = (len(talk.sent), len(opened))
        await _feed(dp, bot, label, number)
        assert (len(talk.sent), len(opened)) != before, f"{label}: тишина"


@pytest.mark.asyncio
async def test_the_panel_button_does_what_the_command_does(wired):
    """`force_new`, а не правка: нажали именно потому, что панели не видно."""
    dp, bot, talk, opened = wired

    await _feed(dp, bot, keyboards.HOME_PANEL, 7)

    assert len(opened) == 1
    assert opened[0]["force_new"] is True


@pytest.mark.asyncio
async def test_the_tap_is_removed_from_the_chat(wired):
    """Три «📋 Панель» подряд в ленте ночной переписки хуже трёх /panel."""
    dp, bot, talk, opened = wired

    await _feed(dp, bot, keyboards.HOME_PANEL, 7)

    assert talk.deleted == [7]


@pytest.mark.asyncio
async def test_a_tap_works_without_the_right_to_delete(wired):
    """Уборка — украшение, ровно как закрепление панели.

    Без права `can_delete_messages` нажатие останется в ленте, и это не повод
    не делать то, ради чего его нажали. Прямой аналог
    `test_panel_survives_missing_pin_rights`.
    """
    dp, bot, talk, opened = wired
    talk.refuse_delete = True

    await _feed(dp, bot, keyboards.HOME_PANEL, 7)

    assert len(opened) == 1
    assert talk.deleted == []
    assert talk.sent == [], "ругани в чат быть не должно"


@pytest.mark.asyncio
async def test_the_question_button_works_like_the_one_on_the_panel(wired):
    """Та же кнопка, что на панели, и тот же код за ней.

    Вторая половина теста важнее первой: она доказывает, что вынос
    `start_question` не потерял контракт — следующая реплика доходит до Q&A.
    """
    dp, bot, talk, opened = wired

    await _feed(dp, bot, keyboards.HOME_ASK, 7)

    ask = pending.peek(CHAT.id, AUTHOR.id)
    assert ask is not None and ask.what == pending.QUESTION
    assert texts.ASK_QUESTION in talk.sent

    await _feed(dp, bot, "а пустышка это нормально?", 8)
    assert pending.peek(CHAT.id, AUTHOR.id) is None, "вопрос обязан дойти до qa"


@pytest.mark.asyncio
async def test_the_day_button_says_exactly_what_the_command_says(wired):
    """Переиспользование настоящее, а не вторая, более бедная копия."""
    dp, bot, talk, opened = wired

    await _feed(dp, bot, "/today", 7)
    by_command, markup = talk.sent[-1], talk.markups[-1]

    await _feed(dp, bot, keyboards.HOME_TODAY, 8)

    assert talk.sent[-1] == by_command
    assert talk.markups[-1] == markup, "переключатель периодов обязан остаться"


@pytest.mark.asyncio
async def test_a_tap_beats_a_waiting_question(wired):
    """Нажали «❓ Вопрос», передумали и нажали «📋 Панель».

    Метка обязана уйти в `home`, а не в поиск по базе знаний: роутер стоит
    выше `qa` именно для этого.
    """
    dp, bot, talk, opened = wired
    pending.remember(CHAT.id, AUTHOR.id, pending.QUESTION)

    await _feed(dp, bot, keyboards.HOME_PANEL, 7)

    assert len(opened) == 1


@pytest.mark.asyncio
async def test_an_ordinary_phrase_passes_home_by(wired):
    """Совпадение точное. «панель где-то выше» — это реплика, а не нажатие."""
    dp, bot, talk, opened = wired

    await _feed(dp, bot, "панель где-то выше", 7)

    assert opened == []
    assert talk.deleted == []


@pytest.mark.asyncio
async def test_a_tap_mid_wizard_does_not_become_the_name(wired):
    """Главное про мастер: тап не становится именем ребёнка, и это слышно.

    Роутер `home` стоит выше `baby` и апдейт заберёт себе в любом случае — но
    состояние FSM без строки в `middlewares` осталось бы висеть, и следующая
    фраза человека, хоть через три дня, молча стала бы именем.
    """
    dp, bot, talk, opened = wired
    context = dp.fsm.resolve_context(bot=bot, chat_id=CHAT.id, user_id=AUTHOR.id)
    await context.set_state(baby.New.name)

    await _feed(dp, bot, keyboards.HOME_PANEL, 7)

    assert await context.get_state() is None
    assert texts.WIZARD_DROPPED in talk.sent
    assert len(opened) == 1

    # И главное: следующая фраза именем тоже не становится
    await _feed(dp, bot, "купи молока", 8)
    assert texts.BABY_ASK_SEX not in talk.sent


@pytest.mark.asyncio
async def test_a_bare_number_still_reaches_measure(wired):
    """Новый роутер не должен отнять ввод числа — он стоит выше `measure`."""
    dp, bot, talk, opened = wired
    pending.remember(CHAT.id, AUTHOR.id, pending.WEIGHT_G)

    await _feed(dp, bot, "5200", 7)

    assert pending.peek(CHAT.id, AUTHOR.id) is None, "число обязано дойти до measure"
    assert talk.deleted == []
