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
    rows: list[tuple["Preset", Reminder | None, int]], tz: str, now: datetime
) -> str:
    lines = ["⏰ <b>Напоминания</b>", ""]
    lines += [_preset_line(*row, tz, now) for row in rows]
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

PRIVATE_ONLY = (
    "Я работаю в семейном чате, а не в личке: записи общие, и оба родителя "
    "должны их видеть. Добавьте меня в группу с женой или мужем."
)

HELP = """👶 <b>Что я умею</b>

<b>Записывать</b> — кнопками на панели, в один тап.
Кормление и сон — таймером: тап «начали», тап «закончили».
Подгузники, вес — сразу.

<b>Считать</b> — /today за сутки, /week за неделю с графиками.
Вес — с центилями ВОЗ. /export — выгрузка для педиатра.
/stage — что происходит на этой неделе.

<b>Напоминать</b> — /remind. Кормление по интервалу, витамин D,
взвешивание; прививки и осмотры — по национальному календарю,
с датами от дня рождения. Всё выключено, пока не включите.

<b>Команды:</b> /panel /today /week /weight /baby /stage /remind /export

<b>Отвечать</b> — кнопка «❓ Вопрос» на панели. Ищу в клинических
рекомендациях Минздрава, приказах и руководствах ВОЗ и показываю,
где именно написано. Не нашёл — так и скажу.

<b>Тревожные признаки</b> — /redflags. Если в сообщении названо
то, что в клинических рекомендациях считается поводом для осмотра,
я скажу об этом сам, не дожидаясь вопроса.

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

QA_DISCLAIMER = (
    "<i>Я не врач. Я нашёл это в документе и показываю, где написано, — "
    "ничего не оценивал и не советовал от себя. Решение принимаете вы.</i>"
)

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


def _quote(chunk: "Chunk") -> str:
    text = " ".join(chunk.text.split())
    if len(text) > QUOTE_LIMIT:
        text = text[:QUOTE_LIMIT].rsplit(" ", 1)[0] + "…"
    return html_escape(text)


# Путь заголовков в документе доходит до двух с половиной сотен знаков —
# «Приложение А3. Справочные материалы, включая соответствие показаний…».
# Целиком он в шапке над цитатой в одну строку выглядит дико
HEAD_LIMIT = 90


def _fragment(chunk: "Chunk") -> str:
    head = chunk.heading or chunk.title
    if len(head) > HEAD_LIMIT:
        head = head[:HEAD_LIMIT].rsplit(" ", 1)[0] + "…"
    return f"<b>{html_escape(head)}</b>\n<blockquote>{_quote(chunk)}</blockquote>\n"


def qa_sources(chunks: "tuple[Chunk, ...]") -> str:
    """Документы под ответом, без повторов, отечественные первыми.

    Порядок по тиру — это то, ради чего тир вообще заведён. Случай не
    выдуманный: российский порог 37.5 °C уже расходился с международными 38.0,
    и правильным ответом был российский, потому что семья пойдёт к российскому
    педиатру. Когда в одном ответе сошлись документы разного веса, человек
    должен видеть, какой из них чей.

    Подпись тира появляется только при смешанной выдаче. Когда все источники
    одного уровня — а так чаще всего и бывает, — она не несёт ничего, кроме
    лишней строки над и без того длинным сообщением.
    """
    seen: dict[str, Chunk] = {}
    for chunk in chunks:
        seen.setdefault(chunk.source_id, chunk)
    ordered = sorted(seen.values(), key=lambda chunk: chunk.tier)
    mixed = len({chunk.tier for chunk in ordered}) > 1

    lines = ["<b>Источники</b>"]
    tier = ""
    for chunk in ordered:
        if mixed and chunk.tier != tier:
            tier = chunk.tier
            lines.append(f"<i>{html_escape(chunk.tier_name)}</i>")
        lines.append(
            f'• <a href="{html_escape(chunk.url)}">{html_escape(chunk.title)}</a>'
            f" — {html_escape(chunk.publisher)}"
        )
    return "\n".join(lines)


def qa_answer(answer: str, chunks: "tuple[Chunk, ...]") -> str:
    """Ответ модели плюс то, из чего он собран.

    Цитаты идут ПОД ответом всегда, даже когда ответ выглядит убедительно.
    Именно рядом с настоящим текстом видно приписанное от себя — а сторож
    в `services/qa.py` ловит только числа.
    """
    lines = [html_escape(answer), ""]
    lines += [_fragment(chunk) for chunk in chunks[:2]]
    lines += [qa_sources(chunks), "", QA_DISCLAIMER]
    return "\n".join(lines)


def qa_extract(chunks: "tuple[Chunk, ...]") -> str:
    """Найденное без пересказа: ключей нет, квота вышла или модель молчит.

    Это не заглушка и не деградация. Цитата из клинических рекомендаций с
    названием документа отвечает на вопрос сама, просто без гладкой
    формулировки — и ровно за это боту можно доверять.
    """
    lines = [QA_FOUND_ONLY, ""]
    lines += [_fragment(chunk) for chunk in chunks[:3]]
    lines += [qa_sources(chunks), "", QA_DISCLAIMER]
    return "\n".join(lines)
