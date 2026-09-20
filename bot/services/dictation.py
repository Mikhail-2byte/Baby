"""Расшифровка → поля дневника. Промпт, приведение ответа модели и порядок отказов.

`voice.py` — транспорт голоса, `llm.py` — транспорт к модели. Здесь домен: что
именно спросить («сейчас пятница, 14 сентября, 03:41, ребёнку шесть дней») и как
превратить пришедший JSON в то, что можно показать человеку на карточке.

Модуль чистый: ни aiogram, ни БД, ни таймзон. Из `db.models` берутся только
названия видов — тем же способом, каким их берёт `keyboards.py`: один источник
правды о том, какие записи существуют.

**Всё время внутри — локальное наивное время семьи**, ровно в том виде, в каком
его выдала модель. Перевод в UTC делает хендлер через `timeutil.to_utc`: граница
таймзоны в проекте одна, и она проходит по вводу-выводу, а не по середине
разбора.

**Конца записи модель не сообщает вообще.** В схеме есть только `minutes`, а
`ended_at` считает код. Это то же правило «числа не через LLM», применённое ко
времени, и оно разом убирает весь класс ошибок «конец раньше начала».

**Красных флагов этот модуль не знает и знать не должен.** Их перехватывает
`handlers/alarm` — на расшифровке, до обращения к модели, — и фраза про судороги
сюда не доходит вовсе. Проверка здесь была бы второй копией границы
безопасности, а копия расходится с оригиналом молча. Отсюда же правило: этот
модуль не импортирует `redflags`, и это проверяется тестом, а не обещанием.

`normalize` — вторая линия обороны после `llm.extract_json`. Тот отвечает лишь
за то, что ответ вообще является объектом JSON; за то, что внутри осмысленные
значения, не отвечает никто. Поэтому здесь каждое поле проверяется по
отдельности, а негодное значение выбрасывает **поле или запись**, а не роняет
разбор целиком: «покормила левой» без правдоподобного объёма — всё ещё полезная
запись.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from bot.db.models import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    FEED_BOTTLE,
    FEED_BREAST,
    FEED_SOLID,
    LEFT,
    MED,
    NOTE,
    PUMP,
    RIGHT,
    SLEEP,
    TEMP,
    TIMED_KINDS,
    WEIGHT,
)
from bot.services import llm, qa_log

log = logging.getLogger(__name__)

PROMPT = Path(__file__).resolve().parent.parent / "content" / "voice_prompt.md"

# Куски, без которых промпт перестаёт быть тем, что задумано. Проверяются на
# импорте: бот, потерявший запрет выдумывать числа или правило про местное
# время, не должен стартовать молча — он выглядел бы работающим
REQUIRED = (
    "Ничего не выдумывай",
    "Никогда не UTC",
    "уже случилось",
    '"intent": "record|question|unclear"',
)

# Виды, которые голос умеет записывать. Ровно то, что объявлено в `models.py`,
# плюс `weight` — он уезжает в `measurements`, а не в `events`, но для человека
# «вес три двести» такая же фраза, как «подгузник мокрый», и разводить их на два
# намерения значило бы просить у модели различение, которое ей незачем делать
KINDS = frozenset(
    {
        FEED_BREAST,
        FEED_BOTTLE,
        FEED_SOLID,
        SLEEP,
        PUMP,
        DIAPER_WET,
        DIAPER_DIRTY,
        DIAPER_MIXED,
        MED,
        TEMP,
        NOTE,
        WEIGHT,
    }
)

# Виды, у которых бывает сторона
SIDED_KINDS = frozenset({FEED_BREAST, PUMP})
# Виды, у которых `value` — миллилитры
ML_KINDS = frozenset({FEED_BOTTLE, PUMP})

# Исходы разбора. Строки, а не Enum — как причины в `llm` и исходы в `qa`
RECORD = "record"
QUESTION = "question"
UNCLEAR = "unclear"
FAILED = "failed"  # до модели не дошли или она не ответила

# Одна фраза столько записей не даёт — это защита от зациклившейся модели,
# а не ожидаемый режим
MAX_RECORDS = 3

# Насколько названное время может обогнать «сейчас», прежде чем мы сочтём его
# вчерашним. Не поблажка: «сейчас» модель узнала ДО вызова, а хендлер читает
# часы ПОСЛЕ, и между этими двумя моментами лежит вся цепочка с повторами
FUTURE_SLACK = timedelta(minutes=2)

# Дальше в прошлое запись не отодвигаем. Сдвиг на сутки назад может добавить
# только 24 часа, так что двух суток хватает на «вчера вечером», сказанное
# в полдень. Всё, что старше, — галлюцинация модели, а кормление, уехавшее на
# три недели назад, портит `/week`, `/chart` и выгрузку для врача незаметно.
#
# Это же окно закрывает и `0001-01-01`, который модель охотно отдаёт вместо
# «даты нет» и который в `to_utc` даёт `OverflowError`, то есть падение
# хендлера и потерянный навсегда апдейт. Отдельных границ года здесь **нет
# намеренно**: они тут стояли, и мутационный прогон показал, что ни один тест
# не может отличить их наличие от отсутствия — до `to_utc` крайняя дата не
# доходит ни при каком значении, её забирает это окно. Недостижимый сторож
# хуже отсутствующего: он создаёт уверенность, которой не подкреплён
MAX_BACKDATE = timedelta(days=2)

# Правдоподобие объёма: больше 400 мл младенцу за раз не дают, а 0 мл — это
# не запись
ML_MIN, ML_MAX = 1.0, 400.0

# Правдоподобие показания термометра, а НЕ порог тревоги. Пороги живут в
# `redflags.yaml` и применяются раньше, в триаже: сюда фраза с опасной
# температурой просто не доходит. Диапазон здесь шире триажного намеренно —
# он отсекает мусор вроде «400 градусов», а не судит о состоянии ребёнка.
# Тест сторожит, что он шире: иначе голос отбрасывал бы температуру, о которой
# триаж имеет что сказать
TEMP_MIN, TEMP_MAX = 30.0, 45.0

# Сколько минут может длиться запись каждого вида. Сон 14 часов — уже
# неправдоподобно, но всё ещё возможно; сутки — нет
MINUTES_MAX = {FEED_BREAST: 120, PUMP: 120, SLEEP: 840}

# Столько же, сколько в `models.Event.subtype` и в `Event.note`
SUBTYPE_LIMIT = 64
NOTE_LIMIT = 500

_WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)
_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _load() -> str:
    """Системный промпт из `content/voice_prompt.md`, ниже разделителя.

    Файл читается человеком целиком, а модели уходит только вторая половина:
    первая объясняет, что здесь лежит и что нельзя убирать.
    """
    text = PROMPT.read_text(encoding="utf-8")
    _, sep, body = text.partition("\n---\n")
    if not sep:
        raise ValueError(f"{PROMPT}: нет разделителя `---`, непонятно, что слать модели")
    body = body.strip()
    if not body:
        raise ValueError(f"{PROMPT}: после разделителя пусто")
    for required in REQUIRED:
        if required not in body:
            raise ValueError(f"{PROMPT}: из промпта пропало «{required}»")
    return body


SYSTEM = _load()


@dataclass(frozen=True, slots=True)
class Record:
    """Одна разобранная запись. `at` — локальное наивное, не UTC."""

    kind: str
    at: datetime
    minutes: int | None = None
    side: str | None = None
    value: float | None = None
    subtype: str | None = None
    note: str | None = None
    # Время назвали, но разобрать не удалось, и мы взяли «сейчас». Карточка
    # говорит об этом вслух: молча подставленное время выглядит как измеренное
    guessed_time: bool = False

    @property
    def open_timer(self) -> bool:
        """Таймер, который пойдёт считать дальше, — «уснула», а не «поспала»."""
        return self.kind in TIMED_KINDS and self.minutes is None


@dataclass(frozen=True)
class Result:
    """Чем закончился разбор. Текст собирает `texts`, здесь только смысл."""

    kind: str  # RECORD | QUESTION | UNCLEAR | FAILED
    records: tuple[Record, ...] = ()
    reason: str = ""  # техническая причина отказа, для лога и для текста


def build_system(now_local: datetime, tz: str, child_name: str, age: str) -> str:
    """Промпт плюс то, чего модель знать не может.

    Без штампа «сейчас» фраза «в три сорок» не разбирается вовсе, а возраст
    ребёнка решает правдоподобие: «прикорм» у двухнедельного — почти наверняка
    ослышка.
    """
    stamp = (
        f"{_WEEKDAYS[now_local.weekday()]}, {now_local.day} "
        f"{_MONTHS[now_local.month - 1]} {now_local.year} года, "
        f"{now_local:%H:%M}"
    )
    return "\n\n".join(
        [
            SYSTEM,
            f"Сейчас: {stamp}. Таймзона семьи: {tz}.\n"
            f"Ребёнок: {child_name}, возраст {age}.",
        ]
    )


def normalize(
    raw: dict | None, now_local: datetime
) -> tuple[str, list[Record]]:
    """Ответ модели → `(intent, записи)`. Всё непонятное отбрасывается молча."""
    if not isinstance(raw, dict):
        return UNCLEAR, []

    intent = raw.get("intent")
    intent = intent if intent in (RECORD, QUESTION, UNCLEAR) else UNCLEAR

    items = raw.get("records")
    if not isinstance(items, list):
        return intent, []

    records = [_record(one, now_local) for one in items[:MAX_RECORDS]]
    kept = [one for one in records if one is not None]
    if intent == RECORD and not kept:
        # Модель сказала «запись», но ни одного годного поля не прислала.
        # Молчать нельзя, и врать «записал» тоже
        return UNCLEAR, []
    return intent, kept


def _record(raw: object, now_local: datetime) -> Record | None:
    if not isinstance(raw, dict):
        return None

    kind = raw.get("kind")
    if kind not in KINDS:
        # Вид не угадываем. У планировщика неизвестный тип становился задачей,
        # и это было безобидно; здесь подмена вида — порча данных о ребёнке
        return None

    at, guessed = _moment(raw.get("at"), now_local)
    return Record(
        kind=kind,
        at=at,
        minutes=_minutes(raw.get("minutes"), kind),
        side=_side(raw.get("side"), kind),
        value=_value(raw.get("value"), kind),
        subtype=_text(raw.get("subtype"), SUBTYPE_LIMIT),
        note=_text(raw.get("note"), NOTE_LIMIT),
        guessed_time=guessed,
    )


def _moment(value: object, now_local: datetime) -> tuple[datetime, bool]:
    """Когда это случилось, и пришлось ли время угадать.

    Порядок проверок существен, и каждая из них закрывает свой случай:

    1. снятие смещения — доверять смещению от модели нельзя, неизвестно, из
       какой зоны она его взяла, а `to_utc` потом молча предпочтёт его нашему;
    2. сдвиг на сутки назад **один раз**: названное вслух время суток всегда
       про прошлое, «в три сорок» в 02:00 — это вчера. В цикле этот сдвиг
       превратил бы ошибку модели в дату месячной давности;
    3. всё ещё будущее или слишком глубокое прошлое — берём «сейчас» и
       признаёмся в этом. Эта же проверка закрывает `0001-01-01` и всё
       остальное, что в `to_utc` дало бы `OverflowError`.
    """
    parsed = _dt(value)
    if parsed is None:
        # Времени не назвали вовсе (`null`) — это норма, а не догадка: «уснула»
        # без часов означает «сейчас», и говорить об этом человеку незачем.
        # А вот присланная и не разобранная строка — именно догадка: модель
        # время назвала, мы его не прочли, и молчать об этом нельзя
        return now_local, value is not None

    if parsed > now_local + FUTURE_SLACK:
        parsed -= timedelta(days=1)

    if parsed > now_local + FUTURE_SLACK or parsed < now_local - MAX_BACKDATE:
        return now_local, True
    return parsed, False


def _dt(value: object) -> datetime | None:
    """Строка ISO-8601 → наивный datetime. Мусор — `None`, не исключение."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _minutes(value: object, kind: str) -> int | None:
    """Длительность, если она осмысленна для этого вида и правдоподобна."""
    limit = MINUTES_MAX.get(kind)
    if limit is None:
        # У подгузника и взвешивания длительности не бывает: это момент
        return None
    number = _number(value)
    if number is None or not 1 <= number <= limit:
        return None
    return int(number)


def _side(value: object, kind: str) -> str | None:
    if kind not in SIDED_KINDS or not isinstance(value, str):
        return None
    letter = value.strip().upper()[:1]
    return letter if letter in (LEFT, RIGHT) else None


def _value(value: object, kind: str) -> float | None:
    """Число, смысл которого задаёт вид записи.

    Вес не проверяется здесь намеренно: у него единственный вход в базу —
    `measure.to_grams`, который заодно решает «килограммы это или граммы».
    Вторая проверка рядом разошлась бы с ним молча.
    """
    number = _number(value)
    if number is None:
        return None
    if kind in ML_KINDS:
        return number if ML_MIN <= number <= ML_MAX else None
    if kind == TEMP:
        return number if TEMP_MIN <= number <= TEMP_MAX else None
    if kind in (MED, WEIGHT):
        return number if number > 0 else None
    return None


def _number(value: object) -> float | None:
    # bool — подкласс int, и `True` превратился бы в «1 мл»
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:limit]
    return cleaned or None


async def interpret(
    text: str,
    *,
    now_local: datetime,
    tz: str,
    child_name: str,
    age: str,
    chat: int | None = None,
) -> Result:
    """Расшифровка → разбор. Исключений наружу не выпускает.

    Обещание держится здесь, а не надеждой на вызываемых: `llm.ask` свой
    контракт соблюдает, но `normalize` работает с чем угодно, что придёт от
    модели, а упавший хендлер — это потерянный навсегда апдейт.

    `chat` едет только в `qa.log` и больше никуда: модуль остаётся чистым —
    ни aiogram, ни БД, ни таймзон. Без него по журналу не видно, какая семья
    тратит общую суточную квоту, а семей с 19.09.2026 больше одной.
    """
    try:
        return await _interpret(
            text,
            now_local=now_local,
            tz=tz,
            child_name=child_name,
            age=age,
            chat=chat,
        )
    except Exception:
        log.exception("Не удалось разобрать расшифровку")
        return Result(FAILED, (), "broken")


async def _interpret(
    text: str,
    *,
    now_local: datetime,
    tz: str,
    child_name: str,
    age: str,
    chat: int | None,
) -> Result:
    # `model_chain` первым, `answer_chain` запасным — именно в этом порядке.
    # `llm.answer_chain` падает на `model_chain`, но не наоборот, и `.env`,
    # заполненный под этап 6 (`LLM_ANSWER_MODELS` без `LLM_MODELS`), дал бы
    # работающие ответы по базе знаний и разбор, отвечающий «нет модели» всем
    # и всегда
    chain = llm.model_chain() or llm.answer_chain()
    if not chain:
        return Result(FAILED, (), llm.NO_MODEL)

    if qa_log.quota_spent():
        # Свой тормоз, а не чужой: дойдя до лимита, провайдер просто начинает
        # отвечать 429, и разбор молча деградирует в «не понял». Останавливаемся
        # сами и говорим об этом вслух, заодно не тратя на заведомый отказ всю
        # цепочку с её повторами
        log.warning("Суточная квота обращений к модели исчерпана")
        qa_log.write(
            event="voice",
            chat=chat,
            text=text,
            outcome=FAILED,
            reason=llm.QUOTA,
            calls=0,
        )
        return Result(FAILED, (), llm.QUOTA)

    system = build_system(now_local, tz, child_name, age)
    reply = await llm.ask(system, text, models=chain)

    if not reply.ok:
        qa_log.write(
            event="voice",
            chat=chat,
            text=text,
            outcome=FAILED,
            model=reply.model,
            reason=reply.reason,
            detail=reply.detail[:160],
            calls=reply.calls,
            seconds=round(reply.elapsed, 1),
        )
        return Result(FAILED, (), reply.reason)

    intent, records = normalize(reply.data, now_local)
    qa_log.write(
        event="voice",
        chat=chat,
        text=text,
        outcome=intent,
        kinds=[one.kind for one in records],
        model=reply.model,
        calls=reply.calls,
        seconds=round(reply.elapsed, 1),
    )
    return Result(intent, tuple(records))
