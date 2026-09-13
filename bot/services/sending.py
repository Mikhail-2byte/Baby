"""Отправка и удаление в чате семьи, плюс политика ошибок Telegram.

Отдельный модуль, потому что политика нужна тикеру, дайджесту и панели
(этап 2п). Держать её в `ticker.py` значило бы заставить `digest.py`
импортировать тикер, который сам импортирует дайджест.

С этапа 11 здесь же удаление: у утренней уборки политика ошибок своя, но живёт
она по тому же правилу — «разбор ответов Telegram в одном месте». Новых
статусов удаление не завело, обошлось существующими.
"""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (
    ForceReply,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

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


async def send(
    bot: Bot,
    family: Family,
    text: str,
    *,
    silent: bool = False,
    reply_markup: Markup = None,
) -> tuple[str, int | None]:
    """Отправить сообщение в чат семьи. Исключений наружу не выпускает.

    Отдаёт ещё и `message_id`: панели он нужен, чтобы потом её редактировать.

    `reply_markup` появился ради разбора незакрытого (этап 5п): фоновые
    сообщения кнопок раньше не знали вовсе — их принимал только `edit`.
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
    except Exception:
        log.exception("Не удалось отправить в чат %s", family.chat_id)
        return RETRY, None


async def deliver(
    bot: Bot, family: Family, text: str, *, reply_markup: Markup = None
) -> str:
    """То же, что `send`, но без `message_id` — тикеру и дайджесту он не нужен."""
    status, _ = await send(bot, family, text, reply_markup=reply_markup)
    return status


async def edit(
    bot: Bot,
    family: Family,
    message_id: int,
    text: str,
    reply_markup: EditMarkup = None,
) -> str:
    """Перерисовать сообщение в чате семьи. Исключений наружу не выпускает.

    Фоновый двойник `views.edit_or_ignore`: тот работает от `CallbackQuery` и
    обязан пропускать наружу неожиданные ошибки, чтобы они дошли до нажавшего.
    Здесь наоборот — видимых ошибок быть не должно, а вместо исключения
    возвращается статус, по которому панель решает, править дальше или
    выпускать новую.
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
    except Exception:
        log.exception("Не удалось отредактировать в чате %s", family.chat_id)
        return RETRY


# --- Удаление (этап 11) -------------------------------------------------------

# Признаки «прав не хватает» в тексте `TelegramBadRequest`. Telegram отвечает
# по-разному в зависимости от того, что именно отобрали, а различать это важно:
# без разделения бот без права `can_delete_messages` делал бы каждое утро одну
# неудачную пачку плюс сотню неудачных одиночных удалений.
#
# **«message can't be deleted» сюда не входит, и это не описка.** Оно означает
# «вот это конкретное сообщение неудаляемо» — служебное о создании супергруппы,
# слишком старое, — а не «прав нет». Приняв его за отказ в правах, уборка
# останавливалась бы на первой же пачке, в которую попало служебное сообщение,
# то есть на первом боевом утре и потом каждое утро: знак не двигался бы
# никогда, и вся затея молча не делала бы ничего
_NO_RIGHTS = ("not enough rights", "chat_admin_required", "have no rights")


def _delete_status(exc: TelegramBadRequest) -> str:
    return FORBIDDEN if any(m in exc.message.lower() for m in _NO_RIGHTS) else BROKEN


async def delete_batch(bot: Bot, family: Family, message_ids: list[int]) -> str:
    """Удалить до ста сообщений разом. Исключений наружу не выпускает.

    `NOT_FOUND` отсюда не приходит: несуществующие id Telegram пропускает молча,
    и это как раз то, на чём держится уборка диапазоном — знать заранее, какие
    из id живые, боту неоткуда.

    А вот про существующие, но **неудаляемые** (служебное о создании
    супергруппы, слишком старое) документация молчит вовсе. Проектируем от
    худшего: считаем, что одно такое роняет всю пачку, — отсюда `BROKEN` и
    поштучный откат у вызывающего.
    """
    try:
        await bot.delete_messages(family.chat_id, message_ids)
        return OK
    except TelegramForbiddenError:
        log.warning("Нет доступа в чат %s", family.chat_id)
        return FORBIDDEN
    except TelegramBadRequest as exc:
        status = _delete_status(exc)
        log.warning(
            "Чат %s: пачка из %s не удалилась (%s): %s",
            family.chat_id, len(message_ids), status, exc.message,
        )
        return status
    except TelegramRetryAfter as exc:
        log.warning("Флуд-контроль в чате %s, ждём %s с", family.chat_id, exc.retry_after)
        return RETRY
    except Exception:
        log.exception("Не удалось удалить пачку в чате %s", family.chat_id)
        return RETRY


async def delete_one(bot: Bot, family: Family, message_id: int) -> str:
    """Удалить одно сообщение — откат после упавшей пачки.

    `BROKEN` здесь означает «именно это удалить нельзя» и ошибкой уборки не
    считается: она идёт дальше по остальным id.
    """
    try:
        await bot.delete_message(family.chat_id, message_id)
        return OK
    except TelegramForbiddenError:
        return FORBIDDEN
    except TelegramBadRequest as exc:
        return _delete_status(exc)
    except TelegramRetryAfter as exc:
        log.warning("Флуд-контроль в чате %s, ждём %s с", family.chat_id, exc.retry_after)
        return RETRY
    except Exception:
        log.exception("Не удалось удалить #%s в чате %s", message_id, family.chat_id)
        return RETRY

