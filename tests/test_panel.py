"""Панель: дебаунс, перевыпуск и поведение при сбоях Telegram.

Панель правится на каждый тап, то есть чаще всего остального в боте. Ошибка
здесь либо упирается во флуд-контроль Telegram, либо засыпает чат копиями
панели — и то и другое видно семье немедленно.
"""

import asyncio
from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest

from bot.db import repo
from bot.db.models import DIAPER_WET
from bot.services import panel
from bot.services import timeutil as tu


def _bad_request(message: str) -> TelegramBadRequest:
    """`TelegramBadRequest` требует объект метода — подсовываем заглушку."""
    return TelegramBadRequest(method=None, message=message)  # type: ignore[arg-type]


@pytest.fixture
def fast_debounce(monkeypatch):
    """Дебаунс на время теста — миллисекунды вместо полутора секунд."""
    monkeypatch.setattr(
        "bot.services.panel.settings.panel_debounce_seconds", 0.01
    )


@pytest.mark.asyncio
async def test_panel_is_published_and_pinned(
    session_maker, session, family, child, bot, monkeypatch
):
    """Первая перерисовка выпускает панель и закрепляет её."""
    monkeypatch.setattr(panel, "Session", session_maker)
    await panel.refresh(bot, family.id)

    assert len(bot.sent) == 1
    assert child.name in bot.texts[0]
    assert bot.pinned == [101]

    await session.refresh(family)
    assert family.panel_message_id == 101
    assert family.panel_day == tu.local_today(family.tz)


@pytest.mark.asyncio
async def test_second_refresh_edits_instead_of_sending(
    session_maker, session, family, child, bot, monkeypatch
):
    """Панель правится, а не дублируется. Иначе чат завалит копиями."""
    monkeypatch.setattr(panel, "Session", session_maker)
    await panel.refresh(bot, family.id)
    await panel.refresh(bot, family.id, last_message_id=102)

    assert len(bot.sent) == 1
    assert len(bot.edited) == 1


@pytest.mark.asyncio
async def test_burst_of_taps_makes_one_edit(
    session_maker, family, child, bot, monkeypatch, fast_debounce
):
    """Серия тапов подряд превращается в одну правку.

    Не косметика: Telegram ограничивает частоту правок одного сообщения, и без
    дебаунса пять быстрых тапов упёрлись бы во флуд-контроль.
    """
    monkeypatch.setattr(panel, "Session", session_maker)
    await panel.refresh(bot, family.id)  # выпустили панель

    for _ in range(5):
        panel.schedule(bot, family.id)
    await asyncio.sleep(0.1)

    assert len(bot.edited) == 1


@pytest.mark.asyncio
async def test_panel_reissued_when_it_scrolled_away(
    session_maker, session, family, child, bot, monkeypatch
):
    """Уехавшая вверх панель выпускается заново, а старая откалывается."""
    monkeypatch.setattr(panel, "Session", session_maker)
    await panel.refresh(bot, family.id)

    far = 101 + panel.settings.panel_max_messages + 5
    await panel.refresh(bot, family.id, last_message_id=far)

    assert len(bot.sent) == 2
    assert bot.unpinned == [101]
    assert bot.pinned == [101, 102]


@pytest.mark.asyncio
async def test_yesterday_panel_is_reissued(
    session_maker, session, family, child, bot, monkeypatch
):
    """Панель за вчера перевыпускается, даже если она последняя в чате."""
    monkeypatch.setattr(panel, "Session", session_maker)
    await panel.refresh(bot, family.id)

    await panel.refresh(
        bot, family.id, now=tu.now_utc() + timedelta(days=1)
    )
    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_deleted_panel_is_replaced(
    session_maker, family, child, bot, monkeypatch
):
    """Панель удалили руками — правка не проходит, выпускаем новую."""
    monkeypatch.setattr(panel, "Session", session_maker)
    await panel.refresh(bot, family.id)

    bot._fail_on_edit = {0: _bad_request("message to edit not found")}
    await panel.refresh(bot, family.id, last_message_id=102)

    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_unmodified_panel_does_not_spawn_a_copy(
    session_maker, family, child, bot, monkeypatch
):
    """«message is not modified» — это норма, а не повод выпускать новую.

    Так отсеиваются холостые перерисовки: тап, ничего не изменивший в тексте,
    обязан закончиться тишиной, а не второй панелью в чате.
    """
    monkeypatch.setattr(panel, "Session", session_maker)
    await panel.refresh(bot, family.id)

    bot._fail_on_edit = {0: _bad_request("message is not modified")}
    await panel.refresh(bot, family.id, last_message_id=102)

    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_panel_survives_missing_pin_rights(
    session_maker, session, family, child, bot, monkeypatch
):
    """Без прав админа панель просто живёт внизу чата, а не ломает бота."""
    monkeypatch.setattr(panel, "Session", session_maker)
    bot._fail_on_pin = {0: _bad_request("not enough rights to pin a message")}

    await panel.refresh(bot, family.id)

    assert len(bot.sent) == 1
    await session.refresh(family)
    assert family.panel_message_id == 101


@pytest.mark.asyncio
async def test_panel_without_child_invites_to_create_one(
    session_maker, family, bot, monkeypatch
):
    """Пустая семья видит приглашение завести карточку, а не пустую панель."""
    monkeypatch.setattr(panel, "Session", session_maker)
    await panel.refresh(bot, family.id)

    assert "/baby" in bot.texts[0]
    # Кнопок записи быть не должно: записывать пока некому
    assert bot.kwargs[0]["reply_markup"] is None


@pytest.mark.asyncio
async def test_panel_reflects_logged_events(
    session_maker, session, family, child, mother, bot, monkeypatch
):
    """Записанное видно на панели — сквозная проверка от repo до текста."""
    monkeypatch.setattr(panel, "Session", session_maker)
    now = tu.now_utc()
    for _ in range(3):
        await repo.add_event(
            session, child.id, mother.id, DIAPER_WET, started_at=now
        )

    await panel.refresh(bot, family.id, now=now)
    assert "3 мокрых" in bot.texts[0]


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_debounce(
    session_maker, family, child, bot, monkeypatch
):
    """При остановке бота недоспавший дебаунс гасится.

    Иначе задача проснётся уже после закрытия сетевой сессии и на каждой
    остановке будет писать в лог сетевую ошибку.
    """
    monkeypatch.setattr(panel, "Session", session_maker)
    panel.schedule(bot, family.id)
    assert panel._tasks

    await panel.shutdown()
    assert panel._tasks == {}
    assert bot.sent == []
