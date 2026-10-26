"""Переезд группы в супергруппу: двойник, гонка и вторая дорога.

21.09.2026 бот потерял вторую семью. Группу повысили до супергруппы, и всё,
что было написано про этот случай в проекте, оказалось наполовину неверным:
`_handle_migration` сработал — оба служебных сообщения, и из старого чата, и
из нового, — а упал на `UNIQUE constraint failed: families.chat_id`. Семью с
новым `chat_id` успел завести параллельный апдейт `my_chat_member`: aiogram
обрабатывает апдейты одновременно, и наблюдатель приветствия дошёл первым.

Отсюда два теста, которые здесь главные.

`test_a_race_does_not_split_the_family` идёт **двумя апдейтами через боевой
диспетчер**, и иначе его писать нельзя: поломка жила в шве между двумя
апдейтами, то есть ровно там, куда поштучная проверка не смотрит. Оба апдейта
по отдельности были исправны.

`test_a_moved_chat_is_learned_from_the_error` — про вторую дорогу. Служебное
сообщение о переезде теряется вместе с очередью Telegram, когда ПК уходит в
сон, и тогда единственный, кто знает новый `chat_id`, — это сам Telegram,
отвечающий ошибкой на отправку. Без этой дороги тикер долбил в мёртвый чат
каждую минуту: 43 трейсбека за сорок три минуты до того, как это заметили.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from aiogram.exceptions import TelegramMigrateToChat
from aiogram.methods import SendMessage
from aiogram.types import (
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    Update,
)
from sqlalchemy import select, text

from bot.db import repo
from bot.db.models import Family, Member, Usage
from bot.services import digest, sending
from bot.services import timeutil as tu
from tests.conftest import AUTHOR, ME, NOW, FakeBot

OLD = -1001  # чат семьи из фикстуры `family`
NEW = -1002  # он же после повышения до супергруппы


def _migrated_away(update_id: int) -> Update:
    """Служебное сообщение в СТАРОМ чате: «группа уехала вот туда»."""
    message = Message(
        message_id=update_id,
        date=NOW,
        chat=Chat(id=OLD, type="group", title="Гнездо"),
        from_user=AUTHOR,
        migrate_to_chat_id=NEW,
    )
    return Update(update_id=update_id, message=message)


def _bot_added(update_id: int) -> Update:
    """Апдейт о членстве из НОВОГО чата — тот самый, что обгонял служебное."""
    event = ChatMemberUpdated(
        chat=Chat(id=NEW, type="supergroup", title="Гнездо"),
        from_user=AUTHOR,
        date=NOW,
        old_chat_member=ChatMemberLeft(user=ME, status="left"),
        new_chat_member=ChatMemberMember(user=ME, status="member"),
    )
    return Update(update_id=update_id, my_chat_member=event)


def _migration_error() -> TelegramMigrateToChat:
    """Ровно то, чем Telegram отвечает на отправку в переехавшую группу."""
    return TelegramMigrateToChat(
        method=SendMessage(chat_id=OLD, text="."),
        message="Bad Request: group chat was upgraded to a supergroup chat",
        migrate_to_chat_id=NEW,
    )


@pytest.mark.asyncio
async def test_a_move_takes_the_family_with_it(session, family):
    """Обычный переезд: семья уехала, панель обнулена.

    Панель обнуляется потому, что `message_id` принадлежит конкретному чату:
    в новом старый номер указывает в пустоту, и правка по нему молча не
    находит сообщения.
    """
    family.panel_message_id = 7
    family.panel_day = date(2026, 9, 21)
    await session.commit()

    assert await repo.migrate_family_chat_id(session, OLD, NEW)
    assert family.chat_id == NEW
    assert family.panel_message_id is None
    assert family.panel_day is None

    # Второе служебное сообщение о том же переезде: делать уже нечего
    assert not await repo.migrate_family_chat_id(session, OLD, NEW)


@pytest.mark.asyncio
async def test_an_empty_twin_is_folded_with_its_people_and_taps(
    session, family, mother, child
):
    """Двойник — это чат, а не семья.

    Что описывает чат, у него и берётся: в новом чате двойник уже поздоровался
    и уже выпустил панель, и обнулить её значило бы оставить в чате живую
    кнопку, которую бот больше не правит.

    Участники и нажатия переезжают, а не удаляются. Каскадов в схеме нет
    нигде, и оставленная строка `usage` уронила бы удаление двойника висячим
    ключом — ровно та поломка, из-за которой `usage` чистится первой в
    `tools/wipe.py`.
    """
    twin = await repo.get_or_create_family(session, NEW, "Гнездо")
    twin.panel_message_id = 5
    twin.panel_day = date(2026, 9, 21)
    twin.last_digest_on = date(2026, 9, 21)
    # Тот же человек, что и `mother`, и ещё один, которого старая семья не знает
    same = await repo.get_or_create_member(session, twin.id, 222, "Аня")
    other = await repo.get_or_create_member(session, twin.id, 333, "Антон")
    for member_id in (same.id, other.id):
        session.add(
            Usage(
                at=NOW,
                family_id=twin.id,
                member_id=member_id,
                kind="button",
                action="pnl:refresh",
                handled=True,
                ms=12,
            )
        )
    await session.commit()

    assert await repo.migrate_family_chat_id(session, OLD, NEW)

    families = list((await session.scalars(select(Family))).all())
    assert [fam.id for fam in families] == [family.id], "двойник остался в базе"
    assert family.chat_id == NEW
    assert family.panel_message_id == 5, "панель нового чата потеряна"
    assert family.last_digest_on == date(2026, 9, 21), "дайджест уйдёт дважды за день"
    assert await repo.active_child(session, family.id) is not None

    members = list((await session.scalars(select(Member))).all())
    assert {member.tg_user_id for member in members} == {222, 333}
    assert {member.family_id for member in members} == {family.id}

    rows = list((await session.scalars(select(Usage))).all())
    assert {row.family_id for row in rows} == {family.id}
    assert {row.member_id for row in rows} == {member.id for member in members}
    broken = (await session.execute(text("PRAGMA foreign_key_check"))).fetchall()
    assert broken == [], f"висячие ключи: {broken}"


@pytest.mark.asyncio
async def test_a_twin_with_a_child_is_not_folded(session, family, child, born):
    """Две семьи с данными на один чат автоматика разводить не должна.

    Это уже не гонка: пока семья жила под старым `chat_id`, в новом чате
    успели завести карточку. Чей ребёнок настоящий, из базы не видно, и
    догадка здесь стоила бы дневника. Человек узнаёт об этом из лога — а не из
    пропавших записей.
    """
    twin = await repo.get_or_create_family(session, NEW, "Гнездо")
    await repo.create_child(
        session, twin.id, name="Лев", sex="m", born_at=born, birth_weight_g=3680
    )

    assert not await repo.migrate_family_chat_id(session, OLD, NEW)
    assert family.chat_id == OLD, "семья уехала на занятый чат"

    families = list((await session.scalars(select(Family))).all())
    assert len(families) == 2, "двойник с ребёнком обязан остаться целым"


@pytest.mark.asyncio
async def test_a_race_does_not_split_the_family(wired, session_maker, family, child):
    """Главный тест файла: апдейт о членстве обгоняет служебное сообщение.

    Двумя апдейтами и через боевой диспетчер — иначе проверялась бы не та
    поломка. Поштучно оба апдейта были исправны и в тот день: приветствие
    поздоровалось, служебное сообщение позвало переезд. Развалилось всё в шве
    между ними, где до этого теста не смотрел никто.

    Читаем своей сессией: middleware открывает собственную, и семья, лежащая в
    тестовой с прежним `chat_id`, ничего не докажет.
    """
    dp, bot, talk = wired

    await dp.feed_update(bot, _bot_added(1))
    await dp.feed_update(bot, _migrated_away(2))

    async with session_maker() as own:
        families = list((await own.scalars(select(Family))).all())
        assert len(families) == 1, "на один чат завелись две семьи"
        moved = families[0]
        assert moved.id == family.id, "выжил двойник, а с ним потерян дневник"
        assert moved.chat_id == NEW
        assert await repo.active_child(own, moved.id) is not None

        rows = list((await own.scalars(select(Usage))).all())
        assert rows, "ни одного нажатия не записалось — тест проверял бы пустоту"
        assert {row.family_id for row in rows} == {family.id}


@pytest.mark.asyncio
async def test_a_moved_chat_is_learned_from_the_error(session, family):
    """Вторая дорога: новый `chat_id` приезжает ошибкой отправки.

    Первая дорога — служебное сообщение — теряется вместе с очередью Telegram
    на каждом сне ПК: `bot/__main__.py` зовёт `delete_webhook
    (drop_pending_updates=True)`. У фичи, доезжающей одним необязательным
    путём, нет ни одного пути.
    """
    bot = FakeBot(fail_on={0: _migration_error()})

    status, _ = await sending.send(bot, family, "текст", session=session)

    assert status == sending.OK
    assert family.chat_id == NEW
    assert [chat for chat, _ in bot.sent] == [OLD, NEW], "сообщение не доехало"


@pytest.mark.asyncio
async def test_without_a_session_the_move_is_only_named(session, family):
    """Без сессии чинить переезд некому, и повторять отправку незачем.

    `MOVED` обязан быть терминальным у всех вызывающих: повтор в мёртвый чат
    даст ту же ошибку через минуту, и так до полуночи.
    """
    bot = FakeBot(fail_on={0: _migration_error()})

    status, _ = await sending.send(bot, family, "текст")

    assert status == sending.MOVED
    assert len(bot.sent) == 1, "отправка повторилась в мёртвый чат"
    assert family.chat_id == OLD


@pytest.mark.asyncio
async def test_an_edit_after_the_move_asks_for_a_new_panel(session, family):
    """Правка переехавшего чата — это `NOT_FOUND`, а не сбой.

    Правили мы сообщение старого чата, и в новом его нет: нужна новая панель,
    а это уже написанная ветка `panel._redraw` → `_publish`.
    """
    bot = FakeBot(fail_on_edit={0: _migration_error()})

    status = await sending.edit(bot, family, 7, "текст", session=session)

    assert status == sending.NOT_FOUND
    assert family.chat_id == NEW


@pytest.mark.asyncio
async def test_the_digest_stops_hammering_a_moved_chat(session, family, child, mother):
    """Поведенческий сторож той самой боевой поломки.

    `TelegramMigrateToChat` проваливался в голый `except Exception` и давал
    `RETRY` — то есть «сеть моргнула, попробуем на следующем тике». Отметка
    `last_digest_on` при этом не ставилась никогда, и дайджест уходил в мёртвый
    чат каждую минуту.
    """
    moment = datetime(2026, 9, 21, 9, 0)  # понедельник, полдень по Москве
    family.digest_dow = tu.local_today(family.tz, moment).weekday()
    family.digest_time = "10:00"
    await session.commit()

    bot = FakeBot(fail_on={0: _migration_error()})
    await digest.send_pending(bot, session, moment)
    after_first = len(bot.sent)

    await digest.send_pending(bot, session, moment)

    assert after_first == len(bot.sent), "дайджест ушёл во второй раз"
    chats = [chat for chat, _ in bot.sent]
    assert chats.count(OLD) == 1, "в мёртвый чат писали больше одного раза"
    assert family.chat_id == NEW
    assert family.last_digest_on == tu.local_today(family.tz, moment)
