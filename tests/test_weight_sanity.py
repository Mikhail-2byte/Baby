"""Невозможное число не должно выглядеть медицинской оценкой.

Живая проверка 14.09: `/weight 400` у девятинедельного ребёнка бот принял и
ответил «0-й центиль ⚠️ ниже обычного коридора», а 400 г встали заголовком
панели как текущий вес. Столько весит плод на 22-й неделе; это опечатка, а
формулировка выдавала её за находку.

Это тот же случай, ради которого в `growth.evaluate` стоит проверка
`value <= 0` — там записано дословно: «формула LMS на нуле не падает, а отдаёт
z около −9, и бот показал бы „⚠️ ниже обычного коридора“ вместо „проверьте
число“». Разница лишь в том, что порог здесь не ноль и зависит от возраста, а
возраст у бота есть.
"""

import pytest

from bot import texts
from bot.db.models import HEAD, LENGTH, WEIGHT, WhoLms
from bot.services import growth

# L, M, S здесь синтетические, и это намеренно: проверяется механизм флага, а
# не верность таблиц ВОЗ. Настоящие числа сверяются в двух других местах —
# `test_growth.py` держит строки из скачанных файлов, а `kb/load_who.py` при
# каждой загрузке сверяет формулу с 78 тысячами контрольных точек самой ВОЗ.
# Вписать сюда «настоящие» числа по памяти значило бы повторить ошибку с
# L = 0.3487, которую уже ловили: константа из вторичного источника
L, M, S = 1.0, 6.0, 0.12

AGE_DAYS = 6  # столько тестовому ребёнку из conftest на момент `now`


@pytest.fixture
def boy(child):
    child.sex = "m"
    return child


async def _lms_row(session) -> None:
    session.add(
        WhoLms(sex="m", indicator=WEIGHT, age_days=AGE_DAYS, l=L, m=M, s=S)
    )
    await session.commit()


def test_who_flags_are_not_ours() -> None:
    """Границы взяты у ВОЗ, а не придуманы.

    Это флаги контроля качества данных из WHO Child Growth Standards — те же,
    что применяет WHO Anthro, исключая измерение из расчётов. Проверяется не
    формула, а то, что числа не поехали при правке.
    """
    assert growth.FLAG_Z[WEIGHT] == (-6.0, 5.0)
    assert growth.FLAG_Z[LENGTH] == (-6.0, 6.0)
    assert growth.FLAG_Z[HEAD] == (-5.0, 5.0)


@pytest.mark.parametrize(
    ("z", "expected"),
    [
        (0.0, False),
        (-2.5, False),  # вне обычного коридора, но вполне возможно
        (-5.9, False),
        (-6.1, True),
        (4.9, False),
        (5.1, True),
    ],
)
def test_flags_bound_only_the_impossible(z, expected) -> None:
    assert growth.implausible(z, WEIGHT) is expected


def test_unknown_indicator_never_flags() -> None:
    """Молчание безопаснее выдумки: про чужой показатель мы ничего не знаем."""
    assert growth.implausible(-99.0, "temperature") is False


@pytest.mark.asyncio
async def test_a_typo_is_called_a_typo_not_a_percentile(session, boy, now) -> None:
    from bot.handlers import measure

    await _lms_row(session)
    text = await measure._zscore_text(session, boy, 400.0, now)
    assert text == texts.WEIGHT_IMPLAUSIBLE
    assert "центиль" not in text


@pytest.mark.asyncio
async def test_a_real_weight_still_gets_its_percentile(session, boy, now) -> None:
    """Сторож не должен съесть обычный случай — включая низкий, но возможный."""
    from bot.handlers import measure

    await _lms_row(session)
    normal = await measure._zscore_text(session, boy, 5100.0, now)
    assert normal is not None
    assert "центиль" in normal

    # Заметно ниже коридора, но вполне возможный вес: центиль обязан остаться
    # вместе с пометкой «ниже обычного коридора». Именно здесь сторож и мог бы
    # навредить, съев случай, ради которого центили и заведены
    low = await measure._zscore_text(session, boy, 4000.0, now)
    assert low is not None
    assert "центиль" in low
    assert "ниже обычного коридора" in low


@pytest.mark.asyncio
async def test_the_panel_headline_says_the_same(session, family, boy, now) -> None:
    """Шапка панели — второе место, где показывается центиль.

    Разойтись им нельзя: в ответе на взвешивание сказано «проверьте число», а
    в панели ребёнок остался бы с «0-м центилем» до следующего взвешивания.
    """
    from bot.db import repo
    from bot.services import stats

    await _lms_row(session)
    member = await repo.get_or_create_member(session, family.id, 1, "Мама")
    await repo.add_measurement(
        session, boy.id, member.id, WEIGHT, taken_at=now, value=400.0
    )
    text = await stats.panel_text(session, family, boy, now)
    assert texts.WEIGHT_IMPLAUSIBLE_SHORT in text
    assert "центиль" not in text
