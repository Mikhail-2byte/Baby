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

from bot import middlewares
from bot.db.models import Family
from bot.middlewares import FamilyMiddleware

NOW = datetime(2026, 9, 16, 8, 0)
STRANGER = Chat(id=-5437929633, type="group", title="Стенд")
HUMAN = User(id=1778570470, is_bot=False, first_name="Михаил")
ME = User(id=8992988295, is_bot=True, first_name="Бот")


class _Bot:
    """Заглушка: middleware нужен от бота ровно один метод."""

    async def me(self) -> User:
        return ME


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


async def _feed(event, chat, user, session_maker, monkeypatch) -> dict:
    monkeypatch.setattr(middlewares, "Session", session_maker)
    seen: dict = {}

    async def handler(_event, data):
        seen.update(data)
        return "ok"

    await FamilyMiddleware()(
        handler,
        event,
        {"event_chat": chat, "event_from_user": user, "bot": _Bot()},
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
    """В личке семьи нет — там только вежливый отказ."""
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
