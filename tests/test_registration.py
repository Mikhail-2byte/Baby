"""Авторегистрация семьи: от какого апдейта она заводится, а от какого нет.

Заведён по боевой находке 16.09. Стенд работает тем же токеном, что боевой бот,
но в отдельной группе. На каждом прогоне он закреплял панель, Telegram клал в
чат служебное сообщение о закреплении, боевой бот получал его — и заводил у
себя семью по чужому чату. Обработчика у такого апдейта нет ни одного.

Дальше становилось хуже: сторож `is_production_chat`, написанный ровно против
прогона стенда по боевой группе, спотыкался о собственную грязь и отказывал
следующему акту. То есть авторегистрация ломала защиту, придуманную против
того же самого.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from aiogram.types import (
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    User,
)
from sqlalchemy import select

from bot import middlewares, texts
from bot.db.models import Family
from bot.handlers import common
from bot.middlewares import FamilyMiddleware

NOW = datetime(2026, 9, 16, 8, 0)
STRANGER = Chat(id=-5437929633, type="group", title="Стенд")
HUMAN = User(id=1778570470, is_bot=False, first_name="Михаил")
ME = User(id=8992988295, is_bot=True, first_name="Бот")


class _Bot:
    """Заглушка бота для middleware.

    Методов три, и два из них появились вместе с опоздавшим приветствием: оно
    спрашивает про права (`get_chat_member`) и говорит (`send_message`).
    `admin` задаёт ответ про права; `None` изображает сетевой сбой.
    """

    def __init__(self, admin: bool | None = False) -> None:
        self.sent: list[str] = []
        self._admin = admin

    async def me(self) -> User:
        return ME

    async def get_chat_member(self, chat_id: int, user_id: int):
        if self._admin is None:
            raise RuntimeError("сеть отвалилась")
        status = "administrator" if self._admin else "member"
        return SimpleNamespace(status=status)

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(message_id=len(self.sent))


def _pin_service(chat: Chat) -> Message:
    """Служебное сообщение «бот закрепил сообщение».

    Автор — сам бот, текста нет вовсе. Именно такой апдейт и заводил лишнюю
    семью: `alarm` висит на `F.text | F.caption`, `measure` на `F.text`, и ни
    один из них его не видит — обработчика нет, а семья появлялась.
    """
    pinned = Message(message_id=1, date=NOW, chat=chat, from_user=ME, text="панель")
    return Message(
        message_id=2, date=NOW, chat=chat, from_user=ME, pinned_message=pinned
    )


async def _feed(event, chat, user, session_maker, monkeypatch, bot=None) -> dict:
    monkeypatch.setattr(middlewares, "Session", session_maker)
    seen: dict = {}

    async def handler(_event, data):
        seen.update(data)
        return "ok"

    await FamilyMiddleware()(
        handler,
        event,
        {"event_chat": chat, "event_from_user": user, "bot": bot or _Bot()},
    )
    return seen


async def _families(session) -> list[Family]:
    return list(await session.scalars(select(Family)))


@pytest.mark.asyncio
async def test_our_own_pin_does_not_create_a_family(session, session_maker, monkeypatch):
    """Главный сторож файла: эхо собственного закрепления семью не заводит."""
    seen = await _feed(
        _pin_service(STRANGER), STRANGER, ME, session_maker, monkeypatch
    )

    assert await _families(session) == []
    assert "family" not in seen, "семьи нет — и ключа быть не должно"


@pytest.mark.asyncio
async def test_a_human_in_the_same_chat_still_creates_it(
    session, session_maker, monkeypatch
):
    """Отсекаем эхо, а не чат.

    Без этой половины правка выглядела бы одинаково при «не заводим от эха» и
    при «не заводим вовсе», а второе сломало бы главное: семья обязана
    заводиться сама, без `/start`.
    """
    message = Message(
        message_id=3, date=NOW, chat=STRANGER, from_user=HUMAN, text="привет"
    )

    seen = await _feed(message, STRANGER, HUMAN, session_maker, monkeypatch)

    families = await _families(session)
    assert len(families) == 1
    assert families[0].chat_id == STRANGER.id
    assert seen["family"].id == families[0].id
    assert seen["member"].tg_user_id == HUMAN.id


@pytest.mark.asyncio
async def test_a_pin_in_a_known_chat_keeps_the_family(
    session, session_maker, family, monkeypatch
):
    """Уже заведённую семью отдаём как обычно.

    У настоящей семьи панель закрепляется на каждом перевыпуске, и потерять на
    этом апдейте `family` значило бы уронить резолв аргументов у любого
    будущего хендлера — то есть потерять апдейт навсегда.
    """
    chat = Chat(id=family.chat_id, type="supergroup", title=family.title)

    seen = await _feed(_pin_service(chat), chat, ME, session_maker, monkeypatch)

    assert seen["family"].id == family.id
    assert "child" in seen
    assert "member" not in seen, "автор — бот, участника заводить не из кого"
    assert len(await _families(session)) == 1


@pytest.mark.asyncio
async def test_being_added_to_a_group_creates_the_family(
    session, session_maker, monkeypatch
):
    """Приветствие не должно пострадать.

    `my_chat_member` — не сообщение, автор у него человек, и семья к моменту
    вызова хендлера обязана быть в `data`: иначе aiogram не резолвит аргумент
    `family` у `common.bot_added`, и приветствие не уходит вовсе.
    """
    event = ChatMemberUpdated(
        chat=STRANGER,
        from_user=HUMAN,
        date=NOW,
        old_chat_member=ChatMemberLeft(user=ME),
        new_chat_member=ChatMemberMember(user=ME),
    )

    seen = await _feed(event, STRANGER, HUMAN, session_maker, monkeypatch)

    assert len(await _families(session)) == 1
    assert seen["family"].chat_id == STRANGER.id


@pytest.mark.asyncio
async def test_a_private_chat_creates_nothing(session, session_maker, monkeypatch):
    """В личке семьи нет — там только гид по настройке."""
    private = Chat(id=HUMAN.id, type="private", first_name="Михаил")
    message = Message(
        message_id=4, date=NOW, chat=private, from_user=HUMAN, text="привет"
    )

    seen = await _feed(message, private, HUMAN, session_maker, monkeypatch)

    assert await _families(session) == []
    assert "family" not in seen
    assert seen["session"] is not None, "сессия нужна даже там, где семьи нет"


@pytest.mark.asyncio
async def test_the_stand_no_longer_dirties_the_live_base(
    session, session_maker, family, monkeypatch
):
    """Тот самый сценарий целиком, в одном тесте.

    Боевая семья одна; стенд закрепляет панель в своей группе тем же токеном.
    Раньше после этого семей становилось две, и `is_production_chat` начинал
    отказывать стенду по его же собственному чату.
    """
    before = {f.chat_id for f in await _families(session)}
    assert before == {family.chat_id}

    for _ in range(3):
        await _feed(
            _pin_service(STRANGER), STRANGER, ME, session_maker, monkeypatch
        )

    assert {f.chat_id for f in await _families(session)} == before


# --- Опоздавшее приветствие -------------------------------------------------
#
# Приветствие — носитель постоянной клавиатуры, то есть без него у семьи нет
# кнопок вовсе. Первый его путь, апдейт `my_chat_member` о добавлении, на
# домашней машине теряется: `delete_webhook(drop_pending_updates=True)` на
# старте выбрасывает всё, что пришло, пока ПК спал. Добавили бота ночью —
# приветствия не будет никогда.


def _hello(chat: Chat) -> Message:
    return Message(message_id=7, date=NOW, chat=chat, from_user=HUMAN, text="привет")


@pytest.mark.asyncio
async def test_a_family_that_missed_the_join_is_greeted_on_the_first_message(
    session, session_maker, monkeypatch
):
    """Второй путь приветствия — первая же реплика человека."""
    bot = _Bot()

    await _feed(_hello(STRANGER), STRANGER, HUMAN, session_maker, monkeypatch, bot)

    assert bot.sent, "семья без приветствия осталась бы и без клавиатуры"
    assert bot.sent[0].startswith(texts.GREETING)


@pytest.mark.asyncio
async def test_the_late_greeting_does_not_swallow_the_message(
    session, session_maker, monkeypatch
):
    """Реплику человека приветствие не съедает и вперёд себя не пускает.

    Middleware хендлеру не конкурент — он его зовёт, — и порядок здесь
    существенный: ответ на саму реплику важнее, приветствие идёт следом. Тот же
    порядок, что у `drop_wizard_state`.

    Мутация «поздороваться до хендлера» красит именно этот тест.
    """
    monkeypatch.setattr(middlewares, "Session", session_maker)
    bot = _Bot()

    async def handler(_event, data):
        await bot.send_message(STRANGER.id, "ответ на реплику")
        return "обработано"

    result = await FamilyMiddleware()(
        handler,
        _hello(STRANGER),
        {"event_chat": STRANGER, "event_from_user": HUMAN, "bot": bot},
    )

    assert result == "обработано", "ответ хендлера обязан дойти наружу"
    assert bot.sent[0] == "ответ на реплику"
    assert bot.sent[1].startswith(texts.GREETING)


@pytest.mark.asyncio
async def test_a_family_is_greeted_once(session, session_maker, monkeypatch):
    """Вторая реплика второго приветствия не порождает."""
    bot = _Bot()

    for _ in range(3):
        await _feed(_hello(STRANGER), STRANGER, HUMAN, session_maker, monkeypatch, bot)

    assert len([line for line in bot.sent if line.startswith(texts.GREETING)]) == 1


@pytest.mark.asyncio
async def test_an_old_family_is_not_greeted_again(
    session, session_maker, family, monkeypatch
):
    """Семья из ревизии здоровалась давно — просто отметки тогда не было.

    Ровно это и проставляет `UPDATE families SET greeted_at = created_at` в
    ревизии. Без него боевая семья получила бы приветствие из ниоткуда на
    первой же реплике после деплоя.
    """
    family.greeted_at = family.created_at
    await session.commit()
    chat = Chat(id=family.chat_id, type="supergroup", title=family.title)
    bot = _Bot()

    await _feed(_hello(chat), chat, HUMAN, session_maker, monkeypatch, bot)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_the_late_greeting_stays_quiet_about_rights_it_cannot_check(
    session, session_maker, monkeypatch
):
    """Зонд не ответил — про права молчим, напомнит мастер.

    Мутация «свернуть `None` в `False`» красит этот тест: попрекать семью
    из-за сетевого сбоя хуже, чем не напомнить вовсе.
    """
    bot = _Bot(admin=None)

    await _feed(_hello(STRANGER), STRANGER, HUMAN, session_maker, monkeypatch, bot)

    assert bot.sent and texts.ADMIN_WHY not in bot.sent[0]


# --- Добавление в группу здоровается ОДИН раз ------------------------------
#
# Живая поломка 19.09.2026, в первый же день: семья, добавившая бота, получила
# приветствие дважды. На добавление Telegram шлёт ДВА апдейта — `my_chat_member`
# и служебное сообщение «такой-то добавил такого-то», — и второе приходит от
# человека, то есть `ours` его не отсекает. Опоздавшее приветствие принимало
# его за первую реплику семьи.


def _added_service(chat: Chat) -> Message:
    """Служебное «Михаил добавил бота в группу».

    Автор — человек, текста нет, обработчика нет ни одного: в боевом логе этот
    апдейт так и лежит, `is not handled`.
    """
    return Message(
        message_id=5, date=NOW, chat=chat, from_user=HUMAN, new_chat_members=[ME]
    )


@pytest.mark.asyncio
async def test_the_join_service_message_does_not_greet(
    session, session_maker, monkeypatch
):
    """Второй путь существует для потерянного апдейта о добавлении.

    Сообщение, которое об этом же добавлении и рассказывает, — доказательство
    того, что апдейт не потерян.
    """
    bot = _Bot()

    await _feed(_added_service(STRANGER), STRANGER, HUMAN, session_maker, monkeypatch, bot)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_a_group_created_with_the_bot_does_not_greet_twice(
    session, session_maker, monkeypatch
):
    """Группу создали сразу с ботом — служебное сообщение другое, смысл тот же."""
    bot = _Bot()
    created = Message(
        message_id=6, date=NOW, chat=STRANGER, from_user=HUMAN, group_chat_created=True
    )

    await _feed(created, STRANGER, HUMAN, session_maker, monkeypatch, bot)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_the_real_first_message_still_greets(
    session, session_maker, monkeypatch
):
    """Положительный двойник: настоящую реплику второй путь по-прежнему ловит.

    Без него «не поздоровались» доказывало бы ровно ничего — второй путь мог
    бы оказаться выключенным целиком.
    """
    bot = _Bot()

    await _feed(_hello(STRANGER), STRANGER, HUMAN, session_maker, monkeypatch, bot)

    assert bot.sent and bot.sent[0].startswith(texts.GREETING)


@pytest.mark.asyncio
async def test_being_added_greets_exactly_once(session, session_maker, monkeypatch):
    """Та самая боевая последовательность 19.09.2026, целиком и в том же порядке.

    В логе она выглядела так:

        22:04:32,087  Поздоровался в чате -100… (семья #3): ok
        22:04:32,088  Update id=795124609 is not handled
        22:04:32,648  Поздоровался в чате -100… (семья #3): ok
        22:04:32,649  Update id=795124610 is handled

    Первым идёт служебное сообщение о добавлении — обработчика у него нет, и
    поздоровался на него middleware. Вторым `my_chat_member`, и поздоровался
    на него `bot_added`. Семья получила два одинаковых набора сообщений.

    Сторож обязан быть про последовательность, а не про один апдейт: по
    отдельности оба пути ведут себя правильно, и поломка живёт ровно в их
    стыке. Порядок здесь тот же, что в логе, — иначе тест проверял бы более
    лёгкий случай.
    """
    monkeypatch.setattr(middlewares, "Session", session_maker)
    bot = _Bot()

    async def dispatch(event, data):
        """Грубая замена aiogram: `my_chat_member` ведёт в `bot_added`."""
        if isinstance(event, ChatMemberUpdated):
            await common.bot_added(
                event, bot, data["session"], data["family"], data.get("child")
            )
        return None

    joined = ChatMemberUpdated(
        chat=STRANGER,
        from_user=HUMAN,
        date=NOW,
        old_chat_member=ChatMemberLeft(user=ME),
        new_chat_member=ChatMemberMember(user=ME),
    )

    for event in (_added_service(STRANGER), joined):
        await FamilyMiddleware()(
            dispatch,
            event,
            {"event_chat": STRANGER, "event_from_user": HUMAN, "bot": bot},
        )

    greetings = [line for line in bot.sent if line.startswith(texts.GREETING)]
    assert len(greetings) == 1, bot.sent
