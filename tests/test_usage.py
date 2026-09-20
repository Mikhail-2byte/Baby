"""Учёт использования: кто что нажал, во что обошлись модели, помог ли ответ.

Заведён вместе с этапом, и главный тест здесь один — `test_not_one_human_word
_reaches_the_table`. Остальные про механику, а он про обещание: в таблице
`usage` нет ни одного слова человека. Обещание это не выводится ни из схемы
(колонка `action` — обычная строка), ни из кода `describe` (он выглядит
правильным и с `return TEXT, message.text`), то есть держится ровно на нём.

Второй по важности — `test_a_failed_write_does_not_cost_the_answer`. Учёт
побочен: строка, не записавшаяся в журнал, не должна стоить человеку ответа
про его ребёнка. Это пункт 3 контракта ошибок проекта, и без теста он
соблюдается до первой правки.

Прогон идёт через настоящий диспетчер с **боевыми** middleware — той самой
`middlewares.install`, которую зовут `bot/__main__.py` и стенд. Свой порядок
из двух строк доказывал бы, что механизм работает при правильной сборке, и
молчал бы о том, какова сборка на самом деле.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    Update,
    User,
    Voice,
)
from sqlalchemy import select

from bot import handlers, keyboards, middlewares
from bot.db.models import Usage
from bot.handlers import qa as qa_handler
from bot.services import qa, qa_log, usage

CHAT = Chat(id=-1001, type="supergroup", title="Малышка")
PRIVATE = Chat(id=555, type="private")
AUTHOR = User(id=222, is_bot=False, first_name="Аня")
ME = User(id=42, is_bot=True, first_name="Бот", username="BabyBot")
NOW = datetime(2026, 9, 13, 12, 0)


# --- чистая функция разбора ------------------------------------------------


@pytest.mark.parametrize(
    ("data", "action"),
    [
        ("trk:start:feed_breast:L", "trk:start:feed_breast"),
        ("trk:add:diaper_wet:", "trk:add:diaper_wet"),
        ("undo:event:412", "undo:event"),
        ("rec:list:e:0:0:0:0:0:0", "rec:list:e"),
        ("rec:shift:e:57::::-15:0:0", "rec:shift:e"),
        ("rec:setkind:e:57:kind:sleep::0:0:0", "rec:setkind:e"),
        ("ci:when:sleep:30:0:0", "ci:when:sleep"),
        ("rmd:on:feed:3", "rmd:on:feed"),
        ("qav:up:7a3f1c88", "qav:up"),
        ("st:week", "st:week"),
        ("pnl:refresh", "pnl:refresh"),
        ("msr:weight", "msr:weight"),
        ("vc:save", "vc:save"),
        ("sex:f", "sex:f"),
    ],
)
def test_a_row_id_never_becomes_an_action(data: str, action: str) -> None:
    """Номер строки в имени действия — это и мусор, и утечка.

    Мусор — потому что у каждой записи дневника получилось бы «своё действие»
    со счётчиком единица, и топ нажатий перестал бы что-либо означать. Утечка —
    потому что 57 в `rec:shift:e:57` это номер записи в дневнике ребёнка.

    Параметры перечисляют все четырнадцать префиксов проекта: правило одно на
    всех, и проверять его на одном классе значило бы проверять пример.
    """
    assert usage.button_action(data)[0] == action


def test_the_verdict_keeps_its_question_and_nobody_else_does() -> None:
    """`detail` — единственное поле, где лежит не имя действия.

    И лежит там `qid`, только у кнопок вердикта. Если бы `describe` таскала
    туда хвост любой кнопки, номера строк вернулись бы тем же путём, которым их
    выгнали из `action`.
    """
    assert usage.button_action("qav:down:7a3f1c88")[1] == "7a3f1c88"
    assert usage.button_action("rec:shift:e:57::::-15:0:0")[1] == ""
    assert usage.button_action("trk:start:feed_breast:L")[1] == ""


def test_a_qid_never_looks_like_an_action() -> None:
    """Первый знак `qid` — цифра, и это не косметика.

    Чистый hex вида `a3f1c8ad` проходит правило «токен похож на имя», то есть
    примерно раз на две-три тысячи вопросов уехал бы в `action` мусорным
    действием. Поймать такое в отчёте было бы нечем — оно выглядит как
    настоящая кнопка.
    """
    for _ in range(200):
        qid = qa.new_qid()
        assert qid[0].isdigit(), qid
        assert ":" not in qid, "двоеточие уронило бы pack() кнопки"
        assert usage.button_action(f"qav:up:{qid}") == ("qav:up", qid)


def test_a_command_leaves_its_arguments_outside() -> None:
    """В аргументе `/weight` едет вес ребёнка — то самое, чего тут быть не должно."""
    update = _message_update(1, text="/weight@BabyBot 3420")
    assert usage.describe(update) == ("command", "/weight", "")


# --- прогон через настоящие middleware -------------------------------------


class RecordingSession(BaseSession):
    """Сеть не трогаем."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []
        self.markups: list[Any] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Any, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if isinstance(method, GetMe):
            return ME
        payload = method.model_dump(exclude_none=True)
        if "text" in payload:
            self.sent.append(payload["text"])
            self.markups.append(getattr(method, "reply_markup", None))
        return Message(message_id=999, date=NOW, chat=CHAT, from_user=AUTHOR, text="")

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _message_update(update_id: int, **fields: Any) -> Update:
    message = Message(message_id=update_id, date=NOW, chat=CHAT, from_user=AUTHOR, **fields)
    return Update(update_id=update_id, message=message)


def _tap_update(update_id: int, data: str) -> Update:
    message = Message(message_id=update_id, date=NOW, chat=CHAT, from_user=ME, text="x")
    call = CallbackQuery(
        id=str(update_id), from_user=AUTHOR, chat_instance="ci", message=message, data=data
    )
    return Update(update_id=update_id, callback_query=call)


@pytest_asyncio.fixture
async def wired(session_maker, session, family, child, mother, monkeypatch):
    """Диспетчер, собранный боевой `middlewares.install`.

    Подменяется ровно одна вещь — фабрика сессий: и `FamilyMiddleware`, и
    `UsageMiddleware` открывают свою, а боевая смотрит в `data/baby.db`. Всё
    остальное настоящее, включая порядок middleware, ради которого тест и
    существует: `UsageMiddleware` обязана оказаться **внутри**
    `FamilyMiddleware`, иначе `family` и `member` к ней не приедут.

    Отметка приветствия ставится и **коммитится**: `FamilyMiddleware` читает
    семью своей сессией, и непрокоммиченное присваивание она не увидит — пойдёт
    здороваться и спросит у Telegram про права, то есть уткнётся в заглушку.
    """
    family.greeted_at = NOW
    await session.commit()
    monkeypatch.setattr(middlewares, "Session", session_maker)

    talk = RecordingSession()
    bot = Bot(token="42:TEST", session=talk)
    dp = Dispatcher()
    middlewares.install(dp)

    order = tuple(handlers.routers)
    for router in order:
        dp.include_router(router)

    async def refresh(*args: Any, **kwargs: Any) -> None:
        return None

    # `panel.schedule` зовут без `await` — она синхронная и заводит задачу
    # дебаунса. Асинхронная замена дала бы «coroutine was never awaited»
    monkeypatch.setattr("bot.services.panel.refresh", refresh)
    monkeypatch.setattr("bot.services.panel.schedule", lambda *a, **k: None)

    yield dp, bot, talk

    for router in order:
        router._parent_router = None


async def _rows(session_maker) -> list[Usage]:
    """Читаем своей сессией, а не той, что у теста.

    `UsageMiddleware` пишет отдельной сессией, и подглядывать за ней через
    чужую значило бы ещё и сбрасывать её состояние: `expire_all` на тестовой
    сессии роняет следующее же обращение к `family.id` в синхронном контексте.
    """
    async with session_maker() as own:
        found = await own.execute(select(Usage).order_by(Usage.id))
        return list(found.scalars())


@pytest.mark.asyncio
async def test_not_one_human_word_reaches_the_table(wired, session_maker):
    """Главный тест файла: в `usage` нет ни одного слова человека.

    Через бот проходят вопрос о здоровье, голое число, обычная фраза и метка
    клавиатуры. В таблице от них остаётся только род реплики — `text` или
    `home`, — и ни одной буквы содержимого.

    Без этого теста `describe` можно вернуть `message.text` одной правкой, и
    выглядеть она будет улучшением: «а давайте видеть, о чём спрашивают». Что
    при этом дневник здоровья ребёнка переезжает в аналитическую таблицу,
    которая не ротируется и уезжает в чат по `/backup`, из такой правки не
    видно вовсе.
    """
    dp, bot, talk = wired
    secrets = [
        "почему у неё желтеет кожа",
        "120",
        "поели и легли спать",
        keyboards.HOME_TODAY,
    ]
    for number, text in enumerate(secrets, start=1):
        await dp.feed_update(bot, _message_update(number, text=text))

    rows = await _rows(session_maker)
    assert rows, "ни одной строки не записалось — тест проверял бы пустоту"

    printed = " ".join(f"{row.kind} {row.action} {row.detail or ''}" for row in rows)
    for text in secrets[:3]:
        assert text not in printed, f"в таблицу уехало слово человека: {text}"

    kinds = [row.kind for row in rows]
    assert kinds == ["text", "text", "text", "home"]
    # Метка клавиатуры — исключение, и оно безопасно: это константа модуля,
    # а не то, что человек написал
    assert rows[-1].action == keyboards.HOME_TODAY


@pytest.mark.asyncio
async def test_a_tap_knows_who_and_when(wired, session_maker, family, mother):
    """Строка несёт семью и человека — иначе «кто и сколько раз» не посчитать.

    Обе ссылки приезжают из `FamilyMiddleware`, и приезжают только потому, что
    `UsageMiddleware` зарегистрирована **после** неё. Поменяв две строки в
    `install` местами, мы получили бы таблицу без единого автора — и выглядела
    бы она исправной.
    """
    dp, bot, talk = wired

    await dp.feed_update(bot, _tap_update(1, keyboards.PanelCB(action="refresh").pack()))

    rows = await _rows(session_maker)
    assert len(rows) == 1
    assert rows[0].family_id == family.id
    assert rows[0].member_id == mother.id
    assert rows[0].kind == "button"
    assert rows[0].action == "pnl:refresh"
    assert rows[0].handled is True


@pytest.mark.asyncio
async def test_a_dead_button_is_recorded_as_unhandled(wired, session_maker):
    """Кнопка, под которой нет ни одного фильтра, в чате молчит.

    В коде она при этом выглядит целой — ровно та порода поломок, против
    которой заведён `test_every_home_button_has_an_action`. Колонка `handled`
    единственная, по которой такую кнопку видно постфактум, и берётся она из
    сентинела `UNHANDLED`, а не из догадки.
    """
    dp, bot, talk = wired

    await dp.feed_update(bot, _tap_update(1, "нетакойкнопки:жмём"))

    rows = await _rows(session_maker)
    assert len(rows) == 1
    assert rows[0].handled is False


@pytest.mark.asyncio
async def test_a_chat_move_is_not_a_tap(wired, session_maker):
    """Переезд в супергруппу не нажатие, и обработанным он не считается.

    Этот тест — единственное, что держит порядок регистрации middleware.
    Довод «иначе не видно семью» неверен: `data` один и тот же словарь, и
    подставленное в него видно с обеих сторон — проверено мутацией.

    Верный довод вот: `FamilyMiddleware` на переезде возвращает `None`, не
    позвав ни одного роутера. Снаружи `None is not UNHANDLED`, то есть
    служебный апдейт записался бы обработанным нажатием и поехал бы в отчёт
    наравне с тапом по панели. Изнутри мы зовём прямо `dp.update.trigger`.

    Поменяйте две строки в `install` местами — тест покраснеет. Это
    проверено, а не предположено.
    """
    dp, bot, talk = wired
    message = Message(
        message_id=1,
        date=NOW,
        chat=CHAT,
        from_user=AUTHOR,
        migrate_to_chat_id=-1002,
    )
    await dp.feed_update(bot, Update(update_id=1, message=message))

    assert await _rows(session_maker) == [], "переезд чата попал в нажатия"


@pytest.mark.asyncio
async def test_a_private_poke_is_counted_without_a_family(wired, session_maker):
    """Личку считаем, но по людям не раскладываем.

    `FamilyMiddleware` возвращает управление хендлеру раньше, чем заводит
    семью, — значит ни `family`, ни `member` в `data` не будет. Это цена
    инварианта, а не недосмотр: видно, что кто-то постучался, и не видно кто.
    """
    dp, bot, talk = wired
    message = Message(message_id=1, date=NOW, chat=PRIVATE, from_user=AUTHOR, text="привет")
    await dp.feed_update(bot, Update(update_id=1, message=message))

    rows = await _rows(session_maker)
    assert len(rows) == 1
    assert rows[0].family_id is None
    assert rows[0].member_id is None
    assert rows[0].handled is True, "гид в личке обязан отвечать"


@pytest.mark.asyncio
async def test_a_voice_leaves_a_trace_without_its_words(wired, session_maker, monkeypatch):
    """Голосовое считается видом реплики, и расшифровка в таблицу не попадает."""
    dp, bot, talk = wired
    monkeypatch.setattr("bot.services.voice.available", lambda: False)
    voice = Voice(file_id="f", file_unique_id="u", duration=6)
    message = Message(message_id=1, date=NOW, chat=CHAT, from_user=AUTHOR, voice=voice)
    await dp.feed_update(bot, Update(update_id=1, message=message))

    rows = await _rows(session_maker)
    assert [row.kind for row in rows] == ["voice"]
    assert rows[0].action == "voice"


@pytest.mark.asyncio
async def test_a_broken_handler_still_leaves_a_trace(wired, session_maker, monkeypatch):
    """Упавший хендлер обязан оставить строку, и исключение — пройти насквозь.

    Нажатия, на которых бот падает, — самые интересные в отчёте, и терять их
    значило бы завести аналитику, слепую ровно к поломкам. Вторая половина
    важнее первой: поглоти мы тут исключение, aiogram счёл бы апдейт
    обработанным, а человек не получил бы ничего и не узнал бы почему.
    """
    dp, bot, talk = wired

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("сломалось")

    monkeypatch.setattr(qa_handler, "start_question", boom)

    with pytest.raises(RuntimeError):
        await dp.feed_update(bot, _tap_update(1, keyboards.AskCB().pack()))

    rows = await _rows(session_maker)
    assert len(rows) == 1, "строка об упавшем нажатии потерялась"
    assert rows[0].action == "ask"
    assert rows[0].handled is False


@pytest.mark.asyncio
async def test_a_failed_write_does_not_cost_the_answer(wired, session_maker, monkeypatch):
    """Учёт побочен: сбой записи не должен стоить человеку ответа.

    Пункт 3 контракта ошибок проекта. Без теста он соблюдается ровно до первой
    правки — а симптомом нарушения будет бот, молча теряющий апдейты при
    полном диске.
    """
    dp, bot, talk = wired

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("база отказала")

    monkeypatch.setattr("bot.db.repo.add_usage", boom)

    await dp.feed_update(bot, _message_update(1, text="/ping"))

    assert talk.sent, "ответ человеку не ушёл из-за строки в журнале"


# --- вердикт ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_verdict_names_the_question_it_judges(wired, session_maker, monkeypatch):
    """Тап 👍 склеивается с вопросом по `qid`, и склейка живёт в двух местах.

    В `usage` едет сам факт нажатия, в `qa.log` — вердикт рядом с текстом
    вопроса. По `message_id` склеить было бы нельзя: ответ и его оценка
    приходят разными апдейтами, а номер сообщения живёт только в чате.
    """
    dp, bot, talk = wired
    qid = qa.new_qid()

    await dp.feed_update(
        bot, _tap_update(1, keyboards.QaCB(action="down", qid=qid).pack())
    )

    rows = await _rows(session_maker)
    assert len(rows) == 1
    assert rows[0].action == "qav:down"
    assert rows[0].detail == qid, "вердикт не знает, какой вопрос судит"

    written = qa_log.PATH.read_text(encoding="utf-8")
    assert f'"qid": "{qid}"' in written
    assert '"verdict": "down"' in written


@pytest.mark.asyncio
async def test_the_verdict_buttons_ride_under_every_outcome(wired, monkeypatch):
    """👍/👎 едут и под молчанием тоже.

    «И правильно, что промолчал» — ровно то число, ради которого заведён
    семантический сторож молчания: цена его осторожности названа в проекте
    вслух и до сих пор не измерялась ничем.
    """
    dp, bot, talk = wired
    qid = qa.new_qid()

    async def answer(question: str, *, chat: int | None = None) -> qa.Result:
        return qa.Result(qa.NOT_FOUND, "", (), "", qid)

    monkeypatch.setattr(qa_handler.qa, "answer", answer)
    monkeypatch.setattr("bot.services.pending.peek", lambda *a, **k: _AwaitedQuestion())
    monkeypatch.setattr("bot.services.pending.take", lambda *a, **k: None)

    await dp.feed_update(bot, _message_update(1, text="когда заживает пупок"))

    assert keyboards.qa_verdict(qid) in talk.markups


class _AwaitedQuestion:
    what = "question"


@pytest.mark.asyncio
async def test_an_answer_without_a_qid_keeps_the_keyboard(wired, monkeypatch):
    """Пустой `qid` — это заглушка, а не боевой путь.

    Кнопка `qav:up:` склеилась бы не с чем: тап по ней записал бы вердикт,
    которому нечего судить. Поэтому разметка остаётся прежней — та самая
    `keyboards.home()`, которую ответ на вопрос носил до этого этапа.

    Без отката все тесты проекта, подменяющие `qa.answer`, прогоняли бы путь,
    которого в бою не бывает, — и разошлись бы с ним молча.
    """
    dp, bot, talk = wired

    async def answer(question: str, *, chat: int | None = None) -> qa.Result:
        return qa.Result(qa.NOT_FOUND, "", ())

    monkeypatch.setattr(qa_handler.qa, "answer", answer)
    monkeypatch.setattr("bot.services.pending.peek", lambda *a, **k: _AwaitedQuestion())
    monkeypatch.setattr("bot.services.pending.take", lambda *a, **k: None)

    await dp.feed_update(bot, _message_update(1, text="когда заживает пупок"))

    assert keyboards.home() in talk.markups


@pytest.mark.asyncio
async def test_even_a_broken_answer_can_be_judged(monkeypatch):
    """`qid` штампуется и на ветке `except`, где всё сломалось.

    Ответ «база знаний ещё не собрана» человек видит как обычный ответ бота, и
    👎 под ним — самый ценный сигнал из всех: бот отказал там, где спрашивали.
    Без метки кнопки под таким ответом не рисуются вовсе, и отказы выпадают из
    замера ровно потому, что они отказы.

    Мутация «вернуть `Result(NO_KB, "", (), BROKEN)` без `qid`» не ловилась
    ничем, пока этого теста не было.
    """
    def boom() -> bool:
        raise RuntimeError("база подменена на середине")

    monkeypatch.setattr("bot.services.kb.available", boom)

    result = await qa.answer("когда заживает пупок")

    assert result.kind == qa.NO_KB
    assert result.qid, "у сломавшегося ответа нет метки — судить его нечем"


# --- журнал моделей --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_log_line_names_the_chat(wired, session_maker, monkeypatch):
    """Боевой путь обязан класть чат в `qa.log`.

    У `qa.answer` аргумент `chat` со значением по умолчанию — иначе пришлось бы
    переписать два десятка вызовов в тестах ради строки в логе. Дыру «боевой
    путь забыл передать» закрывает этот тест, а не внимательность: без чата по
    журналу не видно, какая семья тратит общую суточную квоту, а семей больше
    одной.
    """
    dp, bot, talk = wired

    monkeypatch.setattr("bot.services.pending.peek", lambda *a, **k: _AwaitedQuestion())
    monkeypatch.setattr("bot.services.pending.take", lambda *a, **k: None)
    monkeypatch.setattr("bot.services.kb.available", lambda: False)

    await dp.feed_update(bot, _message_update(1, text="когда заживает пупок"))

    # `NO_KB` до журнала не доходит намеренно — записи нет, и это правильно:
    # обращения к модели не было. Проверяем на пути, где запись есть
    monkeypatch.setattr("bot.services.kb.available", lambda: True)

    async def nothing(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr("bot.services.kb.search", nothing)
    await dp.feed_update(bot, _message_update(2, text="когда заживает пупок"))

    written = qa_log.PATH.read_text(encoding="utf-8")
    assert f'"chat": {CHAT.id}' in written


def test_transcription_does_not_eat_the_model_quota() -> None:
    """У расшифровки свой ключ и свой потолок.

    Приписав её вызовы к `calls`, мы выключали бы «❓ Вопрос» семье тем
    быстрее, чем больше она говорит вслух, — то есть наказывали бы ровно за то
    поведение, ради которого голос и заводился.
    """
    before = qa_log.calls_today()
    qa_log.write(event="stt", chat=CHAT.id, ok=True, secs=6, seconds=1.2, calls=0)
    assert qa_log.calls_today() == before


# --- сборка --------------------------------------------------------------


def test_the_stand_wires_the_same_middleware_as_production() -> None:
    """Диспетчер собирают двое, и собирать обязаны одинаково.

    Пока middleware было две, обещание стенда «собирается ровно так же»
    держалось на внимательности: две одинаковые строки в двух файлах. Третья
    разошлась бы молча, и симптомом была бы живая проверка с зелёным
    протоколом, не проверяющая ровно того, ради чего написана.

    Тест структурный намеренно: поведенческий пришлось бы поднимать стендом,
    который ходит в настоящую сеть.
    """
    from pathlib import Path

    for path in (Path("bot/__main__.py"), Path("tools/stand/harness.py")):
        source = path.read_text(encoding="utf-8")
        assert "middlewares.install(" in source, f"{path} собирает middleware сам"
        assert "outer_middleware(" not in source, f"{path} завёл свою копию сборки"


def test_usage_is_wiped_with_the_family() -> None:
    """Нажатия уходят вместе с семьёй, про которую они.

    Иначе после `tools/reset.py` останутся висячие ключи, а подготовка стенда
    упадёт на `FOREIGN KEY constraint failed`. Порядок тоже проверяется: `usage`
    ссылается на `families` и `members`, значит удаляется раньше обеих.
    """
    from tools.wipe import WIPE_ORDER

    assert "usage" in WIPE_ORDER
    assert WIPE_ORDER.index("usage") < WIPE_ORDER.index("families")
    assert WIPE_ORDER.index("usage") < WIPE_ORDER.index("members")
