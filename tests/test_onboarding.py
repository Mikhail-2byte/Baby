"""Первый контакт: личка, приветствие, кнопка в мастер, права, `/backup`.

Бот написан для одной семьи на одной машине, и до этого этапа первый контакт
с ним был устроен под человека, который и так знает, что делать. Незнакомец,
которому переслали ссылку, попадал в личку и получал отказ из двух строк: что
за группа, кого туда звать и зачем боту права — не сказано нигде, кроме
`README.md`, написанного оператору.

Здесь проверяется то, что из этого следует: личка объясняет, группа здоровается
и даёт кнопку, права объясняются и напоминаются один раз, а `/backup` перестаёт
отдавать базу со всеми семьями сразу кому попало.

Объекты aiogram настоящие: `start_from_button` проверяет
`isinstance(call.message, Message)` — у `CallbackQuery.message` тип допускает
`InaccessibleMessage`, — и заглушка прошла бы мимо этой ветки, ничего не
проверив.
"""

from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetMe, SendDocument, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from bot import handlers, keyboards, texts
from bot.config import settings
from bot.handlers import baby, common
from bot.keyboards import StartCB

CHAT = Chat(id=-1001, type="supergroup", title="Малышка")
PRIVATE = Chat(id=222, type="private", first_name="Аня")
AUTHOR = User(id=222, is_bot=False, first_name="Аня")
ME = User(id=42, is_bot=True, first_name="Бот", username="BabyBot")
NOW = datetime(2026, 9, 19, 12, 0)


class RecordingSession(BaseSession):
    """Сети нет: складываем текст, разметку и имена методов.

    `GetMe` отвечает настоящим `User`: ссылка «добавить в группу» собирается из
    имени бота, и без этой ветки заглушка вернула бы `Message`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []
        self.markups: list[Any] = []
        self.methods: list[str] = []
        self.documents: list[Any] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Any, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        self.methods.append(type(method).__name__)
        if isinstance(method, GetMe):
            return ME
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, SendDocument):
            # Выгружать в dict нельзя: внутри `FSInputFile`, а тесту нужен сам
            # факт отправки файла
            self.documents.append(method.caption)
            return Message(message_id=998, date=NOW, chat=CHAT, from_user=AUTHOR)
        payload = method.model_dump(exclude_none=True)
        if "text" in payload:
            self.sent.append(payload["text"])
            self.markups.append(getattr(method, "reply_markup", None))
        return Message(
            message_id=999, date=NOW, chat=CHAT, from_user=AUTHOR,
            text=payload.get("text", ""),
        )

    async def stream_content(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class FakeState:
    """FSM-контекст в том объёме, в каком его трогает вход в мастер."""

    def __init__(self, data: dict | None = None) -> None:
        self.state: Any = None
        self.cleared = False
        self._data = dict(data or {})

    async def set_state(self, value: Any) -> None:
        self.state = value

    async def clear(self) -> None:
        self.cleared = True
        self.state = None
        self._data = {}

    async def get_data(self) -> dict:
        return dict(self._data)

    async def update_data(self, **kwargs: Any) -> dict:
        self._data.update(kwargs)
        return dict(self._data)


@pytest.fixture
def talk():
    session = RecordingSession()
    return Bot(token="42:TEST", session=session), session


def _message(bot: Bot, text: str, chat: Chat = CHAT) -> Message:
    return Message(
        message_id=1, date=NOW, chat=chat, from_user=AUTHOR, text=text
    ).as_(bot)


def _tap(bot: Bot) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=AUTHOR,
        chat_instance="ci",
        message=_message(bot, texts.FIRST_STEP),
        data=StartCB(action="baby").pack(),
    ).as_(bot)


# --- Личка: гид, а не отказ -------------------------------------------------


@pytest.fixture
def private_dp():
    """Боевые роутеры целиком: в личке важен и порядок, и то, что `common` первый.

    Семью и ребёнка не подставляем намеренно — в личке их нет, и хендлер,
    которому они понадобились бы, уронил бы резолв аргументов.
    """
    session = RecordingSession()
    bot = Bot(token="42:TEST", session=session)
    dp = Dispatcher()
    order = tuple(handlers.routers)
    for router in order:
        dp.include_router(router)
    yield dp, bot, session
    for router in order:
        router._parent_router = None


async def _say_private(dp, bot, text: str) -> None:
    await dp.feed_update(
        bot,
        Update(
            update_id=1,
            message=Message(
                message_id=1, date=NOW, chat=PRIVATE, from_user=AUTHOR, text=text
            ),
        ),
    )


@pytest.mark.asyncio
async def test_the_private_chat_explains_how_to_start(private_dp):
    """Человек, которому переслали бота, обязан узнать, что делать."""
    dp, bot, session = private_dp

    await _say_private(dp, bot, "привет")

    assert session.sent == [texts.PRIVATE_GUIDE]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/start", "/today", "привет", "а это что такое?"])
async def test_the_private_guide_answers_anything(private_dp, text):
    """Ловим всё подряд, а не только команды.

    Человек в личке находится ровно в одном состоянии — «не понимаю, что
    делать», — и ответ у этого состояния один. Отдельного `/start` поэтому нет:
    он проваливается сюда же, а второй текст разошёлся бы с первым.
    """
    dp, bot, session = private_dp

    await _say_private(dp, bot, text)

    assert session.sent == [texts.PRIVATE_GUIDE]


@pytest.mark.asyncio
async def test_the_private_guide_carries_a_link_to_add_the_bot(private_dp):
    """Кнопка — ссылка, а не `callback_data`, и права в ней уже отмечены.

    `?startgroup=true` открывает выбор группы механизмом самого Telegram, а
    `&admin=` превращает пункт «сделайте администратором» из поручения человеку
    в уже сделанное действие. У `url`-кнопки нет обработчика — она не стареет в
    истории чата и не может молча ничего не сделать.
    """
    dp, bot, session = private_dp

    await _say_private(dp, bot, "привет")

    markup = session.markups[0]
    assert isinstance(markup, InlineKeyboardMarkup)
    url = markup.inline_keyboard[0][0].url
    assert url is not None
    assert "startgroup=true" in url
    assert ME.username in url
    assert "pin_messages" in url and "delete_messages" in url


@pytest.mark.asyncio
async def test_the_private_guide_has_no_reply_keyboard(private_dp):
    """Все три кнопки `home()` ведут в групповые хендлеры.

    В личке нет ни семьи, ни ребёнка — вышла бы вечная тройка кнопок, не
    отвечающих ничего. Инвариант «каждый текстовый ответ несёт клавиатуру»
    говорит про ответы **без** inline-разметки, а она здесь есть.
    """
    dp, bot, session = private_dp

    await _say_private(dp, bot, "привет")

    assert not isinstance(session.markups[0], ReplyKeyboardMarkup)


# --- Кнопка из приветствия --------------------------------------------------


@pytest.mark.asyncio
async def test_the_greeting_button_starts_the_wizard(talk, family):
    """До неё вход в мастер был один — набрать `/baby` руками.

    Для семьи, которая только что добавила бота и не знает ни одной его
    команды, это шаг, который должен выполнить человек, то есть дыра.
    """
    bot, session = talk
    state = FakeState()

    await baby.start_from_button(_tap(bot), state, None, family)

    assert texts.BABY_ASK_NAME in session.sent
    assert state.state == baby.New.name


@pytest.mark.asyncio
async def test_a_stale_greeting_button_shows_the_card(talk, family, child):
    """Кнопка живёт в истории чата неограниченно долго.

    Тап по годовалому приветствию обязан ответить осмысленно — и отвечает тем
    же, чем `/baby`: показывает готовую карточку, а не зовёт заводить вторую.
    """
    bot, session = talk
    state = FakeState()

    await baby.start_from_button(_tap(bot), state, child, family)

    assert session.sent and child.name in session.sent[0]
    assert state.state is None


@pytest.mark.asyncio
async def test_the_greeting_button_restarts_a_half_filled_wizard(talk, family):
    """Тап посреди мастера начинает заново, а не подклеивается к черновику.

    Без `state.clear()` человек вернулся бы на первый шаг, но в данных остались
    бы имя и пол от прежнего захода — и «Готово» собрало бы карточку из двух
    разных. Мутация «только `set_state`» красит этот тест.
    """
    bot, session = talk
    state = FakeState({"name": "Прежнее", "sex": "f"})

    await baby.start_from_button(_tap(bot), state, None, family)

    assert await state.get_data() == {}


# --- Права: напоминание в конце мастера -------------------------------------


async def _finish_wizard(talk, session, family, mother, monkeypatch, *, admin):
    """Довести мастер до «Готово» и вернуть, что бот сказал."""
    bot, recorded = talk

    async def refresh(*a: Any, **kw: Any) -> None:
        return None

    async def probe(_bot: Any, _family: Any) -> bool | None:
        return admin

    monkeypatch.setattr(baby.panel, "refresh", refresh)
    monkeypatch.setattr(baby.sending, "is_admin", probe)

    state = FakeState(
        {"name": "Ева", "sex": "f", "born": datetime(2026, 9, 13, 4, 20).isoformat()}
    )
    await baby._finish(
        _message(bot, "3420"), state, session, family, mother, bot, grams=3420
    )
    return recorded.sent


@pytest.mark.asyncio
async def test_the_wizard_reminds_about_rights_once(
    talk, session, family, mother, monkeypatch
):
    """Единственное напоминание за жизнь семьи, и оно получается устройством.

    Мастер проходят один раз — ровно поэтому в нём вообще оставлен FSM. Значит
    ни отметки в базе, ни ветки в тикере, ни риска напомнить на каждом тике.
    """
    said = await _finish_wizard(talk, session, family, mother, monkeypatch, admin=False)

    assert said[0] == texts.BABY_SAVED, "ответ человеку уходит первым"
    assert texts.ADMIN_REMINDER in said


@pytest.mark.asyncio
async def test_the_wizard_stays_quiet_when_it_has_the_rights(
    talk, session, family, mother, monkeypatch
):
    said = await _finish_wizard(talk, session, family, mother, monkeypatch, admin=True)

    assert texts.ADMIN_REMINDER not in said


@pytest.mark.asyncio
async def test_an_unanswered_probe_says_nothing(
    talk, session, family, mother, monkeypatch
):
    """`None` — «спросить не удалось», и на нём молчим.

    Мутация «свернуть `None` в `False`» красит этот тест: напоминать про права
    из-за сетевого сбоя хуже, чем не напомнить вовсе.
    """
    said = await _finish_wizard(talk, session, family, mother, monkeypatch, admin=None)

    assert texts.ADMIN_REMINDER not in said


# --- `/backup` стал владельческим -------------------------------------------


@pytest.mark.asyncio
async def test_backup_in_a_strangers_chat_is_refused(talk, family, monkeypatch):
    """В снимке лежит база целиком, а база одна на все семьи.

    Та же команда, набранная в чужой группе, отдала бы туда дневник чужого
    ребёнка. Молчать при этом нельзя: `/backup` объявлена в `texts.COMMANDS`,
    то есть предложена самим ботом.
    """
    bot, recorded = talk
    monkeypatch.setattr(settings, "owner_chat_id", family.chat_id - 1)

    taken: list[Any] = []

    async def snapshot(dest: Any) -> None:
        taken.append(dest)

    monkeypatch.setattr(common.backup, "snapshot", snapshot)

    await common.make_backup(_message(bot, "/backup"), family)

    assert recorded.sent == [texts.OWNER_ONLY]
    assert taken == [], "до снятия копии дело доходить не должно"


@pytest.mark.asyncio
async def test_backup_without_an_owner_set_works_as_before(talk, family, monkeypatch):
    """Пустая настройка — прежнее поведение.

    У того, кто держит бота для себя одного, чужих чатов нет вовсе, и новая
    проверка не должна отнимать у него бэкап.
    """
    bot, recorded = talk
    monkeypatch.setattr(settings, "owner_chat_id", 0)

    taken: list[Any] = []

    async def snapshot(dest: Any) -> None:
        taken.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"snapshot")

    monkeypatch.setattr(common.backup, "snapshot", snapshot)

    await common.make_backup(_message(bot, "/backup"), family)

    assert taken, "копия обязана сниматься"
    assert recorded.documents, "и уходить файлом в чат"


@pytest.mark.asyncio
async def test_the_owner_keeps_the_backup(talk, family, monkeypatch):
    """Положительный двойник: в своём чате команда работает как раньше.

    Без него «в чужом отказали» доказывало бы ровно ничего — проверка могла бы
    отказывать всем.
    """
    bot, recorded = talk
    monkeypatch.setattr(settings, "owner_chat_id", family.chat_id)

    async def snapshot(dest: Any) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"snapshot")

    monkeypatch.setattr(common.backup, "snapshot", snapshot)

    await common.make_backup(_message(bot, "/backup"), family)

    assert texts.OWNER_ONLY not in recorded.sent
    assert recorded.documents


def test_the_link_asks_only_for_rights_the_bot_uses():
    """Права в ссылке — ровно те два, которые бот умеет использовать.

    Закрепление панели и уборка нажатий. Просить больше значило бы просить
    впрок, а человек читает этот список в ту секунду, когда решает, доверять
    боту или нет.
    """
    assert set(keyboards.ADD_RIGHTS.split("+")) == {
        "pin_messages",
        "delete_messages",
    }


def test_the_wizard_button_is_a_value_not_a_field():
    """У `StartCB` ровно одно поле, и оно обязательное.

    Кнопки живут в истории чата неограниченно долго: новое **значение** старые
    кнопки не ломает, новое **поле** ломает — распаковка падает, и нажатие
    молча ничего не делает. Прецедент — `UndoCB.what="reopen"`.
    """
    assert list(StartCB.model_fields) == ["action"]
    assert StartCB.unpack(StartCB(action="baby").pack()).action == "baby"


def test_one_message_never_carries_both_markups():
    """Почему приветствие — два сообщения, а не одно.

    Telegram не принимает сообщение сразу с клавиатурой под полем ввода и с
    inline-кнопками, и это единственная причина, по которой приветствие
    разрезано надвое. Сторож структурный намеренно: он объясняет форму, которую
    иначе через год спрямят обратно в одно сообщение.
    """
    assert isinstance(keyboards.home(), ReplyKeyboardMarkup)
    assert isinstance(keyboards.start_card(), InlineKeyboardMarkup)
    assert not isinstance(keyboards.start_card(), ReplyKeyboardMarkup)


def test_nothing_promises_a_command_that_does_not_exist():
    """Ни один текст бота не обещает несуществующей команды.

    Расширение сторожа, стоявшего только над `DESCRIPTION`: тексты первого
    контакта читает человек, который ещё ничего про бота не знает, и `/setup`,
    написанный по вдохновению, он попробует набрать.
    """
    import re

    from tests.test_texts import _registered_commands

    known = _registered_commands()
    for name, text in (
        ("DESCRIPTION", texts.DESCRIPTION),
        ("GREETING", texts.GREETING),
        ("PRIVATE_GUIDE", texts.PRIVATE_GUIDE),
        ("FIRST_STEP", texts.FIRST_STEP),
        ("ADMIN_WHY", texts.ADMIN_WHY),
        ("ADMIN_THANKS", texts.ADMIN_THANKS),
        ("ADMIN_REMINDER", texts.ADMIN_REMINDER),
        ("OWNER_ONLY", texts.OWNER_ONLY),
    ):
        promised = set(re.findall(r"(?<!<)/([a-z_]+)", text))
        assert promised <= known, (name, promised - known)


def test_the_greeting_no_longer_hardcodes_the_rights_paragraph():
    """Просьба о правах вынесена, иначе её нельзя не сказать.

    Пришедший по ссылке с `admin=` добавляется сразу администратором, и просьба
    о том, что уже дали, в первом же слове бота читалась бы как
    невнимательность.
    """
    assert "администратор" not in texts.GREETING.lower()
    assert "администратор" in texts.ADMIN_WHY.lower()


def test_the_guide_mentions_both_rights_by_their_telegram_names():
    """Человек ищет эти слова глазами в меню Telegram, а не по смыслу."""
    assert "Закреплять сообщения" in texts.PRIVATE_GUIDE
    assert "Удалять сообщения" in texts.PRIVATE_GUIDE


def test_the_guide_says_who_can_be_invited():
    """Про «кого звать» спрашивал владелец прямо, и ответ обязан быть в тексте."""
    lowered = texts.PRIVATE_GUIDE.lower()
    assert "группу" in lowered
    assert any(word in lowered for word in ("мужа", "жену", "бабушку", "няню"))


def test_the_refusal_text_is_gone():
    """Старое имя не должно остаться: оно называло отказ, а константа стала гидом.

    Тот же случай, что `voice_closed` → `closed_previous`: имя, называвшее
    одного вызывающего, врало остальным.
    """
    assert not hasattr(texts, "PRIVATE_ONLY")
