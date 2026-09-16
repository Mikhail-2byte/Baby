"""Авторегистрация семьи и участников, плюс подстановка текущего ребёнка.

Отдельной команды-инициализации нет: любой апдейт из группового чата заводит
семью и добавляет автора в участники. Это исключает тупик «бота добавили,
никто не нажал /start, ничего не работает» — а в первые дни с новорождённым
такой тупик стоил бы не удобства, а пропущенных записей.

Бот доверяет всем участникам чата: ролей и прав нет. Семья из двух человек,
которые и так видят всё, что пишет друг другу.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Chat, Message, TelegramObject, Update, User
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db import repo
from bot.db.session import Session

log = logging.getLogger(__name__)

GROUP_CHATS = {"group", "supergroup"}


# Команды, которые мастер обрывать НЕ должны.
#
# `/skip` — шаг самого мастера (пропустить вес при рождении); обрыв на нём
# означал бы, что пропустить шаг вообще нельзя.
# `/cancel` — чистит состояние сам и сам отвечает; без исключения человек
# получил бы два сообщения об одной отмене.
WIZARD_SAFE = frozenset({"skip", "cancel"})


def _interrupts_the_wizard(event: TelegramObject) -> bool:
    """Это реплика, которая должна прервать мастер?

    Смотрим на текст, а не на фильтры: outer-middleware работает до них, и в
    этом весь смысл — решение обязано быть одинаковым для всех роутеров, а не
    зависеть от того, чей фильтр сойдётся первым.

    Кроме команд сюда входят метки постоянной клавиатуры: Telegram присылает
    нажатие обычным сообщением, и для мастера оно неотличимо от ответа на
    «Как зовут?». Роутер `home` стоит выше `baby` и апдейт заберёт себе, но
    состояние FSM без этой строки осталось бы висеть — и следующая фраза
    человека, хоть через три дня, молча стала бы именем ребёнка.
    """
    if not isinstance(event, Message):
        return False
    text = (event.text or "").strip()
    if text in keyboards.HOME_LABELS:
        return True
    if not text.startswith("/"):
        return False
    # «/today@BabyBot arg» → «today»
    name = text[1:].split()[0].split("@")[0].lower()
    return bool(name) and name not in WIZARD_SAFE


async def drop_wizard_state(
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
) -> Any:
    """Команда посреди мастера обрывает мастер вслух.

    У FSM нет таймаута: брошенный на середине `/baby` висит вечно, и следующая
    реплика человека — хоть через три дня — молча становится именем ребёнка.
    Роутеры просмотра стоят раньше мастера и выигрывают у его шага; этот
    middleware делает перехват заметным.

    Ключ состояния — «чат + пользователь», поэтому реплика второго родителя
    чужой мастер не трогает.

    Вешается outer-middleware'ом на `dp.message` (`bot/__main__.py`). Год он
    пролежал написанным, но не зарегистрированным, и поломка, от которой он
    придуман, была живой: `/baby` → `/today` → следующая фраза человека молча
    становилась именем ребёнка.
    """
    state: FSMContext | None = data.get("state")
    if state is None or not _interrupts_the_wizard(event):
        return await handler(event, data)

    dropped = await state.get_state() is not None
    if dropped:
        await state.clear()

    result = await handler(event, data)
    if dropped and isinstance(event, Message):
        # Ответ на саму команду важнее — уведомление идёт следом
        await event.answer(texts.WIZARD_DROPPED)
    return result


def _display_name(user: User) -> str:
    return user.full_name or user.username or str(user.id)


def _message_of(event: TelegramObject) -> Message | None:
    """Middleware висит на `dp.update`, поэтому сюда приходит `Update`."""
    if isinstance(event, Update):
        return event.message
    return event if isinstance(event, Message) else None


class FamilyMiddleware(BaseMiddleware):
    """Кладёт в data `session`, `family`, `member` и `child`.

    `child` может быть `None` — пока карточку не завели. Хендлеры, которым он
    обязателен, проверяют это сами и отвечают `texts.NO_CHILD`: заворачивать
    апдейт здесь значило бы, что `/baby` тоже не сработает.

    **Собственное эхо бота семью не заводит.** Апдейт, автор которого — сам
    бот, приходит ровно один: служебное сообщение Telegram о закреплении, за
    которым стоит наша же `panel._pin`. Обработчика у него нет ни одного, а
    авторегистрация всё равно срабатывала — и это не теория. Стенд работает тем
    же токеном в отдельной группе; на каждом прогоне он закреплял панель, и
    боевой бот заводил у себя семью «Testy» по чужому чату. Дальше сторож
    `is_production_chat`, написанный ровно против прогона по боевой группе,
    спотыкался о собственную грязь и отказывал следующему акту.

    Заведённую семью при этом отдаём как обычно: у настоящей семьи панель
    закрепляется на каждом перевыпуске, и потерять `family` на этом апдейте
    значило бы уронить резолв аргументов у любого будущего хендлера.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat: Chat | None = data.get("event_chat")
        user: User | None = data.get("event_from_user")

        async with Session() as session:
            data["session"] = session

            if chat is None or chat.type not in GROUP_CHATS:
                return await handler(event, data)

            if await self._handle_migration(session, _message_of(event), chat):
                # Чат уехал в супергруппу — под старым chat_id семью не заводим
                return None

            # Отсекаем себя, а не «любого бота»: сообщение от анонимного админа
            # приходит от `GroupAnonymousBot` с `is_bot=True`, и проверка по
            # этому флагу решила бы иначе. Считается один раз и используется
            # дважды — для заведения семьи и для ключа `member`
            bot: Bot | None = data.get("bot")
            me = await bot.me() if bot is not None else None
            ours = user is not None and me is not None and user.id == me.id

            family = (
                await repo.family_by_chat(session, chat.id)
                if ours
                else await repo.get_or_create_family(session, chat.id, chat.title)
            )
            if family is None:
                # Наше эхо в чате, где семьи нет и заводить её не на чем.
                # `family` в `data` не кладём вовсе: хендлер, которому она
                # нужна, до такого апдейта не доберётся — у служебного
                # сообщения нет ни текста, ни подписи, ни голоса
                return await handler(event, data)

            data["family"] = family
            data["child"] = await repo.active_child(session, family.id)

            # `member` объявляют почти все хендлеры — aiogram упал бы на
            # резолве аргумента, и апдейт потерялся бы навсегда: offset
            # Telegram сдвигается независимо от исхода обработки
            if user is not None and not ours:
                data["member"] = await repo.get_or_create_member(
                    session, family.id, user.id, _display_name(user)
                )

            return await handler(event, data)

    @staticmethod
    async def _handle_migration(
        session: AsyncSession, message: Message | None, chat: Chat
    ) -> bool:
        """`True`, если чат перестал существовать под текущим chat_id.

        Служебных сообщений о переезде приходит два — из старого чата и из
        нового, — поэтому второй вызов штатно не делает ничего. Логируем по
        результату: строка «семья переехала» на несостоявшемся переезде прятала
        бы как раз ту поломку, которую ловим.
        """
        if message is None:
            return False
        if message.migrate_to_chat_id:
            moved = await repo.migrate_family_chat_id(
                session, chat.id, message.migrate_to_chat_id
            )
            if moved:
                log.info("Семья переехала: %s → %s", chat.id, message.migrate_to_chat_id)
            return True
        if message.migrate_from_chat_id:
            moved = await repo.migrate_family_chat_id(
                session, message.migrate_from_chat_id, chat.id
            )
            if moved:
                log.info(
                    "Семья переехала: %s → %s", message.migrate_from_chat_id, chat.id
                )
        return False
