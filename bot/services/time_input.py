"""Строгий локальный разбор времени. Не читает переписку и не вызывает LLM."""
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from bot.services import timeutil as tu

CLOCK = r"\d{1,2}:\d{2}"
MOMENT = re.compile(rf"(?:(сегодня|вчера|\d{{4}}-\d{{2}}-\d{{2}})\s+)?(?:в\s+)?({CLOCK})")
RELATIVE = re.compile(r"(\d{1,5})\s*(?:минут(?:у|ы)?|мин\.?)\s+назад")
FINISH = re.compile(
    r"(закончили кормить|закончили кормление|закончили|закончила|закончил|"
    r"проснулась|проснулся|проснулись)\s+(?:в\s+)?(.+)", re.IGNORECASE,
)
INTERVAL = re.compile(
    rf"(?:(сегодня|вчера|\d{{4}}-\d{{2}}-\d{{2}})\s+)?"
    rf"(кормили|кормила|кормил|кормление|сон|спала|спал)"
    rf"(?:\s+(левой|правой)(?:\s+грудью)?)?\s+с\s+({CLOCK})\s+до\s+({CLOCK})",
    re.IGNORECASE,
)


class TimeInputError(ValueError):
    """Безопасное сообщение человеку, данные не изменены."""


@dataclass(frozen=True)
class Interval:
    kind: str
    side: str | None
    started_at: datetime
    ended_at: datetime


def clean(text: str) -> str:
    return " ".join(text.lower().strip().rstrip(".! ").split())


def finish_request(text: str) -> tuple[str | None, str] | None:
    match = FINISH.fullmatch(clean(text))
    if match is None:
        return None
    verb, value = match.groups()
    if not looks_like_time(value):
        return None
    kind = "sleep" if verb.startswith("просну") else "feed_breast"
    return kind, value


def looks_like_time(text: str) -> bool:
    value = clean(text)
    return bool(MOMENT.fullmatch(value) or RELATIVE.fullmatch(value))


def _clock(value: str) -> time:
    try:
        return tu.parse_hhmm(value)
    except ValueError as exc:
        raise TimeInputError("Нужно время от 00:00 до 23:59, например 14:35.") from exc


def _day(label: str, now: datetime, tz: str) -> date:
    today = tu.local_today(tz, now)
    if label == "сегодня":
        return today
    if label == "вчера":
        return today - timedelta(days=1)
    try:
        return datetime.strptime(label, "%Y-%m-%d").date()
    except ValueError as exc:
        raise TimeInputError("Неверная дата. Пример: 2026-10-26 14:35.") from exc


def moment(
    text: str, *, now: datetime, tz: str, lower: datetime, upper: datetime,
    reference: datetime | None = None,
) -> datetime:
    """UTC без tzinfo. Для правки дата берётся у записи, не у сегодняшнего дня.

    Для открытого таймера reference=None: проверяем сегодня и вчера; если
    допустимы оба дня, не угадываем. Более старую дату можно назвать явно.
    """
    value = clean(text)
    relative = RELATIVE.fullmatch(value)
    if relative:
        result = now - timedelta(minutes=int(relative[1]))
    else:
        match = MOMENT.fullmatch(value)
        if match is None:
            raise TimeInputError(
                "Напишите 14:35, «вчера в 23:50» или «20 минут назад». "
                "Для выхода — «отмена»."
            )
        label, clock = match.groups()
        parsed = _clock(clock)
        if label:
            result = tu.at_local_time(_day(label, now, tz), parsed, tz)
        elif reference is not None:
            result = tu.at_local_time(tu.to_local(reference, tz).date(), parsed, tz)
        else:
            today = tu.local_today(tz, now)
            candidates = [tu.at_local_time(today - timedelta(days=back), parsed, tz)
                          for back in (0, 1)]
            valid = [item for item in candidates if lower < item <= upper]
            if len(valid) > 1:
                raise TimeInputError(
                    "Уточните день: «сегодня в " + clock + "» или «вчера в " + clock + "»."
                )
            if not valid:
                raise TimeInputError(
                    "Это время вне границ записи или в будущем. "
                    "Уточните день и время, например «вчера в 23:50»."
                )
            result = valid[0]
    if not lower < result <= upper:
        raise TimeInputError(
            "Время должно быть после начала записи и не позже её конца или текущего момента."
        )
    return result


def interval(text: str, *, now: datetime, tz: str, born_at: datetime) -> Interval | None:
    match = INTERVAL.fullmatch(clean(text))
    if match is None:
        return None
    label, verb, side, start_text, end_text = match.groups()
    start_clock, end_clock = _clock(start_text), _clock(end_text)
    start_day = _day(label or "сегодня", now, tz)
    end_day = start_day + timedelta(days=int(end_clock < start_clock))
    start = tu.at_local_time(start_day, start_clock, tz)
    end = tu.at_local_time(end_day, end_clock, tz)
    if not born_at <= start < end <= now:
        raise TimeInputError(
            "Интервал должен быть после рождения, окончание — позже начала и не в будущем. "
            "Если начали вчера, напишите: «вчера кормили левой с 23:50 до 00:15»."
        )
    kind = "sleep" if verb in {"сон", "спала", "спал"} else "feed_breast"
    if kind == "sleep" and side is not None:
        raise TimeInputError("Для сна сторона груди не нужна: «сон с 13:10 до 14:00».")
    return Interval(kind, {"левой": "L", "правой": "R"}.get(side), start, end)
