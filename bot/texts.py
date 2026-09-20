"""Все тексты бота в одном месте.

Причина та же, что в Family_planer: текст, разбросанный по хендлерам,
расходится сам собой — в одном месте «покормили», в другом «кормление», и
через месяц бот разговаривает пятью голосами. Здесь же лежат и функции
форматирования: они про то, как бот говорит, а не про то, что он делает.

Правила тона, которых стоит держаться, дописывая сюда:

* никаких «Ошибка!» — семья и так не спит третью ночь;
* числа всегда с единицами, время всегда локальное;
* ни одного слова, которое можно прочитать как оценку родителей.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from bot.db.models import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    FEED_BOTTLE,
    FEED_BREAST,
    FEED_SOLID,
    HEAD,
    LEFT,
    LENGTH,
    MED,
    NOTE,
    PUMP,
    SLEEP,
    TEMP,
    TIMED_KINDS,
    WEIGHT,
    Child,
    Event,
    Measurement,
    Reminder,
)
from bot.services import timeutil as tu

if TYPE_CHECKING:
    # Только для проверки типов: в рантайме импорта нет, потому что `stats`
    # сам обращается к `texts` — цикл разорван тем, что тот импорт локальный
    from bot.services.checkin import Card, Kind, Shift
    from bot.services.dictation import Record
    from bot.services.kb import Chunk
    from bot.services.redflags import Watch
    from bot.services.schedule import Entry, Preset, Section, Source
    from bot.services.stage import Milestones, Period
    from bot.services.stats import Span

NL = chr(10)

COMMANDS = [
    ("panel", "показать панель"),
    ("today", "сводка за сегодня"),
    ("week", "статистика за неделю"),
    ("chart", "графики: вес и ритм суток"),
    ("stage", "что происходит на этой неделе"),
    ("weight", "записать вес"),
    ("baby", "данные о ребёнке"),
    ("remind", "напоминания и календарь"),
    ("export", "выгрузка для врача"),
    ("redflags", "когда звонить 103"),
    ("backup", "снять копию базы"),
    ("cancel", "отменить текущий ввод"),
    ("help", "что я умею"),
]

# Описание бота в Telegram. Публикуется на старте рядом с меню команд
# (`__main__._publish_profile`), а не выставляется руками у BotFather, по той же
# причине, по которой здесь лежит и `COMMANDS`: то, что бот обещает про себя, —
# это текст, и он обязан лежать в проекте, а не в чужом интерфейсе. Иначе он
# расходится с тем, что бот умеет, и заметить это можно только открыв BotFather.
#
# Короткое видно в профиле бота, длинное — в пустом чате с ним, то есть до
# первого сообщения. Оба были пустыми до этапа 7, и человек, которому бота
# переслали, не узнавал о нём ничего.
#
# Про голос сказано в обоих, и в длинном — первым делом после списка: это
# единственная возможность бота, у которой нет ни команды, ни кнопки, то есть
# единственная, о которой нельзя догадаться, глядя на экран.
#
# Лимиты Telegram: 120 знаков на короткое, 512 на длинное. Сторожит тест —
# превышение обрезается не нами, а API, то есть молча и посреди фразы.
SHORT_DESCRIPTION = (
    "Дневник первого года: кормления, сон, подгузники, вес с центилями ВОЗ. "
    "Записываю с кнопки и с голоса."
)

DESCRIPTION = (NL * 2).join(
    [
        "Дневник первого года: кормления, сон, подгузники, вес с центилями "
        "ВОЗ. Сводки за сутки и неделю, графики, выгрузка для педиатра, "
        "напоминания и календарь прививок.",
        "Записывать можно кнопкой в один тап — или голосом: пришлите "
        "голосовое, я расшифрую, покажу, что услышал, и спрошу, записывать "
        "ли. Голосом пишется и то, чего нет на кнопках: лекарство, "
        "температура, заметка, время задним числом.",
        "Работаю в групповом чате, а не в личке: записи общие, их видят оба "
        "родителя. Нажмите «Начать» — покажу, как настроить.",
    ]
)

KIND_NAMES = {
    FEED_BREAST: "кормление грудью",
    FEED_BOTTLE: "бутылочка",
    FEED_SOLID: "прикорм",
    SLEEP: "сон",
    DIAPER_WET: "мокрый подгузник",
    DIAPER_DIRTY: "стул",
    DIAPER_MIXED: "подгузник",
    PUMP: "сцеживание",
    MED: "лекарство",
    TEMP: "температура",
    NOTE: "заметка",
}

KIND_ICONS = {
    FEED_BREAST: "🤱",
    FEED_BOTTLE: "🍼",
    FEED_SOLID: "🥄",
    SLEEP: "😴",
    DIAPER_WET: "💧",
    DIAPER_DIRTY: "💩",
    DIAPER_MIXED: "💧💩",
    PUMP: "🥛",
    MED: "💊",
    TEMP: "🌡",
    NOTE: "📝",
}

MEASURE_NAMES = {WEIGHT: "вес", LENGTH: "рост", HEAD: "окружность головы"}

SIDE_NAMES = {LEFT: "левая", "R": "правая"}


# --- Мелкое форматирование ------------------------------------------------


def duration(delta: timedelta) -> str:
    """'1 ч 40 мин', '12 мин', '40 с'.

    Секунды показываем только на первой минуте: в остальное время они шум.
    """
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total} с"
    minutes, _ = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


def ago(moment: datetime, now: datetime) -> str:
    """'2 ч назад', 'только что'."""
    delta = now - moment
    if delta < timedelta(minutes=1):
        return "только что"
    return f"{duration(delta)} назад"


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение после числительного. '1 раз / 2 раза / 5 раз'."""
    if 11 <= count % 100 <= 14:
        return many
    last = count % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def age_phrase(child: Child, now: datetime) -> str:
    """'6 дней', '3 недели 2 дня', '4 месяца'.

    Единица меняется вместе с тем, чем меряют возраст родители: первые недели
    считают днями, потом неделями, после трёх месяцев — месяцами.
    """
    days = max(0, (now - child.born_at).days)
    if days < 14:
        return f"{days} {plural(days, 'день', 'дня', 'дней')}"
    if days < 90:
        weeks, rest = divmod(days, 7)
        head = f"{weeks} {plural(weeks, 'неделя', 'недели', 'недель')}"
        if rest:
            return f"{head} {rest} {plural(rest, 'день', 'дня', 'дней')}"
        return head
    months = days // 30
    return f"{months} {plural(months, 'месяц', 'месяца', 'месяцев')}"


# Узкий неразрывный пробел между разрядами: «3 420 г». Обычный пробел
# Telegram переносит по строке, и вес в заголовке панели разрывается пополам.
# Записан escape-последовательностью намеренно — в исходнике он неотличим от
# обычного пробела, и тест, сверяющий текст, ловил бы невидимую разницу
THIN_SPACE = " "


def weight_str(grams: float) -> str:
    """'3 420 г' — разряды через узкий неразрывный пробел."""
    return f"{int(round(grams)):,}".replace(",", THIN_SPACE) + " г"


WEIGHT_IMPLAUSIBLE = (
    "Проверьте число: для этого возраста такой вес невозможен. "
    "Запись я оставил: если число верное, ничего делать не нужно."
)
WEIGHT_IMPLAUSIBLE_SHORT = "проверьте число"


def percentile_str(z: float, pct: float) -> str:
    """'52-й центиль' плюс пометка, если значение вне обычного коридора.

    Границы ±2 SD — это те самые «-2 / +2», по которым ВОЗ отделяет вариант
    нормы от повода поговорить с врачом. Формулировка мягкая намеренно: центиль
    сам по себе диагнозом не является, важна динамика.
    """
    text = f"{pct:.0f}-й центиль"
    if z < -2:
        return f"{text} ⚠️ ниже обычного коридора"
    if z > 2:
        return f"{text} ⚠️ выше обычного коридора"
    return text


# --- Панель ---------------------------------------------------------------

NO_CHILD = (
    "Здесь пока нет ребёнка.\n\n"
    "Заведите карточку командой /baby — понадобятся имя, пол и дата рождения. "
    "Вес при рождении можно пропустить и добавить позже."
)

PANEL_HINT = "Нажмите кнопку, чтобы записать. Панель обновляется сама."


def panel(
    child: Child,
    now: datetime,
    tz: str,
    *,
    running: Event | None,
    last_feed: Event | None,
    last_sleep: Event | None,
    wet: int,
    dirty: int,
    feeds_today: int,
    weight: Measurement | None,
    weight_note: str | None,
) -> str:
    """Главный экран. Всё, что нужно знать в три часа ночи, без прокрутки."""
    head = f"👶 <b>{child.name}</b> · {age_phrase(child, now)}"
    if weight is not None:
        head += f" · {weight_str(weight.value)}"
        if weight_note:
            head += f" ({weight_note})"

    lines = [head, ""]

    if running is not None:
        icon = KIND_ICONS.get(running.kind, "⏱")
        name = KIND_NAMES.get(running.kind, running.kind)
        went = duration(now - running.started_at)
        side = f", {SIDE_NAMES[running.side]}" if running.side else ""
        lines.append(f"{icon} <b>{name.capitalize()}: идёт {went}</b>{side}")
    else:
        lines.append("⏱ Сейчас ничего не идёт")

    if last_feed is not None:
        when = ago(last_feed.ended_at or last_feed.started_at, now)
        lines.append(f"🍼 Кормление: {when}, всего за сутки {feeds_today}")
    else:
        lines.append("🍼 Кормлений сегодня ещё не было")

    if last_sleep is not None and last_sleep.ended_at is not None:
        slept = duration(last_sleep.ended_at - last_sleep.started_at)
        lines.append(f"😴 Последний сон: {slept}, {ago(last_sleep.ended_at, now)}")

    lines.append(f"💧 Подгузники: {wet} мокрых, {dirty} со стулом")
    return "\n".join(lines)


# --- Подтверждения записи -------------------------------------------------


def started(event: Event, tz: str) -> str:
    name = KIND_NAMES.get(event.kind, event.kind)
    side = f" ({SIDE_NAMES[event.side]})" if event.side else ""
    local = tu.to_local(event.started_at, tz)
    return f"{KIND_ICONS.get(event.kind, '⏱')} {name.capitalize()}{side} — начали в {local:%H:%M}"


def finished(event: Event, tz: str) -> str:
    name = KIND_NAMES.get(event.kind, event.kind)
    # `ended_at` здесь всегда заполнен — функцию зовут после закрытия таймера.
    # Подстраховка не ради типов, а ради инварианта проекта: упавший assert
    # стоит потерянного апдейта, а нулевая длительность — нет
    went = duration((event.ended_at or event.started_at) - event.started_at)
    side = f" ({SIDE_NAMES[event.side]})" if event.side else ""
    return f"{KIND_ICONS.get(event.kind, '⏱')} {name.capitalize()}{side} — {went}"


def logged(event: Event, tz: str) -> str:
    name = KIND_NAMES.get(event.kind, event.kind)
    local = tu.to_local(event.started_at, tz)
    # Уточнение вида в скобках — название лекарства, вид прикорма. До
    # голосового ввода `subtype` не заполнялся ни одной кнопкой и не
    # показывался нигде; теперь он заполняется, и «дала витамин D» без названия
    # в подтверждении выглядело бы так, будто название потерялось
    tag = f" ({html_escape(event.subtype)})" if event.subtype else ""
    extra = ""
    if event.value_num is not None:
        extra = f", {event.value_num:g}"
        if event.kind in (FEED_BOTTLE, PUMP):
            extra += " мл"
        elif event.kind == TEMP:
            extra += " °C"
        elif event.kind == MED:
            extra = f", доза {event.value_num:g}"
    return (
        f"{KIND_ICONS.get(event.kind, '📝')} {name.capitalize()}{tag} "
        f"в {local:%H:%M}{extra}"
    )


# Обещание здесь обязано быть исполнимым, и до кнопки оно таким не было:
# текст звал в `/today`, где отдельных записей не видно вовсе, а обработчика у
# слова «поправьте» не существовало ни одного. Теперь под сообщением висит
# `keyboards.record_fix` с номером ровно той записи, которую тикер закрыл.
AUTO_CLOSED = (
    "⏱ Таймер «{name}» висел дольше {hours:g} ч — закрыл его сам, "
    "длительность приблизительная. Знаете точное время — поправьте кнопкой ниже."
)

UNDONE = "↩️ Запись удалена."
REOPENED = "Таймер снова идёт"

# Отказ вернуть ход: инвариант «ровно один открытый таймер» держит `repo`, и
# молча закрыть чужой ради этого нельзя — человек просил вернуть один, а не
# оборвать другой
ANOTHER_TIMER_RUNS = "Сейчас идёт другой таймер. Сначала закончите его."

NOTHING_TO_UNDO = "Эту запись уже удалили."

CANCELLED = "Отменил."
NOTHING_TO_CANCEL = "Сейчас нечего отменять."


# --- Правка записей -------------------------------------------------------
#
# Экраны собираются из тех же `started`/`finished`/`logged`, что и
# подтверждения, — иначе одна и та же запись выглядела бы в ленте и в карточке
# по-разному, и человек не узнал бы её в списке.

RECORD_GONE = "Этой записи уже нет — её удалили."
RECORD_STALE = "Кнопка устарела: запись изменилась. Откройте её заново."

# Отдельный текст, а не `RECORD_STALE`, и разница не косметическая. «Кнопка
# устарела» объясняет гонку двоих, а здесь ничего не двигали: этот вид записи
# невозможен в принципе. Человек, прочитавший неверную причину, нажмёт ещё раз
# и получит то же самое — то есть увидит сломавшегося бота
RECORD_WRONG_KIND = (
    "Так поменять вид нельзя: мгновенная запись не становится длящейся и "
    "наоборот. Откройте запись заново."
)
RECORD_EMPTY = "За эти сутки записей нет."
RECORD_ASK_ML = "Сколько мл? Напишите числом. Не ответите — останется как было."
RECORD_ASK_DELETE = "Удалить эту запись? Вернуть её будет нечем."
RECORD_BIRTH_WEIGHT = (
    "Это вес при рождении. Он живёт ещё и в карточке ребёнка, поэтому правится "
    "там: /baby. Поправив здесь, мы развели бы карточку с графиком."
)
RECORD_PICK = "Какую поправить?"


def record_line(row: Event | Measurement, tz: str) -> str:
    """Одна строка списка: время слева, суть справа.

    Время вынесено вперёд намеренно. Список читают, ища глазами «тот самый сон
    в восемь», а не «сон вообще», — и колонка времени сканируется быстрее, чем
    время, спрятанное в конце фразы.
    """
    if isinstance(row, Measurement):
        local = tu.to_local(row.taken_at, tz)
        name = MEASURE_NAMES.get(row.kind, row.kind)
        value = weight_str(row.value) if row.kind == WEIGHT else f"{row.value:g}"
        return f"{local:%H:%M}  ⚖️ {name.capitalize()}, {value}"

    local = tu.to_local(row.started_at, tz)
    name = KIND_NAMES.get(row.kind, row.kind)
    side = f" ({SIDE_NAMES[row.side]})" if row.side else ""
    tag = f" ({html_escape(row.subtype)})" if row.subtype else ""
    icon = KIND_ICONS.get(row.kind, "📝")

    tail = ""
    if row.ended_at is not None:
        tail = f" — {duration(row.ended_at - row.started_at)}"
        if row.estimated:
            tail += " (прибл.)"
    elif row.kind in TIMED_KINDS:
        tail = " — идёт"
    elif row.value_num is not None:
        tail = f", {row.value_num:g}"
        if row.kind in (FEED_BOTTLE, PUMP):
            tail += " мл"
        elif row.kind == TEMP:
            tail += " °C"

    return f"{local:%H:%M}  {icon} {name.capitalize()}{side}{tag}{tail}"


def records_screen(
    rows: Sequence[Event | Measurement],
    tz: str,
    day: date,
    today: date,
    *,
    first: int,
    total: int,
) -> str:
    """Список записей за сутки с нумерацией, совпадающей с кнопками."""
    when = tu.day_label(day, today) or ""
    stamp = tu.day_stamp(day)
    head = f"📋 Записи · {when}, {stamp}" if when else f"📋 Записи · {stamp}"
    if not rows:
        return f"{head}\n\n{RECORD_EMPTY}"

    lines = [head, ""]
    for offset, row in enumerate(rows):
        lines.append(f"{first + offset}. {record_line(row, tz)}")
    lines.append("")
    if total > len(rows):
        last = first + len(rows) - 1
        lines.append(f"Показаны {first}–{last} из {total}. {RECORD_PICK}")
    else:
        lines.append(RECORD_PICK)
    return "\n".join(lines)


def record_card(row: Event | Measurement, tz: str, now: datetime) -> str:
    """Карточка одной записи: всё, что о ней известно, и кто её сделал.

    «Кто записал: Аня», а не «Записала Аня»: имя автора — данные, а форма
    глагола была бы догадкой о поле родителя. Тот же довод, по которому бот
    говорит о ребёнке без рода.
    """
    author = row.author.display_name if row.author is not None else "—"

    if isinstance(row, Measurement):
        name = MEASURE_NAMES.get(row.kind, row.kind)
        value = weight_str(row.value) if row.kind == WEIGHT else f"{row.value:g}"
        return "\n".join(
            [
                f"⚖️ {name.capitalize()}",
                "",
                f"Когда: {tu.fmt_when(row.taken_at, tz, now)}",
                f"Сколько: {value}",
                f"Кто записал: {html_escape(author)}",
            ]
        )

    name = KIND_NAMES.get(row.kind, row.kind)
    side = f" ({SIDE_NAMES[row.side]})" if row.side else ""
    tag = f" ({html_escape(row.subtype)})" if row.subtype else ""
    icon = KIND_ICONS.get(row.kind, "📝")

    lines = [f"{icon} {name.capitalize()}{side}{tag}", ""]
    lines.append(f"Начало: {tu.fmt_when(row.started_at, tz, now)}")

    if row.ended_at is not None:
        lines.append(f"Конец: {tu.fmt_when(row.ended_at, tz, now)}")
        went = duration(row.ended_at - row.started_at)
        if row.estimated:
            lines.append(f"Длилось: {went} (приблизительно — закрыл таймер сам)")
        else:
            lines.append(f"Длилось: {went}")
    elif row.kind in TIMED_KINDS:
        lines.append(f"Идёт: {duration(now - row.started_at)}")

    if row.value_num is not None:
        unit = " мл" if row.kind in (FEED_BOTTLE, PUMP) else ""
        lines.append(f"Сколько: {row.value_num:g}{unit}")
    if row.note:
        lines.append(f"Заметка: {html_escape(row.note)}")

    lines.append(f"Кто записал: {html_escape(author)}")
    return "\n".join(lines)


FIELD_NAMES = {"s": "начало", "e": "конец", "t": "время"}


def record_time(
    row: Event | Measurement,
    tz: str,
    field: str,
    was: datetime,
    becomes: datetime,
    now: datetime,
) -> str:
    """Экран сдвига: что было и чем станет.

    «Станет» печатается всегда, даже при нулевом сдвиге, и это не шум: строка
    объявляет, что в базе пока ничего не изменилось. Без неё экран выглядел бы
    так, будто каждый тап уже записан.
    """
    if isinstance(row, Measurement):
        title = MEASURE_NAMES.get(row.kind, row.kind).capitalize()
        icon = "⚖️"
    else:
        title = KIND_NAMES.get(row.kind, row.kind).capitalize()
        icon = KIND_ICONS.get(row.kind, "📝")
    what = FIELD_NAMES.get(field, "время")
    return "\n".join(
        [
            f"🕐 {icon} {title} · правим {what}",
            "",
            f"Было: {tu.fmt_when(was, tz, now)}",
            f"Станет: {tu.fmt_when(becomes, tz, now)}",
        ]
    )


RECORD_KINDS_ASK = "Какая это запись на самом деле?"


def record_moved(row: Event | Measurement, tz: str, field: str, was: datetime) -> str:
    """Подтверждение сдвига в ленту: откуда и куда уехала граница.

    Названы обе точки, а не только новая. Подтверждение читает и второй
    родитель, который экрана правки не видел, — «конец 03:40» сказало бы ему,
    что запись такой и была.
    """
    if isinstance(row, Measurement):
        title = MEASURE_NAMES.get(row.kind, row.kind).capitalize()
        became = row.taken_at
    else:
        title = KIND_NAMES.get(row.kind, row.kind).capitalize()
        became = row.ended_at if field == "e" and row.ended_at else row.started_at
    what = FIELD_NAMES.get(field, "время")
    return (
        f"🕐 {title}: {what} {tu.to_local(was, tz):%H:%M} → "
        f"{tu.to_local(became, tz):%H:%M}"
    )


def record_kind_changed(event: Event, tz: str, was: str, dropped: str | None) -> str:
    """Подтверждение смены вида, и вслух — что при этом снялось.

    Молча стёртый объём означал бы, что число исчезло из дневника без единого
    слова. Пометка `estimated` тем же правилом объявляется тикером, а
    погашенные подписки — экраном напоминаний.
    """
    became = KIND_NAMES.get(event.kind, event.kind).capitalize()
    side = f" ({SIDE_NAMES[event.side]})" if event.side else ""
    icon = KIND_ICONS.get(event.kind, "📝")
    line = f"✏️ Было «{was}», стало «{icon} {became}{side}»."
    return f"{line} {dropped}" if dropped else line


def record_value_dropped(kind: str, value: float) -> str:
    """Вид записи здесь не склоняется намеренно.

    «У «сон» его не бывает» — именительный падеж в родительном месте, и чинить
    это пришлось бы вторым словарём имён на одиннадцать видов. Оборот «у этого
    вида» обходится без него и не устаревает от нового вида в `models.py`.
    """
    return f"Объём {value:g} мл убрал: у этого вида его не бывает."


ESTIMATE_DROPPED = "Пометку «приблизительно» снял: конец назвали вы, а не таймер."


def record_deleted(row: Event | Measurement, tz: str) -> str:
    """Удаление называет запись целиком, и это вместо отмены.

    Вернуть удалённое нечем: черновиков в памяти здесь нет по доктрине, а
    возить все поля события в `callback_data` не даёт потолок в 64 байта.
    Единственная страховка от промаха — возможность завести запись заново, не
    вспоминая, что в ней было.
    """
    return f"🗑 Удалил: {record_line(row, tz)}"


# --- Голос и карточка разбора ---------------------------------------------
#
# Карточка живёт здесь, рядом с `started`/`finished`/`logged`, а не в разделе
# про базу знаний: читающий «как бот отчитывается о записи» обязан найти все
# четыре на одном экране.
#
# Но это ОТДЕЛЬНЫЕ функции, и не от `Event`, а от разбора. Причин четыре, и все
# про этот проект. Питоновские `default` до записи в базу не применяются:
# у несохранённого `Event` поле `estimated` равно `None`, а не `False`, и
# первый же рендер «прибл.» окажется правым по совпадению. `Event.author`
# грузится `selectin`, и любая будущая строка «кто записал» упала бы на
# неприкреплённом объекте. Дальше — время: разбор держит локальное наивное, а
# `started` делает `to_local`, то есть пришлось бы звать `to_utc` ДО
# подтверждения человеком и конвертировать на двух путях, превью и сохранении,
# — один из них однажды разъедется. И наконец голос другой: карточка
# спрашивает, а те три сообщают о факте.

# Сколько знаков расшифровки показываем. Обрезаем ДО экранирования: «&»
# превращается в пять знаков, и срез по середине entity даёт Telegram
# «can't parse entities» — то есть потерю всего сообщения, а не хвоста
VOICE_ECHO_LIMIT = 800

VOICE_OFF = (
    "🎤 Голос выключен: не заполнен <code>STT_KEY</code>.\n\n"
    "Записывать можно кнопками на панели — /panel.\n\n"
    "<i>И отдельно: тревожные признаки я слышу только текстом. "
    "Пока расшифровки нет, голосовые я не разбираю вовсе.</i>"
)

VOICE_FAILED = "🎤 Не разобрал запись. Попробуйте ещё раз или запишите кнопкой — /panel."

VOICE_UNCLEAR = "Не понял, что записать. Скажите короче — «покормила 20 минут левой»."

VOICE_STALE = "Эта карточка устарела — надиктуйте заново."

VOICE_DROPPED = "✖️ Не записал."

VOICE_CARD_ASK = "Записать?"

VOICE_OPEN_TIMER = (
    "<i>Отсчёт пойдёт дальше — закончите кнопкой «⏹ Закончить» на панели.</i>"
)

VOICE_TIME_GUESSED = "<i>Время не разобрал — поставил «сейчас».</i>"

# Три текста, а не один: `llm.py` существует в нынешнем виде именно потому, что
# двенадцать разных отказов однажды слиплись в один `None`, и понять, сломан ли
# бот, было нельзя
VOICE_NO_MODEL = (
    "🎤 Услышал, но разбирать нечем: не заполнен <code>LLM_MODELS</code>. "
    "Запишите кнопкой — /panel."
)
VOICE_QUOTA = (
    "🎤 Услышал, но суточный лимит обращений к модели исчерпан. "
    "Запишите кнопкой — /panel, к утру лимит обновится."
)
VOICE_NOT_PARSED = (
    "🎤 Услышал, но разобрать не удалось — модель не ответила. "
    "Запишите кнопкой — /panel."
)


def voice_heard(text: str) -> str:
    """Эхо расшифровки. Уходит до разбора, и это его главная работа.

    Во-первых, человек видит, что именно бот услышал, и ошибка распознавания не
    превращается молча в неправильную запись. Во-вторых, это единственный
    признак жизни на всё время разбора: «печатает…» у Telegram гаснет через пять
    секунд, а расшифровка и модель вместе занимают до трёх минут.

    Текст внешний — его написал не проект, а провайдер распознавания, — поэтому
    экранируется обязательно.
    """
    shown = " ".join(text.split())
    if len(shown) > VOICE_ECHO_LIMIT:
        shown = shown[:VOICE_ECHO_LIMIT].rsplit(" ", 1)[0] + "…"
    return f"🎤 Услышал: «{html_escape(shown)}»"


def _voice_at(moment: datetime, now_local: datetime) -> str:
    """Когда это случилось, из локального наивного времени.

    Не `fmt_when`: тот принимает UTC, а здесь на руках разбор, который в UTC
    ещё не переводили и до подтверждения человеком переводить не будем.
    """
    label = tu.day_label(moment.date(), now_local.date())
    if label == "сегодня":
        return f"в {moment:%H:%M}"
    if label:
        return f"{label} в {moment:%H:%M}"
    return f"{tu.day_stamp(moment.date())} в {moment:%H:%M}"


def voice_line(record: "Record", now_local: datetime) -> str:
    """Одна разобранная запись — так, как она ляжет в дневник."""
    if record.kind == WEIGHT:
        # Вес живёт в `measurements`, и названия видов событий про него не знают
        value = weight_str(record.value) if record.value else "без числа"
        return f"⚖️ Вес {_voice_at(record.at, now_local)} — {value}"

    name = KIND_NAMES.get(record.kind, record.kind)
    icon = KIND_ICONS.get(record.kind, "📝")

    # Уточнение идёт в скобках сразу за названием, как сторона груди: «доза 1,
    # витамин D» читалось задом наперёд — сначала число, потом от чего оно
    detail = record.side and SIDE_NAMES.get(record.side)
    detail = detail or (html_escape(record.subtype) if record.subtype else "")
    tag = f" ({detail})" if detail else ""
    head = f"{icon} {name.capitalize()}{tag} {_voice_at(record.at, now_local)}"

    tail: list[str] = []
    if record.minutes is not None:
        tail.append(duration(timedelta(minutes=record.minutes)))
    if record.value is not None:
        shown = f"{record.value:g}"
        if record.kind in (FEED_BOTTLE, PUMP):
            shown += " мл"
        elif record.kind == TEMP:
            shown += " °C"
        elif record.kind == MED:
            # Единицу дозы мы не знаем — её не называют или называют словом,
            # которое уехало в `subtype`. Но голое «1» в строке про лекарство
            # читается как что угодно, поэтому подписываем тем, что знаем точно
            shown = f"доза {shown}"
        tail.append(shown)
    if record.note:
        tail.append(html_escape(record.note))

    return f"{head} — {', '.join(tail)}" if tail else head


def voice_card(
    records: "tuple[Record, ...]", now_local: datetime, heard: str
) -> str:
    """Карточка подтверждения: что услышал, что запишу, и вопрос.

    Число, названное вслух, попадает в базу только через эту карточку — она и
    есть сторож правила «числа не через LLM» на голосовом пути. Сверять число с
    подстрокой расшифровки нельзя: «двадцать минут» Whisper может оставить
    словами, и сторож отверг бы верный разбор. А человек, который это число
    только что произнёс, узнаёт его на экране мгновенно.
    """
    lines = [voice_heard(heard), ""]
    lines += [voice_line(record, now_local) for record in records]
    if any(record.guessed_time for record in records):
        lines += ["", VOICE_TIME_GUESSED]
    if any(record.open_timer for record in records):
        lines += ["", VOICE_OPEN_TIMER]
    lines += ["", VOICE_CARD_ASK]
    return "\n".join(lines)


def closed_previous(event: Event, tz: str) -> str:
    """Идущий таймер закрыт, чтобы открыть новый.

    Раньше называлась `voice_closed` и звучала только на голосовом пути: кнопка
    делала это молча, и в докстроке стояло почему — «панель меняется под
    пальцем, и закрытие видно сразу». Довод сломался вместе с интерфейсом. С
    появлением клавиатуры под полем ввода панель чаще всего выше по чату, а
    действуют с подтверждений; «видно сразу» перестало быть правдой, и молчание
    превратилось в тихую потерю: предыдущий таймер закрыт «сейчас», а человек
    об этом не знает и отменить может только новый.

    Теперь вызывающих трое — кнопка «начали», диктовка «уснула» и карточка
    опроса, — и имя, называвшее один из них, врало остальным двум.
    """
    return f"⏹ Закрыл предыдущее: {finished(event, tz)}"


def voice_too_long(limit: int) -> str:
    return (
        f"🎤 Запись длиннее {limit} с я не разбираю — скажите короче.\n\n"
        "<i>Дело не в размере файла: в длинной записи обычно несколько дел "
        "сразу, и разобрать их правильно у меня не выйдет.</i>"
    )


def voice_saved(lines: list[str]) -> str:
    """Отчёт о записанном. Строки собирает хендлер теми же функциями, что кнопки."""
    return "\n".join(["✅ Записал:", "", *lines])


# --- Сводки ---------------------------------------------------------------


def day_summary(
    child: Child,
    day_label: str,
    *,
    feeds: int,
    feed_minutes: int,
    bottle_ml: float,
    sleep_total: timedelta,
    sleep_count: int,
    wet: int,
    dirty: int,
    longest_sleep: timedelta | None,
) -> str:
    lines = [f"📊 <b>{child.name}, {day_label}</b>", ""]

    feed_parts = [f"{feeds} {plural(feeds, 'раз', 'раза', 'раз')}"]
    if feed_minutes:
        feed_parts.append(f"{duration(timedelta(minutes=feed_minutes))} у груди")
    if bottle_ml:
        feed_parts.append(f"{bottle_ml:g} мл из бутылочки")
    lines.append("🍼 Кормления: " + ", ".join(feed_parts))

    if sleep_count:
        line = (
            f"😴 Сон: {duration(sleep_total)} за "
            f"{sleep_count} {plural(sleep_count, 'раз', 'раза', 'раз')}"
        )
        if longest_sleep:
            line += f", самый долгий {duration(longest_sleep)}"
        lines.append(line)
    else:
        lines.append("😴 Сон: записей нет")

    lines.append(f"💧 Подгузники: {wet} мокрых, {dirty} со стулом")
    return "\n".join(lines)


def week_summary(
    child: Child, total: "Span", days: list, *, plain: bool = False
) -> str:
    """Неделя: итог плюс построчная разбивка по дням.

    Разбивка нужна не для красоты: без неё «спала 8 часов в среднем» скрывает
    ровно то, что родители хотят увидеть, — была ли бессонная ночь вчера или
    неделю назад.

    `plain=True` — для PDF. В чате значки в построчной разбивке работают
    подписями («🍼 5 · 😴 5 ч»), и это компактно. В PDF их нет в шрифте, а
    вырезать их нельзя: останется «5 · 5 ч · 0/0» — строка без единого
    указания, что это за числа. Поэтому там подписи словами, и решается это
    здесь, а не заменой значков постфактум: такая замена дала бы
    «кормления Кормления: 10 раз» везде, где слово уже есть.
    """
    marks = ("кормл.", "сон", "подгуз.") if plain else ("🍼", "😴", "💧")

    lines = [day_summary_from_span(child, total, "за неделю"), "", "<b>По дням:</b>"]
    for day, span in days:
        sleep = duration(span.sleep_time) if span.sleep_time else "—"
        lines.append(
            f"{tu.WEEKDAYS_SHORT[day.weekday()]} {tu.day_stamp(day)}: "
            f"{marks[0]} {span.feeds} · {marks[1]} {sleep} · "
            f"{marks[2]} {span.wet}/{span.dirty}"
        )
    return NL.join(lines)


def day_summary_from_span(child: Child, span: "Span", label: str) -> str:
    """`stats.Span` → текст сводки. Обёртка, чтобы вызывающие не разбирали поля."""
    return day_summary(
        child,
        label,
        feeds=span.feeds,
        feed_minutes=span.feed_minutes,
        bottle_ml=span.bottle_ml,
        sleep_total=span.sleep_time,
        sleep_count=span.sleep_count,
        wet=span.wet,
        dirty=span.dirty,
        longest_sleep=span.longest_sleep,
    )


def average_summary(child: Child, span: "Span", days: int) -> str:
    """Средние за сутки. Именно средние, а не суммы.

    «812 кормлений за месяц» не говорит ни о чём; «8 кормлений в сутки»
    сравнимо и с нормой, и с прошлым месяцем.
    """
    return (
        f"📊 <b>{child.name}, последние {days} "
        f"{plural(days, 'день', 'дня', 'дней')}</b>\n\n"
        f"🍼 Кормления: {span.feeds / days:.1f} в сутки\n"
        f"😴 Сон: {duration(span.sleep_time / days)} в сутки\n"
        f"💧 Подгузники: {span.wet / days:.1f} мокрых, "
        f"{span.dirty / days:.1f} со стулом в сутки"
    )


DIGEST_HEAD = "📬 <b>Итоги недели</b>\n\n"

DIGEST_NO_DATA = (
    "📬 За неделю записей не было. Если бот молчал не по делу — проверьте /ping."
)

EXPORT_CAPTION = (
    "Выгрузка для врача: {events} событий, {measures} измерений.\n"
    "Длительности с пометкой «прибл.» посчитал я сам — таймер забыли закрыть."
)
EXPORT_EMPTY = "Пока нечего выгружать: ни одной записи."
CHART_EMPTY = "Пока не из чего строить график."


# --- Карточка ребёнка -----------------------------------------------------

BABY_ASK_NAME = "Как зовут малышку или малыша?"
BABY_ASK_SEX = "Девочка или мальчик? Это нужно для таблиц роста ВОЗ — они разные."
BABY_ASK_BORN = (
    "Когда родился ребёнок? Напишите дату и время: "
    "<code>13.09.2026 04:20</code>\n"
    "Если время не помните, хватит и даты."
)
BABY_ASK_WEIGHT = (
    "Вес при рождении в граммах — например <code>3420</code>.\n"
    "Можно пропустить: /skip"
)
BABY_BAD_DATE = (
    "Не понял дату. Нужен формат <code>13.09.2026</code> "
    "или <code>13.09.2026 04:20</code>."
)
BABY_FUTURE_DATE = "Эта дата ещё не наступила. Проверьте год."
BABY_BAD_WEIGHT = "Вес нужен в граммах, числом: например <code>3420</code>."


def baby_card(child: Child, now: datetime, tz: str) -> str:
    local = tu.to_local(child.born_at, tz)
    lines = [
        f"👶 <b>{child.name}</b>",
        f"Дата рождения: {local:%d.%m.%Y в %H:%M} · {age_phrase(child, now)}",
    ]
    if child.birth_weight_g:
        lines.append(f"При рождении: {weight_str(child.birth_weight_g)}")
    if child.gestation_weeks:
        lines.append(f"Срок при рождении: {child.gestation_weeks} нед.")
    return "\n".join(lines)


BABY_SAVED = "Готово. Панель ниже — ей и пользуйтесь."


# --- Правка карточки ------------------------------------------------------
#
# Карточку заполняют один раз, в первые часы после родов и не выспавшись.
# Опечатка в моменте рождения тянет за собой все центили и порог «младше трёх
# месяцев» в триаже, поэтому правка обязана быть в чате, а не в SQL.

BABY_EDIT_HINT = "Что поправить?"

BABY_EDIT_ASK_NAME = "Как зовут на самом деле?"
BABY_EDIT_ASK_SEX = "Девочка или мальчик?"
BABY_EDIT_ASK_BORN = (
    "Когда родился ребёнок? Напишите дату и время: "
    "<code>13.09.2026 04:20</code>\n"
    "Если время не помните, хватит и даты."
)
BABY_EDIT_ASK_WEIGHT = (
    "Вес при рождении в граммах — например <code>3420</code>.\n"
    "Это тот вес, что записан в выписке из роддома, а не сегодняшний: "
    "от него считается потеря массы первых дней."
)


def baby_edited(field: str, value: str) -> str:
    """Что именно изменилось — словами, а не «сохранено».

    Человек правит карточку потому, что раньше ошибся; показать новое значение
    вслух дешевле, чем заставлять его открывать карточку и сверять.
    """
    return f"Поправил. {field}: <b>{value}</b>"


def presets_dropped(count: int) -> str:
    """Сказать, что подписки на календарь погасли вместе с датой рождения.

    Молчать здесь нельзя. Даты прививок и осмотров замораживаются в момент
    включения подписки, и после правки даты рождения они считаются от старой.
    Разъехавшийся календарь прививок не виден ничем до самой поликлиники.
    """
    word = plural(count, "напоминание", "напоминания", "напоминаний")
    return (
        f"\n\n⚠️ Дата рождения изменилась, поэтому я выключил подписки на "
        f"прививки и осмотры: {count} {word} были посчитаны от старой даты.\n"
        f"Включите их заново в /remind — пересчитаю от новой."
    )


# --- Измерения ------------------------------------------------------------

ASK_WEIGHT = (
    "Сколько грамм? Например <code>3560</code>.\n"
    "Можно и в килограммах: <code>3.56</code>"
)
BAD_WEIGHT = "Нужно число — граммы (<code>3560</code>) или килограммы (<code>3.56</code>)."
WEIGHT_OUT_OF_RANGE = "Это не похоже на вес младенца. Проверьте число."


def weight_saved(
    measurement: Measurement,
    tz: str,
    *,
    zscore_text: str | None,
    delta_text: str | None,
) -> str:
    local = tu.to_local(measurement.taken_at, tz)
    lines = [f"⚖️ {weight_str(measurement.value)} · {local:%d.%m в %H:%M}"]
    if delta_text:
        lines.append(delta_text)
    if zscore_text:
        lines.append(zscore_text)
    return "\n".join(lines)


def weight_delta(grams: float, days: float) -> str:
    """'+180 г за 6 дней (30 г/сутки)' — то, что спрашивает педиатр."""
    sign = "+" if grams >= 0 else "−"
    word = plural(int(days), "день", "дня", "дней")
    body = f"{sign}{abs(int(round(grams)))} г за {days:.0f} {word}"
    if days >= 1:
        body += f" ({grams / days:+.0f} г/сутки)"
    return body


# --- Напоминания ----------------------------------------------------------


def reminder_message(reminder: Reminder, tz: str, now: datetime, *, late: bool) -> str:
    """Одно напоминание.

    Текст экранируется, хотя приходит из своего же `schedule_ru.yaml`: в дозе
    или в примечании легко появится «<» или «&», а Telegram на сломанной
    разметке не ругается — он просто не отправляет сообщение. Курсив про время
    добавляется снаружи экранированной части и продолжает работать.
    """
    text = f"⏰ {html_escape(reminder.text)}"
    if late:
        local = tu.to_local(reminder.fire_at, tz)
        text += f"\n<i>Было запланировано на {local:%H:%M}</i>"
    return text


def missed_summary(reminders: list[Reminder], tz: str, now: datetime) -> str:
    """Одно сообщение вместо очереди — после долгого простоя ПК.

    Без этого после шести часов сна машины семья получила бы восемь
    «пора кормить» в одну секунду, и все восемь — бесполезные.
    """
    count = len(reminders)
    lines = [
        f"⏰ Пока меня не было, пропустил "
        f"{count} {plural(count, 'напоминание', 'напоминания', 'напоминаний')}:"
    ]
    for reminder in reminders[:10]:
        local = tu.to_local(reminder.fire_at, tz)
        lines.append(f"• {html_escape(reminder.text)} — {local:%d.%m %H:%M}")
    if count > 10:
        lines.append(f"…и ещё {count - 10}")
    return "\n".join(lines)


# Ниже — экран `/remind`. Ни одной фразы про медицину и ни одного числа с
# медицинским смыслом здесь нет: всё это приходит из `schedule_ru.yaml`, а
# тексты только склеивают иконку, название, дату и список.

REMIND_HINT = (
    "<i>Тап по строке включает или выключает. Даты календаря считаю от дня "
    "рождения — это ориентир, точную назначает поликлиника.</i>"
)
REMIND_PICK = "Как часто напоминать?"
REMIND_NEEDS_CHILD = "Сначала заведите карточку ребёнка — /baby. Без даты рождения календаря нет."
REMIND_ALL_PAST = "Всё, что есть в календаре, уже позади. Напоминать не о чем."
CALENDAR_HOSPITAL = "🏥 — делают в роддоме, напоминать не о чем."


def _interval_hours(rrule: str | None) -> int | None:
    """Интервал почасового правила, чтобы показать его на экране словами."""
    if not rrule or "HOURLY" not in rrule:
        return None
    for part in rrule.split(";"):
        if part.startswith("INTERVAL="):
            return int(part.removeprefix("INTERVAL="))
    return None


def _preset_line(
    preset: "Preset", reminder: Reminder | None, count: int, tz: str, now: datetime
) -> str:
    label = f"{preset.icon} {preset.title}"
    if reminder is None:
        return f"⬜ {label}"

    when = tu.fmt_due(reminder.fire_at, tz, now=now)
    hours = _interval_hours(reminder.rrule)
    if hours:
        return f"✅ {label} — каждые {hours} ч, ближайшее {when}"
    if count > 1:
        return f"✅ {label} — {count} впереди, ближайшее {when}"
    return f"✅ {label} — ближайшее {when}"


def remind_screen(
    rows: list[tuple["Preset", Reminder | None, int]],
    tz: str,
    now: datetime,
    *,
    checkin: str = "",
) -> str:
    """Экран `/remind`. `checkin` — готовая строка опроса или пусто.

    Строкой, а не парой «карточка и часы»: опрос не пресет, в `rows` ему места
    нет, и собирает его соседняя `checkin_row` — здесь только порядок строк.
    """
    lines = ["⏰ <b>Напоминания</b>", ""]
    lines += [_preset_line(*row, tz, now) for row in rows]
    if checkin:
        lines.append(checkin)
    lines += ["", REMIND_HINT]
    return "\n".join(lines)


def schedule_reminder(preset: "Preset", entry: "Entry", day: date) -> str:
    """Текст, который уедет в `reminders.text` и придёт семье в свой день.

    Собирается один раз при включении подписки и хранится строкой: колонка
    `Reminder.text` — 500 знаков, и `tests/test_texts.py` следит, чтобы самая
    длинная запись календаря в неё влезала. Исследования сюда намеренно не
    идут — они длинные и лежат на экране календаря.
    """
    what = "; ".join(entry.items or entry.doctors)
    return (
        f"{preset.icon} {preset.title}: {entry.age_label}, {tu.day_stamp(day)}. "
        f"{what}."
    )


def calendar_screen(
    child: Child,
    section: "Section",
    source: "Source",
    rows: list[tuple["Entry", date, bool]],
) -> str:
    """Календарь целиком, с датами этого ребёнка.

    Пройденное помечается, ближайшее выделяется — ровно затем, чтобы человек,
    включающий подписку за день до прививки, всё равно увидел её дату. Само
    напоминание в этом случае не заведётся: его момент уже в прошлом.
    """
    title = "Прививки" if section.id == "vaccines" else "Осмотры"
    lines = [f"<b>{title}</b> · {child.name}", ""]

    next_shown = False
    for entry, day, past in rows:
        mark = "✓" if past else " "
        if not past and not next_shown and not entry.in_hospital:
            mark, next_shown = "➡️", True
        hospital = " 🏥" if entry.in_hospital else ""
        lines.append(f"{mark} <b>{tu.day_stamp(day)}</b> · {entry.age_label}{hospital}")
        body = "; ".join(entry.items or entry.doctors)
        if body:
            lines.append(f"    {html_escape(body)}")
        if entry.tests:
            lines.append(f"    <i>{html_escape('; '.join(entry.tests))}</i>")
        if entry.note:
            lines.append(f"    <i>{html_escape(entry.note)}</i>")

    lines += ["", CALENDAR_HOSPITAL]
    lines.append(
        f'<i>Источник: <a href="{html_escape(source.url)}">'
        f"{html_escape(source.publisher)}</a></i>"
    )
    return "\n".join(lines)


def preset_turned_on(
    preset: "Preset", count: int, fire_at: datetime, tz: str, now: datetime
) -> str:
    when = tu.fmt_due(fire_at, tz, now=now)
    head = f"✅ {preset.icon} {preset.title}"
    if count > 1:
        word = plural(count, "напоминание", "напоминания", "напоминаний")
        return (
            f"{head} — завёл {count} {word} по календарю, ближайшее {when}.\n"
            "<i>Даты посчитаны от дня рождения. Если поправите её в /baby — "
            "выключите и включите снова.</i>"
        )
    body = f"{head} — напомню {when}."
    if preset.text:
        body += f"\n<i>{html_escape(preset.text)}</i>"
    return body


def preset_turned_off(preset: "Preset", count: int) -> str:
    head = f"⬜ {preset.icon} {preset.title} — выключил"
    if count > 1:
        word = plural(count, "напоминание", "напоминания", "напоминаний")
        return f"{head}, снял {count} {word}."
    return f"{head}."


# --- Прочее ---------------------------------------------------------------

# Первое слово бота человеку, которому его переслали. Раньше здесь стоял отказ
# из двух строк («работаю в семейном чате»), и он был верен ровно для того, кто
# уже знал, что делать дальше. Незнакомому человеку он не говорил ни что за
# группа, ни кого туда звать, ни зачем боту права, — а всё это написано только
# в `README.md`, то есть для того, кто разворачивает бота, а не пользуется им.
#
# Четвёртым пунктом напрашивалось «напишите /baby», и его здесь нет намеренно:
# в группе на это будет кнопка, а тут он стал бы четвёртым поручением подряд.
PRIVATE_GUIDE = """👶 Я семейный дневник: кормления, сон, подгузники, вес — в один тап.

Записи общие, поэтому работаю в групповом чате, а не здесь: оба родителя
должны видеть одно и то же и не спрашивать друг у друга «ты её кормил?».

Что сделать:

1. Создайте группу. Позвать в неё можно кого угодно — мужа, жену, бабушку,
няню. Годится и та группа, которая у вас уже есть.

2. Добавьте меня в неё кнопкой ниже.

3. Там же Telegram спросит про права администратора — оставьте отмеченными
«Закреплять сообщения» и «Удалять сообщения». Буду держать панель внизу чата
и убирать нажатия кнопок из ленты. Без прав всё работает, просто в чате
будет неопрятнее.

Дальше поздороваюсь уже в группе и покажу, с чего начать."""

GREETING = """👶 Я семейный дневник: кормления, сон, подгузники, вес — в один тап.

Кнопки под полем ввода уже здесь: «📋 Панель», «❓ Вопрос», «📊 Сутки».
Команду для них писать не надо.

Записывать можно и голосом: пришлите голосовое, я расшифрую и спрошу,
записывать ли.

Что умею целиком — /help."""

# Подклеивается к приветствию через пустую строку и только тогда, когда прав
# нет. Статус лежит в самом апдейте о добавлении, спрашивать Telegram не надо —
# а пришедший по ссылке из лички добавляется сразу администратором, и просьба
# о том, что уже дали, в первом же слове бота читалась бы как невнимательность.
ADMIN_WHY = (
    "Сделайте меня администратором: буду закреплять панель и убирать нажатия "
    "кнопок из ленты. Без прав всё работает, просто в чате будет неопрятнее."
)

ADMIN_THANKS = (
    "Права получил. Панель теперь буду закреплять, а нажатия кнопок — убирать "
    "из ленты."
)

# Говорится один раз за жизнь семьи, в конце мастера. Один раз получается не
# отметкой в базе, а устройством: мастер проходят однажды.
ADMIN_REMINDER = (
    "Кстати, прав администратора у меня нет — панель не закрепится и нажатия "
    "кнопок останутся в ленте. Всё остальное работает. Дать права: меню чата → "
    "«Администраторы» → добавить меня."
)

# Второе сообщение приветствия. Отдельным, потому что одно сообщение не носит
# сразу и клавиатуру под полем ввода, и inline-кнопку.
FIRST_STEP = (
    "Первым делом — карточка ребёнка: имя, пол, дата рождения и вес. Без неё "
    "не будет ни сводок, ни центилей ВОЗ."
)

OWNER_ONLY = (
    "Это команда владельца бота: в копии лежит база целиком, со всеми семьями "
    "сразу. Ваши записи никуда не денутся — они и так на диске и в ежедневном "
    "снимке."
)


HELP = """👶 <b>Что я умею</b>

<b>Кнопки под полем ввода</b> — «📋 Панель», «❓ Вопрос», «📊 Сутки».
Видны всегда, набирать команду не надо. Нажатие убираю из чата сам,
если дали права администратора. Скрыли клавиатуру — вернёт /help.

<b>Записывать</b> — кнопками на панели, в один тап.
Кормление и сон — таймером: тап «начали», тап «закончили».
Подгузники, вес — сразу.

Промахнулись? Под каждым ответом есть кнопка. «↩️ Отменить» убирает
запись; если случайно закончили таймер — «↩️ Не заканчивать» вернёт
его, а запись останется целой.

<b>Поправить</b> — «✏️ Поправить» под ответом, а если он уже уехал вверх,
то «📊 Сутки» → «📋 Записи» и номер нужной. Там можно подвинуть начало и
конец, сменить вид, поправить объём и удалить. Забытый таймер чинится
кнопкой прямо под сообщением о нём.

<b>Слушать</b> — просто пришлите голосовое, нажимать ничего не надо.
Расшифрую, покажу, что услышал, и спрошу, записывать ли. Голосом
можно то, чего нет на кнопках: лекарство, температуру, заметку,
прикорм — и время задним числом: «покормила 20 минут левой в 3:40».
Пока не подтвердите, не запишу ничего. Голосом же можно и спросить.

<b>Считать</b> — /today за сутки, /week за неделю с графиками.
Вес — с центилями ВОЗ. /export — выгрузка для педиатра.
/stage — что происходит на этой неделе.

<b>Напоминать</b> — /remind. Кормление по интервалу, витамин D,
взвешивание; прививки и осмотры — по национальному календарю,
с датами от дня рождения. Всё выключено, пока не включите.

<b>Отвечать</b> — кнопка «❓ Вопрос» на панели. Ищу в клинических
рекомендациях Минздрава, приказах и руководствах ВОЗ и показываю,
где именно написано. Не нашёл — так и скажу.

<b>Тревожные признаки</b> — /redflags. Если в сообщении названо
то, что в клинических рекомендациях считается поводом для осмотра,
я скажу об этом сам, не дожидаясь вопроса. Это работает и в
написанном, и в сказанном голосом.

<b>Команды:</b> /panel /today /week /weight /baby /stage /remind /export

<i>Я не врач и не ставлю диагнозов. При тревожных признаках —
звоните 103, а не мне.</i>"""

WIZARD_DROPPED = "<i>Предыдущий ввод отменил.</i>"


# --- Тревожные признаки ---------------------------------------------------
#
# Тексты фиксированные и собираются из `content/redflags.yaml`. Через модель
# они не проходят никогда: экстренный ответ обязан быть одинаковым каждый раз.

EMERGENCY_HEAD = "🚨 <b>Это признак, при котором вызывают скорую</b>"
URGENT_HEAD = "⚠️ <b>Это повод связаться с врачом сегодня</b>"

PHONES = "☎️ <b>103</b> — скорая. <b>112</b> — единый номер, работает без денег на счёте."

# Оговорка идёт в каждом таком ответе и написана так, чтобы её нельзя было
# прочитать как оценку состояния ребёнка. Бот нашёл слова, а не поставил диагноз
TRIAGE_DISCLAIMER = (
    "<i>Я не врач и не оцениваю состояние ребёнка. Я нашёл в сообщении то, "
    "что в клинических рекомендациях названо поводом для осмотра, и напоминаю "
    "правило. Решение принимаете вы.</i>"
)

# Что делать, пока ждёте скорую: единственный совет, который уместно дать
# автоматически, потому что он ничего не может ухудшить
WHILE_WAITING = (
    "Пока ждёте: держите ребёнка на руках, не кутайте, не давайте лекарств "
    "«на всякий случай» и запомните, когда всё началось — это спросят первым."
)

URGENT_ESCALATION = (
    "Если появятся вялость, трудное дыхание, изменение цвета кожи или сыпь, "
    "не бледнеющая под стеклом, — звоните 103, не дожидаясь приёма."
)


def triage_alert(hits: list, level: str) -> str:
    """Ответ на сообщение, в котором названы тревожные признаки.

    Собирается из данных, а не сочиняется: одни и те же слова в третьем часу
    ночи должны давать один и тот же ответ.
    """
    urgent = level != "emergency"
    # Телефон скорой — сразу под заголовком, но только когда речь о ней. В
    # ответе «свяжитесь с врачом сегодня» он на первой строке сбивал бы сигнал:
    # два разных уровня тревоги перестают различаться
    lines = [URGENT_HEAD if urgent else EMERGENCY_HEAD, ""]
    if not urgent:
        lines += [PHONES, ""]

    for hit in hits:
        lines.append(f"• <b>{hit.flag.title}</b>")
        lines.append(hit.flag.action)
        lines.append("")

    lines.append(URGENT_ESCALATION if urgent else WHILE_WAITING)
    lines.append("")

    sources = sorted({hit.flag.source for hit in hits})
    lines.append(TRIAGE_DISCLAIMER)
    lines.append(f"<i>Источники: {'; '.join(sources)}.</i>")
    return "\n".join(lines)


def watch_alert(watch: "Watch") -> str:
    """Тревога, поднятая по данным трекера, а не по вопросу человека.

    Тон мягче, чем у триажа по тексту: человек ни о чём не спрашивал, и первое,
    что он должен услышать, — что это может быть просто пропуск в записях.
    """
    return (
        f"⚠️ <b>{watch.title}</b>\n\n"
        f"{watch.action}\n\n"
        f"{TRIAGE_DISCLAIMER}"
    )


def redflags_list(flags: list) -> str:
    """Полный список признаков — чтобы прочитать спокойно, а не в панике.

    Нужен затем, что встречать этот список впервые в три часа ночи — плохой
    способ с ним знакомиться.
    """
    emergency = [f for f in flags if f.level == "emergency"]
    urgent = [f for f in flags if f.level == "urgent"]

    lines = [
        "🚑 <b>Когда звонить 103</b>",
        "",
        "Температура 38 °C и выше или ниже 36 °C у ребёнка младше 3 месяцев.",
    ]
    lines += [f"{flag.title}." for flag in emergency]
    lines += ["", "⚠️ <b>Когда связаться с врачом в тот же день</b>", ""]
    lines += [f"{flag.title}." for flag in urgent]
    lines += [
        "",
        "Мало мокрых подгузников: меньше четырёх за сутки после пятого дня жизни.",
        "Потеря больше 10% от веса при рождении.",
        "",
        TRIAGE_DISCLAIMER,
        "<i>Источники: NICE NG143, WHO IMCI, КР МЗ РФ «Здоровый новорождённый» 2025.</i>",
    ]
    return "\n".join(lines)


# --- Этап развития --------------------------------------------------------


def stage_report(
    child: Child,
    now: datetime,
    *,
    period: "Period",
    milestones: "Milestones | None",
    windows: list,
    caveat: str,
    corrected: bool,
) -> str:
    """Что происходит на этой неделе. Собирается из `weeks.yaml`, не сочиняется."""
    lines = [f"🌱 <b>{period.title}</b> · {child.name}, {age_phrase(child, now)}"]
    if corrected:
        # Молча подменять возраст нельзя: родители сверяют вехи с ровесниками,
        # и разница в три недели без объяснения выглядит как ошибка бота
        lines.append("<i>Считаю по скорректированному возрасту, с поправкой на срок.</i>")
    lines += ["", period.happening]

    if period.do:
        lines += ["", "<b>Что делать на этой неделе</b>"]
        lines += [f"• {item.text}" for item in period.do]

    if period.talk:
        lines += ["", "<b>О чём и как говорить</b>"]
        lines += [f"• {item.text}" for item in period.talk]

    if period.safety:
        lines += ["", "<b>Безопасность</b>"]
        lines += [f"• {item.text}" for item in period.safety]

    if period.normal:
        lines += ["", "<b>Это нормально</b>"]
        lines += [f"• {item}" for item in period.normal]

    if windows:
        names = ", ".join(
            f"{w.name.lower()} ({w.from_months:g}–{w.to_months:g} мес)" for w in windows
        )
        lines += [
            "",
            f"<b>Сейчас открыты окна ВОЗ:</b> {names}.",
            "<i>Окно — это возраст, внутри которого веху осваивают 99% детей. "
            "Обе границы — норма.</i>",
        ]

    if milestones is not None:
        lines += ["", f"<b>Вехи CDC к {milestones.at_months} месяцам</b>", f"<i>{caveat}</i>"]
        for title, items in (
            ("Общение", milestones.social),
            ("Речь", milestones.language),
            ("Мышление", milestones.cognitive),
            ("Движение", milestones.movement),
        ):
            if items:
                lines.append(f"\n<u>{title}</u>")
                lines += [f"• {item}" for item in items]

    return "\n".join(lines)


def stage_sources(period: "Period") -> str:
    """Источники советов этого периода — отдельным сообщением, по запросу."""
    seen = {item.source for item in (*period.do, *period.talk, *period.safety)}
    return "Источники: " + "; ".join(sorted(seen)) + "."


STAGE_OVER = (
    "Содержание пока доходит до года с небольшим. Дальше бот продолжает "
    "трекать и считать, но рассказывать про этап ему уже нечего."
)


# --- Вопросы по базе знаний -----------------------------------------------
#
# Ответ всегда состоит из двух частей: что нашлось и где это написано. Вторая
# часть не украшение — она и есть отличие этого бота от чата с моделью. По
# ссылке можно открыть документ и прочитать абзац целиком.

ASK_QUESTION = (
    "Напишите вопрос следующим сообщением. Поищу в клинических "
    "рекомендациях, приказах Минздрава и руководствах ВОЗ.\n\n"
    "<i>Если передумали — просто не отвечайте, через десять минут забуду.</i>"
)

# Ответ на тап по 👍/👎. Уходит алертом поверх чата, а не сообщением: оценка —
# дело того, кто её поставил, и лишняя строка в общей ленте семье не нужна.
# Спасибо говорится одинаковое обеим кнопкам намеренно: «жаль» в ответ на 👎
# читалось бы как обида, а оценка нужна нам, а не человеку
QA_THANKS = "Спасибо, учту"

# Тап по кнопке, чей вопрос уже не найти. `qa.log` ротируется, и вердикт,
# нажатый под ответом недельной давности, склеивать не с чем
QA_VERDICT_STALE = "Этот ответ уже забыт — спасибо всё равно"

QA_NO_KB = (
    "База знаний ещё не собрана. Пока могу только записывать и считать — "
    "а вопросы подождут до <code>python kb/build_kb.py</code>."
)

QA_NOT_FOUND = (
    "Не нашёл об этом в источниках. Это не значит, что вопрос глупый — "
    "значит, в тех документах, что у меня есть, про это не написано.\n\n"
    "Спросите педиатра, а если тревожно прямо сейчас — /redflags."
)

QA_FOUND_ONLY = "Вот что нашлось по этому вопросу в источниках."

# Сколько знаков цитаты показываем. Фрагмент бывает до 1200, а в сообщении их
# несколько, и у Telegram потолок 4096
QUOTE_LIMIT = 400


def html_escape(text: str) -> str:
    """Экранирование для parse_mode=HTML.

    Нужно именно здесь: в цитату идёт текст из документа, а в нём попадаются
    «<1>» (сноска в приказе) и «>» в диапазонах. Telegram на таком не ругается
    внятно — он просто отказывается разобрать разметку, и сообщение не уходит
    вовсе. Для всего остального в этом файле экранирование не нужно: тексты
    свои, а имена детей приходят через мастер.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut(text: str, limit: int) -> str:
    """Обрезать по слову, а не по букве."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _quote(chunk: "Chunk") -> str:
    return html_escape(_cut(" ".join(chunk.text.split()), QUOTE_LIMIT))


# Путь заголовков в документе доходит до двух с половиной сотен знаков —
# «Приложение А3. Справочные материалы, включая соответствие показаний…».
# Целиком он в шапке над цитатой в одну строку выглядит дико
HEAD_LIMIT = 90

# Название документа бывает не короче: «Консультирование по грудному
# вскармливанию: курс обучения (часть 2: положение у груди, трудности
# кормления)». Обрезается отдельно от заголовка, а не вместе с ним: общий
# потолок на двоих съедал бы то одно, то другое, смотря какой документ
TITLE_LIMIT = 70


def _fragment(chunk: "Chunk") -> str:
    """Цитата, подписанная документом.

    Название стоит здесь, а не списком «Источники» под сообщением: список
    повторял то же самое пятью строками со ссылками, а ночное сообщение и без
    него длинное. Правка 19.09.2026 по просьбе владельца.

    Совсем без подписи цитату оставлять нельзя. Абзац из клинических
    рекомендаций тогда неотличим от абзаца, сочинённого моделью, — а вся
    разница между этим ботом и чатом с моделью в том, что он показывает, где
    написано.

    Потеряно при этом одно, и это надо знать: уровень источника (тир) в
    сообщении больше не печатается. На отбор он влияет по-прежнему
    (`TIER_BONUS` в `kb.py`), но глазами «это российские рекомендации, а это
    зарубежные» теперь различается только по названию документа.
    """
    lines = [f"<b>{html_escape(_cut(chunk.title, TITLE_LIMIT))}</b>"]
    if chunk.heading:
        lines.append(f"<i>{html_escape(_cut(chunk.heading, HEAD_LIMIT))}</i>")
    lines.append(f"<blockquote>{_quote(chunk)}</blockquote>\n")
    return "\n".join(lines)


def qa_answer(answer: str, chunks: "tuple[Chunk, ...]") -> str:
    """Ответ модели плюс то, из чего он собран.

    Цитаты идут ПОД ответом всегда, даже когда ответ выглядит убедительно.
    Именно рядом с настоящим текстом видно приписанное от себя — а сторож
    в `services/qa.py` ловит только числа.
    """
    lines = [html_escape(answer), ""]
    lines += [_fragment(chunk) for chunk in chunks[:2]]
    return "\n".join(lines).rstrip()


def qa_extract(chunks: "tuple[Chunk, ...]") -> str:
    """Найденное без пересказа: ключей нет, квота вышла, провайдер не ответил.

    Это не заглушка и не деградация. Цитата из клинических рекомендаций с
    названием документа отвечает на вопрос сама, просто без гладкой
    формулировки — и ровно за это боту можно доверять.

    **Пустой ответ модели сюда больше не приходит.** Заголовок обещает, что
    нашлось «по этому вопросу»; там о релевантности судили и вынесли «нет», и
    печатать над цитатами обратное значило бы переспорить собственного
    сторожа. Все оставшиеся причины — это случаи, когда не судил никто.
    """
    lines = [QA_FOUND_ONLY, ""]
    lines += [_fragment(chunk) for chunk in chunks[:3]]
    return "\n".join(lines).rstrip()


# --- Периодический опрос --------------------------------------------------
#
# Опрос спрашивает про то, что уже случилось, поэтому каждое его подтверждение
# обязано называть час начала: «сейчас» здесь неверно по определению, а именно
# «сейчас» показывают подтверждения кнопок панели.


def checkin_card(card: "Card", hours: int) -> str:
    """Сама карточка: что спрашиваем и про какое окно.

    Окно названо словами — «за последние 3 часа», — а не отсылкой к «прошлому
    разу». Карточка живёт в истории чата вечно, и тап по вчерашней обязан
    читаться однозначно; отсылка к прошлому разу к тому моменту уже врёт.
    """
    return (
        f"{card.icon} <b>{html_escape(card.title)}</b>{NL}"
        f"За последние {hours} {plural(hours, 'час', 'часа', 'часов')} "
        f"записей нет. {html_escape(card.question)}"
    )


def checkin_when(kind: "Kind") -> str:
    """Второй экран. У длящегося вида спрашиваем начало, у мгновенного — момент."""
    what = "Когда началось?" if kind.kind in TIMED_KINDS else "Когда это было?"
    return f"{html_escape(kind.title)}. {what}"


def checkin_how_long(kind: "Kind", shift: "Shift", *, closed: bool) -> str:
    """Третий экран. `closed` — есть ли хоть одна длительность, влезающая в сдвиг.

    При нулевом сдвиге не влезает ни одна, и вопрос «сколько длилось» теряет
    смысл: началось только что. Тогда на экране одна кнопка «ещё идёт», и текст
    обязан сказать об этом, а не спрашивать про длительность в пустоту.
    """
    head = f"{html_escape(kind.title)}, {html_escape(shift.label)}."
    return f"{head} Сколько длилось?" if closed else f"{head} Отметьте, если ещё идёт."


def checkin_logged(event: Event, tz: str) -> str:
    """Подтверждение записи задним числом.

    Собирается из `started`/`finished`/`logged`, а не пишется заново: у бота
    один голос, и «сон 1 ч» с карточки опроса обязан читаться так же, как «сон
    1 ч» от кнопки панели. Отличие ровно одно и оно существенное — у
    закончившегося отрезка назван ещё и час начала. Без него «😴 Сон — 1 ч»,
    пришедшее в семь утра, неотличимо от сна, который только что кончился.
    """
    if event.kind not in TIMED_KINDS:
        return logged(event, tz)
    if event.ended_at is None:
        return started(event, tz)
    local = tu.to_local(event.started_at, tz)
    return f"{finished(event, tz)}, начали в {local:%H:%M}"


def checkin_row(card: "Card", hours: int) -> str:
    """Шестая строка экрана `/remind`. Форма та же, что у пресетов."""
    label = f"{card.icon} {html_escape(card.short)}"
    if hours <= 0:
        return f"⬜ {label}"
    return f"✅ {label} — каждые {hours} ч"


def checkin_turned_on(card: "Card", hours: int) -> str:
    """Что сказать, когда опрос включили. Условия молчания называются сразу.

    Иначе первая же тихая ночь читается как поломка: включили «каждые 3 часа»,
    а вопроса нет.
    """
    return (
        f"✅ {card.icon} {html_escape(card.short)} — каждые {hours} ч.{NL}"
        "Спрошу, только если за это время ничего не записали, и не ночью."
    )


CHECKIN_PICK = "Как часто спрашивать?"
CHECKIN_TURNED_OFF = "⬜ Опрос выключил."
CHECKIN_NOTHING = "Хорошо, ничего не записал."
CHECKIN_MUTED = "🔕 Больше спрашивать не буду. Включить обратно — /remind."
CHECKIN_STALE = "Кнопка устарела — дождитесь следующего вопроса."
