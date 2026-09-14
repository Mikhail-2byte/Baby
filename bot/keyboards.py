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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
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

    `what`: `event` или `measure` — таблицы разные, а id в них пересекаются.
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


def panel(*, running_kind: str | None) -> InlineKeyboardMarkup:
    """Главная клавиатура. Вид зависит от того, идёт ли таймер.

    Когда таймер идёт, его кнопки уступают место одной большой «Закончить».
    Это не украшение: перепутать «начали» и «закончили» в полусне легко, а
    лишний старт поверх идущего таймера портит статистику суток.
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
            InlineKeyboardButton(
                text="🍼 Бутылочка",
                callback_data=TrackCB(action="add", kind=FEED_BOTTLE).pack(),
            ),
        )
        builder.row(
            InlineKeyboardButton(
                text="😴 Уснула",
                callback_data=TrackCB(action="start", kind=SLEEP).pack(),
            ),
            InlineKeyboardButton(
                text="🥛 Сцеживание",
                callback_data=TrackCB(action="start", kind=PUMP).pack(),
            ),
        )

    # Подгузники доступны всегда: они мгновенные и с таймером не конфликтуют
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
            text="⚖️ Вес", callback_data=MeasureCB(kind="weight").pack()
        ),
        InlineKeyboardButton(
            text="📊 Сутки", callback_data=StatsCB(period="today").pack()
        ),
        InlineKeyboardButton(
            text="❓ Вопрос", callback_data=AskCB().pack()
        ),
        InlineKeyboardButton(
            text="🔄", callback_data=PanelCB(action="refresh").pack()
        ),
    )
    return builder.as_markup()


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
    presets: Sequence[tuple[str, str, bool]], *, pickable: frozenset[str] = frozenset()
) -> InlineKeyboardMarkup:
    """Список пресетов: по кнопке на строку плюс календарь снизу.

    `presets` — тройки «id, подпись, включён ли». Клавиатура ничего не знает
    про содержание: что за пресеты и как они называются, решает `schedule`, а
    здесь только раскладка.

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
