"""Контракт нового простого режима: отмена, напоминания, повторная доставка."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Message, Update
from sqlalchemy import func, select

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import FEED_BREAST, LEFT, SLEEP, Event, ProcessedMessage, RecordRevision
from bot.keyboards import HistoryCB, NudgeCB, TrackCB
from bot.services import replay, revisions, stats, ticker
from bot.services import timeutil as tu
from tests.test_record_edit import AUTHOR, CHAT, _feed, _tap
from tests.test_record_edit import wired as wired

NOW = datetime(2026, 10, 26, 8, 40)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(tu, "now_utc", lambda: NOW)


async def feeding(session, child, mother):
    return await repo.start_event(
        session, child.id, mother.id, FEED_BREAST,
        started_at=NOW - timedelta(minutes=40), side=LEFT,
    )


@pytest.mark.asyncio
async def test_legacy_side_button_starts_generic_feeding(wired, session, child):
    dp, bot, _, _ = wired
    await _tap(dp, bot, TrackCB(action="start", kind=FEED_BREAST, side="R"))
    event = await repo.any_open_event(session, child.id)
    assert event is not None and event.side is None


@pytest.mark.asyncio
async def test_end_edit_has_actor_and_can_be_undone(wired, session, child, mother):
    dp, bot, talk, _ = wired
    event = await feeding(session, child, mother)
    await _feed(dp, bot, "закончили в 11:20")
    change = await session.scalar(select(RecordRevision))
    assert change.actor_id == mother.id
    assert event.ended_at == NOW - timedelta(minutes=20)
    await _tap(dp, bot, HistoryCB(action="list", row_id=event.id))
    assert "Аня" in talk.sent[-1] and "11:20" in talk.sent[-1]
    await _tap(dp, bot, HistoryCB(action="undo", row_id=event.id))
    await session.refresh(event)
    assert event.ended_at is None
    assert event.side == LEFT, "Старые данные не должны теряться"
    await session.refresh(change)
    assert change.undone_at == NOW and change.undo_actor_id == mother.id
    await _tap(dp, bot, HistoryCB(action="undo", row_id=event.id))
    assert "Нет правки" in talk.alerts[-1]


@pytest.mark.asyncio
async def test_undo_does_not_open_two_timers(wired, session, child, mother):
    dp, bot, talk, _ = wired
    event = await feeding(session, child, mother)
    await _feed(dp, bot, "закончили в 11:20")
    other = await repo.start_event(session, child.id, mother.id, SLEEP, started_at=NOW)
    await _tap(dp, bot, HistoryCB(action="undo", row_id=event.id))
    await session.refresh(event)
    assert event.ended_at is not None
    assert "Уже идёт другой" in talk.alerts[-1]
    assert (await repo.any_open_event(session, child.id)).id == other.id


@pytest.mark.asyncio
async def test_undo_refuses_unlogged_concurrent_change(wired, session, child, mother):
    dp, bot, talk, _ = wired
    event = await feeding(session, child, mother)
    await _feed(dp, bot, "закончили в 11:20")
    event.ended_at = NOW - timedelta(minutes=10)
    await session.commit()
    await _tap(dp, bot, HistoryCB(action="undo", row_id=event.id))
    assert event.ended_at == NOW - timedelta(minutes=10)
    assert "уже изменилась" in talk.alerts[-1]


@pytest.mark.asyncio
async def test_reminder_choice_changes_only_current_family(wired, session, family):
    dp, bot, _, _ = wired
    other = await repo.get_or_create_family(session, -3333, "Другой чат")
    await _tap(dp, bot, NudgeCB(kind="feed", minutes=45))
    await session.refresh(family)
    await session.refresh(other)
    assert family.feed_nudge_minutes == 45
    assert other.feed_nudge_minutes == 0
    await _tap(dp, bot, NudgeCB(kind="feed", minutes=44))
    assert family.feed_nudge_minutes == 45


@pytest.mark.asyncio
async def test_disabled_nudge_does_not_close_or_send(session, family, child, mother, bot):
    event = await feeding(session, child, mother)
    await ticker.close_stale_timers(bot, session, NOW + timedelta(days=1))
    await session.refresh(event)
    assert event.ended_at is None and event.nudged_at is None
    assert bot.sent == []


@pytest.mark.asyncio
async def test_quiet_summary_is_not_zero_diapers(session, family, child):
    span = await stats.collect(session, child, NOW - timedelta(days=1), NOW)
    assert not span.diapers_tracked
    result = texts.day_summary_from_span(child, span, "за сутки")
    assert "учёт не ведётся" in result
    assert "0 мокрых" not in result


@pytest.mark.asyncio
async def test_quiet_mode_never_raises_diaper_count_alarm(session, family, child, mother, bot):
    from bot.db.models import DIAPER_WET
    await repo.add_event(session, child.id, mother.id, DIAPER_WET, started_at=NOW)
    await ticker.watch_tracker(bot, session, NOW)
    assert not any("подгузник" in text.lower() for text in bot.texts)


@pytest.mark.asyncio
async def test_repeated_text_message_does_not_create_second_interval(wired, session, child):
    dp, bot, _, _ = wired
    message = Message(message_id=873, date=NOW, chat=CHAT, from_user=AUTHOR,
                      text="кормили с 11:00 до 11:20")
    update = Update(update_id=873, message=message)
    await dp.feed_update(bot, update)
    await dp.feed_update(bot, update)
    assert await session.scalar(select(func.count()).select_from(Event)) == 1
    assert await session.get(ProcessedMessage, (CHAT.id, 873)) is not None


@pytest.mark.asyncio
async def test_delayed_relative_finish_uses_message_time(wired, session, child, mother):
    dp, bot, _, _ = wired
    event = await feeding(session, child, mother)
    message = Message(message_id=874, date=NOW - timedelta(minutes=10), chat=CHAT,
                      from_user=AUTHOR, text="закончили 5 минут назад")
    await dp.feed_update(bot, Update(update_id=874, message=message), replayed=True)
    await session.refresh(event)
    assert event.ended_at == NOW - timedelta(minutes=15)


@pytest.mark.asyncio
async def test_deleted_record_id_is_not_reused(session, child, mother):
    event = await feeding(session, child, mother)
    old_id = event.id
    await repo.delete_event(session, event)
    new = await feeding(session, child, mother)
    assert new.id > old_id


@pytest.mark.asyncio
async def test_revision_actor_context_does_not_leak(session, child, mother):
    event = await feeding(session, child, mother)
    with revisions.actor(mother.id):
        await repo.close_event(session, event, NOW)
    await repo.reopen_event(session, event)
    rows = list(await session.scalars(select(RecordRevision).order_by(RecordRevision.id)))
    assert [row.actor_id for row in rows] == [mother.id, None]


def callback_update(update_id):
    return Update(update_id=update_id, callback_query=CallbackQuery(
        id=str(update_id), from_user=AUTHOR, chat_instance="test", data="trk:start:feed_breast:",
        message=Message(message_id=1, date=NOW, chat=CHAT, from_user=AUTHOR),
    ))


@pytest.mark.asyncio
async def test_startup_queue_skips_old_buttons_and_commands_but_keeps_text():
    message = Update(update_id=103, message=Message(
        message_id=3, date=NOW, chat=CHAT, from_user=AUTHOR, text="закончили в 11:20"
    ))
    command = Update(update_id=102, message=Message(
        message_id=2, date=NOW, chat=CHAT, from_user=AUTHOR, text="/weight 3500"
    ))
    bot = SimpleNamespace(get_updates=AsyncMock(side_effect=[
        [callback_update(101), command, message], []
    ]), send_message=AsyncMock())
    dp = SimpleNamespace(resolve_used_update_types=lambda: ["message", "callback_query"],
                         feed_update=AsyncMock())
    await replay.drain(bot, dp)
    dp.feed_update.assert_awaited_once_with(bot, message, replayed=True)
    assert bot.get_updates.await_args_list[1].kwargs["offset"] == 104
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_does_not_ack_batch_after_processing_failure():
    message = Update(update_id=100, message=Message(
        message_id=1, date=NOW, chat=CHAT, from_user=AUTHOR, text="закончили в 11:20"
    ))
    bot = SimpleNamespace(get_updates=AsyncMock(return_value=[message]))
    dp = SimpleNamespace(resolve_used_update_types=lambda: ["message"],
                         feed_update=AsyncMock(side_effect=RuntimeError("failure")))
    with pytest.raises(RuntimeError):
        await replay.drain(bot, dp)
    assert bot.get_updates.await_count == 1, "Следующий offset не должен подтверждать пачку"


def test_main_panel_does_not_offer_removed_actions():
    values = [button.text for row in keyboards.panel(running_kind=None).inline_keyboard
              for button in row]
    assert values == ["🤱 Кормление", "😴 Сон", "⋯ Ещё"]


@pytest.mark.asyncio
async def test_measurement_edit_can_be_undone(wired, session, child, mother):
    from bot.db.models import WEIGHT
    dp, bot, _, _ = wired
    reading = await repo.add_measurement(
        session, child.id, mother.id, WEIGHT, value=4300, taken_at=NOW - timedelta(hours=1)
    )
    with revisions.actor(mother.id):
        await repo.update_measurement(session, reading, value=4400)
    await _tap(dp, bot, HistoryCB(action="undo", row_id=reading.id, what="m"))
    await session.refresh(reading)
    assert reading.value == 4300


@pytest.mark.asyncio
async def test_kind_change_invalidates_outstanding_time_prompt(wired, session, child, mother):
    from bot.keyboards import TimeCB
    dp, bot, talk, _ = wired
    event = await feeding(session, child, mother)
    await _tap(dp, bot, TimeCB(action="ask", row_id=event.id))
    await repo.set_event_kind(session, event, kind=SLEEP, side=None, value_num=None)
    await _feed(dp, bot, "11:20")
    await session.refresh(event)
    assert event.ended_at is None and event.kind == SLEEP
    assert "уже изменена" in talk.sent[-1]


@pytest.mark.asyncio
async def test_voice_batch_failure_rolls_back_every_record(
    session, family, child, mother, monkeypatch
):
    from bot.handlers import voice
    from bot.services import dictation
    event = await feeding(session, child, mother)
    local = tu.to_local(NOW - timedelta(minutes=10), family.tz)
    draft = voice.Draft(
        family_id=family.id, records=(dictation.Record(kind=SLEEP, at=local),
                                     dictation.Record(kind="note", at=local, note="тест")),
        source_message_id=5000, now_local=tu.to_local(NOW, family.tz), heard="тест",
    )
    original = repo.add_event

    async def fail_add(*args, **kwargs):
        raise RuntimeError("DB failure on second record")

    monkeypatch.setattr(repo, "add_event", fail_add)
    with pytest.raises(RuntimeError):
        await voice._save(session, family, mother, child, draft)
    monkeypatch.setattr(repo, "add_event", original)
    await session.refresh(event)
    assert event.ended_at is None
    assert await session.scalar(select(func.count()).select_from(Event)) == 1
    assert await session.scalar(select(func.count()).select_from(RecordRevision)) == 0
