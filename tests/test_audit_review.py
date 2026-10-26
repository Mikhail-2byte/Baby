"""Дополнительный аудит: желаемые гарантии, которых не хватало прежним тестам.

Это воспроизведения найденных дефектов, а не изменения поведения бота.
"""

import ast
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage

from bot.db import repo
from bot.db.models import FEED_BREAST, LEFT, SLEEP
from bot.handlers import voice
from bot.keyboards import TimeCB
from bot.services import dictation, time_input
from bot.services import timeutil as tu
from tests.test_record_edit import _feed, _tap
from tests.test_record_edit import wired as wired

NOW = datetime(2026, 10, 26, 8, 40)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(tu, "now_utc", lambda: NOW)


async def current_event(session, child, mother):
    return await repo.start_event(
        session,
        child.id,
        mother.id,
        FEED_BREAST,
        started_at=NOW - timedelta(minutes=40),
        side=LEFT,
    )


@pytest.mark.parametrize("prefix", ["закончили кормить", "закончили кормление"])
@pytest.mark.asyncio
async def test_full_feeding_phrase_really_closes_the_event(wired, session, child, mother, prefix):
    dp, bot, talk, _ = wired
    row = await current_event(session, child, mother)
    await _feed(dp, bot, prefix + " в 11:20")
    await session.refresh(row)
    assert row.ended_at == NOW - timedelta(minutes=20), talk.sent


@pytest.mark.asyncio
async def test_ordinary_finished_sentence_does_not_invoke_the_tracker(
    wired, session, child, mother
):
    dp, bot, talk, _ = wired
    row = await current_event(session, child, mother)
    await _feed(dp, bot, "закончила готовить обед")
    await session.refresh(row)
    assert row.ended_at is None
    assert talk.sent == [], "Обычная семейная переписка не должна вызывать анкету времени"


@pytest.mark.asyncio
async def test_panel_is_scheduled_even_if_callback_acknowledgement_fails(
    wired, session, child, mother, family, monkeypatch
):
    dp, bot, talk, drawn = wired
    row = await current_event(session, child, mother)
    original = talk.make_request

    async def fail_ack(bot_, method, timeout=None):
        if isinstance(method, AnswerCallbackQuery):
            raise TelegramBadRequest(method=method, message="query is too old")
        return await original(bot_, method, timeout=timeout)

    monkeypatch.setattr(talk, "make_request", fail_ack)
    with suppress(TelegramAPIError):
        await _tap(dp, bot, TimeCB(action="stop", row_id=row.id))
    await session.refresh(row)
    assert row.ended_at == NOW
    assert drawn == [family.id], "Запись сохранена, но перерисовка панели потеряна"


@pytest.mark.asyncio
async def test_panel_is_scheduled_even_if_text_receipt_fails(
    wired, session, child, mother, family, monkeypatch
):
    dp, bot, talk, drawn = wired
    row = await current_event(session, child, mother)
    original = talk.make_request

    async def fail_receipt(bot_, method, timeout=None):
        if isinstance(method, SendMessage):
            raise TelegramBadRequest(method=method, message="message delivery failed")
        return await original(bot_, method, timeout=timeout)

    monkeypatch.setattr(talk, "make_request", fail_receipt)
    with suppress(TelegramAPIError):
        await _feed(dp, bot, "закончили в 11:20")
    await session.refresh(row)
    assert row.ended_at == NOW - timedelta(minutes=20)
    assert drawn == [family.id], "После сохранения отправка подтверждения не должна терять панель"


@pytest.mark.asyncio
async def test_replacement_card_becomes_the_current_card(
    wired, session, child, mother, monkeypatch
):
    dp, bot, talk, _ = wired
    row = await current_event(session, child, mother)
    row.card_message_id = 900
    await session.commit()

    async def missing_card(*args, **kwargs):
        raise TelegramBadRequest(method=None, message="message to edit not found")

    monkeypatch.setattr(bot, "edit_message_text", missing_card)
    await _tap(dp, bot, TimeCB(action="stop", row_id=row.id))
    await session.refresh(row)
    assert len(talk.sent) == 1
    assert row.card_message_id == 999, "Новая карточка должна заменить ссылку на удалённую"


@pytest.mark.asyncio
async def test_backdated_voice_cannot_close_a_timer_before_its_start(
    session, child, mother, family
):
    row = await current_event(session, child, mother)
    old_local = tu.to_local(NOW - timedelta(hours=2), family.tz)
    draft = voice.Draft(
        family_id=family.id,
        records=(dictation.Record(kind=SLEEP, at=old_local),),
        source_message_id=991,
        now_local=tu.to_local(NOW, family.tz),
        heard="уснула два часа назад",
    )
    with pytest.raises(time_input.TimeInputError):
        await voice._save(session, family, mother, child, draft)
    await session.refresh(row)
    assert row.ended_at is None or row.ended_at >= row.started_at, (
        "Голосовая запись задним числом создала отрицательный интервал",
        row.started_at,
        row.ended_at,
    )


def test_startup_does_not_delete_unprocessed_messages():
    source = Path("bot/__main__.py").read_text()
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "delete_webhook"
    ]
    assert calls
    drops = [
        kw.value.value
        for node in calls
        for kw in node.keywords
        if kw.arg == "drop_pending_updates" and isinstance(kw.value, ast.Constant)
    ]
    assert True not in drops, "При перезапуске не должны удаляться непрочитанные сообщения"


@pytest.mark.parametrize("text", ["закончили в 11:20", "проснулась в 11:20"])
def test_short_commands_keep_the_right_clock(text):
    assert time_input.finish_request(text)[1] == "11:20"
