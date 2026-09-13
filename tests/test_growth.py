"""Расчёт центилей по стандартам ВОЗ.

Самый важный файл тестов в проекте. Всё остальное в худшем случае неудобно;
неверный центиль — это либо зря отменённый поход к врачу, либо зря устроенная
паника. Формула проверяется на свойствах, которые обязаны выполняться по
определению LMS, а не на «ожидаемых» числах, списанных с собственного вывода.

Коэффициенты и контрольные точки взяты из настоящих таблиц ВОЗ
(`kb/raw/who/wfa-girls.xlsx`, `hcfa-girls.xlsx`). Медиана 3.2322 кг при
рождении правдоподобна — это заодно проверка, что таблицу не перепутали.

Полную сверку формулы с ВОЗ делает `kb/load_who.py` при загрузке: он проходит
все 78 тысяч контрольных точек шести таблиц. Здесь — небольшой набор тех же
точек, зашитый в код, чтобы формула оставалась проверенной даже на машине без
загруженной базы.
"""

from datetime import timedelta

import pytest

from bot.db.models import HEAD, WEIGHT, WhoLms
from bot.services import growth

# Девочки, weight-for-age, день 0
L0, M0, S0 = 0.3809, 3.2322, 0.14171

# Настоящие строки таблиц ВОЗ: (индикатор, L, M, S, {n отклонений: значение}).
# Значения в единицах ВОЗ — килограммы и сантиметры
WHO_ROWS = [
    # Девочки, вес-к-возрасту
    (WEIGHT, 0.3809, 3.2322, 0.14171, {-2: 2.395, 0: 3.232, 2: 4.230, 3: 4.793}),
    (WEIGHT, 0.1727, 4.1716, 0.13738, {-2: 3.148, 0: 4.172, 2: 5.456, 3: 6.212}),
    (WEIGHT, -0.0729, 7.2650, 0.12208, {-2: 5.703, 0: 7.265, 2: 9.295, 3: 10.531}),
    (WEIGHT, -0.2022, 8.9462, 0.12267, {-2: 7.041, 0: 8.946, 2: 11.506, 3: 13.114}),
    # Девочки, окружность головы. Здесь L = 1 ровно, то есть ветка формулы
    # другая — и без этих строк она осталась бы непроверенной на боевых данных
    (HEAD, 1.0, 33.8787, 0.03496, {-2: 31.510, 0: 33.879, 2: 36.247, 3: 37.432}),
    (HEAD, 1.0, 36.5163, 0.03211, {-2: 34.171, 0: 36.516, 2: 38.861, 3: 40.034}),
    (HEAD, 1.0, 42.1403, 0.03089, {-2: 39.537, 0: 42.140, 2: 44.744, 3: 46.045}),
]


@pytest.mark.parametrize(("indicator", "l", "m", "s", "points"), WHO_ROWS)
def test_matches_who_own_numbers(indicator, l, m, s, points):  # noqa: E741
    """Наша формула воспроизводит значения, посчитанные самой ВОЗ.

    Самая ценная проверка в файле: она сверяет реализацию не с нашими же
    ожиданиями, а с авторитетом. Допуск 0.01 — таблицы округлены до трёх знаков,
    и ошибка округления входного значения умножается на крутизну z.
    """
    for n, value in points.items():
        assert growth.zscore(value, l, m, s, indicator) == pytest.approx(n, abs=0.01)


def test_two_sd_is_the_familiar_percentile():
    """±2 SD — это 2.3-й и 97.7-й центили. Их родители и видят в выписке."""
    assert growth.percentile(-2) == pytest.approx(2.275, abs=0.01)
    assert growth.percentile(2) == pytest.approx(97.725, abs=0.01)


def test_median_gives_zero():
    """Вес ровно по медиане — это z = 0 и 50-й центиль. По определению."""
    z = growth.zscore(M0, L0, M0, S0, WEIGHT)
    assert z == pytest.approx(0, abs=1e-12)
    assert growth.percentile(z) == pytest.approx(50, abs=1e-9)


@pytest.mark.parametrize("n", [-2, -1, 1, 2])
def test_sd_points_round_trip(n):
    """Значение на n стандартных отклонений даёт ровно z = n.

    Проверяет согласованность прямого и обратного преобразования: `sd_at`
    считает по формуле Бокса–Кокса, `zscore` разворачивает её обратно. Ошибка
    в любой из двух сломала бы равенство.
    """
    value = growth.sd_at(L0, M0, S0, n)
    assert growth.zscore(value, L0, M0, S0, WEIGHT) == pytest.approx(n, abs=1e-9)


def test_l_zero_branch_is_lognormal():
    """При L = 0 распределение логнормальное — отдельная ветка формулы."""
    value = M0 * 2.718281828459045**S0  # ровно одно SD вверх
    assert growth.zscore(value, 0.0, M0, S0, WEIGHT) == pytest.approx(1, abs=1e-9)


@pytest.mark.parametrize("k", [1, 2, 5])
def test_upper_tail_becomes_linear(k):
    """За +3 SD шкала линейная: шаг в (SD3 − SD2) стоит ровно единицы z.

    Это и есть содержание подрезки ВОЗ. Формула Бокса–Кокса в хвосте скошенного
    распределения ведёт себя непредсказуемо, поэтому стандарт заменяет её на
    линейную с шагом, равным расстоянию между третьим и вторым отклонением.
    """
    sd3, sd2 = growth.sd_at(L0, M0, S0, 3), growth.sd_at(L0, M0, S0, 2)
    value = sd3 + k * (sd3 - sd2)
    assert growth.zscore(value, L0, M0, S0, WEIGHT) == pytest.approx(3 + k)


@pytest.mark.parametrize("k", [1, 2, 5])
def test_lower_tail_becomes_linear(k):
    """То же в нижнем хвосте, где шаг равен (SD−2 − SD−3).

    Знаменатель здесь другой, и перепутать их легко: в тексте стандарта это
    два разных выражения, а не одно со сменой знака.
    """
    sd3, sd2 = growth.sd_at(L0, M0, S0, -3), growth.sd_at(L0, M0, S0, -2)
    value = sd3 - k * (sd2 - sd3)
    assert growth.zscore(value, L0, M0, S0, WEIGHT) == pytest.approx(-3 - k)


def test_length_is_not_capped():
    """У длины распределение близко к нормальному — подрезка не применяется.

    Это не упущение, а текст стандарта, и тест существует затем, чтобы
    «заодно» её сюда не добавили.
    """
    huge = growth.sd_at(L0, M0, S0, 3) * 2
    raw = ((huge / M0) ** L0 - 1) / (L0 * S0)
    assert growth.zscore(huge, L0, M0, S0, "length") == pytest.approx(raw)


# --- Возраст --------------------------------------------------------------


def test_corrected_age_ignores_term_babies(child, born):
    """У доношенного скорректированный возраст равен обычному."""
    at = born + timedelta(days=40)
    assert growth.corrected_age_days(child, at) == growth.age_days(child, at)


def test_corrected_age_subtracts_prematurity(child, born):
    """Родился на 34-й неделе — считаем от предполагаемой даты родов.

    Без поправки такой ребёнок «отстаёт» ровно на недоношенность и полгода
    пугает родителей низким центилем, будучи совершенно здоровым.
    """
    child.gestation_weeks = 34
    at = born + timedelta(days=40)
    assert growth.corrected_age_days(child, at) == 40 - 3 * 7


def test_corrected_age_never_negative(child, born):
    """Первые недели жизни недоношенного дают отрицательный возраст.

    Отрицательный возраст в таблицу ВОЗ не попадёт всё равно, но ронять на нём
    расчёт нельзя — взвешивают таких детей чаще всех.
    """
    child.gestation_weeks = 30
    assert growth.corrected_age_days(child, born + timedelta(days=3)) == 0


# --- Обращение к таблицам -------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_without_tables_returns_none(session, child, now):
    """Пустая таблица ВОЗ — штатный режим первых дней, а не поломка.

    Бот в этом случае показывает вес без центиля. Выдумать центиль было бы
    хуже, чем не показать его.
    """
    assert await growth.evaluate(session, child, WEIGHT, 3500, now) is None


@pytest.mark.asyncio
async def test_evaluate_converts_grams_to_kilograms(session, child, born):
    """Наши граммы против килограммов ВОЗ.

    Ошибка на порядок здесь дала бы z-score вроде −40 — это выглядит как
    катастрофа со здоровьем, а не как перепутанные единицы, и именно поэтому
    проверяется отдельно.
    """
    session.add(WhoLms(sex="f", indicator=WEIGHT, age_days=0, l=L0, m=M0, s=S0))
    await session.commit()

    evaluated = await growth.evaluate(session, child, WEIGHT, M0 * 1000, born)
    assert evaluated is not None
    z, pct = evaluated
    assert z == pytest.approx(0, abs=1e-9)
    assert pct == pytest.approx(50, abs=1e-9)


@pytest.mark.asyncio
async def test_lms_interpolates_between_rows(session):
    """Попадание между строками таблицы — линейная интерполяция."""
    session.add(WhoLms(sex="f", indicator=WEIGHT, age_days=0, l=1.0, m=3.0, s=0.1))
    session.add(WhoLms(sex="f", indicator=WEIGHT, age_days=10, l=1.0, m=4.0, s=0.2))
    await session.commit()

    lms = await growth.lms_at(session, "f", WEIGHT, 5)
    assert lms is not None
    assert lms[1] == pytest.approx(3.5)
    assert lms[2] == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_lms_outside_table_gives_none(session):
    """За пределами таблицы не экстраполируем — отдаём None."""
    session.add(WhoLms(sex="f", indicator=WEIGHT, age_days=0, l=1.0, m=3.0, s=0.1))
    await session.commit()

    assert await growth.lms_at(session, "f", WEIGHT, 500) is None
    assert await growth.lms_at(session, "f", WEIGHT, growth.MAX_AGE_DAYS + 1) is None


@pytest.mark.asyncio
async def test_lms_is_sex_specific(session, child):
    """Таблицы для девочек и мальчиков разные — перепутать их нельзя."""
    session.add(WhoLms(sex="m", indicator=WEIGHT, age_days=0, l=L0, m=M0, s=S0))
    await session.commit()

    assert await growth.lms_at(session, "f", WEIGHT, 0) is None
    assert await growth.lms_at(session, "m", WEIGHT, 0) is not None


# --- Потеря веса от рождения ----------------------------------------------


def test_loss_from_birth(child):
    """Порог 10% — из клинрекомендаций «Здоровый новорождённый»."""
    assert growth.loss_from_birth(child, 3420) == pytest.approx(0)
    assert growth.loss_from_birth(child, 3078) == pytest.approx(10)
    # Прибавка — отрицательная «потеря»
    assert growth.loss_from_birth(child, 3600) < 0


def test_loss_without_birth_weight(child):
    """Веса при рождении может не быть — тогда и считать нечего."""
    child.birth_weight_g = None
    assert growth.loss_from_birth(child, 3500) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("garbage", [0, -100])
async def test_garbage_measurement_gives_no_percentile(session, child, born, garbage):
    """Неположительный вес — отказ считать, а не абсурдный центиль.

    Формула LMS на нуле не падает: она отдаёт z около −9, и бот показал бы
    «⚠️ ниже обычного коридора» вместо честного «проверьте число». Отрицательное
    значение при дробном L уже роняет возведение в степень. До сюда ни то, ни
    другое дойти не должно — ввод проверяется на границах правдоподобия, — но
    расчёт центилей обязан быть устойчив сам по себе.
    """
    session.add(WhoLms(sex="f", indicator=WEIGHT, age_days=0, l=L0, m=M0, s=S0))
    await session.commit()
    assert await growth.evaluate(session, child, WEIGHT, garbage, born) is None


def test_corrected_age_keeps_the_fever_window_open_for_preterm():
    """Недоношенный дольше остаётся «младше трёх месяцев» — и это правильно.

    Тест сторожит выбор возраста в `handlers/alarm.py`. Там стоял
    хронологический, и у ребёнка с 34 недель гестации окно порога 38.0
    закрывалось на три недели раньше срока. Ошибка шла в сторону молчания.
    """
    from datetime import datetime
    from types import SimpleNamespace

    born = datetime(2026, 9, 1, 4, 20)
    preterm = SimpleNamespace(born_at=born, gestation_weeks=34)
    term = SimpleNamespace(born_at=born, gestation_weeks=40)

    at = born + timedelta(days=95)
    assert growth.age_days(preterm, at) == 95
    # Доношенным считается срок от 37 полных недель, поэтому поправка —
    # 37 − 34 = три недели, а не «до сорока»
    assert growth.corrected_age_days(preterm, at) == 95 - 21
    assert growth.corrected_age_days(term, at) == 95

    from bot.services import redflags as rf

    limit = rf.THRESHOLDS["fever_age_days"]
    assert growth.age_days(preterm, at) > limit, "по паспорту окно уже закрыто"
    assert growth.corrected_age_days(preterm, at) <= limit, "по сроку — ещё открыто"
