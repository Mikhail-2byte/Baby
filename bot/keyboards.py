"""Клавиатуры и фабрики callback_data.

Отдельный класс `CallbackData` на каждый сценарий, а не один общий с новыми
полями. Причина не в чистоте: кнопки живут в истории чата неограниченно долго,
и после деплоя человек может нажать ту, что висит со вчера. Общий класс,
у которого добавилось поле, перестанет распаковывать старые кнопки — и нажатие
молча ничего не сделает. Отдельные классы стареют независимо.

`pack()` у aiogram ограничен 64 байтами, поэтому префиксы короткие.
"""

from collections.abc import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.db.models import (
    DIAPER_DIRTY,
    DIAPER_WET,
    FEED_BOTTLE,
    FEED_BREAST,
    LEFT,
    PUMP,
    RIGHT,
    SLEEP,
)


class TrackCB(CallbackData, prefix="trk"):
    """Кнопки записи на панели.

    `action`: `start` — запустить таймер, `stop` — закрыть идущий,
    `add` — мгновенное событие (подгузник).
    """

    action: str
    kind: str
    side: str = ""


class PanelCB(CallbackData, prefix="pnl"):
    action: str


class MeasureCB(CallbackData, prefix="msr"):
    kind: str


class UndoCB(CallbackData, prefix="undo"):
    """Отмена только что записанного.

    `what`: `event` или `measure` — таблицы разные, а id в них пересекаются, —
    а также `reopen`: вернуть ход закрытому таймеру. Третье значение, а не
    третье поле: у класса, у которого добавилось поле, старые кнопки в истории
    чата перестают распаковываться, а у нового значения — нет.
    """

    what: str
    row_id: int


class StatsCB(CallbackData, prefix="st"):
    period: str


class AskCB(CallbackData, prefix="ask"):
    """Кнопка «Вопрос» на панели.

    Полей нет и не предвидится: вопрос приходит следующей репликой, а не
    кнопкой. Отдельный класс всё равно свой — см. докстроку модуля про то,
    как стареют кнопки в истории чата.
    """


class VoiceCB(CallbackData, prefix="vc"):
    """Карточка разбора голосового: записать или отвергнуть.

    Ровно одно поле, и запасных «на потом» здесь нет намеренно — см. докстроку
    модуля про то, как стареют кнопки в истории чата. Правки полей у карточки
    тоже нет: ночью переговорить фразу заново дешевле, чем читать карточку
    глазами и править её по полю.
    """

    action: str


class SexCB(CallbackData, prefix="sex"):
    value: str


class EditCB(CallbackData, prefix="edit"):
    """Правка карточки ребёнка: какое поле человек собрался менять.

    `field`: `name`, `sex`, `born` или `weight`.

    Этот класс отвечает только на вопрос «что правим». Сам новый пол потом
    приезжает обычным `SexCB` с той же клавиатуры `sex()`, что и в мастере, —
    мастер от правки там отличает не префикс, а состояние FSM.
    """

    field: str


class RemindCB(CallbackData, prefix="rmd"):
    """Экран `/remind`.

    `action`: `on` — включить пресет, `off` — выключить, `pick` — спросить
    интервал перед включением, `cal` — показать календарь секции, `back` —
    вернуться к списку пресетов.

    `hours` заполнен только у `on` для почасового пресета: интервал выбирает
    человек, и выбранное должно пережить путь через кнопку.
    """

    action: str
    preset: str = ""
    hours: int = 0


class CheckinCB(CallbackData, prefix="ci"):
    """Периодический опрос: карточка в чате и ряд настройки на экране `/remind`.

    `action`: `kind` — вид выбран, спросить «когда»; `when` — сдвиг выбран;
    `dur` — длительность выбрана, записать отрезок; `run` — «ещё идёт», открыть
    таймер задним числом; `none` — ничего не было; `back` — вернуться к видам;
    `mute` — «не спрашивать» с самой карточки; `off` — выключить с экрана
    `/remind`; `pick` — спросить интервал; `on` — включить.

    `mute` и `off` делают с настройкой одно и то же и всё-таки разные: после
    `off` надо перерисовать экран настроек, а после `mute` — погасить карточку,
    на которой нажали. Одно действие на оба случая означало бы, что хендлер
    угадывает, откуда пришёл тап, по содержимому сообщения.

    `back` — минут назад от МОМЕНТА НАЖАТИЯ, `mins` — сколько длилось,
    `hours` — интервал опроса.

    Вид едет полной константой из `models`, а не номером строки в файле
    содержания: номер сдвинется от перестановки строк, и вчерашняя кнопка
    «мокрый» запишет сегодня «стул» — молча и без единого следа в логе.

    Один класс на оба экрана, а не два: сценарий один, и делить его значило бы
    завести две вещи, которые обязаны стареть вместе.
    """

    action: str
    kind: str = ""
    back: int = 0
    mins: int = 0
    hours: int = 0


# Метки постоянной клавиатуры под полем ввода. Лежат здесь, а не в `texts`, по
# тому же основанию, что и подписи кнопок панели: это часть клавиатуры. И это
# единственный источник правды — `handlers/home.py` и `middlewares` берут их
# отсюда же, а не повторяют строки у себя. Совпадение там точное: метка с
# эмодзи руками не набирается, и семейная реплика со словом «панель» проходит
# мимо.
#
# Разница с inline-кнопками принципиальная и объясняет всю осторожность ниже.
# Inline-кнопка опознаётся по `callback_data`, её подпись — только для глаз.
# Reply-кнопка опознаётся **по самой подписи**: она уходит на клиент и
# возвращается оттуда обычным текстом. Отсюда два правила.
#
# Первое: эмодзи берутся без селектора вариаций U+FE0F. `⚖️` и подобные
# возвращаются с клиента в другой нормализации и перестают равняться своей же
# константе — кнопка есть, бот молчит, и видно это только глазами в чате.
#
# Второе: метка не должна содержать слов из `redflags.yaml`. Роутер `alarm`
# стоит выше и такую метку заберёт себе — молча и навсегда.
#
# «❓ Вопрос» и «📊 Сутки» дословно повторяют подписи на панели намеренно: одно
# действие — одно имя. Правя подпись там, надо править и здесь.
HOME_PANEL = "📋 Панель"
HOME_ASK = "❓ Вопрос"
HOME_TODAY = "📊 Сутки"
HOME_LABELS = frozenset({HOME_PANEL, HOME_ASK, HOME_TODAY})


def home() -> ReplyKeyboardMarkup:
    """Три кнопки под полем ввода — единственное, что видно всегда.

    Панель уезжает вверх по истории от каждого подтверждения, и до неё нужно
    было либо прокручивать чат, либо писать `/panel`. Ночью, одной рукой, не
    глядя — это ровно то, ради отсутствия чего панель и заводилась.
    Обычная клавиатура — единственный механизм Telegram, который держит кнопку
    на экране постоянно.

    `is_persistent` обязателен: без него клавиатура сворачивается в иконку, то
    есть перестаёт быть одним тапом. `selective` не ставим намеренно — оба
    родителя видят одно и то же, в этом весь смысл бота.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=HOME_PANEL), KeyboardButton(text=HOME_ASK)],
            [KeyboardButton(text=HOME_TODAY)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Скажите голосом или напишите",
    )


def panel(*, running_kind: str | None) -> InlineKeyboardMarkup:
    """Главная клавиатура. Вид зависит от того, идёт ли таймер.

    Когда таймер идёт, его кнопки уступают место одной большой «Закончить».
    Это не украшение: перепутать «начали» и «закончили» в полусне легко, а
    лишний старт поверх идущего таймера портит статистику суток.

    **По две кнопки в ряду, и это замер, а не вкус.** В нижнем ряду стояло
    четыре, и на телефоне Telegram резал подписи до «Су…» и «во.» — то есть
    кнопка переставала называть себя ровно там, где на неё жмут не глядя. Три в
    ряду тоже мало: «🍼 Бутылочка» не помещалась. Две — единственная ширина, при
    которой не обрезается ни одна подпись, и заодно вдвое более крупная цель
    для пальца ночью.

    **Трёх кнопок здесь больше нет, и все три ушли не в никуда.** «📊 Сутки» и
    «❓ Вопрос» переехали на постоянную клавиатуру под полем ввода
    (`home()`) — там они доступны в один тап всегда, а не только когда панель
    на экране. «🔄» перерисовывала панель **на месте**, то есть была нужна
    ровно тогда, когда панель уже видно; её работу забрала кнопка «📋 Панель»,
    которая приводит панель вниз чата.

    Обработчики `StatsCB`, `AskCB` и `PanelCB` при этом остаются: кнопки живут
    в истории чата неограниченно долго, и тап по вчерашней панели обязан
    работать — см. докстроку модуля.

    **«🍼 Бутылочка» переехала в нижний ряд и стала доступна всегда.** Это не
    побочный эффект раскладки, а исправление: бутылочка — мгновенное событие,
    как подгузник, и с идущим таймером не конфликтует (`_add` его не трогает
    вовсе). Пока она стояла в ряду кормлений, её прятало любое включённое
    сцеживание.
    """
    builder = InlineKeyboardBuilder()

    if running_kind is not None:
        builder.row(
            InlineKeyboardButton(
                text="⏹ Закончить",
                callback_data=TrackCB(action="stop", kind=running_kind).pack(),
            )
        )
    else:
        builder.row(
            InlineKeyboardButton(
                text="🤱 Левая",
                callback_data=TrackCB(action="start", kind=FEED_BREAST, side=LEFT).pack(),
            ),
            InlineKeyboardButton(
                text="🤱 Правая",
                callback_data=TrackCB(action="start", kind=FEED_BREAST, side=RIGHT).pack(),
            ),
        )
        builder.row(
            InlineKeyboardButton(
                text="😴 Сон",
                callback_data=TrackCB(action="start", kind=SLEEP).pack(),
            ),
            InlineKeyboardButton(
                text="🥛 Сцеживание",
                callback_data=TrackCB(action="start", kind=PUMP).pack(),
            ),
        )

    # Мгновенные события: с таймером не конфликтуют, поэтому доступны всегда
    builder.row(
        InlineKeyboardButton(
            text="💧 Мокрый",
            callback_data=TrackCB(action="add", kind=DIAPER_WET).pack(),
        ),
        InlineKeyboardButton(
            text="💩 Стул",
            callback_data=TrackCB(action="add", kind=DIAPER_DIRTY).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="🍼 Бутылочка",
            callback_data=TrackCB(action="add", kind=FEED_BOTTLE).pack(),
        ),
        InlineKeyboardButton(
            text="⚖️ Вес", callback_data=MeasureCB(kind="weight").pack()
        ),
    )
    return builder.as_markup()


def running(kind: str, *, row_id: int | None = None) -> InlineKeyboardMarkup:
    """«Закончить» под сообщением о начатом таймере, рядом с отменой.

    Заведено по жалобе из чата: «начинаешь действие, а кнопка завершения
    где-то сверху в панели, это даже не читается». Так и было — подтверждение
    «Сцеживание — начали в 23:40» предлагало только «Отменить», а «⏹ Закончить»
    оставалось в панели выше, отделённое от него служебной строкой о
    закреплении. То есть в момент, когда таймер запущен и палец ещё над
    последним сообщением, до кнопки остановки надо было прокручивать чат.

    Панель кнопку не теряет: она остаётся главным экраном. Но подтверждение
    начала — это единственное сообщение, про которое точно известно, что
    человек на него смотрит **сейчас**, и именно оно обязано уметь закончить.

    `row_id` необязателен: у карточки голосового отменять нечего — она правится
    на месте, и записи под ней не одна.
    """
    row = [
        InlineKeyboardButton(
            text="⏹ Закончить",
            callback_data=TrackCB(action="stop", kind=kind).pack(),
        )
    ]
    if row_id is not None:
        row.append(
            InlineKeyboardButton(
                text="↩️ Отменить",
                callback_data=UndoCB(what="event", row_id=row_id).pack(),
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def stopped(row_id: int) -> InlineKeyboardMarkup:
    """Что делать, если «Закончить» нажали случайно.

    Две кнопки, потому что промахи бывают разные и лечатся по-разному.
    «Не заканчивать» возвращает таймеру ход: сон продолжается, запись цела.
    «Удалить запись» — прежнее поведение кнопки отмены, но названное своим
    именем: до этой правки под ответом об окончании стояло «↩️ Отменить», и
    человек, промахнувшийся по окончанию, жал её в надежде вернуть таймер — а
    терял весь сон.

    Отсюда и порядок: сперва то, что чинит промах, потом то, что стирает.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Не заканчивать",
                    callback_data=UndoCB(what="reopen", row_id=row_id).pack(),
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить запись",
                    callback_data=UndoCB(what="event", row_id=row_id).pack(),
                ),
            ]
        ]
    )


def undo(what: str, row_id: int) -> InlineKeyboardMarkup:
    """Одна кнопка под подтверждением записи.

    Отмена нужна чаще, чем кажется: тап мимо в темноте — обычное дело, а
    «лишнее кормление» в статистике недели искажает её заметно.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Отменить",
                    callback_data=UndoCB(what=what, row_id=row_id).pack(),
                )
            ]
        ]
    )


def voice_card() -> InlineKeyboardMarkup:
    """Две кнопки под разбором голосового.

    Первая карточка в проекте, у которой их две: до сих пор подтверждение было
    одной кнопкой «↩️ Отменить» под уже сделанной записью. Здесь наоборот —
    записи ещё нет, и «ничего не сохраняется молча» означает, что между
    разбором и базой стоит человек с кнопкой.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Записать",
                    callback_data=VoiceCB(action="save").pack(),
                ),
                InlineKeyboardButton(
                    text="✖️ Не то",
                    callback_data=VoiceCB(action="drop").pack(),
                ),
            ]
        ]
    )


def sex() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👧 Девочка", callback_data=SexCB(value="f").pack()
                ),
                InlineKeyboardButton(
                    text="👦 Мальчик", callback_data=SexCB(value="m").pack()
                ),
            ]
        ]
    )


def edit_child() -> InlineKeyboardMarkup:
    """Что можно поправить в карточке.

    Четыре поля — ровно те, что спрашивает мастер. Порядок тот же, что и в
    самой карточке: человек читает её сверху вниз, находит неверную строку и
    тянется к кнопке, стоящей на том же месте.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Имя", callback_data=EditCB(field="name").pack()
                ),
                InlineKeyboardButton(
                    text="✏️ Пол", callback_data=EditCB(field="sex").pack()
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Дата рождения",
                    callback_data=EditCB(field="born").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Вес при рождении",
                    callback_data=EditCB(field="weight").pack(),
                ),
            ],
        ]
    )


def remind(
    presets: Sequence[tuple[str, str, bool]],
    *,
    pickable: frozenset[str] = frozenset(),
    checkin: tuple[str, bool] | None = None,
) -> InlineKeyboardMarkup:
    """Список пресетов: по кнопке на строку плюс календарь снизу.

    `presets` — тройки «id, подпись, включён ли». Клавиатура ничего не знает
    про содержание: что за пресеты и как они называются, решает `schedule`, а
    здесь только раскладка.

    `checkin` — пара «подпись, включён ли» для периодического опроса. Он
    отдельным аргументом, а не шестым элементом `presets`, потому что это не
    пресет: строки в `reminders` у него нет, и кнопка у него своя, с другим
    `callback_data`. Слить их в один список значило бы соврать читателю о том,
    чем опрос является.

    Выключение — то же нажатие, что и включение. Отдельной кнопки «выключить»
    нет: строк стало бы вдвое больше, а промахнуться в темноте по соседней
    вдвое легче.
    """
    builder = InlineKeyboardBuilder()
    for preset_id, label, is_on in presets:
        mark = "✅" if is_on else "⬜"
        action = "off" if is_on else ("pick" if preset_id in pickable else "on")
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} {label}",
                callback_data=RemindCB(action=action, preset=preset_id).pack(),
            )
        )
    if checkin is not None:
        label, is_on = checkin
        builder.row(
            InlineKeyboardButton(
                text=f"{'✅' if is_on else '⬜'} {label}",
                callback_data=CheckinCB(action="off" if is_on else "pick").pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="📅 Календарь",
            callback_data=RemindCB(action="cal", preset="vaccine").pack(),
        )
    )
    return builder.as_markup()


def remind_hours(preset: str, choices: Sequence[int]) -> InlineKeyboardMarkup:
    """Выбор интервала перед включением почасового пресета.

    Спрашиваем один раз при включении, а не переключаем по кругу одной
    кнопкой: «2 → 3 → 4 → 2» читается только на свежую голову.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        *(
            InlineKeyboardButton(
                text=f"{hours} ч",
                callback_data=RemindCB(action="on", preset=preset, hours=hours).pack(),
            )
            for hours in choices
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=RemindCB(action="back").pack()
        )
    )
    return builder.as_markup()


def remind_calendar(shown: str) -> InlineKeyboardMarkup:
    """Переключатель между прививками и осмотрами плюс возврат к пресетам."""
    sections = (("vaccine", "💉 Прививки"), ("checkup", "🩺 Осмотры"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"• {label}" if preset == shown else label,
                    callback_data=RemindCB(action="cal", preset=preset).pack(),
                )
                for preset, label in sections
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=RemindCB(action="back").pack()
                )
            ],
        ]
    )


def checkin_card(
    kinds: Sequence[tuple[str, str]], *, nothing: str, mute: str
) -> InlineKeyboardMarkup:
    """Первый экран опроса: виды по двое в ряд, снизу отказ и отключение.

    По двое, а не по одному: видов пять, и пять строк подряд под вопросом
    читаются как список дел. По трое подписи с иконкой уже режутся.

    «Не спрашивать» стоит на самой карточке, а не только в `/remind`, потому
    что отказаться от навязанного должно быть не труднее, чем включить нужное,
    — а карточка приходит сама.
    """
    builder = InlineKeyboardBuilder()
    for kind, label in kinds:
        builder.button(text=label, callback_data=CheckinCB(action="kind", kind=kind))
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(text=nothing, callback_data=CheckinCB(action="none").pack())
    )
    builder.row(
        InlineKeyboardButton(text=mute, callback_data=CheckinCB(action="mute").pack())
    )
    return builder.as_markup()


def checkin_when(kind: str, shifts: Sequence[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Второй экран: как давно это было.

    По двое в ряд: подписи здесь длиннее, чем «2 ч» у интервала напоминаний, и
    пятью в строку Telegram обрежет их до неузнаваемости.
    """
    builder = InlineKeyboardBuilder()
    for minutes, label in shifts:
        builder.button(
            text=label, callback_data=CheckinCB(action="when", kind=kind, back=minutes)
        )
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data=CheckinCB(action="back").pack())
    )
    return builder.as_markup()


def checkin_how_long(
    kind: str, back: int, durations: Sequence[tuple[int, str]], *, still: str
) -> InlineKeyboardMarkup:
    """Третий экран, только у длящихся видов: сколько это длилось.

    Третий тап — цена того, что ни одно число в дневнике не придумано ботом.
    Подставить длительность из файла было бы на тап дешевле и означало бы
    запись, которую никто не называл, — а в сводке она сложилась бы с
    измеренными и стала бы от них неотличима.

    `still` — «ещё идёт», и он последний намеренно: это единственный ответ, у
    которого конца нет вовсе, и таких ночей меньше, чем закончившихся.
    """
    builder = InlineKeyboardBuilder()
    for minutes, label in durations:
        builder.button(
            text=label,
            callback_data=CheckinCB(action="dur", kind=kind, back=back, mins=minutes),
        )
    builder.adjust(4)
    builder.row(
        InlineKeyboardButton(
            text=still, callback_data=CheckinCB(action="run", kind=kind, back=back).pack()
        )
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data=CheckinCB(action="back").pack())
    )
    return builder.as_markup()


def checkin_hours(choices: Sequence[int]) -> InlineKeyboardMarkup:
    """Выбор интервала опроса. Близнец `remind_hours`, но со своим CB."""
    builder = InlineKeyboardBuilder()
    builder.row(
        *(
            InlineKeyboardButton(
                text=f"{hours} ч",
                callback_data=CheckinCB(action="on", hours=hours).pack(),
            )
            for hours in choices
        )
    )
    return builder.as_markup()


def stats() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Сутки", callback_data=StatsCB(period="today").pack()
                ),
                InlineKeyboardButton(
                    text="Неделя", callback_data=StatsCB(period="week").pack()
                ),
                InlineKeyboardButton(
                    text="Месяц", callback_data=StatsCB(period="month").pack()
                ),
            ]
        ]
    )
