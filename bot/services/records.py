"""Правила правки записи: что можно сдвинуть, на сколько и во что превратить.

Чистый слой между `repo` и хендлером — ни базы, ни aiogram. Заведён по образцу
`services/checkin.py` и ради того же самого: **одна и та же функция рисует
кнопки и проверяет то, что пришло обратно с клиента**. Две копии правила
разошлись бы молча, и разошлись бы ровно в ту сторону, в какую дешевле —
кнопки перестали бы соответствовать проверке, а проверка пропустила бы то,
чего кнопкой не нажать.

Главное правило здесь одно, и оно записано в `fits`: **фильтр честнее
клампинга**. Шаг, после которого отрезок становится невозможным, не
превращается в кнопку вовсе. Клампинг молча поменял бы число, которое человек
назвал, — то же решение, что у `checkin.durations_for`.

Второе правило — **форма записи не меняется**. Мгновенную запись нельзя
превратить в длящуюся и наоборот: `ended_at` означает идущий таймер только у
`TIMED_KINDS`, и пересечение границы сломало бы этот инвариант молча. Отсюда
`kinds_for`, а не общий список видов.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

from bot.db.models import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    FEED_BOTTLE,
    FEED_BREAST,
    FEED_SOLID,
    INSTANT_KINDS,
    LEFT,
    PUMP,
    RIGHT,
    SLEEP,
    TIMED_KINDS,
    Event,
)

# Шаги сдвига. «Забыла выключить» — это часы, а не минуты, поэтому час стоит
# в ряду наравне с пятью минутами: тремя тапами закрывается ночной промах, а
# пятиминутным шагом — дневная неточность.
STEPS: tuple[int, ...] = (-60, -15, -5, 5, 15, 60)

STEP_LABELS: dict[int, str] = {
    -60: "−1 ч",
    -15: "−15",
    -5: "−5",
    5: "+5",
    15: "+15",
    60: "+1 ч",
}

# Сколько записей на экране списка. Сутки новорождённого — это до сорока
# событий: десять кормлений, десять подгузников, восемь снов и сцеживания.
# Сорок кнопок Telegram примет, но экран перестанет читаться.
PAGE = 12

START = "s"
END = "e"

# Виды с числом. Это единственные два, у которых бот когда-либо спрашивает
# миллилитры (`track.ASK_AFTER` и ветка `PUMP` в `track._stop`).
VALUE_KINDS = frozenset({FEED_BOTTLE, PUMP})

# Сторона бывает только у груди — ровно как на панели, где L и R стоят только
# на двух кнопках из восьми.
SIDE_KINDS = frozenset({FEED_BREAST})

# Список страницы разнороден: события и взвешивания лежат в нём вперемешку,
# в том порядке, в каком случились. Параметр типа нужен затем, чтобы окно
# возвращалось тем же типом, каким пришло, — иначе вызывающему пришлось бы
# приводить его обратно руками
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Choice:
    """Вариант на экране смены вида: подпись, вид и сторона.

    Сторона здесь полноправна, а не довесок: «🤱 Левая» и «🤱 Правая» — это две
    соседние кнопки панели, перепутать их в полусне обычное дело, и другого
    пути исправить сторону в боте нет вовсе.
    """

    kind: str
    side: str
    label: str


# Зеркало панели, ряд в ряд. Промах «нажала не ту кнопку» лечится нажатием той
# кнопки, которую имели в виду, а не выбором из чужого списка, — поэтому
# порядок здесь тот же, что в `keyboards.panel`, и менять его вразнобой нельзя.
TIMED_CHOICES: tuple[Choice, ...] = (
    Choice(FEED_BREAST, LEFT, "Левая грудь"),
    Choice(FEED_BREAST, RIGHT, "Правая грудь"),
    Choice(FEED_BREAST, "", "🤱 Кормление"),
    Choice(SLEEP, "", "😴 Сон"),
    Choice(PUMP, "", "🥛 Сцеживание"),
)

# Здесь зеркало неполное, и это выигрыш, а не недосмотр: `DIAPER_MIXED` и
# `FEED_SOLID` объявлены в `models.py`, имеют имя и иконку в `texts`, и не
# пишутся ни одной кнопкой — только голосом. Экран смены вида даёт им
# кнопочный путь, не занимая места на панели.
INSTANT_CHOICES: tuple[Choice, ...] = (
    Choice(DIAPER_WET, "", "💧 Мокрый"),
    Choice(DIAPER_DIRTY, "", "💩 Стул"),
    Choice(DIAPER_MIXED, "", "💧💩 Смешанный"),
    Choice(FEED_BOTTLE, "", "🍼 Бутылочка"),
    Choice(FEED_SOLID, "", "🥄 Прикорм"),
)


def _check() -> None:
    """Экраны смены вида обязаны покрывать ровно то, что объявлено в моделях.

    Проверяется на импорте, как у `services/checkin._check`. Вид, забытый в
    списке, не отказывает и не падает — он просто исчезает с экрана, и
    поправить такую запись становится нечем; вид, попавший не в тот список,
    переводит запись через границу формы, а это уже порча данных.
    """
    timed = {choice.kind for choice in TIMED_CHOICES}
    instant = {choice.kind for choice in INSTANT_CHOICES}
    if timed != set(TIMED_KINDS):
        raise ValueError(f"Экран длящихся видов разошёлся с TIMED_KINDS: {timed}")
    if instant != set(INSTANT_KINDS):
        raise ValueError(f"Экран мгновенных видов разошёлся с INSTANT_KINDS: {instant}")
    if timed & instant:
        raise ValueError(f"Вид по обе стороны границы формы: {timed & instant}")


_check()


def is_timed(event: Event) -> bool:
    """Длящаяся ли это запись — по форме, а не по одному лишь виду.

    Закрытая запись длящаяся всегда: `ended_at` проставляют только видам из
    `TIMED_KINDS` (`track._stop`, `checkin._chose_duration`, `voice._save`).
    Смотреть на `ended_at` надёжнее, чем на `kind`: если в базе почему-то
    оказалась мгновенная запись с концом, поменять ей вид на длящийся — меньшее
    из зол, чем оставить её без пути правки вовсе.
    """
    return event.ended_at is not None or event.kind in TIMED_KINDS


def running(event: Event) -> bool:
    """Идущий таймер: конца нет, и вид из тех, у кого он бывает."""
    return event.ended_at is None and event.kind in TIMED_KINDS


def carries_value(kind: str) -> bool:
    return kind in VALUE_KINDS


def carries_side(kind: str) -> bool:
    return kind in SIDE_KINDS


def fields_of(event: Event) -> tuple[str, ...]:
    """Какие времена у записи можно двигать.

    У идущего таймера конца нет — двигать нечего; закончить его можно на панели
    и под подтверждением, и это другое действие, а не правка. У мгновенной
    записи время одно, и это начало.
    """
    if event.ended_at is None:
        return (START,)
    return (START, END)


def preview(
    event: Event, field: str, offset: int
) -> tuple[datetime, datetime | None]:
    """Каким станет отрезок, если сдвинуть `field` на `offset` минут.

    Двигается ровно одна граница, вторая стоит. «Сдвинуть запись целиком» было
    бы третьим режимом на том же экране; выражается двумя правками подряд,
    поэтому режима нет.

    Сдвиг складывается в UTC, а не в локальном времени. В ночь перевода часов
    локальная подпись может уехать на два часа при шаге в один — но прошедшее
    время сдвинется ровно на час, а правится именно оно.
    """
    shift = timedelta(minutes=offset)
    if field == END:
        ended = event.ended_at
        return event.started_at, (ended + shift) if ended is not None else None
    return event.started_at + shift, event.ended_at


def fits(
    started_at: datetime,
    ended_at: datetime | None,
    *,
    born_at: datetime,
    now: datetime,
) -> bool:
    """Отрезок возможен. Единственное место, где записаны все четыре границы.

    * не раньше рождения — иначе `export._csv` напечатает врачу отрицательный
      возраст, а `growth` посчитает центиль по возрасту, которого не было;
    * не в будущем — `stats.clip` берёт `min(ended_at, end)` и засчитал бы
      время, которого ещё не случилось, в сегодняшние сутки;
    * конец строго позже начала — иначе `texts.duration` печатает
      отрицательный интервал, и в сводке он вычитается из суммы;
    * то же для начала: обе границы судятся одинаково.
    """
    if started_at < born_at or started_at > now:
        return False
    if ended_at is None:
        return True
    return born_at <= started_at < ended_at <= now


def steps_for(
    event: Event,
    field: str,
    offset: int,
    *,
    born_at: datetime,
    now: datetime,
) -> tuple[int, ...]:
    """Шаги, после которых отрезок остаётся возможным.

    Считается от **накопленного** сдвига: человек на экране уже сдвинул на
    полчаса, и следующий шаг проверяется от того, что он видит, а не от того,
    что лежит в базе.
    """
    allowed = []
    for step in STEPS:
        started_at, ended_at = preview(event, field, offset + step)
        if fits(started_at, ended_at, born_at=born_at, now=now):
            allowed.append(step)
    return tuple(allowed)


def kinds_for(event: Event) -> tuple[Choice, ...]:
    """Чем эта запись могла быть, не сменив формы.

    Возвращается и текущий вариант тоже: экран повторяет панель целиком, и
    выпавшая из ряда кнопка сбила бы узнавание — человек ищет глазами ту
    раскладку, по которой промахнулся.
    """
    return TIMED_CHOICES if is_timed(event) else INSTANT_CHOICES


def same_choice(event: Event, choice: Choice) -> bool:
    """Это ровно то, чем запись уже является. Точка в подписи, а не отказ."""
    side = event.side or ""
    return event.kind == choice.kind and (
        side == choice.side if carries_side(choice.kind) else True
    )


def allows(event: Event, kind: str) -> bool:
    """Можно ли этой записи стать таким видом.

    Сверяется с тем же списком, который рисовал кнопки: `callback_data`
    приходит с клиента, и подделанный `kind=sleep` у подгузника завёл бы
    таймер задним числом.
    """
    return any(choice.kind == kind for choice in kinds_for(event))


def after_kind_change(kind: str, side: str) -> tuple[str | None, float | None]:
    """Что переживает смену вида: сторона и число.

    Сторону приносит сам выбор (это половина кнопки «🤱 Левая»), а число не
    переживает никогда: `FEED_BOTTLE` и `PUMP` — единственные виды с объёмом —
    лежат по разные стороны границы формы и друг в друга не превращаются.
    Значит любое пережившее число оказалось бы у вида, который его не печатает,
    — и уехало бы в выгрузку для педиатра невидимым для всех экранов бота.

    `subtype` и `note` не трогаются вовсе: это слова человека из голосового
    разбора, и стереть их молча было бы потерей, а не уборкой.
    """
    return (side or None) if carries_side(kind) else None, None


def page_of(rows: list[T], page: int) -> tuple[list[T], int, bool, bool]:
    """Окно списка: сами строки, номер первой, есть ли соседи.

    Номер первой возвращается затем, чтобы подписи «1…12» на второй странице
    стали «13…24»: человек, ушедший назад и вернувшийся, обязан увидеть те же
    номера у тех же записей.
    """
    if not rows:
        return [], 1, False, False
    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE
    window = rows[start : start + PAGE]
    return window, start + 1, page > 0, page < pages - 1
