"""Центили и z-score по стандартам роста ВОЗ.

Почему кодом, а не через LLM. Это решение из плана, и оно не про экономию:
модели плохо интерполируют по таблицам и уверенно ошибаются в середине
диапазона. Цена ошибки здесь — родители, зря поехавшие к врачу, или, что хуже,
не поехавшие. Формула LMS занимает десять строк и не ошибается никогда.

Данные — таблицы ВОЗ (`WhoLms`), заполняются один раз скриптом `kb/load_who.py`.
Пока таблица пуста, все функции честно отдают `None`, и бот показывает вес без
центиля. Это штатный режим первых дней, а не поломка.

Модуль чистый в том смысле, что не знает про aiogram и не форматирует текст, но
сессию БД принимает: таблицы ВОЗ — это 20 тысяч строк на индикатор, держать их
в памяти процесса ради нескольких обращений в день незачем.
"""

import logging
import math
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import HEAD, LENGTH, WEIGHT, Child, WhoLms

log = logging.getLogger(__name__)

# Таблицы ВОЗ кончаются на пяти годах; дальше стандарт другой (CDC/ВОЗ 5–19).
# Бот про первый год, но границу лучше знать явно, чем экстраполировать молча
MAX_AGE_DAYS = 1856

# Доношенным считается срок от 37 полных недель. Поправку на недоношенность
# вносим только ниже этого порога: у доношенного ребёнка «скорректированный
# возраст» — это тот же возраст, и лишняя арифметика только вносила бы шум
TERM_WEEKS = 37

# Единицы, в которых лежат наши измерения, против единиц таблиц ВОЗ.
# Вес мы храним в граммах, ВОЗ считает в килограммах; длину и голову мы храним
# в миллиметрах, ВОЗ — в сантиметрах. Расхождение на порядок здесь дало бы
# z-score вроде −40, что выглядит как катастрофа, а не как ошибка единиц
TO_WHO = {WEIGHT: 0.001, LENGTH: 0.1, HEAD: 0.1}

# Индикаторы, для которых ВОЗ предписывает «подрезать» хвосты (см. `_zscore`)
CAPPED = frozenset({WEIGHT})


# Границы, за которыми ВОЗ считает измерение биологически невозможным и
# исключает его из расчётов. Числа не наши: это флаги контроля качества данных
# из WHO Child Growth Standards, те же, что применяет WHO Anthro.
#
# Заведены потому, что живая проверка показала: «/weight 400» у
# девятинедельного ребёнка бот принимал и отвечал «0-й центиль ⚠️ ниже
# обычного коридора», то есть выдавал явную опечатку за медицинскую оценку.
# Это тот же случай, ради которого в `evaluate` стоит проверка `value <= 0`,
# только порог не ноль, а зависит от возраста — и возраст у бота есть.
FLAG_Z = {
    WEIGHT: (-6.0, 5.0),
    LENGTH: (-6.0, 6.0),
    HEAD: (-5.0, 5.0),
}


def implausible(z: float, indicator: str) -> bool:
    """Значение за флагами ВОЗ — почти наверняка опечатка, а не ребёнок.

    Намеренно НЕ повод отказать в записи. Отказ выглядит безопаснее, но бьёт
    по единственной семье, которой дневник нужнее всех: у глубоко
    недоношенного ребёнка вес месяца от роду честно даёт z ниже −6, а поле
    «срок при рождении» не заполняется ни одним путём в интерфейсе, так что
    поправку на недоношенность взять неоткуда. Поэтому запись остаётся, а
    меняется только то, что бот говорит: «проверьте число» вместо центиля.
    """
    bounds = FLAG_Z.get(indicator)
    if bounds is None:
        return False
    low, high = bounds
    return not (low <= z <= high)


def age_days(child: Child, at: datetime) -> int:
    """Возраст в полных сутках на момент `at`. Оба времени — UTC без tzinfo."""
    return max(0, (at - child.born_at).days)


def corrected_age_days(child: Child, at: datetime) -> int:
    """Скорректированный возраст: для недоношенного — от предполагаемых родов.

    У ребёнка, родившегося на 34-й неделе, вес и вехи развития сравнивают не с
    ровесниками по паспорту, а с детьми того же постконцептуального возраста —
    иначе он будет «отставать» ровно на недоношенность и все полгода пугать
    родителей низким центилем.

    Доношенный ребёнок и ребёнок без указанного срока гестации получают обычный
    возраст: гадать, когда срока нет, хуже, чем не корректировать.
    """
    plain = age_days(child, at)
    weeks = child.gestation_weeks
    if weeks is None or weeks >= TERM_WEEKS:
        return plain
    return max(0, plain - (TERM_WEEKS - weeks) * 7)


async def lms_at(
    session: AsyncSession, sex: str, indicator: str, days: int
) -> tuple[float, float, float] | None:
    """Коэффициенты L, M, S на возраст `days`, с линейной интерполяцией.

    Таблицы ВОЗ идут по дням, так что интерполяция почти всегда лишняя. Почти —
    потому что «expanded tables» кончаются раньше, чем недельные, и попасть
    между двумя строками всё-таки можно. Отдельный путь на такой случай дешевле,
    чем разбираться потом, почему один ребёнок центиль получает, а другой нет.
    """
    if days < 0 or days > MAX_AGE_DAYS:
        return None

    exact = await session.scalar(
        select(WhoLms).where(
            WhoLms.sex == sex, WhoLms.indicator == indicator, WhoLms.age_days == days
        )
    )
    if exact is not None:
        return exact.l, exact.m, exact.s

    below = await session.scalar(
        select(WhoLms)
        .where(
            WhoLms.sex == sex,
            WhoLms.indicator == indicator,
            WhoLms.age_days < days,
        )
        .order_by(WhoLms.age_days.desc())
        .limit(1)
    )
    above = await session.scalar(
        select(WhoLms)
        .where(
            WhoLms.sex == sex,
            WhoLms.indicator == indicator,
            WhoLms.age_days > days,
        )
        .order_by(WhoLms.age_days)
        .limit(1)
    )
    if below is None or above is None:
        # Таблица не залита, либо возраст вне её пределов — не выдумываем
        return None

    span = above.age_days - below.age_days
    weight = (days - below.age_days) / span
    return (
        below.l + (above.l - below.l) * weight,
        below.m + (above.m - below.m) * weight,
        below.s + (above.s - below.s) * weight,
    )


async def lms_range(
    session: AsyncSession, sex: str, indicator: str, from_days: int, to_days: int
) -> dict[int, tuple[float, float, float]]:
    """Коэффициенты на весь отрезок сразу — один запрос вместо запроса на день.

    Нужна графику: центильные коридоры рисуются по каждому дню диапазона, и
    `lms_at` в цикле давала около четырёхсот обращений к базе на одну картинку.
    Интерполяции здесь нет намеренно — таблицы идут по дням, а дырки на
    графике честнее выдуманной точки.
    """
    rows = await session.scalars(
        select(WhoLms).where(
            WhoLms.sex == sex,
            WhoLms.indicator == indicator,
            WhoLms.age_days >= from_days,
            WhoLms.age_days <= to_days,
        )
    )
    return {row.age_days: (row.l, row.m, row.s) for row in rows}


def sd_at(l: float, m: float, s: float, n: float) -> float:  # noqa: E741
    """Значение показателя на `n` стандартных отклонений от медианы.

    Публичная: по ней `charts.py` строит центильные коридоры. Имя без
    подчёркивания именно поэтому — приватным оно было только на вид.
    """
    if l == 0:
        return m * math.exp(s * n)
    return m * (1 + l * s * n) ** (1 / l)


def zscore(
    # L, M, S — имена коэффициентов из стандарта ВОЗ; переименовывать их значило
    # бы расходиться с формулой, по которой этот код будут сверять
    value_who: float, l: float, m: float, s: float, indicator: str  # noqa: E741
) -> float:
    """z-score по формуле LMS, с подрезкой хвостов там, где её требует ВОЗ.

    Основная формула распределения Бокса–Кокса даёт в хвостах неправдоподобные
    значения: у весовых показателей распределение сильно скошено, и ребёнок с
    весом чуть выше нормы мог бы получить z = +7. Поэтому ВОЗ предписывает за
    пределами ±3 SD переходить на линейную шкалу, где единица z равна расстоянию
    между третьим и вторым SD.

    Для длины и окружности головы распределение близко к нормальному, и там
    подрезка не применяется — это не упущение, а текст стандарта.
    """
    if l == 0:  # noqa: SIM108 — тернарник здесь длиннее строки и читается хуже
        z = math.log(value_who / m) / s
    else:
        z = ((value_who / m) ** l - 1) / (l * s)

    if indicator not in CAPPED or abs(z) <= 3:
        return z

    if z > 3:
        sd3, sd2 = sd_at(l, m, s, 3), sd_at(l, m, s, 2)
        return 3 + (value_who - sd3) / (sd3 - sd2)
    sd3, sd2 = sd_at(l, m, s, -3), sd_at(l, m, s, -2)
    return -3 + (value_who - sd3) / (sd2 - sd3)


def percentile(z: float) -> float:
    """Процентиль из z-score: функция распределения нормального закона."""
    return 100 * 0.5 * (1 + math.erf(z / math.sqrt(2)))


async def evaluate(
    session: AsyncSession,
    child: Child,
    indicator: str,
    value: float,
    at: datetime,
) -> tuple[float, float] | None:
    """(z-score, центиль) для измерения, или `None` если посчитать не из чего.

    `value` — в наших единицах (граммы, миллиметры); перевод в единицы ВОЗ
    происходит здесь, чтобы вызывающий об этом не думал.
    """
    factor = TO_WHO.get(indicator)
    if factor is None:
        return None
    if value <= 0:
        # До сюда такое дойти не должно: ввод проверяется на границах
        # правдоподобия. Но формула LMS на нуле не падает, а отдаёт z около −9,
        # и бот показал бы «⚠️ ниже обычного коридора» вместо «проверьте число»
        return None

    days = corrected_age_days(child, at)
    lms = await lms_at(session, child.sex, indicator, days)
    if lms is None:
        return None

    try:
        z = zscore(value * factor, *lms, indicator)
    except (ValueError, ZeroDivisionError, OverflowError):
        # Мусорное измерение (ноль, отрицательное) не должно ронять панель
        log.warning(
            "Не удалось посчитать z-score: %s=%s, возраст %s дней",
            indicator, value, days,
        )
        return None
    return z, percentile(z)


def loss_from_birth(child: Child, weight_g: float) -> float | None:
    """Потеря веса от рождения в процентах. Положительное число = потеря.

    Порог 10% — из клинических рекомендаций «Здоровый новорождённый»: большая
    потеря за время пребывания в роддоме требует внимания врача. Сам порог
    проверяет `redflags.py`, здесь только арифметика.
    """
    if not child.birth_weight_g:
        return None
    return (child.birth_weight_g - weight_g) / child.birth_weight_g * 100
