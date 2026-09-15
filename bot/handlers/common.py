"""Команды общего назначения: помощь, панель, отмена, проверка живости."""

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, Message

from bot import keyboards, texts
from bot.db.models import Family
from bot.filters import IN_GROUP, IN_PRIVATE
from bot.services import backup, panel
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

router = Router(name="common")


@router.message(IN_PRIVATE)
async def private_stub(message: Message) -> None:
    """В личке работы нет — только объяснение, почему.

    Ловит любое сообщение, не только команды: человек, написавший боту в личку
    «привет», должен получить ответ, а не молчание.
    """
    await message.answer(texts.PRIVATE_ONLY)


@router.message(IN_GROUP, Command("help", "start"))
async def help_command(message: Message) -> None:
    """Помощь — и она же носитель постоянной клавиатуры.

    Отдельного сообщения «вот вам кнопки» нет намеренно: клавиатуру нельзя
    приложить ни к правке панели (`edit_message_text` её не принимает), ни к
    самой панели (в одном сообщении с inline-кнопками она не уживается), а
    заводить ради неё лишнюю реплику — плохой первый экран. Поэтому она едет с
    ответами, которые бот и так шлёт: с этим и с концом мастера `/baby`.

    Он же путь возврата: клавиатуру можно скрыть, и `/help` её вернёт.
    """
    await message.answer(texts.HELP, reply_markup=keyboards.home())


@router.message(IN_GROUP, Command("panel"))
async def show_panel(message: Message, bot: Bot, family: Family) -> None:
    """Выпустить панель заново — она уедет вниз чата и закрепится.

    Идёт мимо дебаунса: человек попросил явно и ждёт результата сейчас.
    """
    await panel.refresh(bot, family.id, force_new=True)


@router.message(IN_GROUP, Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer(texts.NOTHING_TO_CANCEL)
        return
    await state.clear()
    await message.answer(texts.CANCELLED)


@router.message(IN_GROUP, Command("ping"))
async def ping(message: Message) -> None:
    """Проверка живости. Отвечает временем бота, а не «pong».

    Разница существенная: расхождение часов и уехавшая таймзона — первое, на
    что смотришь, когда напоминание пришло не тогда.
    """
    await message.answer(f"Живой. Сейчас {tu.now_utc():%H:%M} UTC.")


@router.message(IN_GROUP, Command("backup"))
async def make_backup(message: Message, family: Family) -> None:
    """Снимок базы по требованию, файлом в чат.

    Отправка файла в чат, а не только на диск, и есть весь смысл: вес и
    кормления за первый год невосстановимы, а диск домашнего ПК — один.
    Telegram здесь работает бесплатным офсайт-хранилищем.
    """
    day = tu.local_today(family.tz)
    dest = backup.path_for(day)
    try:
        await backup.snapshot(dest)
    except Exception:
        # Единственное место, где сбой бэкапа виден человеку: ежедневный молчит
        # в лог, а тот, кто нажал сам, обязан узнать, что файла не будет
        log.exception("Снимок по /backup не снят")
        await message.answer("Не смог снять копию — подробности в логе.")
        return

    await message.answer_document(
        FSInputFile(dest), caption=f"Копия базы за {day.isoformat()}"
    )
