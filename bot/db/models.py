"""Схема БД.

Все datetime хранятся в UTC и **без tzinfo** — SQLite таймзоны не знает.
Таймзона семьи (`Family.tz`) применяется только на границе: при разборе ввода и
при выводе. Конвертация живёт в `bot/services/timeutil.py`. Нарушение этого
правила даёт ошибки, которые видны только при переводе часов, то есть раз в
полгода — чинить их потом дороже, чем соблюдать сейчас.

Единицы измерения выбраны так, чтобы не хранить дробные значения там, где
человек их не вводит: вес в граммах, длина и окружность головы в миллиметрах,
объём кормления в миллилитрах, длительность считается из `started_at`/`ended_at`
и отдельной колонкой не дублируется.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --- Виды событий ---------------------------------------------------------
# Одна таблица `events` на всё, тип в поле `kind` — паттерн Family_planer.
# Заводить таблицу на каждый вид значило бы писать один и тот же запрос
# «что было за сутки» шесть раз и склеивать результаты в Python.

FEED_BREAST = "feed_breast"
FEED_BOTTLE = "feed_bottle"
FEED_SOLID = "feed_solid"
SLEEP = "sleep"
DIAPER_WET = "diaper_wet"
DIAPER_DIRTY = "diaper_dirty"
DIAPER_MIXED = "diaper_mixed"
PUMP = "pump"
MED = "med"
TEMP = "temp"
NOTE = "note"

# События с длительностью: у них бывает открытый таймер (`ended_at is NULL`)
TIMED_KINDS = frozenset({FEED_BREAST, SLEEP, PUMP})
# Мгновенные: подгузник, температура, лекарство — у них `ended_at` всегда пуст
DIAPER_KINDS = frozenset({DIAPER_WET, DIAPER_DIRTY, DIAPER_MIXED})
FEED_KINDS = frozenset({FEED_BREAST, FEED_BOTTLE, FEED_SOLID})

# Мгновенные виды, перечисленные поимённо. `ended_at` у них пуст всегда — это
# моменты, а не отрезки, — и граница между этим множеством и `TIMED_KINDS`
# определяет, во что запись можно переделать, промахнувшись по кнопке. Перейдя
# границу, мгновенная запись стала бы идущим таймером задним числом, и тикер
# закрыл бы её на отметке «плюс четыре часа» с пометкой `estimated`; закрытая,
# наоборот, унесла бы непустой `ended_at` к виду, у которого его не бывает, и
# `events_between` потащила бы её в чужие окна по ветке `ended_at > start`.
#
# `MED`, `TEMP` и `NOTE` сюда не входят: кнопок у них нет вовсе, они приходят
# только голосом, и промахнуться по ним нечем.
INSTANT_KINDS = DIAPER_KINDS | frozenset({FEED_BOTTLE, FEED_SOLID})


# Стороны груди
LEFT = "L"
RIGHT = "R"

# --- Виды измерений -------------------------------------------------------
# Отдельная таблица, а не `events`: у измерений другая жизнь. Они редкие,
# всегда точечные, и по ним считаются z-score и центили ВОЗ — то есть у них
# есть числовой смысл, которого нет у «сменили подгузник».
WEIGHT = "weight"  # граммы
LENGTH = "length"  # миллиметры
HEAD = "head"  # миллиметры, окружность головы
MEASURE_KINDS = (WEIGHT, LENGTH, HEAD)


class Family(Base):
    """Семейный чат. Заводится сам, по первому апдейту из группы."""

    __tablename__ = "families"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    tz: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")

    # Недельный дайджест: день недели (0 = понедельник) и локальное «ЧЧ:ММ»
    digest_dow: Mapped[int] = mapped_column(Integer, default=0)
    digest_time: Mapped[str] = mapped_column(String(5), default="10:00")
    last_digest_on: Mapped[date | None] = mapped_column(Date)

    # Периодический опрос «Что было с прошлого раза?»: интервал в часах и
    # отметка о последнем заданном вопросе. `0` = выключено, и это значение по
    # умолчанию — бот молчит, пока его не попросили. `server_default` здесь не
    # украшение: без него миграция выпустит NOT NULL без умолчания в DDL, и
    # боевая база с уже заведённой семьёй откажется принять колонку — при
    # зелёных тестах, потому что они строят схему через `create_all`
    checkin_hours: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    checkin_last_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Живая панель: закреплённое сообщение, которое правится на каждый тап.
    # `panel_day` — локальный день, за который она выпущена. Именно день, а не
    # момент: сравнение идёт с `local_today`, как и у `last_digest_on`
    panel_message_id: Mapped[int | None] = mapped_column(Integer)
    panel_day: Mapped[date | None] = mapped_column(Date)

    # Когда бот поздоровался в этом чате. Приветствие — носитель постоянной
    # клавиатуры, то есть без него у семьи нет кнопок вовсе, и доезжать оно
    # обязано наверняка. Первый путь — апдейт `my_chat_member` о добавлении —
    # теряется: бот живёт на домашней машине, а `delete_webhook` на старте идёт
    # с `drop_pending_updates=True`. Добавили ночью, ПК спал — апдейта не будет
    # никогда. Отсюда второй путь, по первой реплике человека, и отметка, без
    # которой его нечем погасить: `created_at` говорит о заведении семьи, а не
    # о сказанном слове
    greeted_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("family_id", "tg_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id"), index=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger)
    display_name: Mapped[str] = mapped_column(String(255))
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Child(Base):
    """Ребёнок. Схема допускает нескольких — двойня или второй ребёнок позже."""

    __tablename__ = "children"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))

    # 'f' или 'm'. Не дань формальности: таблицы роста ВОЗ разные для девочек и
    # мальчиков, и без пола центиль посчитать нельзя
    sex: Mapped[str] = mapped_column(String(1))

    # Момент рождения, а не дата: в первые недели возраст считается в часах
    # (желтуха первых 24 часов — красный флаг), и округление до суток его теряет
    born_at: Mapped[datetime] = mapped_column(DateTime)

    birth_weight_g: Mapped[int | None] = mapped_column(Integer)
    birth_length_mm: Mapped[int | None] = mapped_column(Integer)
    birth_head_mm: Mapped[int | None] = mapped_column(Integer)

    # Срок гестации при рождении, полных недель. Нужен для скорректированного
    # возраста: у недоношенного ребёнка вехи развития и центили считаются от
    # предполагаемой даты родов, а не от фактической. Пусто = доношенный
    gestation_weeks: Mapped[int | None] = mapped_column(Integer)

    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    family: Mapped[Family] = relationship(lazy="selectin")


class Event(Base):
    """Кормление, сон, подгузник, лекарство — всё, что случается часто.

    `ended_at is NULL` у события из `TIMED_KINDS` означает идущий таймер.
    Ровно один открытый таймер на вид событий и ребёнка — это инвариант,
    который держит `repo.open_event`, а не база: частичные уникальные индексы
    в SQLite есть, но Alembic их переносит неохотно, а цена нарушения мала.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("children.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    kind: Mapped[str] = mapped_column(String(16), index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Объём в мл (бутылочка, сцеживание), температура в °C, доза лекарства.
    # Одна колонка на все числа: разводить их по типам значило бы завести
    # шесть почти всегда пустых столбцов
    value_num: Mapped[float | None] = mapped_column(Float)
    # 'L' / 'R' для груди и сцеживания
    side: Mapped[str | None] = mapped_column(String(1))
    # Уточнение вида: название лекарства, вид прикорма, консистенция стула
    subtype: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)

    # Таймер закрыл тикер, а не человек — значит длительность это догадка.
    # В отчёте для педиатра такие события помечаются: разница между «спала
    # 2 часа» и «таймер висел с вечера» существенна
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)

    # Ссылка на исходное сообщение — для дедупликации и чтобы можно было
    # вернуться к контексту записи
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Автор показывается в ленте и в сводке «кто кормил» — грузим сразу,
    # иначе на каждую строку списка уходит отдельный запрос, а в async-сессии
    # ленивая подгрузка при рендере просто падает
    author: Mapped[Member] = relationship(lazy="selectin")


class Measurement(Base):
    """Вес, рост, окружность головы. Точки на графике центилей."""

    __tablename__ = "measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("children.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    kind: Mapped[str] = mapped_column(String(16), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # Граммы для веса, миллиметры для длины и головы
    value: Mapped[float] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    author: Mapped[Member] = relationship(lazy="selectin")


class Reminder(Base):
    """Напоминание. Схема и логика догонки — перенос из Family_planer."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id"), index=True)
    child_id: Mapped[int | None] = mapped_column(ForeignKey("children.id"), index=True)
    text: Mapped[str] = mapped_column(String(500))
    fire_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    rrule: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Тег пресета: 'feed', 'vitd', 'weigh', 'vaccine', 'checkup' или пусто для
    # ручного. Нужен, чтобы «включить напоминание о витамине D» второй раз не
    # заводило дубль, а находило существующее
    preset: Mapped[str | None] = mapped_column(String(32), index=True)

    created_by: Mapped[int] = mapped_column(ForeignKey("members.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Alert(Base):
    """Тревога, поднятая ботом по данным трекера, а не по вопросу человека.

    Существует ради одного: не повторять одну и ту же тревогу. Наблюдение за
    данными идёт каждую минуту, и без отметки «сегодня про это уже сказали»
    семья получала бы «мало мокрых подгузников» 1440 раз в сутки — то есть
    ровно один раз, потому что после третьего уведомления бота отключили бы.

    Отметка в БД, а не в памяти: домашний ПК перезапускается чаще, чем
    проходят сутки, и модульная переменная гарантированно потерялась бы.

    Ключ — сутки, а не момент: если признак сохраняется назавтра, сказать о
    нём стоит ещё раз.
    """

    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("child_id", "flag_id", "raised_on"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("children.id"), index=True)
    flag_id: Mapped[str] = mapped_column(String(32), index=True)
    raised_on: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class WhoLms(Base):
    """Коэффициенты L, M, S таблиц роста ВОЗ — по одному дню возраста.

    Заполняется один раз скриптом из `kb/`. Лежит в той же базе, а не в
    отдельном файле, намеренно: центиль считается на каждое взвешивание, и
    второй источник данных пришлось бы отдельно копировать при переезде,
    отдельно бэкапить и отдельно чинить, когда он разойдётся с основным.

    Расчёт z-score идёт **кодом** (`services/growth.py`), а не через LLM.
    Модели плохо интерполируют по таблицам, а цена ошибки здесь — родители,
    зря поехавшие к врачу, или наоборот.
    """

    __tablename__ = "who_lms"
    __table_args__ = (UniqueConstraint("sex", "indicator", "age_days"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sex: Mapped[str] = mapped_column(String(1), index=True)
    # 'weight' / 'length' / 'head' — те же имена, что у `Measurement.kind`
    indicator: Mapped[str] = mapped_column(String(16), index=True)
    age_days: Mapped[int] = mapped_column(Integer, index=True)
    l: Mapped[float] = mapped_column(Float)  # noqa: E741 — имя из стандарта ВОЗ
    m: Mapped[float] = mapped_column(Float)
    s: Mapped[float] = mapped_column(Float)
