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
    FEED_BREAST,
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


class HistoryCB(CallbackData, prefix="hist"):
    action: str
    row_id: int
    what: str = "e"


class NudgeCB(CallbackData, prefix="ndg"):
    kind: str
    minutes: int = -1


class TimeCB(CallbackData, prefix="tm"):
    """Новые кнопки времени; формат старых TrackCB/RecordCB сохранён."""

    action: str
    row_id: int
    field: str = "e"
    what: str = "e"


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


class RecordCB(CallbackData, prefix="rec"):
    """Правка уже сделанной записи: список, карточка, сдвиг времени, вид.

    Полей девять, и это самый широкий класс проекта — шире `CheckinCB`. Все
    девять используются с первого дня: запасных «на потом» здесь нет по той же
    причине, что у `VoiceCB`, а добавить их потом нельзя — старые кнопки в
    истории чата перестанут распаковываться. Поэтому они заведены сразу.
    Худший `pack()` укладывается в 64 байта с запасом, и это проверяет тест.

    `what`: `e` — событие, `m` — измерение. Таблицы разные, а id в них
    пересекаются — та же развилка, что у `UndoCB`.

    `action` различает `open` и `card` не для полноты: первое правит то
    сообщение, под которым нажали, второе присылает новое. Кнопка «Поправить»
    живёт под подтверждением записи, а подтверждение — это общий журнал семьи;
    превратив его в экран настроек, мы стёрли бы то, ради чего подтверждения
    вообще уходят отдельными сообщениями.

    `mins` — **накопленный** сдвиг, а не примененный: экран сдвига времени
    держит черновик в собственных кнопках и пишет в базу только по «Готово».

    `day` — суток назад от **момента нажатия**, как `CheckinCB.back`. Вшитая
    дата у кнопки, живущей в истории вечно, завтра соврала бы.
    """

    action: str
    what: str = "e"
    row_id: int = 0
    field: str = ""
    kind: str = ""
    side: str = ""
    mins: int = 0
    day: int = 0
    page: int = 0


class StatsCB(CallbackData, prefix="st"):
    period: str


class AskCB(CallbackData, prefix="ask"):
    """Кнопка «Вопрос» на панели.

    Полей нет и не предвидится: вопрос приходит следующей репликой, а не
    кнопкой. Отдельный класс всё равно свой — см. докстроку модуля про то,
    как стареют кнопки в истории чата.
    """


class QaCB(CallbackData, prefix="qav"):
    """Оценка человеком ответа на вопрос: 👍 или 👎.

    Отдельный класс, а не поле в `AskCB`: та ставит ожидание вопроса, эта
    судит уже сказанное. Стареть они обязаны независимо — кнопка вердикта
    висит под конкретным ответом месяцами, а «❓ Вопрос» перевыпускается с
    каждой панелью.

    `qid` — строка, а не номер строки базы: он склеивает тап с записью в
    `data/qa.log`, где лежит сам вопрос. По `message_id` склеить было бы
    нельзя, ответ и его оценка приходят разными апдейтами.

    Двоеточия в `qid` быть не может по построению — `pack()` на нём бросает
    `ValueError`, и сторожит это тест, а не внимательность.
    """

    action: str  # `up` или `down`
    qid: str = ""


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


class StartCB(CallbackData, prefix="new"):
    """Кнопка «Завести карточку» под приветствием в группе.

    Отдельный класс, а не `EditCB`: тот про правку уже заведённой карточки, а
    это первый шаг новой семьи. Сценарии разные, и классы обязаны стареть
    независимо — см. докстроку модуля.

    Поле одно, и оно есть, хотя значение пока единственное. Пустой класс
    пришлось бы расширять **полем**, а от нового поля старые кнопки в истории
    чата перестают распаковываться; новое **значение** их не трогает. Так уже
    было с `UndoCB.what="reopen"`.
    """

    action: str


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


# Права, которые просим отметить на экране добавления бота в группу. Ровно те
# два, которые бот умеет использовать: закрепление панели (`panel._pin`) и
# уборка нажатий постоянной клавиатуры (`sending.delete_one`). Просить больше
# значило бы просить впрок — а человек видит этот список в ту секунду, когда
# решает, доверять боту или нет.
ADD_RIGHTS = "pin_messages+delete_messages"


def add_to_group(username: str) -> InlineKeyboardMarkup:
    """Единственная кнопка проекта со ссылкой, а не с `callback_data`.

    `?startgroup=true` — механизм самого Telegram: клиент открывает выбор
    группы и добавляет бота туда. `&admin=` заодно отмечает галочки прав, то
    есть превращает пункт инструкции «сделайте администратором» в уже сделанное
    действие. Клиент, который параметр проигнорирует, ничего не ломает: о
    правах попросит приветствие в группе, а потом один раз напомнит мастер.

    У `url`-кнопки нет обработчика, и это её главное свойство здесь: она не
    стареет в истории чата и не может молча ничего не сделать.

    Запреты, записанные выше для меток постоянной клавиатуры — без U+FE0F и без
    слов из `redflags.yaml`, — к этой подписи **не относятся**: подпись
    inline-кнопки видят только глаза, обратно в бота она не возвращается.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить в группу",
                    url=f"https://t.me/{username}?startgroup=true&admin={ADD_RIGHTS}",
                )
            ]
        ]
    )


def start_card() -> InlineKeyboardMarkup:
    """Вход в мастер `/baby` из приветствия — чтобы первый шаг был одним тапом."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👶 Завести карточку",
                    callback_data=StartCB(action="baby").pack(),
                )
            ]
        ]
    )


def panel(*, running_kind: str | None, running_id: int | None = None) -> InlineKeyboardMarkup:
    """Только повседневные действия; старые callback-классы не меняем."""
    if running_kind is not None:
        return running(running_kind, row_id=running_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤱 Кормление", callback_data=TrackCB(
                action="start", kind=FEED_BREAST).pack()),
        ],
        [
            InlineKeyboardButton(text="😴 Сон", callback_data=TrackCB(
                action="start", kind=SLEEP).pack()),
            InlineKeyboardButton(text="⋯ Ещё", callback_data=PanelCB(action="more").pack()),
        ],
    ])


def more() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚖️ Вес", callback_data=MeasureCB(kind="weight").pack()),
         InlineKeyboardButton(text="📖 История", callback_data=RecordCB(action="list").pack())],
        [InlineKeyboardButton(text="🕐 Напоминания о таймере", callback_data=NudgeCB(
            kind="menu").pack())],
        [InlineKeyboardButton(text="⚙️ Напоминания", callback_data=RemindCB(action="back").pack())],
        [InlineKeyboardButton(
            text="🔕 Отключить опросы", callback_data=PanelCB(action="quiet").pack())],
    ])


def _fix_button(what: str, row_id: int) -> InlineKeyboardButton:
    """«Поправить» под подтверждением: короткий путь в карточку записи.

    Второй вход в правку существует рядом со списком не для симметрии. Список
    нужен утром, когда подтверждение уехало вверх; эта кнопка — тому, кто
    заметил промах в ту же секунду, и ему искать сводку незачем.
    """
    return InlineKeyboardButton(
        text="✏️ Изменить",
        callback_data=RecordCB(action="card", what=what, row_id=row_id).pack(),
    )


def running(kind: str, *, row_id: int | None = None) -> InlineKeyboardMarkup:
    """Окончание всегда привязано к записи, а не к любому текущему таймеру."""
    if row_id is None:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="📋 Открыть панель", callback_data=PanelCB(action="show").pack())
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Закончить сейчас", callback_data=TimeCB(
            action="stop", row_id=row_id).pack())],
        [InlineKeyboardButton(text="🕐 Указать время окончания", callback_data=TimeCB(
            action="ask", row_id=row_id, field="e").pack())],
    ])


def stopped(row_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_fix_button("e", row_id)]])


def undo(what: str, row_id: int) -> InlineKeyboardMarkup:
    """Одна кнопка под подтверждением записи.

    Отмена нужна чаще, чем кажется: тап мимо в темноте — обычное дело, а
    «лишнее кормление» в статистике недели искажает её заметно.

    Рядом «✏️ Поправить»: промах бывает не только лишней записью, но и записью
    не того вида и не в то время, — а удалять и заводить заново значит терять
    и то, что было верно.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Отменить",
                    callback_data=UndoCB(what=what, row_id=row_id).pack(),
                ),
                _fix_button("m" if what == "measure" else "e", row_id),
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


def qa_verdict(qid: str) -> InlineKeyboardMarkup:
    """Две кнопки под ответом на вопрос — единственный источник правды о том,
    был ли бот прав.

    Автоматикой это не измеряется вовсе. `qa.log` знает исход — ответил,
    показал фрагменты, промолчал, — то есть что бот **сделал**, а не был ли
    он при этом прав. Модель-судья в контуре не годится: её ошибки были бы
    неотличимы от ошибок подсудимого, и этот довод в проекте уже записан — см.
    `kb/eval_qa.py`.

    Кнопки едут под **всеми** исходами, включая «не нашёл». 👍 под молчанием
    означает «и правильно, что промолчал», а это ровно то число, ради которого
    заведён семантический сторож молчания: цена его осторожности названа вслух
    и до сих пор не измерялась ничем.

    Цена самой кнопки названа тут же: одно сообщение двух разметок не носит,
    значит ответ на вопрос перестаёт быть носителем `home()`. Клавиатуру
    привозит предыдущее сообщение — «Напишите вопрос» или эхо расшифровки, —
    и оба на пути обязательны.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👍 Помогло",
                    callback_data=QaCB(action="up", qid=qid).pack(),
                ),
                InlineKeyboardButton(
                    text="👎 Не то",
                    callback_data=QaCB(action="down", qid=qid).pack(),
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
    """Переключатель периода под сводкой — и вход в список записей.

    «Записи» стоят вторым рядом, а не четвёртой кнопкой в первом: там уже три
    подписи, и четвёртая упёрлась бы ровно в тот замер, из-за которого панель
    держит по две кнопки в ряду.

    Вход в правку живёт здесь потому, что сводка — единственный экран, куда
    ведёт один тап с постоянной клавиатуры. «📊 Сутки» → «📋 Записи» — два
    тапа, и это единственный путь к записи, которая уже уехала из ленты.
    """
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
            ],
            [
                InlineKeyboardButton(
                    text="📋 Записи", callback_data=RecordCB(action="list").pack()
                )
            ],
        ]
    )


# --- Правка записей -------------------------------------------------------


def records(
    rows: Sequence[tuple[int, str, int]],
    *,
    day: int,
    page: int,
    earlier: bool,
    later: bool,
    back_day: bool,
    forward_day: bool,
) -> InlineKeyboardMarkup:
    """Номера записей за сутки, листание страниц и переход по дням.

    Кнопки — голые номера, а не подписи видов: подпись уже напечатана строкой
    выше, и повторять её значит потерять ряд на то, что человек только что
    прочитал.

    По четыре в ряду, и это **названное исключение** из правила «не больше
    двух». Замер, из-за которого панель держит по две, был про обрезанные
    подписи — «Сцеживание» превращалось в «Сцеж…». Цифру Telegram не режет, а
    двенадцать номеров по двое дали бы шесть рядов вместо трёх. То же решение,
    что у `checkin_how_long`, где длительности идут по четыре.

    «Позже» при сегодняшнем дне не рисуется: кнопка, ведущая в пустоту, — это
    клампинг молчанием. «⬅️ К сводке» уводит через `StatsCB`, а не через свой
    экран: это тот же обработчик, что у переключателя периода.
    """
    builder = InlineKeyboardBuilder()
    for number, what, row_id in rows:
        builder.button(
            text=str(number),
            callback_data=RecordCB(
                action="open", what=what, row_id=row_id, day=day, page=page
            ),
        )
    builder.adjust(4)

    pages = []
    if earlier:
        pages.append(
            InlineKeyboardButton(
                text="⬆️ Раньше",
                callback_data=RecordCB(action="list", day=day, page=page - 1).pack(),
            )
        )
    if later:
        pages.append(
            InlineKeyboardButton(
                text="⬇️ Позже",
                callback_data=RecordCB(action="list", day=day, page=page + 1).pack(),
            )
        )
    if pages:
        builder.row(*pages)

    days = []
    if back_day:
        days.append(
            InlineKeyboardButton(
                text="⬅️ Днём раньше",
                callback_data=RecordCB(action="list", day=day + 1).pack(),
            )
        )
    if forward_day:
        days.append(
            InlineKeyboardButton(
                text="Днём позже ➡️",
                callback_data=RecordCB(action="list", day=day - 1).pack(),
            )
        )
    if days:
        builder.row(*days)

    builder.row(
        InlineKeyboardButton(
            text="⬅️ К сводке", callback_data=StatsCB(period="today").pack()
        )
    )
    return builder.as_markup()


def record_card(
    *,
    what: str,
    row_id: int,
    day: int,
    page: int,
    fields: Sequence[str] = (),
    value: bool = False,
    kinds: bool = False,
    delete: bool = True,
) -> InlineKeyboardMarkup:
    """Что можно сделать с этой записью.

    Все флаги считает хендлер: доступность зависит от вида, от того, идёт ли
    таймер, и от того, не точка ли это веса при рождении, — то есть от базы,
    про которую клавиатура не знает ничего.

    Кнопки, которой нельзя воспользоваться, здесь не бывает вовсе. Серой
    заглушки Telegram не умеет, а «нажал — ничего не произошло» читается как
    поломка бота: то же правило, по которому метка постоянной клавиатуры
    обязана иметь действие.
    """
    builder = InlineKeyboardBuilder()

    titles = {"s": "🕐 Начало", "e": "🕐 Конец", "t": "🕐 Время"}
    times = [
        InlineKeyboardButton(
            text=titles[field],
            callback_data=TimeCB(
                action="ask",
                what=what,
                row_id=row_id,
                field=field,
            ).pack(),
        )
        for field in fields
        if field in titles
    ]
    if times:
        builder.row(*times)

    second = []
    if value:
        second.append(
            InlineKeyboardButton(
                text="🍼 Объём",
                callback_data=RecordCB(
                    action="ml", what=what, row_id=row_id, day=day, page=page
                ).pack(),
            )
        )
    if kinds:
        second.append(
            InlineKeyboardButton(
                text="✏️ Вид",
                callback_data=RecordCB(
                    action="kinds", what=what, row_id=row_id, day=day, page=page
                ).pack(),
            )
        )
    if second:
        builder.row(*second)

    builder.row(InlineKeyboardButton(
        text="↩️ Отменить правку", callback_data=HistoryCB(
            action="undo", row_id=row_id, what=what).pack()),
        InlineKeyboardButton(text="🕘 История правок", callback_data=HistoryCB(
            action="list", row_id=row_id, what=what).pack()),
    )
    last = []
    if what == "e" and "e" in fields:
        builder.row(InlineKeyboardButton(
            text="↩️ Продолжить таймер",
            callback_data=UndoCB(what="reopen", row_id=row_id).pack(),
        ))
    if delete:
        last.append(
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=RecordCB(
                    action="ask", what=what, row_id=row_id, day=day, page=page
                ).pack(),
            )
        )
    last.append(
        InlineKeyboardButton(
            text="⬅️ К списку",
            callback_data=RecordCB(action="list", day=day, page=page).pack(),
        )
    )
    builder.row(*last)
    return builder.as_markup()


def record_time(
    steps: Sequence[tuple[int, str]],
    *,
    what: str,
    row_id: int,
    field: str,
    mins: int,
    day: int,
    page: int,
) -> InlineKeyboardMarkup:
    """Черновик сдвига. Шаги приходят уже отфильтрованными.

    Недопустимый шаг сюда не доезжает и кнопкой не становится — то же правило,
    что у `services/checkin.durations_for`: клампинг молча поменял бы число,
    которое человек назвал, а исчезнувшая кнопка видна.

    Накопленный сдвиг едет в `mins` самих кнопок, и до «✅ Готово» в базе не
    меняется ничего. Отсюда два следствия: брошенный посреди экран не стоит
    ничего, а в общий чат уходит одно подтверждение на одно намерение, а не по
    строке на каждый тап. При нулевом сдвиге «Готово» не рисуется: кнопка, не
    делающая ничего, — та же несуществующая кнопка.
    """
    builder = InlineKeyboardBuilder()
    for step, label in steps:
        builder.button(
            text=label,
            callback_data=RecordCB(
                action="shift",
                what=what,
                row_id=row_id,
                field=field,
                mins=mins + step,
                day=day,
                page=page,
            ),
        )
    builder.adjust(3)

    if mins:
        builder.row(
            InlineKeyboardButton(
                text="✅ Готово",
                callback_data=RecordCB(
                    action="save",
                    what=what,
                    row_id=row_id,
                    field=field,
                    mins=mins,
                    day=day,
                    page=page,
                ).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=RecordCB(
                action="open", what=what, row_id=row_id, day=day, page=page
            ).pack(),
        )
    )
    return builder.as_markup()


def record_kinds(
    choices: Sequence[tuple[str, str, str]], *, row_id: int, day: int, page: int
) -> InlineKeyboardMarkup:
    """Чем эта запись могла быть на самом деле — зеркало панели.

    По двое в ряд и в том же порядке, что на панели: промах «нажала не ту
    кнопку» лечится нажатием той кнопки, которую имели в виду, а не выбором из
    переупорядоченного списка. Поэтому же здесь есть и текущий вариант — ряд с
    выпавшей кнопкой сбил бы узнавание раскладки.

    Вид едет полной константой из `models`, а не номером в списке: номер
    сдвинулся бы от перестановки, и вчерашняя кнопка «мокрый» записала бы
    «стул». То же решение, что у `CheckinCB`.
    """
    builder = InlineKeyboardBuilder()
    for kind, side, label in choices:
        builder.button(
            text=label,
            callback_data=RecordCB(
                action="setkind",
                row_id=row_id,
                kind=kind,
                side=side,
                day=day,
                page=page,
            ),
        )
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=RecordCB(
                action="open", row_id=row_id, day=day, page=page
            ).pack(),
        )
    )
    return builder.as_markup()


def record_delete(
    *, what: str, row_id: int, day: int, page: int
) -> InlineKeyboardMarkup:
    """Спросить перед удалением — единственное необратимое действие правки.

    Под свежим подтверждением «🗑 Удалить запись» спрашивать незачем: промах
    лечится повторным тапом по панели, то же событие делается заново тем же
    движением. До записи трёхдневной давности так не дотянуться, а номер в
    списке легко перепутать с соседним.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Да, удалить",
                    callback_data=RecordCB(
                        action="del", what=what, row_id=row_id, day=day, page=page
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="⬅️ Нет",
                    callback_data=RecordCB(
                        action="open", what=what, row_id=row_id, day=day, page=page
                    ).pack(),
                ),
            ]
        ]
    )


def record_fix(row_id: int, *, field: str = "") -> InlineKeyboardMarkup:
    """Одна кнопка под сообщением тикера о закрытом им таймере.

    Ради неё правка затевалась в первую очередь. «Забыла выключить» — это
    ровно тот случай, о котором бот пишет **сам**, и починка обязана
    начинаться прямо из его сообщения. До этой кнопки текст обещал «поправьте:
    /today», и обработчика у обещания не было ни одного.

    Действие `card`, а не `open`: сообщение тикера — свидетельство о том, что
    таймер закрыли за семью, и затирать его экраном правки нельзя.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Поправить время",
                    callback_data=RecordCB(
                        action="card", what="e", row_id=row_id, field=field
                    ).pack(),
                )
            ]
        ]
    )


