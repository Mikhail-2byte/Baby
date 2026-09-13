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

from datetime import datetime, timedelta
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
    from bot.services.stats import Span

NL = chr(10)

COMMANDS = [
    ("panel", "показать панель"),
    ("today", "сводка за сегодня"),
    ("week", "статистика за неделю"),
    ("chart", "графики: вес и ритм суток"),
    ("weight", "записать вес"),
    ("baby", "данные о ребёнке"),
    ("remind", "напоминания"),
    ("export", "выгрузка для врача"),
    ("backup", "снять копию базы"),
    ("cancel", "отменить текущий ввод"),
    ("help", "что я умею"),
]

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
    "Вес и рост при рождении можно добавить позже."
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
    extra = ""
    if event.value_num is not None:
        extra = f", {event.value_num:g}"
        if event.kind in (FEED_BOTTLE, PUMP):
            extra += " мл"
        elif event.kind == TEMP:
            extra += " °C"
    return f"{KIND_ICONS.get(event.kind, '📝')} {name.capitalize()} в {local:%H:%M}{extra}"


AUTO_CLOSED = (
    "⏱ Таймер «{name}» висел дольше {hours:g} ч — закрыл его сам, "
    "длительность приблизительная. Если знаете точное время, поправьте: /today"
)

UNDONE = "↩️ Запись удалена."
NOTHING_TO_UNDO = "Эту запись уже удалили."

CANCELLED = "Отменил."
NOTHING_TO_CANCEL = "Сейчас нечего отменять."


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
    "Когда родилась? Напишите дату и время: <code>13.09.2026 04:20</code>\n"
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
        f"Родилась {local:%d.%m.%Y в %H:%M} · {age_phrase(child, now)}",
    ]
    if child.birth_weight_g:
        lines.append(f"При рождении: {weight_str(child.birth_weight_g)}")
    if child.gestation_weeks:
        lines.append(f"Срок при рождении: {child.gestation_weeks} нед.")
    return "\n".join(lines)


BABY_SAVED = "Готово. Панель ниже — ей и пользуйтесь."


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
    text = f"⏰ {reminder.text}"
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
        lines.append(f"• {reminder.text} — {local:%d.%m %H:%M}")
    if count > 10:
        lines.append(f"…и ещё {count - 10}")
    return "\n".join(lines)


# --- Прочее ---------------------------------------------------------------

PRIVATE_ONLY = (
    "Я работаю в семейном чате, а не в личке: записи общие, и оба родителя "
    "должны их видеть. Добавьте меня в группу с женой или мужем."
)

HELP = """👶 <b>Что я умею</b>

<b>Записывать</b> — кнопками на панели, в один тап.
Кормление и сон — таймером: тап «начали», тап «закончили».
Подгузники, вес — сразу.

<b>Напоминать</b> — /remind. Кормление по интервалу, витамин D,
взвешивание, приём у врача. Если компьютер спал, пропущенное
приходит одной сводкой, а не очередью.

<b>Считать</b> — /today за сутки, /week за неделю с графиками.
Вес — с центилями ВОЗ. /export — выгрузка для педиатра.

<b>Команды:</b> /panel /today /week /weight /baby /remind /export

<i>Я не врач и не ставлю диагнозов. При тревожных признаках —
звоните 103, а не мне.</i>"""

WIZARD_DROPPED = "<i>Предыдущий ввод отменил.</i>"
