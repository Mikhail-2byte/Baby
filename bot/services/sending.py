"""Отправка в чат семьи и политика ошибок Telegram.

Отдельный модуль, потому что политика нужна тикеру, дайджесту и панели.
Держать её в `ticker.py` значило бы заставить `digest.py` импортировать тикер,
который сам импортирует дайджест.

Модуль скопирован из Family_planer (см. `CLAUDE.md`). Оттуда же приехали
`delete_batch`/`delete_one` для утренней уборки чата — в этом проекте уборки
нет и в плане её нет, поэтому `delete_batch` убран. `delete_one` вернулся:
нажатие кнопки постоянной клавиатуры Telegram кладёт в чат обычным сообщением
от человека, и его надо убрать сразу.

С 21.09.2026 модуль знает про `repo`, и это расхождение с копией названо
вслух: переезд группы в супергруппу Telegram сообщает **ошибкой отправки**, и
кроме как здесь узнать о нём неоткуда. Служебное сообщение о переезде —
первая дорога, и она теряется вместе с очередью Telegram, когда ПК уходит в
сон: `bot/__main__.py` зовёт `delete_webhook(drop_pending_updates=True)`. У
фичи, доезжающей одним необязательным путём, нет ни одного пути.
"""

import logging

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramRetryAfter,
)
from aiogram.types import (
    ForceReply,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Family

log = logging.getLogger(__name__)

# То же объединение, что принимает сам aiogram. Своё имя, потому что оно
# повторяется в трёх сигнатурах этого модуля, а раньше стояло голое `=None`:
# ошибку «передали текст вместо клавиатуры» никто бы не поймал
Markup = (
    InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | ForceReply | None
)
# У правки объединение уже: `edit_message_text` принимает только inline-кнопки —
# обычную клавиатуру у отправленного сообщения Telegram менять не даёт. Разницу
# нашёл mypy; до него обе сигнатуры стояли с голым `=None` и были неотличимы
EditMarkup = InlineKeyboardMarkup | None

OK = "ok"
FORBIDDEN = "forbidden"  # бота выгнали — ретраить бессмысленно
BROKEN = "broken"  # наш текст не принимают: повтор даст ту же ошибку
RETRY = "retry"  # сеть или флуд-контроль — попробуем на следующем тике
NOT_FOUND = "not_found"  # сообщения больше нет — править нечего, нужно новое
MOVED = "moved"  # группа стала супергруппой, а переехать не вышло — см. `send`


async def send(
    bot: Bot,
    family: Family,
    text: str,
    *,
    silent: bool = False,
    reply_markup: Markup = None,
    session: AsyncSession | None = None,
) -> tuple[str, int | None]:
    """Отправить сообщение в чат семьи. Исключений наружу не выпускает.

    Отдаёт ещё и `message_id`: панели он нужен, чтобы потом её редактировать.

    `reply_markup` нужен фоновым сообщениям с кнопками: раньше клавиатуру
    принимал только `edit`.

    **`session` — это вторая дорога переезда в супергруппу.** С ней ошибка
    `TelegramMigrateToChat` не «сбой отправки», а сообщение Telegram о новом
    `chat_id`: семья переезжает, и отправка повторяется один раз, уже в живой
    чат. Без сессии чинить переезд некому — возвращаем `MOVED`, и вызывающий
    обязан считать его терминальным: повтор в мёртвый чат даст ту же ошибку
    каждую минуту, ровно как это делал дайджест 21.09.2026.

    Правило записано здесь, а не у пяти вызывающих: копий было бы пять, и
    разошлись бы они молча.
    """
    try:
        message = await bot.send_message(
            family.chat_id,
            text,
            disable_notification=silent,
            reply_markup=reply_markup,
        )
        return OK, message.message_id
    except TelegramForbiddenError:
        log.warning("Нет доступа в чат %s", family.chat_id)
        return FORBIDDEN, None
    except TelegramBadRequest as exc:
        # Сломанная разметка или превышение 4096 символов. Ретрай даст ровно то
        # же самое, поэтому сдаёмся сразу — иначе сообщение будет вечно
        # переотправляться на каждом тике
        log.error("Чат %s не принял сообщение: %s", family.chat_id, exc.message)
        return BROKEN, None
    except TelegramRetryAfter as exc:
        log.warning("Флуд-контроль в чате %s, ждём %s с", family.chat_id, exc.retry_after)
        return RETRY, None
    except TelegramMigrateToChat as exc:
        if not await _move(session, family, exc.migrate_to_chat_id):
            return MOVED, None
        # Второй переезд подряд невозможен, поэтому повтор идёт без сессии:
        # зациклиться здесь не на чем
        return await send(bot, family, text, silent=silent, reply_markup=reply_markup)
    except Exception:
        log.exception("Не удалось отправить в чат %s", family.chat_id)
        return RETRY, None


async def _move(
    session: AsyncSession | None, family: Family, new_chat_id: int
) -> bool:
    """Перевезти семью на новый `chat_id`. `False` — перевозить было некому."""
    log.warning("Чат %s стал супергруппой %s", family.chat_id, new_chat_id)
    if session is None:
        return False
    return await repo.migrate_family_chat_id(session, family.chat_id, new_chat_id)


async def deliver(
    bot: Bot,
    family: Family,
    text: str,
    *,
    reply_markup: Markup = None,
    session: AsyncSession | None = None,
) -> str:
    """То же, что `send`, но без `message_id` — тикеру и дайджесту он не нужен."""
    status, _ = await send(
        bot, family, text, reply_markup=reply_markup, session=session
    )
    return status


async def delete_one(bot: Bot, family: Family, message_id: int) -> bool:
    """Убрать одно сообщение из чата семьи. Исключений наружу не выпускает.

    Ловим всё подряд намеренно, той же доктриной, что `panel._pin`: удаление —
    украшение. Без права `can_delete_messages` нажатие просто останется в
    ленте, и это не повод не делать то, ради чего его нажали. «message can't be
    deleted», «not enough rights», сетевой сбой — различать их здесь незачем.

    `message_id` только по ключу: позиционно он уйдёт в
    `business_connection_id` — та же ловушка сигнатуры aiogram 3.31, на которой
    уже попадался `unpin_chat_message` в `panel._pin`.
    """
    try:
        await bot.delete_message(family.chat_id, message_id=message_id)
        return True
    except Exception:
        log.info(
            "Сообщение %s в чате %s осталось", message_id, family.chat_id, exc_info=True
        )
        return False


async def is_admin(bot: Bot, family: Family) -> bool | None:
    """Есть ли у бота права администратора в чате семьи. Не падает никогда.

    Единственное в проекте обращение к Telegram с вопросом о собственных
    правах. Зовут его ровно там, где ответ меняет текст, и оба места случаются
    один раз за жизнь семьи: напоминание в конце мастера `/baby` и опоздавшее
    приветствие, у которого нет апдейта о добавлении со статусом внутри.
    Постоянной проверки нет нигде: и закрепление, и уборка нажатий —
    украшения, они молча глотают отказ и работают дальше.

    **Три значения, а не два.** `None` означает «спросить не удалось»: сеть,
    прокси, что угодно. Свернуть его в `False` значило бы напоминать про права
    на каждом сетевом сбое, свернуть в `True` — читается задом наперёд.
    Догадка остаётся догадкой и объявляется — вызывающий на `None` молчит.
    """
    try:
        me = await bot.me()
        member = await bot.get_chat_member(family.chat_id, me.id)
    except Exception:
        log.info("Не спросить про права в чате %s", family.chat_id, exc_info=True)
        return None
    return member.status == ChatMemberStatus.ADMINISTRATOR


async def edit(
    bot: Bot,
    family: Family,
    message_id: int,
    text: str,
    reply_markup: EditMarkup = None,
    session: AsyncSession | None = None,
) -> str:
    """Перерисовать сообщение в чате семьи. Исключений наружу не выпускает.

    Фоновый двойник `views.edit_or_ignore`: тот работает от `CallbackQuery` и
    обязан пропускать наружу неожиданные ошибки, чтобы они дошли до нажавшего.
    Здесь наоборот — видимых ошибок быть не должно, а вместо исключения
    возвращается статус, по которому панель решает, править дальше или
    выпускать новую.

    Переехавшая группа даёт `NOT_FOUND`, а не `MOVED`: правили мы сообщение
    старого чата, и в новом его нет — значит нужна новая панель, а это уже
    написанная ветка. `MOVED` остаётся на случай, когда перевезти семью не
    удалось; тогда панели в этом чате не будет вовсе, и это правда.
    """
    try:
        await bot.edit_message_text(
            text,
            chat_id=family.chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )
        return OK
    except TelegramForbiddenError:
        log.warning("Нет доступа в чат %s", family.chat_id)
        return FORBIDDEN
    except TelegramBadRequest as exc:
        # Сравнивать надо `exc.message`: в тексте исключения есть и метод, и описание
        if "message is not modified" in exc.message:
            # Панель уже показывает что нужно. Это норма, а не сбой: именно так
            # отсеиваются холостые перерисовки. Важно, что не BROKEN и не
            # NOT_FOUND — иначе каждая из них плодила бы новую панель
            return OK
        if "message to edit not found" in exc.message or "message can't be edited" in exc.message:
            return NOT_FOUND
        log.error("Чат %s не принял правку: %s", family.chat_id, exc.message)
        return BROKEN
    except TelegramRetryAfter as exc:
        log.warning("Флуд-контроль в чате %s, ждём %s с", family.chat_id, exc.retry_after)
        return RETRY
    except TelegramMigrateToChat as exc:
        if not await _move(session, family, exc.migrate_to_chat_id):
            return MOVED
        return NOT_FOUND
    except Exception:
        log.exception("Не удалось отредактировать в чате %s", family.chat_id)
        return RETRY
