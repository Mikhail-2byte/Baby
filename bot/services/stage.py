"""Что происходит на этой неделе: развитие, занятия, вехи.

Содержание целиком в `content/weeks.yaml`, здесь только выбор периода по
возрасту и сборка текста. Ни одной фразы про развитие в Python нет — то же
правило, что у триажа.

**Ничего не генерируется моделью и не будет.** Это самое читаемое сообщение
бота: оно приходит само, каждую неделю, и читается как «так и должно быть».
Сочинённый текст ошибётся здесь один раз, и доверия к остальному уже не будет.

Возраст берётся **скорректированный** — тот же, по которому считаются
центили. У ребёнка, родившегося на 34-й неделе, вехи сравнивают не с
ровесниками по паспорту: иначе он полгода будет «отставать» ровно на
недоношенность.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from bot.db.models import Child
from bot.services import growth

log = logging.getLogger(__name__)

CONTENT = Path(__file__).resolve().parent.parent / "content" / "weeks.yaml"

# Сколько дней до и после вехи считать «сейчас актуально». Месяц — потому что
# визиты к педиатру и сами вехи привязаны к месяцам, а не к дням
MILESTONE_WINDOW_DAYS = 21


@dataclass(frozen=True)
class Advice:
    """Совет с источником. Без источника совета быть не должно."""

    text: str
    source: str


@dataclass(frozen=True)
class Period:
    """Отрезок жизни, про который у нас есть что сказать."""

    from_day: int
    to_day: int
    title: str
    happening: str
    do: tuple[Advice, ...]
    talk: tuple[Advice, ...]
    safety: tuple[Advice, ...]
    normal: tuple[str, ...]


@dataclass(frozen=True)
class Milestones:
    """Набор вех CDC на один возраст."""

    at_months: int
    social: tuple[str, ...]
    language: tuple[str, ...]
    cognitive: tuple[str, ...]
    movement: tuple[str, ...]


@dataclass(frozen=True)
class MotorWindow:
    name: str
    from_months: float
    to_months: float


def _advice(items: list[dict] | None) -> tuple[Advice, ...]:
    return tuple(
        Advice(text=" ".join(item["text"].split()), source=item["source"])
        for item in (items or [])
    )


def _load() -> tuple[str, tuple[Period, ...], tuple[Milestones, ...], tuple[MotorWindow, ...]]:
    """Прочитать содержание. Ошибка обязана уронить бота на старте.

    Тот же довод, что у триажа: бот, молча потерявший содержание, хуже бота,
    который не запустился, — второе заметно сразу.
    """
    raw: dict[str, Any] = yaml.safe_load(CONTENT.read_text(encoding="utf-8"))

    periods = tuple(
        Period(
            from_day=item["from_day"],
            to_day=item["to_day"],
            title=item["title"],
            happening=" ".join(item["happening"].split()),
            do=_advice(item.get("do")),
            talk=_advice(item.get("talk")),
            safety=_advice(item.get("safety")),
            normal=tuple(item.get("normal", ())),
        )
        for item in raw["periods"]
    )
    milestones = tuple(
        Milestones(
            at_months=item["at_months"],
            social=tuple(item.get("social", ())),
            language=tuple(item.get("language", ())),
            cognitive=tuple(item.get("cognitive", ())),
            movement=tuple(item.get("movement", ())),
        )
        for item in raw["milestones"]
    )
    windows = tuple(
        MotorWindow(
            name=item["name"],
            from_months=float(item["from_months"]),
            to_months=float(item["to_months"]),
        )
        for item in raw["motor_windows"]
    )

    _check_periods(periods)
    return " ".join(raw["milestone_caveat"].split()), periods, milestones, windows


def _check_periods(periods: tuple[Period, ...]) -> None:
    """Периоды обязаны идти подряд и без дыр.

    Дыра означает возраст, на котором бот молча ничего не скажет, — и заметить
    это можно будет только в тот самый день. Проверка на импорте дешевле.
    """
    if not periods:
        raise ValueError("В weeks.yaml нет ни одного периода")
    if periods[0].from_day != 0:
        raise ValueError("Периоды обязаны начинаться с нулевого дня")
    for previous, following in zip(periods, periods[1:], strict=False):
        if following.from_day != previous.to_day + 1:
            raise ValueError(
                f"Разрыв между периодами: {previous.to_day} → {following.from_day}"
            )


CAVEAT, PERIODS, MILESTONES, MOTOR_WINDOWS = _load()

# До какого возраста у нас вообще есть содержание
LAST_DAY = PERIODS[-1].to_day


def period_for(age_days: int) -> Period | None:
    """Период, в который попадает возраст. `None` — содержание кончилось."""
    for period in PERIODS:
        if period.from_day <= age_days <= period.to_day:
            return period
    return None


def milestones_near(age_days: int) -> Milestones | None:
    """Набор вех, если он рядом по возрасту.

    Окно в три недели: вехи привязаны к месячным визитам к педиатру, и
    показывать их ровно в день — значит показать один раз и мимо.
    """
    for item in MILESTONES:
        at_day = item.at_months * 30
        if abs(age_days - at_day) <= MILESTONE_WINDOW_DAYS:
            return item
    return None


def open_windows(age_days: int) -> list[MotorWindow]:
    """Моторные вехи, окно которых сейчас открыто.

    Ширина окон и есть сообщение: «ходит в год» — это середина диапазона от
    восьми до семнадцати месяцев, а не норма, от которой отсчитывают отставание.
    """
    months = age_days / 30.44
    return [w for w in MOTOR_WINDOWS if w.from_months <= months <= w.to_months]


def age_for(child: Child, at: datetime) -> int:
    """Скорректированный возраст в днях — тот же, что у центилей."""
    return growth.corrected_age_days(child, at)


def report(child: Child, now: datetime) -> str | None:
    """Готовый текст про этап, или `None`, если содержание на этот возраст кончилось.

    Живёт в сервисе, а не в хендлере, потому что зовут его двое: команда
    `/stage` и недельный дайджест. Обратный импорт — из сервиса в хендлеры —
    был бы выворачиванием слоёв наизнанку.
    """
    from bot import texts  # локально: `texts` знает про типы этого модуля

    age = age_for(child, now)
    period = period_for(age)
    if period is None:
        return None
    return texts.stage_report(
        child,
        now,
        period=period,
        milestones=milestones_near(age),
        windows=open_windows(age),
        caveat=CAVEAT,
        corrected=age != growth.age_days(child, now),
    )
