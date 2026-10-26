"""Чистый разбор локального времени, без моделей и сетевых сервисов."""
from datetime import datetime, timedelta

import pytest

from bot.services import time_input as ti

NOW = datetime(2026, 10, 26, 8, 40)
TZ = "Asia/Yekaterinburg"  # сейчас 13:40
BORN = datetime(2026, 9, 1)


@pytest.mark.parametrize(("text", "expected"), [
    ("13:20", datetime(2026, 10, 26, 8, 20)),
    ("в 13:20", datetime(2026, 10, 26, 8, 20)),
    ("сегодня в 13:20", datetime(2026, 10, 26, 8, 20)),
    ("2026-10-26 13:20", datetime(2026, 10, 26, 8, 20)),
    ("20 минут назад", NOW - timedelta(minutes=20)),
    ("20 мин назад", NOW - timedelta(minutes=20)),
])
def test_direct_time(text, expected):
    assert ti.moment(text, now=NOW, tz=TZ, lower=NOW - timedelta(hours=1), upper=NOW) == expected


@pytest.mark.parametrize("text", ["25:00", "14:99", "-1:20", "завтра", "полтретьего", "14:00"])
def test_invalid_or_future_is_not_saved(text):
    with pytest.raises(ti.TimeInputError):
        ti.moment(text, now=NOW, tz=TZ, lower=NOW - timedelta(hours=1), upper=NOW)


def test_ambiguous_date_is_not_guessed():
    with pytest.raises(ti.TimeInputError, match="Уточните день"):
        ti.moment("13:20", now=NOW, tz=TZ, lower=NOW - timedelta(days=2), upper=NOW)


def test_explicit_yesterday_resolves_ambiguity():
    assert ti.moment("вчера в 13:20", now=NOW, tz=TZ,
                     lower=NOW - timedelta(days=2), upper=NOW) == datetime(2026, 10, 25, 8, 20)


def test_midnight_is_resolved_by_open_timer_bounds():
    now = datetime(2026, 10, 26, 19, 50)  # 27 октября 00:50
    start = datetime(2026, 10, 26, 18, 40)  # 23:40
    assert ti.moment("00:15", now=now, tz=TZ, lower=start, upper=now) == datetime(
        2026, 10, 26, 19, 15
    )


def test_edit_uses_record_day_not_today():
    reference = datetime(2026, 10, 20, 8, 20)
    assert ti.moment("13:30", now=NOW, tz=TZ,
                     lower=reference - timedelta(hours=1), upper=NOW,
                     reference=reference) == datetime(2026, 10, 20, 8, 30)


@pytest.mark.parametrize(("text", "kind", "side"), [
    ("кормили левой с 13:00 до 13:25", "feed_breast", None),
    ("кормила правой грудью с 13:00 до 13:25", "feed_breast", None),
    ("кормление с 13:00 до 13:25", "feed_breast", None),
    ("сон с 13:00 до 13:25", "sleep", None),
])
def test_interval(text, kind, side):
    parsed = ti.interval(text, now=NOW, tz=TZ, born_at=BORN)
    assert parsed.kind == kind and parsed.side == side
    assert parsed.ended_at - parsed.started_at == timedelta(minutes=25)


def test_interval_across_midnight_with_explicit_day():
    parsed = ti.interval("вчера кормили левой с 23:50 до 00:15", now=NOW, tz=TZ, born_at=BORN)
    assert parsed.ended_at - parsed.started_at == timedelta(minutes=25)
    assert parsed.started_at == datetime(2026, 10, 25, 18, 50)


@pytest.mark.parametrize("text", [
    "сон с 14:00 до 14:30", "сон с 13:00 до 13:00",
    "2026-08-01 сон с 13:00 до 13:30", "сон левой с 13:00 до 13:25",
])
def test_invalid_interval(text):
    with pytest.raises(ti.TimeInputError):
        ti.interval(text, now=NOW, tz=TZ, born_at=BORN)


@pytest.mark.parametrize("text", ["встретимся в 13:20", "я закончила работу в 13:20", "13:20"])
def test_ordinary_chat_is_not_a_finish_command(text):
    assert ti.finish_request(text) is None
    assert ti.interval(text, now=NOW, tz=TZ, born_at=BORN) is None


@pytest.mark.parametrize("text", ["закончили в 13:20", "закончила 20 минут назад",
                                   "закончили кормить в 13:20", "проснулась в 13:20"])
def test_explicit_finish_commands(text):
    assert ti.finish_request(text) is not None
