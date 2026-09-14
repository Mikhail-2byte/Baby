"""Календарь прививок и осмотров, и пресеты напоминаний.

Содержание целиком лежит в `content/schedule_ru.yaml`: здесь нет ни одной
даты прививки, ни одной дозы и ни одного названия врача. Модуль умеет ровно
две вещи — прочитать файл, придирчиво его проверив, и посчитать от дня
рождения ребёнка, когда именно что предстоит.

**Возраст здесь паспортный, а не скорректированный.** Это противоположно
правилу `growth.corrected_age_days`, по которому считаются центили и вехи
развития, и так и задумано: приказ 1122н привязывает прививки к возрасту от
рождения. Недоношенность меняет в нём вид вакцины (п. 12 — инактивированная
вместо живой), но не срок. Поэтому `growth` тут не импортируется вовсе:
нечего и перепутать.

Месяцы считаются календарными, через `relativedelta`, а не тридцатью сутками.
«Три месяца» от 30 ноября — это 28 февраля; педиатр считает месяцами, и
расхождение в неделю к полугоду семья заметит раньше, чем мы.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml
from dateutil.relativedelta import relativedelta

from bot.db.models import Child
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

CONTENT = Path(__file__).resolve().parent.parent / "content" / "schedule_ru.yaml"

# Докуда файл обязан молчать. Оба приказа идут дальше — до 14 и 17 лет, — но
# бот про первый год жизни, и граница проверяется тестом, а не обещанием
HORIZON_MONTHS = 36

# Как часто повторяется пресет. `calendar` — не повтор вовсе: такие пресеты
# раскрываются в пачку разовых напоминаний по датам из соответствующей секции
HOURLY = "hourly"
DAILY = "daily"
WEEKLY = "weekly"
CALENDAR = "calendar"
EVERY = frozenset({HOURLY, DAILY, WEEKLY, CALENDAR})


@dataclass(frozen=True)
class Source:
    """Документ, на который ссылается запись. Без него записи быть не должно."""

    id: str
    title: str
    publisher: str
    year: int
    url: str


@dataclass(frozen=True)
class Preset:
    """То, что включается одним тапом в `/remind`."""

    id: str
    icon: str
    title: str
    text: str
    every: str
    at: time | None
    choices: tuple[int, ...]
    default_hours: int
    section: str
    source: str


@dataclass(frozen=True)
class Entry:
    """Одна строка таблицы приказа: возраст и что в этом возрасте предстоит."""

    age_months: int
    age_days: int
    age_label: str
    items: tuple[str, ...]
    doctors: tuple[str, ...]
    tests: tuple[str, ...]
    note: str
    in_hospital: bool
    source: str


@dataclass(frozen=True)
class Section:
    """Календарь целиком: строки плюс правило, за сколько дней напоминать."""

    id: str
    entries: tuple[Entry, ...]
    remind_before_days: int
    at: time


def _strings(raw: Any) -> tuple[str, ...]:
    return tuple(" ".join(str(item).split()) for item in (raw or ()))


def _entry(item: dict[str, Any], section_id: str) -> Entry:
    if "age_months" not in item and "age_days" not in item:
        label = item.get("age_label")
        raise ValueError(
            f"{CONTENT.name}, {section_id}: у записи «{label}» не указан возраст — "
            "нужно age_months, age_days или оба"
        )
    return Entry(
        age_months=int(item.get("age_months", 0)),
        age_days=int(item.get("age_days", 0)),
        age_label=item["age_label"],
        items=_strings(item.get("items")),
        doctors=_strings(item.get("doctors")),
        tests=_strings(item.get("tests")),
        note=" ".join(item.get("note", "").split()),
        in_hospital=bool(item.get("in_hospital", False)),
        source=item["source"],
    )


def _section(raw: dict[str, Any], section_id: str) -> Section:
    entries = tuple(_entry(item, section_id) for item in raw["entries"])
    if not entries:
        raise ValueError(f"{CONTENT.name}: секция {section_id} пуста")
    return Section(
        id=section_id,
        entries=entries,
        remind_before_days=int(raw["remind_before_days"]),
        at=tu.parse_hhmm(raw["at"]),
    )


def _age_of(entry: Entry) -> tuple[int, int]:
    """Возраст записи — только чтобы сравнивать записи между собой."""
    return entry.age_months, entry.age_days


def _check_presets(sources: dict[str, Source], presets: tuple[Preset, ...],
                   sections: dict[str, Section]) -> None:
    if not presets:
        raise ValueError(f"{CONTENT.name}: нет ни одного пресета")

    ids = [preset.id for preset in presets]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{CONTENT.name}: повторяющиеся id пресетов")

    for preset in presets:
        if preset.every not in EVERY:
            raise ValueError(
                f"{CONTENT.name}: у пресета {preset.id} неизвестное every={preset.every!r}"
            )
        if preset.every in {DAILY, WEEKLY} and preset.at is None:
            raise ValueError(f"{CONTENT.name}: у пресета {preset.id} не указано время at")
        if preset.every == HOURLY and not preset.choices:
            raise ValueError(f"{CONTENT.name}: у пресета {preset.id} не указаны choices")
        if preset.every == HOURLY and preset.default_hours not in preset.choices:
            raise ValueError(f"{CONTENT.name}: у пресета {preset.id} default_hours вне choices")
        if preset.every == CALENDAR and preset.section not in sections:
            raise ValueError(
                f"{CONTENT.name}: пресет {preset.id} ссылается на секцию "
                f"{preset.section!r}, которой нет"
            )
        if preset.every != CALENDAR and not preset.text:
            raise ValueError(f"{CONTENT.name}: у пресета {preset.id} нет текста")
        if preset.source not in sources:
            raise ValueError(
                f"{CONTENT.name}: пресет {preset.id} ссылается на источник "
                f"{preset.source!r}, которого нет в sources"
            )


def _check_sections(sources: dict[str, Source], sections: dict[str, Section]) -> None:
    for section in sections.values():
        previous = (-1, -1)
        for entry in section.entries:
            if entry.source not in sources:
                raise ValueError(
                    f"{CONTENT.name}, {section.id}: запись «{entry.age_label}» ссылается "
                    f"на источник {entry.source!r}, которого нет в sources"
                )
            age = _age_of(entry)
            if age <= previous:
                raise ValueError(
                    f"{CONTENT.name}, {section.id}: записи обязаны идти по возрастанию "
                    f"возраста и без повторов, а «{entry.age_label}» нарушает порядок"
                )
            if entry.age_months > HORIZON_MONTHS:
                raise ValueError(
                    f"{CONTENT.name}, {section.id}: запись «{entry.age_label}» старше "
                    f"{HORIZON_MONTHS} месяцев — это за горизонтом файла"
                )
            previous = age


def _load() -> tuple[dict[str, Source], tuple[Preset, ...], dict[str, Section]]:
    """Прочитать содержание. Ошибка обязана уронить бота на старте.

    Тот же довод, что у триажа и у содержания по неделям: бот, молча потерявший
    календарь, хуже бота, который не запустился. Второе видно сразу, первое —
    в день пропущенной прививки.
    """
    raw: dict[str, Any] = yaml.safe_load(CONTENT.read_text(encoding="utf-8"))

    sources = {
        item["id"]: Source(
            id=item["id"],
            title=item["title"],
            publisher=item["publisher"],
            year=int(item["year"]),
            url=item["url"],
        )
        for item in raw["sources"]
    }

    sections = {name: _section(raw[name], name) for name in ("vaccines", "checkups")}

    presets = tuple(
        Preset(
            id=item["id"],
            icon=item["icon"],
            title=item["title"],
            text=" ".join(item.get("text", "").split()),
            every=item["every"],
            at=tu.parse_hhmm(item["at"]) if item.get("at") else None,
            choices=tuple(int(hours) for hours in item.get("choices", ())),
            default_hours=int(item.get("default_hours", 0)),
            section=item.get("section", ""),
            source=item["source"],
        )
        for item in raw["presets"]
    )

    _check_presets(sources, presets, sections)
    _check_sections(sources, sections)
    return sources, presets, sections


SOURCES, PRESETS, SECTIONS = _load()
BY_ID = {preset.id: preset for preset in PRESETS}


def section_of(preset: Preset) -> Section | None:
    """Календарь, который раскрывает пресет. У повторяющихся его нет."""
    return SECTIONS.get(preset.section) if preset.every == CALENDAR else None


def entry_day(child: Child, entry: Entry, tz: str) -> date:
    """Дата, на которую приходится запись календаря.

    День рождения берётся в зоне семьи: `born_at` — момент в UTC, и роды в
    23:40 по Москве сдвинули бы весь календарь на сутки назад.
    """
    born = tu.to_local(child.born_at, tz).date()
    return born + relativedelta(months=entry.age_months, days=entry.age_days)


def fire_at(section: Section, entry: Entry, child: Child, tz: str) -> datetime:
    """Когда напомнить: за `remind_before_days` до даты, в локальное время."""
    day = entry_day(child, entry, tz) - timedelta(days=section.remind_before_days)
    return tu.at_local_time(day, section.at, tz)


def upcoming(
    section: Section, child: Child, tz: str, now: datetime
) -> tuple[tuple[Entry, datetime], ...]:
    """Записи, о которых ещё есть смысл напомнить.

    Роддомовские пропускаются: гепатит B в первые сутки и БЦЖ делают до того,
    как семья узнала про бота. Прошедшие тоже: `fire_at <= now` тикер разберёт
    как догонку и выстрелит немедленно — сюрприз на пустом месте.
    """
    ahead = []
    for entry in section.entries:
        if entry.in_hospital:
            continue
        moment = fire_at(section, entry, child, tz)
        if moment > now:
            ahead.append((entry, moment))
    return tuple(ahead)


def timeline(
    section: Section, child: Child, tz: str, now: datetime
) -> tuple[tuple[Entry, date, bool], ...]:
    """Весь календарь с датами и пометкой «уже прошло» — для показа целиком."""
    today = tu.local_today(tz, now)
    return tuple(
        (entry, day, day < today)
        for entry in section.entries
        for day in (entry_day(child, entry, tz),)
    )


def rrule_of(preset: Preset, hours: int | None = None) -> str | None:
    """Правило повтора по RFC 5545. У календарных пресетов его нет.

    Синтаксис собирается здесь, а не лежит в YAML: `FREQ=HOURLY;INTERVAL=3`
    не читается человеком, ради которого файл содержания и заведён.
    """
    if preset.every == HOURLY:
        return f"FREQ=HOURLY;INTERVAL={hours or preset.default_hours}"
    if preset.every == DAILY:
        return "FREQ=DAILY"
    if preset.every == WEEKLY:
        return "FREQ=WEEKLY"
    return None


def first_fire_at(
    preset: Preset, tz: str, now: datetime, hours: int | None = None
) -> datetime:
    """Первое срабатывание после включения.

    У почасового — через интервал от «сейчас»: кнопку нажимают сразу после
    кормления. У остальных — ближайшее указанное время, сегодня или в
    следующий раз, но всегда в будущем.
    """
    if preset.every == HOURLY:
        return now + timedelta(hours=hours or preset.default_hours)

    moment = preset.at or time(10, 0)
    today = tu.local_today(tz, now)
    planned = tu.at_local_time(today, moment, tz)
    if planned > now:
        return planned
    ahead = 7 if preset.every == WEEKLY else 1
    return tu.at_local_time(today + timedelta(days=ahead), moment, tz)
