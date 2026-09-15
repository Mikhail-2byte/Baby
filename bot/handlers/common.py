"""Команды общего назначения: помощь, панель, отмена, проверка живости."""

import logging

from aiogram import Bot, Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import ChatMemberUpdated, FSInputFile, Message

from bot import keyboards, texts
from bot.db.models import Family
from bot.filters import IN_GROUP, IN_PRIVATE
from bot.services import backup, panel, sending
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

router = Router(name="common")


@router.my_chat_member(
    ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER), IN_GROUP
)
async def bot_added(event: ChatMemberUpdated, bot: Bot, family: Family) -> None:
    """Бота добавили в группу — здороваемся один раз и отдаём клавиатуру.

    Это единственное сообщение, которое бот говорит по своей инициативе, и
    заведено оно не ради вежливости: клавиатуру под полем ввода нельзя
    приложить ни к панели, ни к её правке, а до первого ответа бота её в чате
    нет вообще. Семья, добавившая бота и не написавшая ни одной команды, видела
    бы половину бота несуществующей — ровно это и случилось 15.09.

    **Переход именно `не член → член`.** `member → administrator` под фильтр не
    попадает, а он случается через минуту после добавления, когда боту дают
    права на закрепление. Второе приветствие на выдачу прав было бы первой же
    поломкой.

    **`IN_GROUP` обязателен.** Тот же переход `kicked → member` прилетает из
    лички, когда человек разблокировал бота, — а там `FamilyMiddleware` не
    кладёт `family`, aiogram падает на резолве аргумента, и апдейт теряется
    навсегда: offset Telegram сдвигается независимо от исхода. Та же ловушка,
    что `F.from_user` у голосового.

    `member` в сигнатуре нет по той же причине: у анонимного администратора и в
    группе, привязанной к каналу, автора не существует.

    **Апдейт этого типа приходит только потому, что есть этот хендлер.**
    `bot/__main__.py` отдаёт `allowed_updates=dp.resolve_used_update_types()`, а
    тип, который никто не наблюдает, Telegram не присылает вовсе. Заменивший тот
    вызов явным списком выключит приветствие, и симптомом будет тишина.

    Место роутера тут ни при чём: `my_chat_member` — отдельный наблюдатель, за
    апдейт он ни с кем не спорит.

    Через `sending.send`, а не `bot.send_message`: приветствие уходит в момент,
    когда права в чате наименее определены, и отказ обязан лечь в лог статусом,
    а не исключением.
    """
    status, _ = await sending.send(
        bot, family, texts.GREETING, reply_markup=keyboards.home()
    )
    log.info("Добавлен в чат %s (семья #%s): %s", family.chat_id, family.id, status)


@router.message(IN_PRIVATE)
async def private_stub(message: Message) -> None:
    """В личке работы нет — только объяснение, почему.

    Ловит любое сообщение, не только команды: человек, написавший боту в личку
    «привет», должен получить ответ, а не молчание.

    **Единственное место, где текстовый ответ намеренно идёт без
    `keyboards.home()`**, и исключение написано здесь, а не только в истории
    правок: все три кнопки ведут в групповые хендлеры, а в личке нет ни семьи,
    ни ребёнка — вышла бы вечная тройка кнопок, не отвечающих ничего.
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

    Он же объявленный путь возврата: клавиатуру можно скрыть, и `/help` её
    вернёт. Особенностью `/help` это быть перестало — вернёт её теперь любой
    ответ, — но человеку в тексте помощи обещан именно `/help`.
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
        await message.answer(texts.NOTHING_TO_CANCEL, reply_markup=keyboards.home())
        return
    await state.clear()
    await message.answer(texts.CANCELLED, reply_markup=keyboards.home())


@router.message(IN_GROUP, Command("ping"))
async def ping(message: Message) -> None:
    """Проверка живости. Отвечает временем бота, а не «pong».

    Разница существенная: расхождение часов и уехавшая таймзона — первое, на
    что смотришь, когда напоминание пришло не тогда.
    """
    await message.answer(
        f"Живой. Сейчас {tu.now_utc():%H:%M} UTC.", reply_markup=keyboards.home()
    )


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
        await message.answer(
            "Не смог снять копию — подробности в логе.",
            reply_markup=keyboards.home(),
        )
        return

    await message.answer_document(
        FSInputFile(dest), caption=f"Копия базы за {day.isoformat()}"
    )
