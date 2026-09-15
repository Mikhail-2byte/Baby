"""Постоянная клавиатура под полем ввода: три кнопки, видимые всегда.

Панель — главный экран бота, но добраться до неё можно было только командой:
набрать `/panel` или открыть «/» и выбрать пункт из тринадцати. Ночью, одной
рукой, не глядя — это ровно то, ради отсутствия чего панель и заводилась.
А сама панель уезжает вверх от каждого подтверждения и переезжает вниз только
на пятнадцатом разошедшемся сообщении.

Обычная клавиатура (`keyboards.home`) — единственный механизм Telegram,
который держит кнопку на экране постоянно. Цена у неё одна: нажатие приходит
не колбэком, а обычным сообщением от человека. Отсюда два следствия, и оба
живут в этом файле.

**Сообщение-нажатие убирается сразу.** Три «📋 Панель» подряд в ленте ночной
переписки хуже, чем три `/panel`. Убирается через `sending.delete_one`, то
есть без прав администратора не убирается вовсе — и это не повод не работать.

**Место роутера в списке задано тремя границами, и все три обязательны.**
Ниже `alarm` — граница безопасности не двигается ни для кого, включая
клавиатуру из трёх кнопок. Выше `baby` — иначе тап посреди мастера молча
станет именем ребёнка. Выше `qa` и `measure` — иначе метку съест фильтр
ожидания вопроса или обработчик голого числа. Подробнее — в докстроке
`handlers/__init__.py`.

Своего в файле нет ничего: все три кнопки зовут тот же код, что и команды с
inline-кнопками. Вторая копия «выпустить панель» или «сводка за сутки»
разошлась бы с первой молча.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards
from bot.db.models import Child, Family
from bot.filters import IN_GROUP
from bot.handlers import qa, views
from bot.services import panel, sending

log = logging.getLogger(__name__)

router = Router(name="home")


async def _open_panel(
    message: Message, bot: Bot, session: AsyncSession, family: Family,
    child: Child | None,
) -> None:
    """То же, что `/panel`: выпустить панель заново, мимо дебаунса.

    Человек нажал именно потому, что панели не видит, — значит правка
    невидимого сообщения выглядела бы как «бот не ответил».
    """
    await panel.refresh(bot, family.id, force_new=True)


async def _ask(
    message: Message, bot: Bot, session: AsyncSession, family: Family,
    child: Child | None,
) -> None:
    if message.from_user is None:
        return
    await qa.start_question(message, family, message.from_user.id)


async def _today(
    message: Message, bot: Bot, session: AsyncSession, family: Family,
    child: Child | None,
) -> None:
    await views.send_today(message, session, family, child)


# Ключи — метки из `keyboards`, а не свои строки: клавиатура и её обработчик
# обязаны говорить об одном и том же. Сторожит
# `test_every_home_button_has_an_action` — кнопка без действия не отвечает
# ничего, а молчание бота на им же предложенную кнопку хуже отсутствия кнопки
ACTIONS = {
    keyboards.HOME_PANEL: _open_panel,
    keyboards.HOME_ASK: _ask,
    keyboards.HOME_TODAY: _today,
}


@router.message(IN_GROUP, F.text.in_(set(ACTIONS)))
async def tapped(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    family: Family,
    child: Child | None,
) -> None:
    """Нажата кнопка постоянной клавиатуры.

    Фильтр собран **из таблицы действий**, а не из отдельного списка меток, и
    это важнее, чем выглядит. Метка, попавшая в клавиатуру, но не в таблицу,
    при отдельном списке сошлась бы с фильтром, не сделала бы ничего — и
    сообщение при этом ещё и стёрлось бы, то есть поломка стала бы невидимой.
    Собранный отсюда фильтр такую метку не узнаёт вовсе: она уедет вниз по
    роутерам, останется в ленте без ответа, и это видно глазами.

    Совпадение точное, а не по подстроке: метка с эмодзи руками не набирается,
    и реплика «панель где-то выше» проходит мимо нетронутой.

    Сначала действие, потом уборка. Три довода, по убыванию: то же правило,
    что «ответ человеку уходит раньше, чем перерисовывается панель»; удалять
    первым значило бы навсегда запретить здесь `message.reply` — реплай на
    удалённое Telegram отвергает; и без прав администратора действие обязано
    случиться в любом случае, а такой порядок делает это очевидным.
    """
    await ACTIONS[message.text or ""](message, bot, session, family, child)
    await sending.delete_one(bot, family, message.message_id)
