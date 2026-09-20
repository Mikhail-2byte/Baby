"""Апдейт → имя действия. Чистая функция, из которой растёт вся аналитика.

Ни сети, ни базы, ни состояния: на вход объект aiogram, на выход три строки.
Так она проверяется таблицей примеров, а не прогоном бота.

**Главное правило — ни одного слова человека наружу.** Вопрос о здоровье, имя
ребёнка, число в ответ на «сколько мл», текст расшифровки сюда попадают и
отсюда не выходят: у реплики остаётся только её род. Текст вопроса живёт в
`data/qa.log`, и тот ротируется именно потому, что в нём лежит.

**Почему номера строк отсекаются.** `callback_data` кнопки правки выглядит как
`rec:shift:e:57::::-15:0:0`, и 57 здесь — номер записи в чужом дневнике.
Сложив такие строки в отчёт, мы получили бы и мусор (у каждой записи своё
«действие», счётчик везде единица), и утечку. Поэтому токены берутся, пока они
похожи на имя, — строчная латиница и подчёркивание, — и не больше четырёх.

Цена названа вслух: вместе с номерами теряется сторона кормления
(`trk:start:feed_breast:L` и `:R` сливаются) и часы пресета
(`rmd:on:feed:3` → `rmd:on:feed`). Счёт нажатий по кнопке от этого не
страдает, а разводить исключения по классам значило бы завести вторую копию
реестра кнопок, которая молча состарится.
"""

import re

from aiogram.types import Update

from bot import keyboards

# Токен имени: строчная латиница и подчёркивание. Цифра в токене означает
# номер строки, смещение или `qid` — всё, чему в имени действия не место
_NAME = re.compile(r"^[a-z_]{1,24}$")

# Больше четырёх незачем: ни у одного CallbackData-класса проекта осмысленных
# полей подряд больше нет, а длинный хвост раздувает `action` без пользы
MAX_TOKENS = 4

BUTTON = "button"
COMMAND = "command"
HOME = "home"
TEXT = "text"
VOICE = "voice"
MEMBER = "member"
OTHER = "other"

# Префикс кнопок вердикта. Единственное место, где из `callback_data` берётся
# не только имя: `qid` склеивает тап с вопросом в `qa.log`, и без него вердикт
# не про что
VERDICT_PREFIX = keyboards.QaCB.__prefix__


def button_action(data: str) -> tuple[str, str]:
    """Имя кнопки и её `detail` из сырой `callback_data`.

    Вынесена отдельно от `describe`, потому что проверяется таблицей примеров
    по всем префиксам проекта, а подделывать ради этого `Update` было бы
    длиннее самой проверки.
    """
    parts = data.split(":")
    kept: list[str] = []
    for token in parts:
        if len(kept) >= MAX_TOKENS or not _NAME.match(token):
            break
        kept.append(token)

    action = ":".join(kept)
    detail = ""
    if parts[0] == VERDICT_PREFIX and len(parts) > 2:
        # `qav:up:7a3f1c88` — третье поле и есть `qid`
        detail = parts[2][:32]
    return action or OTHER, detail


def _command(text: str) -> str:
    """«/today@BabyBot сегодня» → «/today».

    Аргументы срезаются не для краткости: в аргументе `/weight` едет вес
    ребёнка, то есть ровно то, чего в этой таблице быть не должно.
    """
    name = text[1:].split()[0].split("@")[0].lower()
    return f"/{name}" if name else OTHER


def describe(update: Update) -> tuple[str, str, str]:
    """Род реплики, имя действия и подробность (сейчас — только `qid`)."""
    if update.callback_query is not None:
        action, detail = button_action(update.callback_query.data or "")
        return BUTTON, action, detail

    if update.my_chat_member is not None:
        return MEMBER, str(update.my_chat_member.new_chat_member.status), ""

    message = update.message or update.edited_message
    if message is not None:
        if message.voice is not None or message.audio is not None:
            return VOICE, VOICE, ""
        text = (message.text or "").strip()
        if text.startswith("/"):
            return COMMAND, _command(text), ""
        if text in keyboards.HOME_LABELS:
            return HOME, text, ""
        if text:
            # Имени у реплики человека нет и быть не может: это и вопрос, и
            # имя ребёнка в мастере, и голое число в ответ на «сколько мл»
            return TEXT, TEXT, ""
        # Сообщение без текста и без голоса: служебное «такой-то добавил
        # такого-то», эхо закрепления панели, фото, стикер. Своя корзина ему
        # нужна затем, что это самый частый необработанный апдейт, и без неё
        # раздел «кнопки, которые ничего не сделали» состоял бы из него
        return OTHER, "message", ""

    return OTHER, _kind_of(update), ""


def _kind_of(update: Update) -> str:
    """Имя поля апдейта — без содержимого, каким бы оно ни было."""
    for name in update.model_dump(exclude_none=True):
        if name != "update_id":
            return str(name)[:64]
    return OTHER
