"""Новые сценарии проходят через полный боевой список роутеров."""
from datetime import datetime, timedelta

import pytest
from aiogram.types import Message, Update, User
from sqlalchemy import func, select

from bot import keyboards
from bot.db import repo
from bot.db.models import FEED_BREAST, LEFT, SLEEP, Event
from bot.keyboards import PanelCB, TimeCB, TrackCB
from bot.services import pending
from bot.services import timeutil as tu
from tests.test_record_edit import AUTHOR, CHAT, _feed, _tap
from tests.test_record_edit import wired as wired

NOW = datetime(2026, 10, 26, 8, 40)  # 11:40 Europe/Moscow из family-фикстуры


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(tu, "now_utc", lambda: NOW)


async def event(session, child, mother, **kwargs):
    return await repo.start_event(session, child.id, mother.id, kwargs.pop("kind", FEED_BREAST),
                                  started_at=kwargs.pop("started_at", NOW - timedelta(minutes=40)),
                                  side=LEFT, **kwargs)


async def count_events(session, child):
    return await session.scalar(
        select(func.count()).select_from(Event).where(Event.child_id == child.id)
    )


def test_idle_panel_has_three_buttons_only():
    labels = [b.text for row in keyboards.panel(running_kind=None).inline_keyboard for b in row]
    assert labels == ["🤱 Кормление", "😴 Сон", "⋯ Ещё"]


def test_running_controls_are_bound_to_a_record():
    controls = keyboards.panel(running_kind=FEED_BREAST, running_id=23)
    values = [TimeCB.unpack(b.callback_data) for row in controls.inline_keyboard for b in row]
    assert len(values) == 2
    assert {item.row_id for item in values} == {23}
    assert {item.action for item in values} == {"stop", "ask"}
    assert all(len(b.callback_data.encode()) <= 64 for row in controls.inline_keyboard for b in row)


@pytest.mark.asyncio
async def test_finish_by_text(wired, session, family, child, mother):
    dp, bot, talk, drawn = wired
    row = await event(session, child, mother)
    await _feed(dp, bot, "закончили в 11:20")
    await session.refresh(row)
    assert row.ended_at == NOW - timedelta(minutes=20)
    assert row.estimated is False
    assert await count_events(session, child) == 1
    assert "11:00–11:20" in talk.sent[-1]
    assert drawn == [family.id]


@pytest.mark.asyncio
async def test_prompt_then_bare_time(wired, session, family, child, mother):
    dp, bot, talk, _ = wired
    row = await event(session, child, mother)
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id))
    assert pending.peek(family.chat_id, AUTHOR.id).row_id == row.id
    await _feed(dp, bot, "11:25")
    await session.refresh(row)
    assert row.ended_at == NOW - timedelta(minutes=15)
    assert pending.peek(family.chat_id, AUTHOR.id) is None


@pytest.mark.asyncio
async def test_bare_time_without_prompt_is_ignored(wired, session, child, mother):
    dp, bot, talk, _ = wired
    row = await event(session, child, mother)
    await _feed(dp, bot, "11:25")
    await session.refresh(row)
    assert row.ended_at is None and talk.sent == []


@pytest.mark.asyncio
async def test_invalid_time_keeps_prompt_and_event(wired, session, family, child, mother):
    dp, bot, talk, _ = wired
    row = await event(session, child, mother)
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id))
    await _feed(dp, bot, "25:90")
    await session.refresh(row)
    assert row.ended_at is None
    assert pending.peek(family.chat_id, AUTHOR.id) is not None
    assert "00:00" in talk.sent[-1]


@pytest.mark.asyncio
async def test_ambiguous_date_can_be_clarified(wired, session, child, mother):
    dp, bot, talk, _ = wired
    row = await event(session, child, mother, started_at=NOW - timedelta(days=2))
    await _feed(dp, bot, "закончили в 11:20")
    assert "Уточните день" in talk.sent[-1]
    await _feed(dp, bot, "вчера в 11:20")
    await session.refresh(row)
    assert row.ended_at == NOW - timedelta(days=1, minutes=20)


@pytest.mark.asyncio
async def test_second_parent_cannot_answer_another_prompt(wired, session, family, child, mother):
    dp, bot, _, _ = wired
    row = await event(session, child, mother)
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id))
    other = User(id=333, is_bot=False, first_name="Папа")
    await dp.feed_update(bot, Update(update_id=80001, message=Message(
        message_id=80001, date=NOW, chat=CHAT, from_user=other, text="11:20",
    )))
    await session.refresh(row)
    assert row.ended_at is None
    assert pending.peek(family.chat_id, AUTHOR.id).row_id == row.id


@pytest.mark.asyncio
async def test_concurrent_edit_does_not_overwrite_new_data(wired, session, family, child, mother):
    dp, bot, talk, _ = wired
    row = await event(session, child, mother)
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id))
    await repo.close_event(session, row, NOW - timedelta(minutes=10))
    await _feed(dp, bot, "11:20")
    await session.refresh(row)
    assert row.ended_at == NOW - timedelta(minutes=10)
    assert "уже изменена" in talk.sent[-1]
    assert pending.peek(family.chat_id, AUTHOR.id) is None


@pytest.mark.asyncio
async def test_old_stop_does_not_end_new_timer(wired, session, child, mother):
    dp, bot, talk, _ = wired
    previous = await event(session, child, mother, started_at=NOW - timedelta(hours=2))
    await repo.close_event(session, previous, NOW - timedelta(hours=1))
    current = await event(session, child, mother)
    await _tap(dp, bot, TimeCB(action="stop", row_id=previous.id))
    await session.refresh(current)
    assert current.ended_at is None
    assert "уже завершена" in talk.alerts[-1]


@pytest.mark.asyncio
async def test_legacy_stop_is_safe(wired, session, child, mother):
    dp, bot, talk, _ = wired
    row = await event(session, child, mother)
    await _tap(dp, bot, TrackCB(action="stop", kind=FEED_BREAST))
    await session.refresh(row)
    assert row.ended_at is None
    assert "старая кнопка" in talk.alerts[-1]


@pytest.mark.asyncio
async def test_bound_stop_updates_card_without_new_message(wired, session, child, mother):
    dp, bot, talk, _ = wired
    row = await event(session, child, mother)
    row.card_message_id = 999
    await session.commit()
    await _tap(dp, bot, TimeCB(action="stop", row_id=row.id))
    assert talk.sent == []
    assert "11:00–11:40" in talk.edited[-1]
    await _tap(dp, bot, TimeCB(action="stop", row_id=row.id))
    assert talk.sent == []
    assert "уже завершена" in talk.alerts[-1]


@pytest.mark.asyncio
async def test_explicit_sleep_finish(wired, session, child, mother):
    dp, bot, _, _ = wired
    row = await event(session, child, mother, kind=SLEEP)
    await _feed(dp, bot, "проснулась в 11:25")
    await session.refresh(row)
    assert row.ended_at == NOW - timedelta(minutes=15)


@pytest.mark.asyncio
async def test_sleep_words_do_not_stop_feeding(wired, session, child, mother):
    dp, bot, talk, _ = wired
    row = await event(session, child, mother)
    await _feed(dp, bot, "проснулась в 11:25")
    await session.refresh(row)
    assert row.ended_at is None
    assert "активного таймера нет" in talk.sent[-1]


@pytest.mark.asyncio
async def test_cancel_time_entry(wired, session, family, child, mother):
    dp, bot, _, _ = wired
    row = await event(session, child, mother)
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id))
    await _feed(dp, bot, "отмена")
    assert pending.peek(family.chat_id, AUTHOR.id) is None
    await session.refresh(row)
    assert row.ended_at is None


@pytest.mark.asyncio
async def test_interval_is_not_duplicated(wired, session, child):
    dp, bot, talk, _ = wired
    text = "кормили левой с 11:00 до 11:20"
    await _feed(dp, bot, text)
    await _feed(dp, bot, text)
    assert await count_events(session, child) == 1
    assert "дубль не добавлял" in talk.sent[-1]


@pytest.mark.asyncio
async def test_interval_does_not_silently_close_a_timer(wired, session, child, mother):
    dp, bot, talk, _ = wired
    row = await event(session, child, mother)
    await _feed(dp, bot, "сон с 11:00 до 11:20")
    await session.refresh(row)
    assert row.ended_at is None
    assert await count_events(session, child) == 1
    assert "Сейчас идёт таймер" in talk.sent[-1]


@pytest.mark.asyncio
async def test_double_start_is_idempotent(wired, session, child):
    dp, bot, _, _ = wired
    await _tap(dp, bot, TrackCB(action="start", kind=FEED_BREAST, side=LEFT))
    await _tap(dp, bot, TrackCB(action="start", kind=FEED_BREAST, side=LEFT))
    assert await count_events(session, child) == 1
    assert (await repo.any_open_event(session, child.id)).card_message_id == 999


@pytest.mark.asyncio
async def test_other_family_record_is_not_editable(wired, session, family, child, mother, born):
    dp, bot, talk, _ = wired
    other_family = await repo.get_or_create_family(session, -1002, "Другая семья")
    other_child = await repo.create_child(session, other_family.id, name="Другой", sex="m",
                                          born_at=born, birth_weight_g=3500)
    other = await event(session, other_child, mother)
    await _tap(dp, bot, TimeCB(action="stop", row_id=other.id))
    await session.refresh(other)
    assert other.ended_at is None
    await _tap(dp, bot, TimeCB(action="ask", row_id=other.id))
    assert pending.peek(family.chat_id, AUTHOR.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("via_button", [False, True])
async def test_quiet_changes_only_current_chat(wired, session, family, via_button):
    dp, bot, talk, _ = wired
    other = await repo.get_or_create_family(session, -1002, "Другая семья")
    await repo.set_checkin(session, family, 3, NOW)
    await repo.set_checkin(session, other, 6, NOW)
    if via_button:
        await _tap(dp, bot, PanelCB(action="quiet"))
    else:
        await _feed(dp, bot, "/quiet")
    await session.refresh(family)
    await session.refresh(other)
    assert family.checkin_hours == 0 and family.checkin_last_at is None
    assert other.checkin_hours == 6
    assert "этом чате" in talk.sent[-1]


@pytest.mark.asyncio
async def test_edit_old_record_uses_its_date(wired, session, child, mother):
    dp, bot, _, _ = wired
    old = NOW - timedelta(days=3)
    row = await event(session, child, mother, started_at=old - timedelta(minutes=40))
    await repo.close_event(session, row, old - timedelta(minutes=10), estimated=True)
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id, field="e"))
    await _feed(dp, bot, "11:20")
    await session.refresh(row)
    assert row.ended_at == old - timedelta(minutes=20)
    assert row.estimated is False


@pytest.mark.asyncio
async def test_start_can_be_corrected_by_direct_time(wired, session, child, mother):
    dp, bot, _, _ = wired
    row = await event(session, child, mother)
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id, field="s"))
    await _feed(dp, bot, "10:55")
    await session.refresh(row)
    assert row.started_at == NOW - timedelta(minutes=45)
    assert row.ended_at is None


@pytest.mark.asyncio
async def test_end_command_does_not_accidentally_change_start(wired, session, child, mother):
    dp, bot, _, _ = wired
    row = await event(session, child, mother)
    initial = row.started_at
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id, field="s"))
    await _feed(dp, bot, "закончили в 11:20")
    await session.refresh(row)
    assert row.started_at == initial
    assert row.ended_at == NOW - timedelta(minutes=20)


@pytest.mark.asyncio
async def test_historical_instant_event_time_remains_editable(wired, session, child, mother):
    from bot.db.models import DIAPER_WET

    dp, bot, _, _ = wired
    row = await repo.add_event(session, child.id, mother.id, DIAPER_WET,
                               started_at=NOW - timedelta(hours=1))
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id, field="s"))
    await _feed(dp, bot, "10:50")
    await session.refresh(row)
    assert row.started_at == NOW - timedelta(minutes=50)
    assert row.ended_at is None
    assert await repo.any_open_event(session, child.id) is None


@pytest.mark.asyncio
async def test_measurement_time_remains_editable(wired, session, child, mother):
    from bot.db.models import WEIGHT

    dp, bot, _, _ = wired
    row = await repo.add_measurement(session, child.id, mother.id, WEIGHT, value=4200,
                                     taken_at=NOW - timedelta(hours=1))
    await _tap(dp, bot, TimeCB(action="ask", what="m", field="t", row_id=row.id))
    await _feed(dp, bot, "10:50")
    await session.refresh(row)
    assert row.taken_at == NOW - timedelta(minutes=50)
    assert row.value == 4200


@pytest.mark.asyncio
async def test_time_reply_to_an_older_prompt_is_not_used(wired, session, family, child, mother):
    dp, bot, talk, _ = wired
    row = await event(session, child, mother)
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id))
    await dp.feed_update(bot, Update(update_id=80002, message=Message(
        message_id=80002, date=NOW, chat=CHAT, from_user=AUTHOR, text="11:20",
        reply_to_message=Message(message_id=111, date=NOW, chat=CHAT, text="Старый вопрос"),
    )))
    await session.refresh(row)
    assert row.ended_at is None
    assert "другой вопрос" in talk.sent[-1]
    assert pending.peek(family.chat_id, AUTHOR.id) is not None


@pytest.mark.asyncio
async def test_expired_prompt_does_not_consume_a_clock(wired, session, family, child, mother):
    dp, bot, _, _ = wired
    row = await event(session, child, mother)
    pending.remember(family.chat_id, AUTHOR.id, pending.TIME_END, row.id)
    pending.peek(family.chat_id, AUTHOR.id).asked_at -= timedelta(minutes=11)
    await _feed(dp, bot, "11:20")
    await session.refresh(row)
    assert row.ended_at is None


@pytest.mark.asyncio
async def test_wrong_kind_in_end_prompt_is_not_overwritten(wired, session, child, mother):
    dp, bot, talk, _ = wired
    row = await event(session, child, mother, kind=SLEEP)
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id))
    await _feed(dp, bot, "закончили кормить в 11:20")
    await session.refresh(row)
    assert row.ended_at is None
    assert "другому виду" in talk.sent[-1]


@pytest.mark.asyncio
async def test_parallel_stops_are_idempotent(wired, session, child, mother):
    import asyncio

    dp, bot, talk, _ = wired
    row = await event(session, child, mother)
    row.card_message_id = 999
    await session.commit()
    await asyncio.gather(
        _tap(dp, bot, TimeCB(action="stop", row_id=row.id)),
        _tap(dp, bot, TimeCB(action="stop", row_id=row.id)),
    )
    await session.refresh(row)
    assert row.ended_at == NOW
    assert len(talk.edited) == 1
    assert await count_events(session, child) == 1


@pytest.mark.asyncio
async def test_cancel_command_clears_pending_time(wired, session, family, child, mother):
    dp, bot, _, _ = wired
    row = await event(session, child, mother)
    await _tap(dp, bot, TimeCB(action="ask", row_id=row.id))
    await _feed(dp, bot, "/cancel")
    assert pending.peek(family.chat_id, AUTHOR.id) is None
    await session.refresh(row)
    assert row.ended_at is None


@pytest.mark.asyncio
async def test_unavailable_card_does_not_lose_saved_end(wired, session, child, mother, monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    dp, bot, talk, _ = wired
    row = await event(session, child, mother)
    row.card_message_id = 999
    await session.commit()

    async def deleted_card(*args, **kwargs):
        raise TelegramBadRequest(method=None, message="message to edit not found")

    monkeypatch.setattr(bot, "edit_message_text", deleted_card)
    await _tap(dp, bot, TimeCB(action="stop", row_id=row.id))
    await session.refresh(row)
    assert row.ended_at == NOW
    assert len(talk.sent) == 1 and "11:00–11:40" in talk.sent[0]
