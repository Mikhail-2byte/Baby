"""Периодический опрос «Что было с прошлого раза?».

Бот сам присылает карточку с кнопками, чтобы записать задним числом то, что
не записали в момент события. Дыра, которую это закрывает, названа в проекте
дважды: **кнопка панели умеет записать только «сейчас»**. Ночью кормление и
сон случаются, а тап — нет, и до этого этапа записать их задним числом можно
было только голосом.

**Опрос — не шестой пресет напоминаний, и это главное решение модуля.**
Напоминания догоняются: тикер разбирает опоздание на три ступени и после
суток простоя присылает сводку пропущенного. Опрос догонять нечего и незачем
— «что было с прошлого раза?», заданное с опозданием на двадцать часов, это
вопрос не о том окне, которое имелось в виду. Кроме того, опрос обязан уметь
промолчать, а `reminders` этого не выражают вовсе: строка либо созрела, либо
нет. Поэтому здесь свой шаг тика, ровно по образцу `digest.py`, а тикер
по-прежнему знает про напоминания ровно два случая — разовое и `rrule`.

Отметка «когда спрашивали в последний раз» — колонка `checkin_last_at`, а не
словарь в памяти, и по той же причине, что у дайджеста: домашний ПК
перезапускается чаще, чем проходят три часа.

Содержание — `content/checkin_ru.yaml`. В этом файле нет ни одной подписи
кнопки, ни одного сдвига и ни одной границы ночного окна.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import TIMED_KINDS, Family
from bot.services import sending
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

CONTENT = Path(__file__).resolve().parent.parent / "content" / "checkin_ru.yaml"


@dataclass(frozen=True)
class Kind:
    """Вид на карточке. `durations` пуст у мгновенных — и только у них."""

    kind: str
    icon: str
    label: str
    durations: tuple[int, ...]

    @property
    def title(self) -> str:
        return f"{self.icon} {self.label}"


@dataclass(frozen=True)
class Shift:
    """Сдвиг «когда это было», в минутах назад от момента нажатия."""

    minutes: int
    label: str


@dataclass(frozen=True)
class Card:
    icon: str
    title: str
    short: str
    question: str
    choices: tuple[int, ...]
    default_hours: int
    quiet_from: time
    quiet_to: time
    shifts: tuple[Shift, ...]
    kinds: tuple[Kind, ...]
    nothing_label: str
    mute_label: str
    still_label: str


def _check(card: Card) -> None:
    """Придирчиво проверить содержание. Каждая проверка — от живой поломки."""
    if not card.choices:
        raise ValueError(f"{CONTENT.name}: не указаны choices")
    if card.default_hours not in card.choices:
        raise ValueError(f"{CONTENT.name}: default_hours вне choices")

    if not card.kinds:
        raise ValueError(f"{CONTENT.name}: на карточке нет ни одного вида")

    seen = [kind.kind for kind in card.kinds]
    if len(set(seen)) != len(seen):
        raise ValueError(f"{CONTENT.name}: повторяющиеся виды")

    for kind in card.kinds:
        # Опечатка в виде уедет в `events` навсегда, и ни одна сводка о нём не
        # узнает: `KIND_NAMES` — это и есть список видов, которые бот умеет
        # назвать словами
        if kind.kind not in texts.KIND_NAMES:
            raise ValueError(f"{CONTENT.name}: неизвестный вид {kind.kind!r}")
        # Длящийся вид без длительностей открыл бы таймер задним числом, и
        # тикер закрыл бы его как измеренный. Мгновенному вопрос «сколько
        # длился мокрый подгузник» задавать нечего
        timed = kind.kind in TIMED_KINDS
        if timed and not kind.durations:
            raise ValueError(f"{CONTENT.name}: у вида {kind.kind!r} нет durations")
        if not timed and kind.durations:
            raise ValueError(f"{CONTENT.name}: у мгновенного {kind.kind!r} есть durations")
        if any(minutes <= 0 for minutes in kind.durations):
            raise ValueError(f"{CONTENT.name}: у вида {kind.kind!r} неположительная длительность")

    if not card.shifts:
        raise ValueError(f"{CONTENT.name}: не указаны shifts")
    if card.shifts[0].minutes != 0:
        raise ValueError(f"{CONTENT.name}: первый сдвиг обязан быть «сейчас», то есть 0")
    minutes = [shift.minutes for shift in card.shifts]
    if minutes != sorted(set(minutes)):
        raise ValueError(f"{CONTENT.name}: сдвиги обязаны идти по возрастанию и без повторов")

    # Равные границы — это либо пустое окно, либо круглосуточное, и какое
    # именно, читатель файла не угадает; оба варианта молчат
    if card.quiet_from == card.quiet_to:
        raise ValueError(f"{CONTENT.name}: границы ночного окна совпадают")


def _load() -> Card:
    """Прочитать содержание. Ошибка обязана уронить бота на старте.

    Тот же довод, что у триажа, у содержания по неделям и у календаря: бот,
    молча потерявший карточку, хуже бота, который не запустился. Второе видно
    сразу, первое — в первую же ночь, когда никто ничего не спросил.
    """
    raw: dict[str, Any] = yaml.safe_load(CONTENT.read_text(encoding="utf-8"))

    card = Card(
        icon=raw["icon"],
        title=raw["title"],
        short=raw["short"],
        question=raw["question"],
        choices=tuple(int(hours) for hours in raw["choices"]),
        default_hours=int(raw["default_hours"]),
        quiet_from=tu.parse_hhmm(raw["quiet"]["from"]),
        quiet_to=tu.parse_hhmm(raw["quiet"]["to"]),
        shifts=tuple(
            Shift(minutes=int(item["minutes"]), label=item["label"])
            for item in raw["shifts"]
        ),
        kinds=tuple(
            Kind(
                kind=item["kind"],
                icon=item["icon"],
                label=item["label"],
                durations=tuple(int(value) for value in item.get("durations", ())),
            )
            for item in raw["kinds"]
        ),
        nothing_label=raw["nothing_label"],
        mute_label=raw["mute_label"],
        still_label=raw["still_label"],
    )
    _check(card)
    return card


CARD = _load()
BY_KIND = {kind.kind: kind for kind in CARD.kinds}


# --- Созревание -----------------------------------------------------------


def in_quiet(tz: str, now: datetime) -> bool:
    """Ночь по ЛОКАЛЬНОМУ времени семьи.

    Окно переваливает через полночь, и форма `start <= t < end` для 23:00→07:00
    дала бы пустое множество — то есть ночная проверка не срабатывала бы ни
    разу, и заметить это можно было бы только по разбуженной семье.
    """
    local = tu.to_local(now, tz).time()
    if CARD.quiet_from <= CARD.quiet_to:
        return CARD.quiet_from <= local < CARD.quiet_to
    return local >= CARD.quiet_from or local < CARD.quiet_to


def due(family: Family, now: datetime) -> bool:
    """Пора ли спрашивать. Чистая: базу не трогает.

    Порядок ветвей существен. Ночь проверяется ДО отметки и до всякой записи в
    базу: отметка, поставленная в тихий час, съела бы утренний вопрос молча.
    """
    if family.checkin_hours <= 0:
        return False
    if in_quiet(family.tz, now):
        return False
    if family.checkin_last_at is None:
        return True
    return now - family.checkin_last_at >= timedelta(hours=family.checkin_hours)


def window_start(family: Family, now: datetime) -> datetime:
    """С какого момента считаем, что «записи есть».

    Окно скользящее и равно интервалу опроса, а не «с прошлого вопроса» и не
    «с полуночи». Окно «с прошлого вопроса» после суток простоя спросило бы
    про сутки, а календарное имеет проблему неполного дня — ту самую, из-за
    которой сторож подгузников не срабатывал в бою ни разу.
    """
    return now - timedelta(hours=family.checkin_hours)


def shift_by(minutes: int) -> Shift | None:
    """Сдвиг из файла или `None`.

    Нужна не для красоты: `callback_data` приходит с клиента, и без сверки со
    списком подделанное `back=99999` записало бы кормление недельной давности.
    """
    for shift in CARD.shifts:
        if shift.minutes == minutes:
            return shift
    return None


def durations_for(kind: str, shift_minutes: int) -> tuple[int, ...]:
    """Длительности, целиком помещающиеся в прошлое. Пустой кортеж — законный.

    Без фильтра «30 мин назад» плюс «3 ч сна» дают `ended_at` в будущем, а это
    ломает обрезку событий по границам периода и всю сводку суток разом.
    Фильтр честнее клампинга: клампинг молча поменял бы число, которое человек
    только что назвал, а отсутствующей кнопки просто нет на экране.

    При нулевом сдвиге у длящегося вида не остаётся ничего — и это правильно:
    «началось сейчас и уже закончилось» не бывает, там уместна одна кнопка
    «ещё идёт», то есть ровно кнопка панели «начали».
    """
    found = BY_KIND.get(kind)
    if found is None:
        return ()
    return tuple(minutes for minutes in found.durations if minutes <= shift_minutes)


# --- Рассылка -------------------------------------------------------------


async def send_pending(
    bot: Bot, session: AsyncSession, now: datetime | None = None
) -> None:
    """Обойти семьи и задать созревшие вопросы. Зовёт тикер.

    В обычный тик не делает ни одного обращения к Telegram и ни одного лишнего
    к базе: сначала чистая `due`, и только у прошедших её спрашиваем, было ли
    что записано.
    """
    moment = now or tu.now_utc()
    for family in await repo.all_families(session):
        if not due(family, moment):
            continue
        try:
            await send_for(bot, session, family, moment)
        except Exception:
            # Сбой одной семьи не срывает остальных; отметку не ставим —
            # попробуем на следующем тике
            log.exception("Опрос семьи #%s не отправлен", family.id)


async def send_for(
    bot: Bot, session: AsyncSession, family: Family, now: datetime
) -> None:
    """Спросить одну семью. Отметка ставится последней и не всегда.

    Три случая молчания и все три разные:

    * карточки ребёнка нет — спрашивать не о ком, отметку не ставим;
    * в окно попала хоть одна запись — семья и так пишет, вопрос не нужен;
      отметку не ставим, чтобы следующий раз пришёл через интервал, а не
      через два;
    * `RETRY` — сеть или флуд-контроль, следующий тик повторит.

    А вот `BROKEN` и `FORBIDDEN` отметку ставят: повтор даст ту же ошибку, и
    без отметки карточка полезла бы в Telegram каждую минуту до полуночи.
    """
    child = await repo.active_child(session, family.id)
    if child is None:
        return

    if await repo.has_events_since(session, child.id, window_start(family, now)):
        return

    status = await sending.deliver(
        bot,
        family,
        texts.checkin_card(CARD, family.checkin_hours),
        reply_markup=keyboards.checkin_card(
            [(kind.kind, kind.title) for kind in CARD.kinds],
            nothing=CARD.nothing_label,
            mute=CARD.mute_label,
        ),
    )
    if status == sending.RETRY:
        return

    await repo.mark_checkin(session, family, now)
    log.info("Опрос семьи #%s задан (%s)", family.id, status)
