"""Постоянная клавиатура под полем ввода: три кнопки и три границы.

Нажатие reply-кнопки Telegram присылает обычным сообщением от человека —
неотличимым от реплики в чат. Отсюда всё, что здесь проверяется: метка обязана
дойти до своего хендлера и не быть съеденной ни ожиданием вопроса, ни
обработчиком голого числа, ни шагом мастера, — и при этом не забрать себе
обычную переписку семьи.

Прогон идёт через настоящий `Dispatcher` с боевым `handlers.routers` целиком,
а не со своей тройкой роутеров в правильном порядке. Своя тройка доказывала бы,
что механизм работает при верном порядке, и молчала бы о том, каков порядок на
самом деле.
"""

from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage, GetMe, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from bot import handlers, keyboards, texts
from bot.handlers import baby, common, home
from bot.middlewares import drop_wizard_state
from bot.services import pending

CHAT = Chat(id=-1001, type="supergroup", title="Малышка")
PRIVATE = Chat(id=222, type="private", first_name="Аня")
AUTHOR = User(id=222, is_bot=False, first_name="Аня")
# Имя бота нужно ссылке «добавить в группу»: она собирается из `bot.me()`
ME = User(id=42, is_bot=True, first_name="Бот", username="BabyBot")
NOW = datetime(2026, 9, 13, 12, 0)


# --- Сторожа, которым диспетчер не нужен ----------------------------------


def test_every_home_button_has_an_action():
    """Метки клавиатуры и таблица действий обязаны совпадать.

    Тот же сторож, что `test_menu_promises_only_what_exists` у меню команд, и
    заведён по той же причине: кнопка без действия не отвечает ничего, а
    молчание бота на им же предложенную кнопку хуже отсутствия кнопки.

    Сравнение трёхстороннее и не тавтология: `keyboards.home()` рисует
    клавиатуру, `home.ACTIONS` решает, что делать, а `keyboards.HOME_LABELS`
    читает `middlewares`, решая, обрывать ли мастер. Разойдись любые два —
    и часть кнопок начнёт вести себя иначе, чем соседние.
    """
    shown = {button.text for row in keyboards.home().keyboard for button in row}
    assert shown == set(home.ACTIONS)
    assert shown == set(keyboards.HOME_LABELS)


def test_labels_carry_no_variation_selector():
    """Эмодзи метки — без U+FE0F, и это не придирка.

    Подпись reply-кнопки уходит на клиент и возвращается оттуда обычным
    текстом. Селектор вариаций клиент вправе нормализовать, и вернувшаяся
    метка перестаёт равняться своей же константе: кнопка есть, бот молчит, и
    видно это только глазами в чате.
    """
    for label in keyboards.HOME_LABELS:
        assert "️" not in label, label


def test_no_label_is_eaten_by_the_triage():
    """Слово из `redflags.yaml` в метке отняло бы кнопку навсегда.

    `alarm` стоит выше и забрал бы такую метку себе — молча. Проверяем тем же
    кодом, каким триаж и работает.
    """
    from bot.services import redflags

    for label in keyboards.HOME_LABELS:
        assert not redflags.triage(label), label


def test_the_router_stands_below_alarm_and_above_the_wizard():
    """Три границы места роутера `home`, и все три по боевому списку.

    Ниже `alarm` — граница безопасности. Выше `baby` — иначе тап посреди
    мастера станет именем ребёнка. Выше `qa` и `measure` — иначе метку съест
    фильтр ожидания вопроса или обработчик голого числа.
    """
    order = [router.name for router in handlers.routers]
    assert order.index("alarm") < order.index("home")
    assert order.index("home") < order.index("baby")
    assert order.index("home") < order.index("qa") < order.index("measure")


def test_the_keyboard_survives_a_tap():
    """`is_persistent` и `resize_keyboard` — не украшение.

    Без первого клавиатура сворачивается в иконку после нажатия, то есть
    перестаёт быть одним тапом, ради которого всё и затевалось. `selective` не
    ставим: показанная одному родителю клавиатура — это второй родитель,
    который не узнал, что кнопки есть.
    """
    markup = keyboards.home()
    assert markup.is_persistent is True
    assert markup.resize_keyboard is True
    assert markup.one_time_keyboard is not True
    assert markup.selective is not True


# --- Прогон через настоящий диспетчер -------------------------------------


class RecordingSession(BaseSession):
    """Сеть не трогаем: складываем отправленное и удалённое в списки.

    Имена методов записываются наравне с текстом: без этого `DeleteMessage` не
    оставлял бы следа вовсе, и тест на уборку был бы тавтологией в одежде
    проверки.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []
        self.markups: list[Any] = []
        self.deleted: list[int] = []
        self.refuse_delete = False

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Any, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if isinstance(method, GetMe):
            # Гид в личке собирает ссылку из имени бота. Без этой ветки
            # заглушка вернула бы `Message`, и падало бы на `.username`
            return ME
        if isinstance(method, DeleteMessage):
            if self.refuse_delete:
                raise TelegramBadRequest(
                    method=method, message="not enough rights to delete a message"
                )
            self.deleted.append(method.message_id)
            return True
        payload = method.model_dump(exclude_none=True)
        if "text" in payload:
            self.sent.append(payload["text"])
            # Объектом, а не выгрузкой в dict: pydantic сравнивает модели по
            # значению, и `== keyboards.home()` читается без переводчика
            self.markups.append(getattr(method, "reply_markup", None))
        return Message(message_id=999, date=NOW, chat=CHAT, from_user=AUTHOR, text="")

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def wired(session, family, child, mother, monkeypatch):
    """Диспетчер с боевыми роутерами и боевыми middleware.

    `drop_wizard_state` регистрируется здесь не для полноты: без него тест про
    тап посреди мастера проверял бы не то, что происходит в бою.
    """
    talk = RecordingSession()
    bot = Bot(token="42:TEST", session=talk)
    dp = Dispatcher()

    async def inject(handler, event, data):
        data.update(session=session, family=family, child=child, member=mother)
        return await handler(event, data)

    dp.update.outer_middleware(inject)
    dp.message.outer_middleware(drop_wizard_state)

    order = tuple(handlers.routers)
    for router in order:
        dp.include_router(router)

    opened: list[dict] = []

    async def refresh(bot_, family_id, last_message_id=None, now=None, *, force_new=False):
        opened.append({"family_id": family_id, "force_new": force_new})

    # Настоящая `panel.refresh` открывает свою сессию — к боевому файлу базы, а
    # не к той, что подставил `inject`. См. её докстроку про то, почему сессию
    # хендлера туда не передают
    monkeypatch.setattr(home.panel, "refresh", refresh)

    yield dp, bot, talk, opened

    # Роутеры — синглтоны уровня модуля, и aiogram запоминает, к какому
    # диспетчеру их подключили. Без отвязки второй тест в файле падает на
    # «Router is already attached»
    for router in order:
        router._parent_router = None


async def _tap(dp, bot, data, message_id=1):
    """Тап по inline-кнопке под сообщением бота."""
    message = Message(
        message_id=message_id, date=NOW, chat=CHAT, from_user=AUTHOR, text="панель"
    )
    call = CallbackQuery(
        id="1", from_user=AUTHOR, chat_instance="1", message=message, data=data
    )
    await dp.feed_update(bot, Update(update_id=message_id, callback_query=call))


async def _feed(dp, bot, text, message_id=1):
    message = Message(
        message_id=message_id, date=NOW, chat=CHAT, from_user=AUTHOR, text=text
    )
    await dp.feed_update(bot, Update(update_id=message_id, message=message))


@pytest.mark.asyncio
async def test_every_button_gets_an_answer(wired):
    """Поведенческая половина сторожа: на каждую кнопку что-то происходит.

    Структурное сравнение таблиц не поймало бы ни метку, съеденную роутером
    выше, ни забытый в `handlers.routers` роутер.
    """
    dp, bot, talk, opened = wired

    for number, label in enumerate(sorted(keyboards.HOME_LABELS), start=10):
        before = (len(talk.sent), len(opened))
        await _feed(dp, bot, label, number)
        assert (len(talk.sent), len(opened)) != before, f"{label}: тишина"


@pytest.mark.asyncio
async def test_the_panel_button_does_what_the_command_does(wired):
    """`force_new`, а не правка: нажали именно потому, что панели не видно."""
    dp, bot, talk, opened = wired

    await _feed(dp, bot, keyboards.HOME_PANEL, 7)

    assert len(opened) == 1
    assert opened[0]["force_new"] is True


@pytest.mark.asyncio
async def test_the_tap_is_removed_from_the_chat(wired):
    """Три «📋 Панель» подряд в ленте ночной переписки хуже трёх /panel."""
    dp, bot, talk, opened = wired

    await _feed(dp, bot, keyboards.HOME_PANEL, 7)

    assert talk.deleted == [7]


@pytest.mark.asyncio
async def test_a_tap_works_without_the_right_to_delete(wired):
    """Уборка — украшение, ровно как закрепление панели.

    Без права `can_delete_messages` нажатие останется в ленте, и это не повод
    не делать то, ради чего его нажали. Прямой аналог
    `test_panel_survives_missing_pin_rights`.
    """
    dp, bot, talk, opened = wired
    talk.refuse_delete = True

    await _feed(dp, bot, keyboards.HOME_PANEL, 7)

    assert len(opened) == 1
    assert talk.deleted == []
    assert talk.sent == [], "ругани в чат быть не должно"


@pytest.mark.asyncio
async def test_the_question_button_works_like_the_one_on_the_panel(wired):
    """Та же кнопка, что на панели, и тот же код за ней.

    Вторая половина теста важнее первой: она доказывает, что вынос
    `start_question` не потерял контракт — следующая реплика доходит до Q&A.
    """
    dp, bot, talk, opened = wired

    await _feed(dp, bot, keyboards.HOME_ASK, 7)

    ask = pending.peek(CHAT.id, AUTHOR.id)
    assert ask is not None and ask.what == pending.QUESTION
    assert texts.ASK_QUESTION in talk.sent

    await _feed(dp, bot, "а пустышка это нормально?", 8)
    assert pending.peek(CHAT.id, AUTHOR.id) is None, "вопрос обязан дойти до qa"


@pytest.mark.asyncio
async def test_the_day_button_says_exactly_what_the_command_says(wired):
    """Переиспользование настоящее, а не вторая, более бедная копия."""
    dp, bot, talk, opened = wired

    await _feed(dp, bot, "/today", 7)
    by_command, markup = talk.sent[-1], talk.markups[-1]

    await _feed(dp, bot, keyboards.HOME_TODAY, 8)

    assert talk.sent[-1] == by_command
    assert talk.markups[-1] == markup, "переключатель периодов обязан остаться"


@pytest.mark.asyncio
async def test_a_tap_beats_a_waiting_question(wired):
    """Нажали «❓ Вопрос», передумали и нажали «📋 Панель».

    Метка обязана уйти в `home`, а не в поиск по базе знаний: роутер стоит
    выше `qa` именно для этого.
    """
    dp, bot, talk, opened = wired
    pending.remember(CHAT.id, AUTHOR.id, pending.QUESTION)

    await _feed(dp, bot, keyboards.HOME_PANEL, 7)

    assert len(opened) == 1


@pytest.mark.asyncio
async def test_an_ordinary_phrase_passes_home_by(wired):
    """Совпадение точное. «панель где-то выше» — это реплика, а не нажатие."""
    dp, bot, talk, opened = wired

    await _feed(dp, bot, "панель где-то выше", 7)

    assert opened == []
    assert talk.deleted == []


@pytest.mark.asyncio
async def test_a_tap_mid_wizard_does_not_become_the_name(wired):
    """Главное про мастер: тап не становится именем ребёнка, и это слышно.

    Роутер `home` стоит выше `baby` и апдейт заберёт себе в любом случае — но
    состояние FSM без строки в `middlewares` осталось бы висеть, и следующая
    фраза человека, хоть через три дня, молча стала бы именем.
    """
    dp, bot, talk, opened = wired
    context = dp.fsm.resolve_context(bot=bot, chat_id=CHAT.id, user_id=AUTHOR.id)
    await context.set_state(baby.New.name)

    await _feed(dp, bot, keyboards.HOME_PANEL, 7)

    assert await context.get_state() is None
    assert texts.WIZARD_DROPPED in talk.sent
    assert len(opened) == 1

    # И главное: следующая фраза именем тоже не становится
    await _feed(dp, bot, "купи молока", 8)
    assert texts.BABY_ASK_SEX not in talk.sent


@pytest.mark.asyncio
async def test_a_bare_number_still_reaches_measure(wired):
    """Новый роутер не должен отнять ввод числа — он стоит выше `measure`."""
    dp, bot, talk, opened = wired
    pending.remember(CHAT.id, AUTHOR.id, pending.WEIGHT_G)

    await _feed(dp, bot, "5200", 7)

    assert pending.peek(CHAT.id, AUTHOR.id) is None, "число обязано дойти до measure"
    assert talk.deleted == []


# --- Доставка ---------------------------------------------------------------
#
# Весь раздел заведён по факту 15.09. Клавиатуру написали, закоммитили и
# перезапустили бота — а в чате её не появилось. Носителей оказалось два из
# полусотни возможных: `/help` и «Готово» мастера. Боевая семья завела карточку
# 14.09 в 23:00, за час до того, как код клавиатуры вообще появился, а `/help` с
# тех пор не писала ни разу: по `data/bot.log` после перезапуска в 09:38 пришло
# десять апдейтов, среди них ни `/help`, ни `/start`. То есть всё, что
# проверялось выше — метки, порядок роутеров, уборка нажатия, — проверялось на
# функции, которой в бою не существовало.
#
# Вывод, ради которого этот раздел и написан: у фичи, доехавшей до чата одним
# необязательным путём, нет ни одного пути.


@pytest.mark.asyncio
async def test_a_family_that_never_says_help_still_gets_the_keyboard(wired):
    """Главный сторож раздела: без единой команды клавиатура обязана доехать.

    Прогоняем ровно то, что делала боевая семья: тап по кнопке записи и реплику
    с тревожным признаком. Ни `/help`, ни `/start`, ни мастера.
    """
    dp, bot, talk, opened = wired

    await _feed(dp, bot, "у неё температура 39", 7)
    assert keyboards.home() in talk.markups, "ответ триажа обязан нести клавиатуру"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["/ping", "/stage", "/redflags", "у неё температура 39"],
    ids=["ping", "stage", "redflags", "красный флаг"],
)
async def test_every_ordinary_answer_carries_the_keyboard(wired, text):
    """Обычный ответ — носитель. Именно этого и не хватило в бою."""
    dp, bot, talk, opened = wired

    await _feed(dp, bot, text, 7)

    assert talk.markups, f"{text}: бот вообще ничего не ответил"
    assert keyboards.home() in talk.markups, f"{text}: ответ без клавиатуры"


@pytest.mark.asyncio
async def test_a_question_after_a_tap_carries_the_keyboard(wired):
    """«Сколько мл?» — носитель, а подтверждение записи им быть не может.

    Оба сообщения приходят из одного `track._say`, и разводит их `markup or
    keyboards.home()`: у подтверждения своя кнопка отмены, у вопроса — ничего.
    Одно сообщение обе разметки не носит, поэтому выбор, а не сумма.
    """
    dp, bot, talk, opened = wired
    from bot.db.models import FEED_BOTTLE
    from bot.keyboards import TrackCB

    await _tap(dp, bot, TrackCB(action="add", kind=FEED_BOTTLE).pack())

    assert keyboards.home() in talk.markups, "вопрос про мл обязан нести клавиатуру"
    undo = [m for m in talk.markups if m is not None and m != keyboards.home()]
    assert undo, "подтверждение обязано сохранить свою кнопку отмены"


@pytest.mark.asyncio
async def test_the_sex_prompt_keeps_its_buttons(wired):
    """Второй сайт идиомы `or`: правка пола отдаёт `keyboards.sex()`, не home."""
    dp, bot, talk, opened = wired
    from bot.keyboards import EditCB

    await _tap(dp, bot, EditCB(field="sex").pack())

    assert keyboards.sex() in talk.markups
    assert keyboards.home() not in talk.markups


# --- Приветствие при добавлении в группу ------------------------------------


@pytest.mark.asyncio
async def test_greeting_carries_the_keyboard(bot, session, family):
    """Иначе до первой команды клавиатуры в чате нет вообще.

    Перенос `Family_planer/tests/test_registration.py:127` — там этот сторож
    стоит с самого начала, и ровно его отсутствие здесь стоило захода.

    Сообщений стало два, и сторож теперь именно про **первое**: носитель
    клавиатуры не должен переехать на второе, где у сообщения своя
    inline-кнопка и клавиатуре места нет вовсе.
    """
    await common.greet(bot, session, family, None, admin=True)

    assert bot.sent[0] == (family.chat_id, texts.GREETING)
    assert bot.kwargs[0]["reply_markup"] == keyboards.home()


@pytest.mark.asyncio
async def test_the_greeting_offers_the_card_in_one_tap(bot, session, family):
    """Первый шаг новой семьи — кнопка, а не набранная руками команда.

    Второе сообщение отдельное не от щедрости: одно сообщение не носит
    одновременно клавиатуру под полем ввода и inline-кнопку.
    """
    await common.greet(bot, session, family, None, admin=True)

    assert len(bot.sent) == 2
    assert bot.sent[1] == (family.chat_id, texts.FIRST_STEP)
    markup = bot.kwargs[1]["reply_markup"]
    packed = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert keyboards.StartCB(action="baby").pack() in packed


@pytest.mark.asyncio
async def test_the_greeting_skips_the_card_offer_when_the_child_exists(
    bot, session, family, child
):
    """Бота вернули в чат, где ребёнок давно заведён.

    Кнопка «Завести карточку» здесь объяснила бы только то, что нажимать её не
    надо, — а приветствие с клавиатурой всё равно нужно: после изгнания
    клавиатуры в чате нет.
    """
    await common.greet(bot, session, family, child, admin=True)

    assert len(bot.sent) == 1
    assert bot.sent[0] == (family.chat_id, texts.GREETING)


@pytest.mark.asyncio
async def test_the_greeting_says_nothing_about_rights_it_cannot_check(
    bot, session, family, child
):
    """`admin=None` — «спросить не удалось», и на неуверенности молчим.

    Мутация «считать `None` за отсутствие прав» красит этот тест: про права
    всё равно напомнит мастер, а вот попрекнуть семью из-за сетевого сбоя —
    поломка, которую никто не заметит.
    """
    await common.greet(bot, session, family, child, admin=None)

    assert texts.ADMIN_WHY not in bot.sent[0][1]


@pytest.mark.asyncio
async def test_the_greeting_marks_the_family(bot, session, family, child):
    """Без отметки опоздавшее приветствие повторялось бы на каждой реплике."""
    assert family.greeted_at is None

    await common.greet(bot, session, family, child, admin=True)

    assert family.greeted_at is not None


def test_telegram_is_asked_for_my_chat_member():
    """Апдейт приходит только потому, что на него есть хендлер.

    `bot/__main__.py` отдаёт `allowed_updates=dp.resolve_used_update_types()`, а
    тип, который никто не наблюдает, Telegram не присылает вовсе. Тот, кто
    заменит вызов явным списком, выключит приветствие — и симптомом будет
    тишина. Проверяем по боевому `handlers.routers`, а не по своему списку:
    свой доказывал бы работу механизма и молчал бы о том, что зарегистрировано
    на самом деле.

    Эту половину стенд закрыть не может в принципе: он собирает диспетчер сам и
    кормит его сам, то есть про `allowed_updates` не знает ничего.
    """
    dp = Dispatcher()
    order = tuple(handlers.routers)
    for router in order:
        dp.include_router(router)
    try:
        assert "my_chat_member" in dp.resolve_used_update_types()
    finally:
        for router in order:
            router._parent_router = None


def _admin(user: User) -> ChatMemberAdministrator:
    """Администратор с выключенными правами.

    Флаги перечисляются по схеме, а не руками: у администратора их полтора
    десятка, список растёт от версии к версии Telegram, и выписанный здесь он
    краснел бы на обновлении aiogram, ничего при этом не проверив. Тесту важен
    один-единственный факт — `status`.
    """
    flags = {
        name: False
        for name, field in ChatMemberAdministrator.model_fields.items()
        if field.is_required() and name != "user"
    }
    return ChatMemberAdministrator(user=user, **flags)


def _joined(me: User, old, new) -> Update:
    return Update(
        update_id=99,
        my_chat_member=ChatMemberUpdated(
            chat=CHAT, from_user=AUTHOR, date=NOW,
            old_chat_member=old, new_chat_member=new,
        ),
    )


@pytest.mark.asyncio
async def test_the_greeting_goes_out_when_the_bot_is_added(wired):
    """Положительный двойник теста ниже.

    Без него «приветствие не ушло» доказывало бы ровно ничего: апдейт мог не
    дойти до хендлера по десятку причин, и тест остался бы зелёным на пустом
    месте.
    """
    dp, bot, talk, opened = wired

    await dp.feed_update(
        bot, _joined(ME, ChatMemberLeft(user=ME), ChatMemberMember(user=ME))
    )

    assert talk.sent[0].startswith(texts.GREETING)
    assert keyboards.home() in talk.markups


@pytest.mark.asyncio
async def test_the_greeting_asks_for_rights_it_does_not_have(wired):
    """Добавили обычным участником — просьба о правах едет с приветствием.

    Одним сообщением, а не вторым: просьба не самостоятельная мысль, а
    оговорка к «вот что я умею».
    """
    dp, bot, talk, opened = wired

    await dp.feed_update(
        bot, _joined(ME, ChatMemberLeft(user=ME), ChatMemberMember(user=ME))
    )

    assert texts.ADMIN_WHY in talk.sent[0]


@pytest.mark.asyncio
async def test_the_greeting_does_not_ask_for_rights_it_already_has(wired):
    """Пришли по ссылке с `admin=` — бота добавили сразу администратором.

    Просить в первом же слове то, что уже дали, значит выглядеть
    невнимательным. Статус лежит в самом апдейте, спрашивать Telegram не надо.
    """
    dp, bot, talk, opened = wired

    await dp.feed_update(bot, _joined(ME, ChatMemberLeft(user=ME), _admin(ME)))

    assert talk.sent[0].startswith(texts.GREETING)
    assert texts.ADMIN_WHY not in talk.sent[0]


@pytest.mark.asyncio
async def test_being_added_as_an_admin_is_not_thanked_twice(wired):
    """Добавление сразу администратором — это знакомство, а не повышение.

    Свойство видимое: человек получает «здравствуйте», а не «здравствуйте» и
    «спасибо за права» подряд в первую же секунду.
    """
    dp, bot, talk, opened = wired

    await dp.feed_update(bot, _joined(ME, ChatMemberLeft(user=ME), _admin(ME)))

    assert texts.ADMIN_THANKS not in talk.sent


@pytest.mark.asyncio
async def test_no_transition_is_claimed_twice():
    """Два наблюдателя `my_chat_member` не должны пересекаться фильтрами.

    Сторож против `PROMOTED_TRANSITION`, и написан он ровно так потому, что
    поведенческий тест на два сообщения был бы зелёным и с ним: aiogram
    останавливается на первом сошедшемся хендлере роутера, а `bot_added`
    зарегистрирован выше. То есть молчание держалось бы на порядке
    регистрации — на том, что в коде никак не выражено, и что следующий
    читатель вправе поменять местами.

    Проверяется поэтому не число сообщений, а само свойство: на каждый переход
    претендует **не больше одного** хендлера. `PROMOTED_TRANSITION` красит этот
    тест на `left → administrator`, то есть на обычном добавлении по нашей же
    ссылке с `admin=`.

    Фильтры берутся боевые, из `common.router`, а не собранные здесь заново:
    собранные доказывали бы, что механизм работает, и молчали бы о том, что
    зарегистрировано на самом деле.
    """
    observer = common.router.my_chat_member
    transitions = {
        "добавили участником": (ChatMemberLeft(user=ME), ChatMemberMember(user=ME)),
        "добавили администратором": (ChatMemberLeft(user=ME), _admin(ME)),
        "повысили": (ChatMemberMember(user=ME), _admin(ME)),
        "поправили права": (_admin(ME), _admin(ME)),
    }

    for label, (old, new) in transitions.items():
        event = ChatMemberUpdated(
            chat=CHAT, from_user=AUTHOR, date=NOW,
            old_chat_member=old, new_chat_member=new,
        )
        matched = []
        for handler in observer.handlers:
            if all([await check.call(event) for check in handler.filters or []]):
                matched.append(handler.callback.__name__)
        assert len(matched) <= 1, (label, matched)


@pytest.mark.asyncio
async def test_a_promotion_is_thanked_and_repins_the_panel(wired):
    """Права дали потом — говорим спасибо и выпускаем панель заново.

    Именно заново: `_pin` приватна и зовётся только из `_publish`, а правка на
    месте до закрепления не доходит вовсе. То есть без `force_new` панель
    осталась бы незакреплённой при уже выданных правах.
    """
    dp, bot, talk, opened = wired

    await dp.feed_update(bot, _joined(ME, ChatMemberMember(user=ME), _admin(ME)))

    assert texts.ADMIN_THANKS in talk.sent
    assert opened and opened[-1]["force_new"] is True


@pytest.mark.asyncio
async def test_rights_toggled_twice_are_thanked_once(wired):
    """`administrator → administrator` Telegram шлёт на каждую правку прав.

    Поэтому переход берётся `MEMBER >> ADMINISTRATOR`, а не
    `IS_MEMBER >> IS_ADMIN`: во второй `IS_MEMBER` включает `ADMINISTRATOR`, и
    благодарность повторялась бы на каждом щелчке галочки в настройках чата.
    """
    dp, bot, talk, opened = wired

    await dp.feed_update(bot, _joined(ME, _admin(ME), _admin(ME)))

    assert texts.ADMIN_THANKS not in talk.sent


@pytest.mark.asyncio
async def test_the_greeting_ignores_promotion(wired):
    """`member → administrator` приветствием не является.

    Переход случается через минуту после добавления, когда боту дают права на
    закрепление и уборку. Второе приветствие на выдачу прав было бы первой же
    поломкой, поэтому фильтр стоит на `IS_NOT_MEMBER >> IS_MEMBER`, а не на
    «стал участником».
    """
    dp, bot, talk, opened = wired

    await dp.feed_update(bot, _joined(ME, ChatMemberMember(user=ME), _admin(ME)))

    assert not any(line.startswith(texts.GREETING) for line in talk.sent)


@pytest.mark.asyncio
async def test_the_panel_never_carries_the_keyboard(
    session_maker, family, child, bot, monkeypatch
):
    """Уцелевший инвариант, проверенный фактом, а не типом.

    `edit_message_text` reply-клавиатуру не принимает вовсе, а одно сообщение не
    носит обе разметки. Панель поэтому носителем быть не может ни при каком
    способе доставки — и правка 15.09, раздавшая клавиатуру полусотне мест, её
    не тронула.
    """
    from bot.services import panel

    monkeypatch.setattr(panel, "Session", session_maker)
    await panel.refresh(bot, family.id)

    for kwargs in bot.kwargs + bot.edit_kwargs:
        assert not isinstance(kwargs.get("reply_markup"), ReplyKeyboardMarkup)


# --- Кнопка окончания -------------------------------------------------------
#
# Живёт в этом файле не по смыслу, а по машинерии: здесь единственная в проекте
# фикстура с боевым диспетчером и конструктором тапа. Проверять структуру
# клавиатуры незачем — это делает `tests/test_panel.py`; здесь проверяется, что
# `track._start` действительно ею пользуется.


@pytest.mark.asyncio
async def test_starting_a_timer_offers_to_finish_it_right_there(wired):
    """Жалоба из чата: «кнопка завершения где-то сверху в панели».

    Так и было: подтверждение «Сцеживание — начали в 23:40» несло только
    «↩️ Отменить», а «⏹ Закончить» оставалось в панели выше — да ещё и за
    служебной строкой о закреплении. То есть в секунду, когда таймер запущен и
    человек смотрит на последнее сообщение, до остановки надо было прокручивать
    чат.

    Структурный сторож этого не поймал бы: `keyboards.running` могла бы быть
    идеальной и не использоваться.
    """
    dp, bot, talk, opened = wired
    from bot.db.models import SLEEP
    from bot.keyboards import TrackCB

    await _tap(dp, bot, TrackCB(action="start", kind=SLEEP).pack())

    assert talk.markups, "бот не подтвердил начало"
    labels = [
        button.text
        for markup in talk.markups
        if markup is not None and hasattr(markup, "inline_keyboard")
        for row in markup.inline_keyboard
        for button in row
    ]
    assert "⏹ Закончить" in labels, labels
    assert "↩️ Отменить" in labels, labels


# --- Случайный тап ----------------------------------------------------------
#
# Вопрос из чата: «некоторые действия могут быть нажаты по случайности, что
# тогда делать?». Проверка показала две дыры, и обе про одно: закрытие таймера
# было необратимым. Под ответом об окончании стояла кнопка «↩️ Отменить», и она
# удаляла запись целиком — то есть промах по окончанию сна лечился потерей
# всего сна. А «начали» поверх идущего таймера закрывало предыдущий молча.


@pytest.mark.asyncio
async def test_a_misfired_finish_can_be_taken_back(wired, session, child):
    """«Не заканчивать» возвращает таймеру ход, а запись оставляет целой.

    Главное здесь — что это НЕ удаление: `ended_at` снова пуст, а событие на
    месте. Прежняя кнопка отмены стирала его, и сон переставал существовать.
    """
    from bot.db import repo
    from bot.db.models import SLEEP
    from bot.keyboards import TrackCB, UndoCB

    dp, bot, talk, opened = wired
    await _tap(dp, bot, TrackCB(action="start", kind=SLEEP).pack())
    started = await repo.any_open_event(session, child.id)
    assert started is not None

    await _tap(dp, bot, TrackCB(action="stop", kind=SLEEP).pack())
    assert await repo.any_open_event(session, child.id) is None

    await _tap(dp, bot, UndoCB(what="reopen", row_id=started.id).pack())

    again = await repo.any_open_event(session, child.id)
    assert again is not None, "таймер обязан снова идти"
    assert again.id == started.id, "это та же запись, а не новая"
    assert again.estimated is False


@pytest.mark.asyncio
async def test_reopening_never_cuts_off_another_timer(wired, session, child):
    """Инвариант «ровно один открытый таймер» сильнее удобства.

    Человек просил вернуть один таймер, а не оборвать другой. Молча закрыть
    идущий ради этого — значит починить промах вторым промахом.
    """
    from bot.db import repo
    from bot.db.models import PUMP, SLEEP
    from bot.keyboards import TrackCB, UndoCB

    dp, bot, talk, opened = wired
    await _tap(dp, bot, TrackCB(action="start", kind=SLEEP).pack())
    sleep_event = await repo.any_open_event(session, child.id)
    await _tap(dp, bot, TrackCB(action="stop", kind=SLEEP).pack())
    await _tap(dp, bot, TrackCB(action="start", kind=PUMP).pack())

    await _tap(dp, bot, UndoCB(what="reopen", row_id=sleep_event.id).pack())

    assert texts.ANOTHER_TIMER_RUNS in talk.sent
    running = await repo.any_open_event(session, child.id)
    assert running.kind == PUMP, "чужой таймер обязан остаться идти"
    await session.refresh(sleep_event)
    assert sleep_event.ended_at is not None, "закрытый так и остался закрытым"


@pytest.mark.asyncio
async def test_starting_over_a_running_timer_says_so(wired, session, child):
    """Закрытие предыдущего больше не молчит.

    Молчало по причине, которая сломалась вместе с интерфейсом: «панель
    меняется под пальцем, закрытие видно сразу». С клавиатурой под полем ввода
    панель чаще всего выше по чату, и человек узнавал о закрытии только из
    статистики — то есть никогда.
    """
    from bot.db.models import PUMP, SLEEP
    from bot.keyboards import TrackCB

    dp, bot, talk, opened = wired
    await _tap(dp, bot, TrackCB(action="start", kind=SLEEP).pack())
    before = len(talk.sent)

    await _tap(dp, bot, TrackCB(action="start", kind=PUMP).pack())

    said = " ".join(talk.sent[before:])
    assert "Закрыл предыдущее" in said, talk.sent[before:]
