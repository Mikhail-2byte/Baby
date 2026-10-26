"""Графики: вес с центильными коридорами ВОЗ и ритм суток за неделю.

Два решения, которые стоит назвать вслух.

**Объектный API matplotlib, а не `pyplot`.** `pyplot` держит глобальный реестр
фигур и не потокобезопасен, а рисуем мы из `asyncio.to_thread`. С `Figure` и
`FigureCanvasAgg` глобального состояния нет вовсе — заодно исчезает и классическая
утечка «забыли `plt.close`», которая за месяц работы съедает память.

**Рендер — всегда в отдельном потоке.** Матплотлиб на паре тысяч точек думает
десятки миллисекунд, и в event loop это ровно тот симптом, от которого стоит
heartbeat: `getUpdates` встаёт, процесс жив, бот молчит.

Оформление следует общим правилам: тонкие штрихи, хайрлайновая сетка,
прямые подписи вместо легенды там, где серия одна. Палитра проверена на
различимость при дальтонизме (ΔE 24.7 при пороге 8).
"""

import asyncio
import io
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import FEED_KINDS, SLEEP, TIMED_KINDS, WEIGHT, Child, Family
from bot.services import growth
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

# --- Палитра --------------------------------------------------------------
# Роли, а не «цвет графика»: слот 1 — сон, слот 2 — вес и кормления. Порядок
# слотов и есть механизм различимости при дальтонизме, менять его нельзя
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
NIGHT = "#f0efec"

SERIES_SLEEP = "#2a78d6"  # слот 1, синий
SERIES_FEED = "#eb6834"  # слот 2, оранжевый

# Центильные коридоры — последовательная шкала одного тона, от светлого края к
# тёмной середине. Крупные заливки обязаны оставаться фоном: насыщенный цвет
# приберегаем для данных, иначе коридор перекричит саму линию веса
BAND_OUTER = "#e3eefc"  # ±2 SD
BAND_INNER = "#cde2fb"  # ±1 SD
BAND_MEDIAN = "#86b6ef"

# Рисуем «в CSS-пикселях»: размер задаём так, будто дюйм это 100 пикселей, а
# печатаем вдвое плотнее. Тогда `linewidth=2` читается как двухпиксельная линия
# из общих правил, а картинка остаётся чёткой на телефоне
DPI = 200
FIGSIZE = (8.0, 4.5)

# Кириллица в этом шрифте есть, и он же стоит у matplotlib по умолчанию.
# Задаём явно на случай, если системные настройки подменят набор: получить
# ряды квадратиков вместо подписей на графике для врача не хочется
FONT_FAMILY = "DejaVu Sans"


@dataclass
class Band:
    """Центильный коридор на один день возраста."""

    day: int
    sd2neg: float
    sd1neg: float
    median: float
    sd1: float
    sd2: float


def _render(figure: Figure) -> bytes:
    """Фигура → PNG в память. Файлы на диск не пишем — они никому не нужны."""
    buffer = io.BytesIO()
    FigureCanvasAgg(figure).print_png(buffer)
    return buffer.getvalue()


def _style(axes: Axes) -> None:
    """Общая отделка: сетка хайрлайном, рамка убрана, подписи приглушены.

    Сетка сплошная, а не пунктирная: пунктир добавляет шум и спорит с данными.
    """
    axes.set_facecolor(SURFACE)
    axes.grid(True, color=GRID, linewidth=0.8)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color(AXIS)
        axes.spines[side].set_linewidth(0.8)
    axes.tick_params(colors=INK_MUTED, labelsize=9, length=0)


# --- Вес ------------------------------------------------------------------


async def collect_weight(
    session: AsyncSession, child: Child, *,
    since: datetime | None = None, until: datetime | None = None,
) -> tuple[list[tuple[int, float]], list[Band], tuple[float, float] | None]:
    """Точки взвешиваний, коридоры ВОЗ на тот же диапазон и центиль последней.

    Точка — это (возраст в днях, граммы). Возраст скорректированный, тот же,
    по которому считается центиль: иначе у недоношенного ребёнка точки легли бы
    не на свой коридор.

    Всё обращение к БД — здесь; рисование получает готовые числа и про базу не
    знает. Иначе ленивая подгрузка уехала бы в чужой поток вместе с сессией.
    """
    rows = await repo.measurements(session, child.id, WEIGHT)
    rows = [row for row in rows
            if (since is None or row.taken_at >= since)
            and (until is None or row.taken_at < until)]
    if not rows:
        return [], [], None

    points = [
        (growth.corrected_age_days(child, row.taken_at), row.value) for row in rows
    ]

    first_day = growth.corrected_age_days(child, rows[0].taken_at)
    last_day = growth.corrected_age_days(child, rows[-1].taken_at)
    # Хвост вперёд: коридор, обрывающийся ровно на последней точке, выглядит
    # так, будто ребёнок дошёл до края нормы. Неделя запаса это снимает
    last_day = max(last_day + 7, first_day + 14)

    # Один запрос на весь диапазон, а не `lms_at` в цикле: на годовалом ребёнке
    # цикл давал около четырёхсот обращений к базе на одну картинку — и это при
    # том, что рисование идёт из `asyncio.to_thread`, а запросы из event loop
    table = await growth.lms_range(session, child.sex, WEIGHT, first_day, last_day)

    bands: list[Band] = []
    for day in range(first_day, last_day + 1):
        lms = table.get(day)
        if lms is None:
            continue
        bands.append(
            Band(
                day=day,
                sd2neg=growth.sd_at(*lms, -2) * 1000,
                sd1neg=growth.sd_at(*lms, -1) * 1000,
                median=growth.sd_at(*lms, 0) * 1000,
                sd1=growth.sd_at(*lms, 1) * 1000,
                sd2=growth.sd_at(*lms, 2) * 1000,
            )
        )

    evaluated = await growth.evaluate(
        session, child, WEIGHT, rows[-1].value, rows[-1].taken_at
    )
    return points, bands, evaluated


def draw_weight(
    child_name: str,
    points: list[tuple[int, float]],
    bands: list[Band],
    evaluated: tuple[float, float] | None,
) -> bytes:
    """Вес по дням на фоне центильных коридоров ВОЗ.

    Ось x — возраст в днях, а не даты. Коридоры ВОЗ определены именно на
    возрасте, и любой пересчёт в даты добавил бы шаг, на котором можно
    ошибиться; к тому же в первые месяцы родители и сами считают днями.

    Легенды нет намеренно: серия одна, её называет заголовок, а границы
    коридоров подписаны прямо у края — это читается быстрее, чем сверка с
    отдельным блоком.
    """
    figure = Figure(figsize=FIGSIZE, dpi=DPI, facecolor=SURFACE)
    axes = figure.add_subplot(111)
    _style(axes)

    if bands:
        days = [band.day for band in bands]
        axes.fill_between(
            days, [b.sd2neg for b in bands], [b.sd2 for b in bands],
            color=BAND_OUTER, linewidth=0,
        )
        axes.fill_between(
            days, [b.sd1neg for b in bands], [b.sd1 for b in bands],
            color=BAND_INNER, linewidth=0,
        )
        axes.plot(days, [b.median for b in bands], color=BAND_MEDIAN, linewidth=1.2)

        # Прямые подписи у правого края вместо легенды
        edge = days[-1]
        for value, label in (
            (bands[-1].sd2, "+2 SD"),
            (bands[-1].median, "медиана ВОЗ"),
            (bands[-1].sd2neg, "−2 SD"),
        ):
            axes.annotate(
                label, (edge, value), xytext=(4, 0), textcoords="offset points",
                color=INK_MUTED, fontsize=8, va="center", family=FONT_FAMILY,
            )

    if points:
        ages = [age for age, _ in points]
        values = [value for _, value in points]
        axes.plot(
            ages, values,
            color=SERIES_FEED, linewidth=2.0,
            marker="o", markersize=5, markerfacecolor=SURFACE,
            markeredgewidth=2.0, markeredgecolor=SERIES_FEED,
            zorder=5,
        )
        # Подписываем только последнюю точку: число на каждой превращает
        # график в таблицу и перестаёт читаться
        caption = f"{int(round(values[-1]))} г"
        if evaluated is not None:
            caption += f" · {evaluated[1]:.0f}-й центиль"
        axes.annotate(
            caption, (ages[-1], values[-1]), xytext=(0, 12),
            textcoords="offset points", color=INK, fontsize=10,
            fontweight="bold", ha="center", zorder=6, family=FONT_FAMILY,
        )

    axes.set_title(
        f"Вес · {child_name}", color=INK, fontsize=13, fontweight="bold",
        loc="left", pad=12, family=FONT_FAMILY,
    )
    axes.set_xlabel("возраст, дней", color=INK_MUTED, fontsize=9, family=FONT_FAMILY)
    axes.set_ylabel("граммы", color=INK_MUTED, fontsize=9, family=FONT_FAMILY)
    axes.yaxis.set_major_formatter(lambda value, _: f"{int(value):,}".replace(",", " "))

    # Правый отступ ровно под подписи коридоров: больше — и справа повиснет
    # пустая сетка, которую читают как «данные кончились раньше, чем норма»
    if bands:
        span = bands[-1].day - bands[0].day
        axes.set_xlim(bands[0].day, bands[-1].day + max(3, span * 0.14))
    figure.tight_layout()
    return _render(figure)


async def weight_png(
    session: AsyncSession, child: Child, now: datetime
) -> bytes | None:
    """Готовый PNG или `None`, если рисовать нечего."""
    points, bands, evaluated = await collect_weight(session, child)
    if not points:
        return None
    return await asyncio.to_thread(draw_weight, child.name, points, bands, evaluated)


# --- Ритм суток -----------------------------------------------------------


@dataclass
class RhythmSpan:
    """Период в часах от полуночи; неточное окончание помечается штриховкой."""

    start: float
    end: float
    uncertain: bool = False


@dataclass
class DayRhythm:
    """Сутки: периоды сна и кормления; точки без известной длительности."""

    day: date
    sleeps: list[RhythmSpan]
    feeds: list[RhythmSpan]
    feed_points: list[float] = field(default_factory=list)

    def has_data(self) -> bool:
        return bool(self.sleeps or self.feeds or self.feed_points)


async def collect_rhythm(
    session: AsyncSession, child: Child, tz: str, now: datetime, days: int = 7,
    *, ending_on: date | None = None,
) -> list[DayRhythm]:
    """Последние `days` суток, каждое событие обрезано по границам своих суток."""
    today = ending_on or tu.local_today(tz, now)
    result: list[DayRhythm] = []

    for offset in range(days - 1, -1, -1):
        target = today - timedelta(days=offset)
        start, end = tu.day_bounds(target, tz)
        events = await repo.events_between(session, child.id, start, end)

        sleeps: list[RhythmSpan] = []
        feeds: list[RhythmSpan] = []
        feed_points: list[float] = []
        for event in events:
            if event.kind == SLEEP or (event.kind in FEED_KINDS and event.kind in TIMED_KINDS):
                began = max(event.started_at, start)
                over = min(event.ended_at or now, end, now)
                if over > began:
                    spans = sleeps if event.kind == SLEEP else feeds
                    spans.append(RhythmSpan(
                        _hours(began, start), _hours(over, start),
                        uncertain=event.estimated or event.ended_at is None,
                    ))
            elif event.kind in FEED_KINDS and start <= event.started_at < end:
                if event.started_at <= now:
                    feed_points.append(_hours(event.started_at, start))

        result.append(DayRhythm(
            day=target, sleeps=sleeps, feeds=feeds, feed_points=feed_points,
        ))
    return result


def _hours(moment: datetime, start: datetime) -> float:
    return (moment - start).total_seconds() / 3600


def draw_rhythm(
    child_name: str, rhythm: list[DayRhythm], *, partial_day: date | None = None
) -> bytes:
    """Полная шкала суток; кормления и сон на отдельных дорожках каждой даты."""
    height = max(3.4, 0.72 * len(rhythm) + 1.9)
    figure = Figure(figsize=(12.0, height), dpi=DPI, facecolor=SURFACE)
    axes = figure.add_subplot(111)
    _style(axes)
    axes.grid(True, axis="x", color=GRID, linewidth=0.8)
    axes.set_xticks(range(25), minor=True)
    axes.grid(True, which="minor", axis="x", color=GRID, linewidth=0.35)
    axes.grid(False, axis="y")

    # Ночь фоном. Это не третья серия, а разметка оси — как затенённые выходные
    # на биржевом графике: помогает читать, ничего не кодирует
    axes.axvspan(0, 7, color=NIGHT, linewidth=0, zorder=0)
    axes.axvspan(22, 24, color=NIGHT, linewidth=0, zorder=0)

    for row, entry in enumerate(rhythm):
        feed_y, sleep_y = row - 0.18, row + 0.18
        for y, spans, color, label in (
            (feed_y, entry.feeds, SERIES_FEED, "кормление"),
            (sleep_y, entry.sleeps, SERIES_SLEEP, "сон"),
        ):
            axes.broken_barh([(0, 24)], (y - 0.13, 0.26),
                             facecolors="#e9e8e3", linewidth=0, zorder=1)
            for span in spans:
                axes.broken_barh(
                    [(span.start, span.end - span.start)], (y - 0.13, 0.26),
                    facecolors=color, edgecolors=INK if span.uncertain else color,
                    linewidth=0.5, hatch="////" if span.uncertain else None, zorder=3,
                )
            axes.text(
                -0.012, y, label, transform=axes.get_yaxis_transform(),
                ha="right", va="center", color=INK_MUTED, fontsize=8, family=FONT_FAMILY,
            )
        axes.plot(entry.feed_points, [feed_y] * len(entry.feed_points),
                  linestyle="none", marker="D", markersize=4, color=SERIES_FEED, zorder=4)

    axes.set_yticks(range(len(rhythm)))
    axes.set_yticklabels(
        [f"{e.day:%d.%m.%Y}" for e in rhythm],
        fontsize=9, color=INK, family=FONT_FAMILY,
    )
    axes.tick_params(axis="y", pad=70)
    axes.set_ylim(-0.7, len(rhythm) - 0.3)
    axes.invert_yaxis()  # сегодня внизу, как в ленте чата

    axes.set_xlim(0, 24)
    axes.set_xticks(range(0, 25, 2))
    axes.set_xticklabels([f"{h:02d}:00" for h in range(0, 25, 2)],
                        fontsize=9, family=FONT_FAMILY)
    axes.tick_params(axis="x", labeltop=True, top=False)

    axes.set_title(
        f"Кормления и сон · {child_name}", color=INK, fontsize=14, fontweight="bold",
        loc="left", pad=30, family=FONT_FAMILY,
    )
    axes.legend(
        handles=[
            Patch(facecolor=SERIES_FEED, label="кормление"),
            Patch(facecolor=SERIES_SLEEP, label="сон"),
            Line2D([], [], color=SERIES_FEED, marker="D", linestyle="none",
                   markersize=4, label="без длительности"),
            Patch(facecolor="white", edgecolor=INK, hatch="////",
                  label="окончание неточное / таймер идёт"),
        ],
        loc="upper left", bbox_to_anchor=(0.0, -0.10), ncol=4,
        frameon=False, fontsize=9, labelcolor=INK_MUTED,
    )
    note = "Местное время семьи. Пустые участки: записей нет."
    if any(entry.day == partial_day for entry in rhythm):
        note += " Текущие сутки показаны не полностью."
    figure.text(
        0.23, 0.025, note,
        fontsize=8, color=INK_MUTED, family=FONT_FAMILY,
    )
    figure.subplots_adjust(left=0.23, right=0.97, top=0.85, bottom=0.17)
    return _render(figure)


async def rhythm_png(
    session: AsyncSession, child: Child, family: Family, now: datetime, days: int = 7
) -> bytes | None:
    rhythm = await collect_rhythm(session, child, family.tz, now, days)
    if not any(entry.has_data() for entry in rhythm):
        return None
    return await asyncio.to_thread(
        draw_rhythm, child.name, rhythm, partial_day=tu.local_today(family.tz, now)
    )
