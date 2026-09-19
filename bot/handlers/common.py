"""Команды общего назначения: помощь, панель, отмена, проверка живости."""

import logging

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import (
    ADMINISTRATOR,
    IS_MEMBER,
    IS_NOT_MEMBER,
    MEMBER,
    ChatMemberUpdatedFilter,
    Command,
)
from aiogram.fsm.context import FSMContext
from aiogram.types import ChatMemberUpdated, FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import settings
from bot.db import repo
from bot.db.models import Child, Family
from bot.filters import IN_GROUP, IN_PRIVATE
from bot.services import backup, panel, sending
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

router = Router(name="common")


@router.my_chat_member(
    ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER), IN_GROUP
)
async def bot_added(
    event: ChatMemberUpdated,
    bot: Bot,
    session: AsyncSession,
    family: Family,
    child: Child | None,
) -> None:
    """Бота добавили в группу — здороваемся один раз и отдаём клавиатуру.

    Первое слово бота в чате, и заведено оно не ради вежливости: клавиатуру под
    полем ввода нельзя приложить ни к панели, ни к её правке, а до первого
    ответа бота её в чате нет вообще. Семья, добавившая бота и не написавшая ни
    одной команды, видела бы половину бота несуществующей — ровно это и
    случилось 15.09.

    Говорит его `greet`, и вызывающих у неё двое: этот хендлер и опоздавшее
    приветствие из `FamilyMiddleware` — апдейт о добавлении теряется на
    выключенном боте вместе со всей очередью Telegram.

    **Переход именно `не член → член`.** `member → administrator` под фильтр не
    попадает, а он случается через минуту после добавления, когда боту дают
    права на закрепление. Второе приветствие на выдачу прав было бы первой же
    поломкой; отвечает на этот переход `bot_promoted`, и отвечает другим — см.
    его докстроку про то, почему переход там взят точный.

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

    **Права видны из самого апдейта.** Пришедший по ссылке из лички добавляется
    сразу администратором — `?startgroup=true&admin=…` отмечает галочки за
    человека, — и просить в первом же слове то, что уже дали, значит выглядеть
    невнимательным. Статус лежит в `new_chat_member`, спрашивать Telegram не
    надо.
    """
    await greet(
        bot,
        session,
        family,
        child,
        admin=event.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR,
    )


async def greet(
    bot: Bot,
    session: AsyncSession,
    family: Family,
    child: Child | None,
    *,
    admin: bool | None,
) -> None:
    """Поздороваться в группе. Зовут двое — добавление и опоздавшая реплика.

    **Сообщений два, и это цена двух разметок.** Одно сообщение не носит
    одновременно клавиатуру под полем ввода и inline-кнопку. Отдать кнопку
    приветствию, а клавиатуру доставить «как-нибудь потом» — это буквально
    поломка 15.09, когда клавиатуру несли три необязательных пути и она не
    доехала ни разу. Поэтому носитель клавиатуры остаётся прежним, а призыв к
    первому шагу едет вторым сообщением — заодно ближе к полю ввода, то есть к
    большому пальцу.

    **Второе сообщение только при пустой карточке.** Бота, вернувшегося в чат,
    где ребёнок давно заведён, звать заводить его заново значит предложить
    кнопку, которая объяснит, что нажимать её не надо.

    `admin=None` — «спросить не удалось»; на неуверенности молчим, а про права
    всё равно напомнит мастер. См. `sending.is_admin`.
    """
    text = texts.GREETING
    if admin is False:
        text = f"{texts.GREETING}{texts.NL * 2}{texts.ADMIN_WHY}"
    status, _ = await sending.send(bot, family, text, reply_markup=keyboards.home())
    if child is None:
        await sending.send(
            bot, family, texts.FIRST_STEP, reply_markup=keyboards.start_card()
        )

    # Отметка последней: поздоровавшись наполовину, лучше поздороваться ещё раз,
    # чем замолчать навсегда
    await repo.mark_greeted(session, family, tu.now_utc())
    log.info("Поздоровался в чате %s (семья #%s): %s", family.chat_id, family.id, status)


@router.my_chat_member(
    ChatMemberUpdatedFilter(MEMBER >> ADMINISTRATOR), IN_GROUP
)
async def bot_promoted(event: ChatMemberUpdated, bot: Bot, family: Family) -> None:
    """Боту дали права — говорим спасибо и выпускаем панель заново.

    **Переход берётся точный, а не `PROMOTED_TRANSITION` из aiogram.** Тот
    разворачивается в `(KICKED | LEFT | MEMBER | RESTRICTED) >> ADMINISTRATOR`,
    то есть включает `left → administrator` — обычное добавление по нашей же
    ссылке с `admin=`, на которое уже сходится `bot_added`.

    Двух сообщений человек при этом сегодня не получил бы: aiogram
    останавливается на первом сошедшемся хендлере роутера, а `bot_added`
    зарегистрирован выше. Но это и есть довод — молчание держалось бы на
    **порядке регистрации**, то есть на том, что в коде никак не выражено.
    Переставив две функции местами, мы поменяли бы поведение бота и не поняли
    бы почему. Точный переход делает фильтры непересекающимися, и порядок
    перестаёт значить что-либо. Сторожит
    `test_no_transition_is_claimed_twice`.

    `IS_MEMBER >> IS_ADMIN` не годится по зеркальной причине: `IS_MEMBER`
    включает `ADMINISTRATOR`, а `administrator → administrator` Telegram шлёт
    на каждую правку набора прав — благодарность повторялась бы.

    Повышение — это переход **внутри** членства, и записывается он так.

    Панель перевыпускается, а не просто закрепляется: `_pin` приватна и зовётся
    только из `_publish`, а `refresh` без `force_new` правит панель на месте и
    до закрепления не доходит вовсе. Побочный эффект желанный — панель заодно
    уезжает вниз чата, туда, где на неё смотрят.

    `IN_GROUP` обязателен по той же ловушке, что у `bot_added`.
    """
    status = await sending.deliver(
        bot, family, texts.ADMIN_THANKS, reply_markup=keyboards.home()
    )
    log.info("Права в чате %s (семья #%s): %s", family.chat_id, family.id, status)
    await panel.refresh(bot, family.id, force_new=True)


@router.message(IN_PRIVATE)
async def private_guide(message: Message, bot: Bot) -> None:
    """В личке работы нет — зато есть инструкция, как её завести.

    Ловит любое сообщение, не только команды: человек, написавший боту «привет»
    или приславший голосовое, находится ровно в одном состоянии — «не понимаю,
    что делать», — и ответ у этого состояния один. Отдельного `/start` поэтому
    нет: он проваливается сюда же, а второй текст разошёлся бы с первым.

    Раньше здесь стоял отказ, и личка была **единственным местом, где текстовый
    ответ намеренно шёл без разметки вовсе**. Теперь у сообщения есть
    inline-кнопка, то есть личка перестала быть исключением из правила про
    `keyboards.home()`: правило говорит про ответы **без inline-разметки**.
    Саму `home()` прикладывать по-прежнему нельзя — все три кнопки ведут в
    групповые хендлеры, а в личке нет ни семьи, ни ребёнка.

    `username` берётся из `bot.me()`; aiogram его кеширует, и на этот вызов
    сеть уходит один раз за жизнь процесса.
    """
    me = await bot.me()
    await message.answer(
        texts.PRIVATE_GUIDE, reply_markup=keyboards.add_to_group(me.username or "")
    )


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
    """Снимок базы по требованию, файлом в чат. Только в чат владельца.

    Отправка файла в чат, а не только на диск, и есть весь смысл: вес и
    кормления за первый год невосстановимы, а диск домашнего ПК — один.
    Telegram здесь работает бесплатным офсайт-хранилищем.

    **И ровно поэтому команда владельческая.** В снимке лежит `baby.db`
    целиком, а база одна на все семьи: та же команда, набранная в чужой группе,
    отдала бы туда дневник чужого ребёнка — имя, дату рождения, веса,
    температуры, заметки. Пока семья одна, этого не видно; с раздачей бота это
    не риск, а прямое следствие.

    Молчать в чужом чате нельзя: `/backup` объявлена в `texts.COMMANDS`, то
    есть предложена самим ботом, а молчание на им же предложенную команду хуже
    отсутствия команды. Убрать её из меню тоже нечем — скоуп у меню один на все
    группы.

    Пустой `OWNER_CHAT_ID` оставляет прежнее поведение: у того, кто держит бота
    для себя одного, чужих чатов нет вовсе.
    """
    if settings.owner_chat_id and family.chat_id != settings.owner_chat_id:
        log.info("Отказ в /backup чату %s (семья #%s)", family.chat_id, family.id)
        await message.answer(texts.OWNER_ONLY, reply_markup=keyboards.home())
        return

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
