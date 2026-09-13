"""Клавиатуры и фабрики callback_data.

Отдельный класс `CallbackData` на каждый сценарий, а не один общий с новыми
полями. Причина не в чистоте: кнопки живут в истории чата неограниченно долго,
и после деплоя человек может нажать ту, что висит со вчера. Общий класс,
у которого добавилось поле, перестанет распаковывать старые кнопки — и нажатие
молча ничего не сделает. Отдельные классы стареют независимо.

`pack()` у aiogram ограничен 64 байтами, поэтому префиксы короткие.
"""

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
