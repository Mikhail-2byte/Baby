"""Ожидание числа: механизм, заменивший FSM.

Проверяется главное его свойство — оно ничего не блокирует. Состояние aiogram
висит до перезапуска и молча съедает следующую реплику человека; здесь реплика,
не похожая на число, просто проходит мимо.
"""

from datetime import timedelta

import pytest

from bot.services import pending
from bot.services import timeutil as tu

CHAT, MOM, DAD = -1001, 222, 333


def test_take_returns_what_was_remembered():
    pending.remember(CHAT, MOM, pending.BOTTLE_ML, row_id=7)
    ask = pending.take(CHAT, MOM)
    assert ask is not None
    assert ask.what == pending.BOTTLE_ML
    assert ask.row_id == 7


def test_take_is_once_only():
    """Забрали — больше нет. Иначе второе число ушло бы в ту же запись."""
    pending.remember(CHAT, MOM, pending.WEIGHT_G)
    assert pending.take(CHAT, MOM) is not None
    assert pending.take(CHAT, MOM) is None


def test_expires_after_ttl():
    """Через десять минут ожидание истекает само.

    Ровно то, чего не умеет FSM: брошенный на середине мастер висит до
    перезапуска бота, и случайное число через час попадает не туда.
    """
    pending.remember(CHAT, MOM, pending.WEIGHT_G)
    later = tu.now_utc() + pending.TTL + timedelta(seconds=1)
    assert pending.take(CHAT, MOM, later) is None


def test_two_parents_do_not_collide():
    """Ключ — «чат плюс человек»: в семейном чате двое.

    Вес, введённый мамой, не должен попасть в бутылочку, про которую
    спрашивали папу.
    """
    pending.remember(CHAT, MOM, pending.WEIGHT_G)
    pending.remember(CHAT, DAD, pending.BOTTLE_ML, row_id=9)

    assert pending.take(CHAT, MOM).what == pending.WEIGHT_G
    assert pending.take(CHAT, DAD).what == pending.BOTTLE_ML


def test_peek_does_not_consume():
    pending.remember(CHAT, MOM, pending.WEIGHT_G)
    assert pending.peek(CHAT, MOM) is not None
    assert pending.take(CHAT, MOM) is not None


def test_dictionary_is_bounded():
    """Страховка от роста словаря. Семья из двух человек столько не наберёт."""
    for user in range(pending.MAX_ENTRIES + 10):
        pending.remember(CHAT, user, pending.WEIGHT_G)
    assert len(pending._asks) <= pending.MAX_ENTRIES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("60", 60.0),
        ("60 мл", 60.0),
        ("3,56", 3.56),
        ("3.56 кг", 3.56),
        ("3560г", 3560.0),
        ("  90  ", 90.0),
    ],
)
def test_parse_number_accepts_plain_numbers(text, expected):
    assert pending.parse_number(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "дал 60 мл где-то в полночь",
        "купи две пачки",
        "",
        "ок",
        # Дата — не число: «13.09.2026» не должно стать весом 13.09
        "13.09.2026",
    ],
)
def test_parse_number_rejects_phrases(text):
    """Только голое число. Фразы разбирает LLM на этапе свободного ввода.

    Путать два механизма нельзя: иначе «купи 2 пачки» в семейном чате
    превратится в вес ребёнка.
    """
    assert pending.parse_number(text) is None
