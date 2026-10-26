"""Авторегистрация семьи и участников, плюс подстановка текущего ребёнка.

Отдельной команды-инициализации нет: любой апдейт из группового чата заводит
семью и добавляет автора в участники. Это исключает тупик «бота добавили,
никто не нажал /start, ничего не работает» — а в первые дни с новорождённым
такой тупик стоил бы не удобства, а пропущенных записей.

Бот доверяет всем участникам чата: ролей и прав нет. Семья из двух человек,
которые и так видят всё, что пишет друг другу.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.types import Chat, Message, TelegramObject, Update, User
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import Family, Member
from bot.db.session import Session
from bot.services import sending
from bot.services import timeutil as tu
from bot.services import usage as usage_names

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


def _announces_the_join(message: Message) -> bool:
    """Это служебное сообщение о том, что бот появился в чате?

    Поймано живьём 19.09.2026, в первый же день: семья, добавившая бота,
    получила приветствие **дважды**.

    Причина в том, что на добавление Telegram присылает два апдейта, а не
    один: `my_chat_member` и служебное сообщение «такой-то добавил такого-то».
    Второе приходит от человека, а не от бота, поэтому `ours` его не отсекает;
    текста у него нет и обработчика тоже нет — в логе оно так и лежит,
    `is not handled`. Для опоздавшего приветствия это выглядело первой
    репликой семьи, то есть второй путь срабатывал ровно тогда, когда первый
    работает.

    Отсюда и формулировка условия: второй путь существует для случая, когда
    апдейт о добавлении **потерян**. Сообщение, которое об этом же добавлении
    и рассказывает, — доказательство обратного.

    Три вида, и каждый назван: бота добавили в существующую группу
    (`new_chat_members`); группу создали вместе с ботом — обычную
    (`group_chat_created`) и сразу супергруппу (`supergroup_chat_created`).

    Цена, названная вслух: если человек напишет настоящую реплику в тот же миг,
    когда бота добавляют, и она обработается раньше `my_chat_member`,
    приветствие уйдёт дважды. Закрыть это отметкой нельзя — `bot_added`
    здоровается безусловно, и правильно делает: бота, вернувшегося после
    изгнания, надо встретить заново. Лишнее приветствие раз в жизни чата
    дешевле, чем семья, оставшаяся без клавиатуры.
    """
    return bool(
        message.new_chat_members
        or message.group_chat_created
        or message.supergroup_chat_created
    )


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

            from bot.services import revisions

            member = data.get("member")
            with revisions.actor(member.id if member is not None else None):
                result = await handler(event, data)

            message = _message_of(event)
            if (
                bot is not None
                and not ours
                and family.greeted_at is None
                and message is not None
                and not _announces_the_join(message)
            ):
                await self._greet_late(bot, session, family, data.get("child"))
            return result

    @staticmethod
    async def _greet_late(
        bot: Bot, session: AsyncSession, family: Family, child: Any
    ) -> None:
        """Поздороваться, если первый путь приветствия не доехал.

        Это не запасной вариант, а вторая половина одного. Первый путь —
        апдейт `my_chat_member` о добавлении — на домашней машине теряется:
        `bot/__main__.py` зовёт `delete_webhook(drop_pending_updates=True)`, то
        есть всё, что пришло, пока ПК спал, выбрасывается. Семья создала группу
        и добавила бота ночью — приветствия не будет **никогда**, а вместе с ним
        не будет и клавиатуры под полем ввода, то есть половины бота. Ровно тот
        класс, про который в проекте записано: у фичи, доезжающей одним
        необязательным путём, нет ни одного пути.

        Стоит ПОСЛЕ хендлера, по образцу `drop_wizard_state`: ответ на саму
        реплику важнее. Реплику это не съедает — middleware хендлеру не
        конкурент, он его зовёт.

        Про права здесь приходится спрашивать: статуса, который лежит в апдейте
        о добавлении, тут нет. `None` от зонда означает молчание про права — то
        же правило, что в конце мастера.

        **Импорт внутри функции намеренно.** Зависимости в проекте идут от
        хендлеров к middleware; модульный импорт назад цикла не даёт сегодня и
        даст в тот день, когда хендлеру понадобится что-нибудь отсюда.
        """
        from bot.handlers.common import greet

        await greet(
            bot, session, family, child, admin=await sending.is_admin(bot, family)
        )

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


class UsageMiddleware(BaseMiddleware):
    """Одна строка в `usage` на апдейт: кто, когда, что нажал и сработало ли.

    Отдельный класс, а не ветка в `FamilyMiddleware`: та уже делает три вещи —
    заводит семью, подставляет ребёнка и догоняет приветствие.

    **Регистрируется следом за `FamilyMiddleware` и потому работает внутри
    неё.** `wrap_middlewares` наматывает список через `reversed`, значит первый
    зарегистрированный оказывается снаружи.

    Довод здесь не в том, что иначе не видно `family`: `data` — один и тот же
    словарь, и подставленное в него видно с обеих сторон. Проверено мутацией —
    перестановка регистрации местами тест на автора не красит.

    Довод в `handled`. Снаружи `await handler(...)` — это вызов
    `FamilyMiddleware`, а та на ветке переезда чата в супергруппу возвращает
    `None`, не позвав ни одного роутера. `None is not UNHANDLED`, то есть
    служебный апдейт о переезде записался бы обработанным нажатием. Изнутри мы
    зовём прямо `dp.update.trigger`, и его результат — сентинел в чистом виде.
    Заодно `ms` меряет хендлер, а не сетевой `bot.me()` соседней middleware.

    **`handled` значит «апдейт забрал какой-то роутер», а не «человек получил
    ответ».** Хендлер, молча вышедший по `child is None`, тоже `handled`.
    Ценность колонки в обратном значении: кнопка, под которой нет ни одного
    сошедшегося фильтра, в чате молчит, а в коде выглядит целой — увидеть её
    больше нечем.

    **Пишем в свою сессию, а не в `data["session"]`.** Довод не в чистоте.
    Упавший на записи хендлер оставляет сессию ждущей отката, и без
    `rollback()` наша строка не записалась бы ровно там, где она ценнее всего.
    А `rollback()` экспайрит все объекты сессии — и `FamilyMiddleware`, которая
    сразу после нас читает `family.greeted_at`, получила бы `MissingGreenlet`
    на пустом месте. То есть строка аналитики отняла бы у семьи приветствие
    вместе с постоянной клавиатурой. Поэтому из `data` берутся только
    `family.id` и `member.id` — обычные числа, загруженные к этому моменту
    (`expire_on_commit=False`), — а пишутся они через свой `Session()`.
    Прецедент на «своя сессия вместо чужой» в проекте есть: так устроен тикер.

    **Две явные ветки, а не `try/finally`.** `await` в `finally` во время
    раскрутки исключения, упав сам, подменил бы исключение хендлера — молча и
    с чужим traceback. Поэтому исключение перехватывается, строка пишется,
    и дальше голый `raise`.

    **Сбой самой записи не стоит человеку ответа** — пункт 3 контракта ошибок
    проекта. Всё тело `_note` глотает исключения целиком, как `qa_log.write`.

    Чего эта middleware не видит, названо вслух: тикер идёт мимо диспетчера, и
    ни одно отправленное ботом напоминание, карточка опроса или дайджест сюда
    не попадают. Видны только нажатия на кнопки под ними.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        started = time.monotonic()
        try:
            result = await handler(event, data)
        except Exception:
            await self._note(event, data, handled=False, started=started)
            raise
        await self._note(
            event, data, handled=result is not UNHANDLED, started=started
        )
        return result

    @staticmethod
    async def _note(
        event: TelegramObject,
        data: dict[str, Any],
        *,
        handled: bool,
        started: float,
    ) -> None:
        """Записать строку. Исключений наружу не выпускает."""
        try:
            if not isinstance(event, Update):
                return
            kind, action, detail = usage_names.describe(event)

            family: Family | None = data.get("family")
            member: Member | None = data.get("member")
            async with Session() as session:
                await repo.add_usage(
                    session,
                    at=tu.now_utc(),
                    family_id=family.id if family is not None else None,
                    member_id=member.id if member is not None else None,
                    kind=kind,
                    action=action,
                    detail=detail,
                    handled=handled,
                    ms=int((time.monotonic() - started) * 1000),
                )
        except Exception:
            log.exception("Не удалось записать использование")


def install(dp: Dispatcher) -> None:
    """Навесить middleware проекта на диспетчер.

    Одно место на всех, и это не украшение. Диспетчер собирают двое —
    `bot/__main__.py` и стенд (`tools/stand/harness.py`), — и докстрока стенда
    обещает, что он собирается «ровно так же». Пока middleware было две,
    обещание держалось на внимательности: две одинаковые строки в двух файлах.
    Третья разошлась бы с первой копией молча, и симптомом была бы живая
    проверка с зелёным протоколом, не проверяющая ровно то, ради чего написана.

    Порядок здесь значим и потому записан один раз:

    * `FamilyMiddleware` первой — она кладёт в `data` семью и участника;
    * `UsageMiddleware` следом, то есть **внутри** неё: снаружи её `handled`
      считал бы обработанным всё, что `FamilyMiddleware` забрала себе, не
      позвав ни одного роутера. Подробности — в её докстроке;
    * `drop_wizard_state` на `dp.message` — outer, потому что решение «команда
      пришла посреди мастера» обязано быть одинаковым для всех роутеров, а не
      зависеть от того, чей фильтр сойдётся первым.
    """
    dp.update.outer_middleware(FamilyMiddleware())
    dp.update.outer_middleware(UsageMiddleware())
    dp.message.outer_middleware(drop_wizard_state)
