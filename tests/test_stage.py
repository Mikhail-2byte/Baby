"""Содержание по неделям: покрытие возрастов, вехи, окна ВОЗ.

Урок предыдущего этапа усвоен: тест, который проверяет данные сам против себя,
ничего не стоит. Здесь проверяется то, что ломается независимо от содержания —
дыры в покрытии, порядок границ, соответствие вех источнику и то, что каждый
совет назвал источник.
"""

from datetime import datetime, timedelta

import pytest

from bot.db.models import Child
from bot.services import stage

BORN = datetime(2026, 9, 1, 4, 20)


@pytest.fixture
def kid():
    return Child(family_id=1, name="Аня", sex="f", born_at=BORN)


# --- Покрытие -------------------------------------------------------------


def test_no_gaps_in_coverage():
    """Каждый день до конца содержания попадает ровно в один период.

    Дыра означает возраст, на котором бот молча ничего не скажет, — и заметить
    это можно будет только в тот самый день. Импорт уже проверяет стыковку
    границ; здесь проверяется результат.
    """
    for day in range(stage.LAST_DAY + 1):
        assert stage.period_for(day) is not None, f"нет периода на день {day}"


def test_periods_do_not_overlap():
    for previous, following in zip(stage.PERIODS, stage.PERIODS[1:], strict=False):
        assert previous.to_day < following.from_day
        assert following.from_day == previous.to_day + 1


def test_coverage_starts_at_birth_and_reaches_a_year():
    assert stage.PERIODS[0].from_day == 0
    assert stage.LAST_DAY >= 365, "содержания должно хватать хотя бы на год"


def test_after_the_last_period_there_is_nothing():
    """За краем содержания бот честно молчит, а не выдумывает."""
    assert stage.period_for(stage.LAST_DAY + 1) is None


# --- Содержание -----------------------------------------------------------


def test_every_advice_names_a_source():
    """Совет без источника — это мнение, а ему здесь не место."""
    for period in stage.PERIODS:
        for item in (*period.do, *period.talk, *period.safety):
            assert item.source, f"{period.title}: совет без источника — {item.text[:40]}"
            assert len(item.text) > 30


def test_every_period_says_what_is_happening():
    for period in stage.PERIODS:
        assert len(period.happening) > 80, f"{period.title}: пустое описание"
        assert period.title


def test_first_week_covers_the_basics():
    """В первой неделе обязаны быть кожа к коже, кормление и безопасный сон.

    Не придирка к формулировкам: это три вещи, которые в первую неделю решают
    больше всего, и их отсутствие означало бы, что содержание собрано небрежно.
    """
    period = stage.period_for(0)
    everything = " ".join(
        item.text for item in (*period.do, *period.talk, *period.safety)
    ).lower()
    assert "кожа к коже" in everything
    assert "по требованию" in everything
    assert "на спине" in everything


def test_no_sleep_training_in_the_first_months():
    """Обучения сну до 4–6 месяцев в содержании быть не должно.

    Тема, где источники прямо спорят друг с другом, и единственное, в чём они
    сходятся, — что в первые месяцы этого не делают.
    """
    for period in stage.PERIODS:
        if period.to_day > 180:
            continue
        everything = " ".join(
            [period.happening, *(i.text for i in (*period.do, *period.talk))]
        ).lower()
        for forbidden in ("приучать к самостоятельному засыпанию", "дать проплакаться"):
            assert forbidden not in everything, f"{period.title}: {forbidden}"


def test_no_wonder_weeks_calendar():
    """«Скачков роста» по календарю в содержании нет и не должно быть.

    Исходное исследование (15 пар, опросники) не воспроизвелось при попытке
    репликации, и календарь скачков — популярная концепция, а не факт.
    """
    everything = " ".join(p.happening for p in stage.PERIODS).lower()
    assert "скачок роста" not in everything
    assert "скачки роста" not in everything


# --- Вехи -----------------------------------------------------------------


def test_milestones_match_the_cdc_ages():
    """CDC публикует чек-листы на 2, 4, 6, 9 и 12 месяцев."""
    assert [m.at_months for m in stage.MILESTONES] == [2, 4, 6, 9, 12]


def test_every_milestone_set_is_complete():
    """У каждого возраста заполнены все четыре категории CDC."""
    for item in stage.MILESTONES:
        assert item.social, f"{item.at_months} мес: нет социальных вех"
        assert item.language
        assert item.cognitive
        assert item.movement


def test_caveat_states_the_seventy_five_percent():
    """Оговорка про 75% обязана быть: это порог «пора спросить», а не норма.

    CDC пересмотрел вехи в 2022 году, сдвинув их с медианы на 75-й перцентиль.
    Без этой оговорки список читается как норма, которой ребёнок «не
    соответствует», — ровно то, чего делать нельзя.
    """
    assert "75%" in stage.CAVEAT
    assert "не норма" in stage.CAVEAT.lower()


@pytest.mark.parametrize(("months", "found"), [(2, True), (3, False), (4, True), (12, True)])
def test_milestones_appear_near_their_age(months, found):
    """Вехи показываются в окне вокруг своего возраста, а не ровно в день."""
    assert (stage.milestones_near(months * 30) is not None) is found


def test_milestone_window_is_symmetric():
    at = 2 * 30
    assert stage.milestones_near(at - stage.MILESTONE_WINDOW_DAYS) is not None
    assert stage.milestones_near(at + stage.MILESTONE_WINDOW_DAYS) is not None
    assert stage.milestones_near(at + stage.MILESTONE_WINDOW_DAYS + 1) is None


# --- Моторные окна ВОЗ ----------------------------------------------------


def test_motor_windows_match_who_table():
    """Границы — дословно из таблицы WHO Motor Development Study (2006).

    Числа зашиты в тест намеренно: если они поедут, это должно сломать сборку,
    а не тихо изменить то, что бот называет нормой.

    Оговорка, без которой тест выглядит тавтологией: он и правда повторяет
    `weeks.yaml`, и сам по себе доказывает только «числа не менялись». Поэтому
    13.09.2026 они были сверены не друг с другом, а со скачанной таблицей ВОЗ
    (`mm_windows_table.pdf`, колонки Left-bound и Right-bound) — все шесть пар
    совпали. Сверять надо с источником и заново, а не «проверить тестом».

    В частности, у «ходит с поддержкой» левая граница именно 6.0. Числа 5.8 и
    6.1 в той же строке — 95% доверительный интервал для самой границы, а не
    само окно; их легко принять за него.
    """
    windows = {w.name: (w.from_months, w.to_months) for w in stage.MOTOR_WINDOWS}
    assert windows["Сидит без поддержки"] == (3.8, 9.2)
    assert windows["Стоит с поддержкой"] == (4.8, 11.4)
    assert windows["Ползает на четвереньках"] == (5.2, 13.5)
    assert windows["Ходит с поддержкой"] == (6.0, 13.7)
    assert windows["Стоит сама"] == (6.9, 16.9)
    assert windows["Ходит сама"] == (8.2, 17.6)


def test_motor_windows_are_ordered():
    for window in stage.MOTOR_WINDOWS:
        assert window.from_months < window.to_months


def test_no_windows_open_for_a_newborn():
    """Самое раннее окно открывается в 3.8 месяца — раньше показывать нечего."""
    assert stage.open_windows(10) == []


def test_walking_window_is_open_at_a_year():
    names = {w.name for w in stage.open_windows(365)}
    assert "Ходит сама" in names


# --- Сборка сообщения -----------------------------------------------------


def test_report_fits_telegram_limit(kid):
    """На любом возрасте сообщение обязано уйти одним куском."""
    for day in range(0, stage.LAST_DAY + 1, 7):
        text = stage.report(kid, BORN + timedelta(days=day))
        assert text is not None
        assert len(text) < 4096, f"день {day}: {len(text)} символов"


def test_report_mentions_the_child(kid):
    text = stage.report(kid, BORN + timedelta(days=3))
    assert "Аня" in text
    assert "Первая неделя" in text


def test_report_is_deterministic(kid):
    at = BORN + timedelta(days=60)
    assert stage.report(kid, at) == stage.report(kid, at)


def test_report_is_none_past_the_content(kid):
    assert stage.report(kid, BORN + timedelta(days=stage.LAST_DAY + 10)) is None


def test_corrected_age_is_used_and_announced(kid):
    """У недоношенного считаем по скорректированному возрасту и говорим об этом.

    Молча подменять возраст нельзя: родители сверяют вехи с ровесниками, и
    разница в три недели без объяснения выглядит как ошибка бота.
    """
    kid.gestation_weeks = 34
    at = BORN + timedelta(days=60)

    text = stage.report(kid, at)
    assert "скорректированному возрасту" in text
    # Период берётся по возрасту минус три недели, а не по паспортному
    assert stage.period_for(60 - 21).title in text


def test_term_baby_gets_no_correction_notice(kid):
    text = stage.report(kid, BORN + timedelta(days=60))
    assert "скорректированному" not in text


def test_sources_are_listed_for_a_period():
    from bot import texts

    period = stage.period_for(0)
    line = texts.stage_sources(period)
    assert line.startswith("Источники: ")
    assert "Cochrane" in line
