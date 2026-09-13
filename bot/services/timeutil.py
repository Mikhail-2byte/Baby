"""Конвертация и форматирование времени.

В БД всё лежит в UTC без tzinfo. Таймзона семьи применяется только здесь —
на границе ввода и вывода. Модуль чистый: никаких обращений к БД и aiogram.

Неоднозначные моменты перевода часов (час, который случается дважды) трактуются
как первый из двух — `fold=0`, поведение Python по умолчанию.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MONTHS_SHORT = (
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
)
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def tz_of(tz_name: str) -> ZoneInfo:
    return ZoneInfo(tz_name)


def now_utc() -> datetime:
    """Текущий момент в UTC без tzinfo — в том же виде, в каком лежит в БД."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_utc(local_dt: datetime, tz_name: str) -> datetime:
    """Наивное локальное время семьи → наивный UTC."""
    aware = local_dt.replace(tzinfo=tz_of(tz_name))
    return aware.astimezone(UTC).replace(tzinfo=None)


def to_local(utc_dt: datetime, tz_name: str) -> datetime:
    """Наивный UTC из БД → наивное локальное время семьи."""
    aware = utc_dt.replace(tzinfo=UTC)
    return aware.astimezone(tz_of(tz_name)).replace(tzinfo=None)


def local_today(tz_name: str, now: datetime | None = None) -> date:
    return to_local(now or now_utc(), tz_name).date()


def parse_hhmm(value: str) -> time:
    """'08:00' → time(8, 0). Бросает ValueError на мусоре."""
    hours, minutes = value.strip().split(":")
    return time(int(hours), int(minutes))


def day_bounds(day: date, tz_name: str) -> tuple[datetime, datetime]:
    """Границы локальных суток в UTC. Полуинтервал [начало, конец)."""
    start = to_utc(datetime.combine(day, time.min), tz_name)
    end = to_utc(datetime.combine(day + timedelta(days=1), time.min), tz_name)
    return start, end


def at_local_time(day: date, moment: time, tz_name: str) -> datetime:
    """Локальные «27 августа, 08:00» → наивный UTC. Момент дайджеста."""
    return to_utc(datetime.combine(day, moment), tz_name)


def week_bounds(day: date, tz_name: str) -> tuple[datetime, datetime]:
    """Границы недели (с понедельника), в которую попадает `day`."""
    monday = day - timedelta(days=day.weekday())
    start, _ = day_bounds(monday, tz_name)
    _, end = day_bounds(monday + timedelta(days=6), tz_name)
    return start, end


def day_stamp(day: date) -> str:
    """'27 авг' — короткая дата без года и без слов «завтра»/«вчера»."""
    return f"{day.day} {MONTHS_SHORT[day.month - 1]}"


def day_label(target: date, today: date) -> str | None:
    """Человеческое имя дня или None, если такого нет."""
    delta = (target - today).days
    return {
        -2: "позавчера",
        -1: "вчера",
        0: "сегодня",
        1: "завтра",
        2: "послезавтра",
    }.get(delta)


def _date_part(target: date, today: date, *, with_label: bool = True) -> str:
    """'завтра, 27 авг' / 'пт, 29 авг' / '3 мар 2027'."""
    stamp = day_stamp(target)
    if target.year != today.year:
        stamp += f" {target.year}"

    label = day_label(target, today) if with_label else None
    if label == "сегодня":
        return label
    if label:
        return f"{label}, {stamp}"
    if 0 < (target - today).days < 7:
        return f"{WEEKDAYS_SHORT[target.weekday()]}, {stamp}"
    return stamp


def fmt_due(
    utc_dt: datetime, tz_name: str, *, all_day: bool = False, now: datetime | None = None
) -> str:
    """Срок записи: 'сегодня, 19:00', 'завтра, 27 авг, 19:00', 'пт, 29 авг'."""
    local = to_local(utc_dt, tz_name)
    today = local_today(tz_name, now)
    stamp = _date_part(local.date(), today)
    if all_day:
        return stamp
    return f"{stamp}, {local:%H:%M}"


def fmt_when(utc_dt: datetime, tz_name: str, now: datetime | None = None) -> str:
    """Когда что-то произошло: 'вчера в 21:14', '26 авг в 21:14'."""
    local = to_local(utc_dt, tz_name)
    today = local_today(tz_name, now)
    label = day_label(local.date(), today)
    stamp = label or _date_part(local.date(), today, with_label=False)
    return f"{stamp} в {local:%H:%M}"
