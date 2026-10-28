"""Одна карточка управления: реальные хендлеры, SQLite и сбои Telegram."""

import asyncio
from datetime import datetime, timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import FEED_BREAST, LEFT, RIGHT, SLEEP, Event
from bot.keyboards import MeasureCB, PanelCB, RecordCB, TimeCB, TimerCB, TrackCB
from bot.services import panel, revisions, time_input
from bot.services import timeutil as tu
from tests.test_record_edit import _feed, _tap
from tests.test_record_edit import wired as wired

NOW = datetime(2026, 10, 28, 9, 0)


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr(tu, "now_utc", lambda: NOW)


async def feeding(session, child, mother, *, kind=FEED_BREAST):
    return await repo.start_event(
        session, child.id, mother.id, kind, started_at=NOW - timedelta(minutes=10),
        side=LEFT if kind == FEED_BREAST else None,
    )


def switch(row, *, kind=FEED_BREAST, side=RIGHT, action="switch"):
    return TimerCB(action=action, row_id=row.id, kind=kind, side=side,
                   stamp=revisions.timer_stamp(row))


@pytest.mark.asyncio
async def test_bound_side_switch_is_atomic_and_repeat_is_harmless(
    wired, session, child, mother,
):
    dp, bot, talk, _ = wired
    old = await feeding(session, child, mother)
    data = switch(old)
    await asyncio.gather(_tap(dp, bot, data), _tap(dp, bot, data))
    rows = list(await session.scalars(select(Event).order_by(Event.id)))
    assert len(rows) == 2
    assert rows[0].side == LEFT and rows[0].ended_at == NOW
    assert rows[1].side == RIGHT and rows[1].started_at == NOW
    assert rows[1].ended_at is None
    assert talk.sent == [], "Нет отдельного подтверждения начала или смены"


@pytest.mark.asyncio
async def test_transition_requires_confirmation_and_stale_confirmation_cannot_close_new_timer(
    wired, session, child, mother,
):
    dp, bot, talk, _ = wired
    old = await feeding(session, child, mother)
    await _tap(dp, bot, switch(old, kind=SLEEP, side=""))
    assert old.ended_at is None
    confirm = TimerCB.unpack(talk.markups[-1].inline_keyboard[0][0].callback_data)
    assert confirm.action == "confirm"
    await _tap(dp, bot, confirm)
    current = await repo.any_open_event(session, child.id)
    assert current.kind == SLEEP and current.started_at == old.ended_at == NOW
    await _tap(dp, bot, confirm)
    assert (await repo.any_open_event(session, child.id)).id == current.id
    assert "изменился" in talk.alerts[-1]


@pytest.mark.asyncio
async def test_transition_cannot_overwrite_a_parent_time_edit(wired, session, child, mother):
    dp, bot, talk, _ = wired
    row = await feeding(session, child, mother)
    data = switch(row, kind=SLEEP, side="", action="confirm")
    await repo.set_event_time(session, row, started_at=row.started_at - timedelta(minutes=2),
                              ended_at=None, estimated=False)
    await _tap(dp, bot, data)
    assert row.ended_at is None
    assert (await repo.any_open_event(session, child.id)).id == row.id
    assert "изменился" in talk.alerts[-1]


@pytest.mark.asyncio
async def test_cancel_transition_does_not_change_event(wired, session, child, mother):
    dp, bot, talk, _ = wired
    row = await feeding(session, child, mother)
    await _tap(dp, bot, TimerCB(action="cancel", row_id=row.id,
                               stamp=revisions.timer_stamp(row)))
    assert row.ended_at is None
    assert "продолжается" in talk.edited[-1]


@pytest.mark.asyncio
async def test_other_family_timer_cannot_be_switched(wired, session, family, child, mother, born):
    dp, bot, talk, _ = wired
    other = await repo.get_or_create_family(session, -3333, "Чужой чат")
    baby = await repo.create_child(session, other.id, name="Другой", sex="m", born_at=born,
                                   birth_weight_g=3000)
    row = await feeding(session, baby, mother)
    await _tap(dp, bot, switch(row))
    assert row.ended_at is None
    assert await repo.any_open_event(session, child.id) is None
    assert "изменился" in talk.alerts[-1]


@pytest.mark.asyncio
async def test_old_start_of_other_side_does_not_toggle_current_feeding(
    wired, session, child, mother,
):
    dp, bot, talk, _ = wired
    row = await feeding(session, child, mother)
    await _tap(dp, bot, TrackCB(action="start", kind=FEED_BREAST, side=RIGHT))
    assert row.ended_at is None and row.side == LEFT
    assert "актуальной карточке" in talk.alerts[-1]


@pytest.mark.asyncio
async def test_text_interval_preserves_side_and_unknown_remains_unknown(wired, session, child):
    dp, bot, _, _ = wired
    await _feed(dp, bot, "кормили правой с 10:00 до 10:20")
    await _feed(dp, bot, "кормили с 11:00 до 11:20")
    rows = list(await session.scalars(select(Event).order_by(Event.id)))
    assert [row.side for row in rows] == [RIGHT, None]


@pytest.mark.asyncio
async def test_navigation_does_not_stop_feeding(wired, session, child, mother):
    dp, bot, _, _ = wired
    row = await feeding(session, child, mother)
    await _tap(dp, bot, TimerCB(action="menu", row_id=row.id,
                               stamp=revisions.timer_stamp(row)))
    await _tap(dp, bot, PanelCB(action="more"))
    await _tap(dp, bot, MeasureCB(kind="weight"))
    assert row.ended_at is None


@pytest.mark.asyncio
async def test_invalid_side_edit_does_not_corrupt_record(wired, session, child, mother):
    dp, bot, talk, _ = wired
    row = await feeding(session, child, mother)
    await _tap(dp, bot, RecordCB(action="setkind", row_id=row.id,
                                 kind=FEED_BREAST, side="X"))
    assert row.side == LEFT and row.ended_at is None
    assert texts.RECORD_WRONG_KIND in talk.alerts[-1]


@pytest.mark.asyncio
async def test_failed_side_switch_rolls_back_the_old_timer(
    wired, session, child, mother, monkeypatch,
):
    dp, bot, _, _ = wired
    row = await feeding(session, child, mother)
    data = switch(row)

    async def failed_insert(*args, **kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(repo, "start_event", failed_insert)
    with pytest.raises(RuntimeError, match="write failed"):
        await _tap(dp, bot, data)
    await session.refresh(row)
    assert row.ended_at is None and row.side == LEFT


@pytest.mark.asyncio
async def test_start_time_edit_acknowledges_in_prompt_without_duplicate_controls(
    wired, session, child, mother,
):
    dp, bot, talk, _ = wired
    row = await feeding(session, child, mother)
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id, field="s"))
    await _feed(dp, bot, "11:45")
    assert row.started_at == NOW - timedelta(minutes=15)
    assert row.ended_at is None
    assert len(talk.sent) == 1, "Только исходный запрос времени, без новой карточки таймера"
    assert "Время начала сохранено" in talk.edited[-1]
    assert talk.markups[-1] is None


@pytest.mark.asyncio
async def test_start_reuses_panel_and_links_the_event(
    session_maker, session, family, child, mother, bot, monkeypatch,
):
    monkeypatch.setattr(panel, "Session", session_maker)
    await panel.refresh(bot, family.id)
    row = await feeding(session, child, mother)
    await panel.refresh(bot, family.id)
    await session.refresh(row)
    await session.refresh(family)
    assert len(bot.sent) == 1
    assert row.card_message_id == family.panel_message_id == 101
    markup = bot.edit_kwargs[-1]["reply_markup"]
    stop_buttons = [b for line in markup.inline_keyboard for b in line
                    if b.callback_data.startswith("tm:stop:")]
    assert len(stop_buttons) == 1
    assert "Левая грудь" in bot.edited[-1][2]


@pytest.mark.asyncio
async def test_return_retires_old_controls_and_moves_both_pointers(
    session_maker, session, family, child, mother, bot, monkeypatch,
):
    monkeypatch.setattr(panel, "Session", session_maker)
    row = await feeding(session, child, mother)
    await panel.refresh(bot, family.id)
    await panel.refresh(bot, family.id, force_new=True)
    await session.refresh(row)
    await session.refresh(family)
    assert row.card_message_id == family.panel_message_id == 102
    assert bot.edited[0][1] == 101 and bot.edit_kwargs[0]["reply_markup"] is None
    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_publish_duplicate_controls(
    session_maker, session, family, child, mother, bot, monkeypatch,
):
    monkeypatch.setattr(panel, "Session", session_maker)
    row = await feeding(session, child, mother)
    await panel.refresh(bot, family.id)
    bot._fail_on_edit[0] = RuntimeError("temporary network failure")
    await panel.refresh(bot, family.id, force_new=True)
    await session.refresh(row)
    assert row.card_message_id == 101 and len(bot.sent) == 1
    bot._fail_on_edit.clear()
    await panel.refresh(bot, family.id, force_new=True)
    await session.refresh(row)
    assert row.card_message_id == 102


@pytest.mark.asyncio
async def test_minute_refresh_edits_without_publishing_or_repinning(
    session_maker, session, family, child, mother, bot, monkeypatch,
):
    monkeypatch.setattr(panel, "Session", session_maker)
    await feeding(session, child, mother)
    await panel.refresh(bot, family.id, now=NOW)
    await panel.refresh(bot, family.id, now=NOW + timedelta(minutes=1))
    assert len(bot.sent) == 1 and bot.pinned == [101]
    assert "11 мин" in bot.edited[-1][2]
    scheduled = []
    monkeypatch.setattr(panel, "schedule", lambda *args: scheduled.append(args[1]))
    await panel.refresh_stale(bot, session, NOW)
    assert scheduled == [family.id]


@pytest.mark.asyncio
async def test_completion_and_side_switch_keep_receipt_and_single_active_card(
    session_maker, session, family, child, mother, bot, monkeypatch,
):
    monkeypatch.setattr(panel, "Session", session_maker)
    first = await feeding(session, child, mother)
    await panel.refresh(bot, family.id)
    async with repo.atomic(session):
        await repo.close_event(session, first, NOW)
        second = await repo.start_event(session, child.id, mother.id, FEED_BREAST,
                                         started_at=NOW, side=RIGHT)
    await panel.refresh(bot, family.id)
    await session.refresh(second)
    assert second.card_message_id == 102
    assert bot.edited[-1][1] == 101
    assert bot.edit_kwargs[-1]["reply_markup"] == keyboards.stopped(first.id)
    assert "Правая грудь" in bot.sent[-1][1]
    await repo.close_event(session, second, NOW + timedelta(minutes=5))
    await panel.refresh(bot, family.id)
    assert "Последнее кормление: правая грудь" in bot.sent[-1][1]
    assert bot.edit_kwargs[-1]["reply_markup"] == keyboards.stopped(second.id)


@pytest.mark.asyncio
async def test_failed_completion_render_is_recovered_from_persisted_pointer(
    session_maker, session, family, child, mother, bot, monkeypatch,
):
    monkeypatch.setattr(panel, "Session", session_maker)
    row = await feeding(session, child, mother)
    await panel.refresh(bot, family.id)
    await repo.close_event(session, row, NOW)
    bot._fail_on_edit[0] = RuntimeError("temporary Telegram failure")
    await panel.refresh(bot, family.id)
    await session.refresh(family)
    assert family.panel_message_id == 101 and len(bot.sent) == 1
    scheduled = []
    monkeypatch.setattr(panel, "schedule", lambda *args: scheduled.append(args[1]))
    await panel.refresh_stale(bot, session, NOW)
    assert scheduled == [family.id]
    bot._fail_on_edit.clear()
    await panel.refresh(bot, family.id)
    assert bot.edit_kwargs[-1]["reply_markup"] == keyboards.stopped(row.id)
    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_legacy_duplicate_start_card_is_retired(
    session_maker, session, family, child, mother, bot, monkeypatch,
):
    monkeypatch.setattr(panel, "Session", session_maker)
    await panel.refresh(bot, family.id)
    row = await feeding(session, child, mother)
    row.card_message_id = 88
    await session.commit()
    await panel.refresh(bot, family.id)
    await session.refresh(row)
    assert row.card_message_id == 101
    assert bot.edited[0][1] == 88 and bot.edit_kwargs[0]["reply_markup"] is None


@pytest.mark.asyncio
async def test_deleted_active_card_is_recovered_without_losing_timer(
    session_maker, session, family, child, mother, bot, monkeypatch,
):
    monkeypatch.setattr(panel, "Session", session_maker)
    row = await feeding(session, child, mother)
    await panel.refresh(bot, family.id)
    bot._fail_on_edit[0] = TelegramBadRequest(method=None, message="message to edit not found")
    await panel.refresh(bot, family.id)
    await session.refresh(row)
    assert row.card_message_id == 102 and row.ended_at is None


@pytest.mark.asyncio
async def test_stored_panel_is_reused_after_restart(
    session_maker, session, family, child, mother, bot, monkeypatch,
):
    monkeypatch.setattr(panel, "Session", session_maker)
    row = await feeding(session, child, mother)
    await panel.refresh(bot, family.id)
    await panel.shutdown()
    await panel.refresh(bot, family.id)
    await session.refresh(row)
    assert row.card_message_id == 101 and len(bot.sent) == 1


def test_compact_card_shows_local_date_for_overnight_timer():
    row = Event(id=1, kind=FEED_BREAST, side=LEFT, started_at=NOW - timedelta(days=1))
    assert "27.10 12:00" in texts.timer_card(row, "Europe/Moscow", NOW)


def test_timer_callbacks_fit_telegram_limit():
    row = Event(id=2**63 - 1, kind=FEED_BREAST, side=RIGHT, started_at=NOW)
    stamp = revisions.timer_stamp(row)
    markups = [keyboards.running(row.kind, row_id=row.id, side=row.side, stamp=stamp),
               keyboards.timer_menu(row.id, row.kind, stamp),
               keyboards.timer_transition(row.id, FEED_BREAST, LEFT, stamp)]
    assert all(len(button.callback_data.encode()) <= 64
               for markup in markups for line in markup.inline_keyboard for button in line)


@pytest.mark.asyncio
async def test_voice_preview_refuses_to_replace_a_changed_timer(session, family, child, mother):
    from bot.handlers.voice import Draft, _save
    from bot.services.dictation import Record

    row = await feeding(session, child, mother)
    draft = Draft(family.id, (Record(kind=SLEEP, at=tu.to_local(NOW, family.tz)),),
                  123, tu.to_local(NOW, family.tz), "уснула",
                  revisions.timer_stamp(row))
    await repo.set_event_time(session, row, started_at=row.started_at - timedelta(minutes=1),
                              ended_at=None, estimated=False)
    with pytest.raises(time_input.TimeInputError, match="Таймер изменился"):
        await _save(session, family, mother, child, draft)
    assert row.ended_at is None
