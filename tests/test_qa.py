"""Ответ на вопрос: порядок отказов, сторож чисел и граница безопасности.

Самый важный тест файла — последний раздел. Он прогоняет апдейты через
настоящий `Dispatcher` с настоящими роутерами и проверяет две вещи, которые
живут не в коде, а в порядке их регистрации: что сообщение с тревожным
признаком до модели не доходит, и что ввод веса числом не сломан.
"""

import socket
from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import keyboards, texts
from bot.handlers import alarm, measure
from bot.handlers import qa as qa_handler
from bot.services import kb, llm, pending, qa, qa_log

CHAT = Chat(id=-1001, type="supergroup", title="Малышка")
NOW = datetime(2026, 9, 13, 12, 0)
AUTHOR = User(id=222, is_bot=False, first_name="Аня")


def _chunk(
    source_id: str = "kr_newborn_2025",
    text: str = "Прикладывать к груди в первые 30 минут после рождения.",
    heading: str = "Базовая помощь",
) -> kb.Chunk:
    return kb.Chunk(
        id=1,
        source_id=source_id,
        tier="T1",
        tier_name="Клинические рекомендации Минздрава России",
        title="Здоровый новорождённый",
        publisher="Клинические рекомендации, 2025",
        url="https://example.invalid/kr",
        heading=heading,
        text=text,
        page=7,
        score=0.016,
        coverage=1.0,
    )


@pytest.fixture
def found(monkeypatch):
    """Подменить поиск: база «есть», выдача задаётся тестом."""

    def apply(chunks):
        async def search(_query, _k=0):
            return list(chunks)

        monkeypatch.setattr(qa.kb, "available", lambda: bool(chunks))
        monkeypatch.setattr(qa.kb, "search", search)

    return apply


@pytest.fixture
def model(monkeypatch):
    """Подменить модель и записать, звали ли её."""
    calls: list[tuple[str, str]] = []

    def apply(answer: llm.Answer):
        async def ask(system, user, *, models=None):
            calls.append((system, user))
            return answer

        monkeypatch.setattr(qa.llm, "ask", ask)
        return calls

    return apply


def _ok(text: str) -> llm.Answer:
    return llm.Answer(data={"answer": text, "used": [1]}, model="test/model", calls=1)


# --- Промпт живёт в content и проверяется на импорте ------------------------


def test_prompt_is_loaded_from_content():
    assert qa.PROMPT.name == "qa_prompt.md"
    assert qa.SYSTEM


def test_prompt_keeps_its_three_rules():
    """Промпт — содержание, и править его будет человек без программирования."""
    for required in qa.REQUIRED:
        assert required in qa.SYSTEM, required


def test_prompt_without_a_rule_stops_the_start(tmp_path, monkeypatch):
    """Бот, потерявший запрет выдумывать числа, стартовать не должен.

    Молчаливая потеря здесь хуже падения: снаружи он выглядел бы работающим,
    а внутри превратился бы в болтуна с видом клинических рекомендаций.
    """
    broken = tmp_path / "qa_prompt.md"
    broken.write_text(
        qa.PROMPT.read_text(encoding="utf-8").replace("Ни одного числа", "Пиши числа"),
        encoding="utf-8",
    )
    monkeypatch.setattr(qa, "PROMPT", broken)
    with pytest.raises(ValueError, match="Ни одного числа"):
        qa._load()


def test_prompt_without_a_separator_stops_the_start(tmp_path, monkeypatch):
    broken = tmp_path / "qa_prompt.md"
    broken.write_text("Просто текст без разделителя", encoding="utf-8")
    monkeypatch.setattr(qa, "PROMPT", broken)
    with pytest.raises(ValueError, match="разделител"):
        qa._load()


def test_explanation_does_not_reach_the_model():
    """Первая половина файла объясняет, что в нём лежит, — модели она не нужна."""
    assert "Этот файл — не документация" not in qa.SYSTEM


# --- Порядок отказов --------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_base_the_model_is_not_called(found, model):
    found([])
    calls = model(_ok("что-то"))
    result = await qa.answer("когда прикладывать к груди")
    assert result.kind == qa.NO_KB
    assert calls == []


@pytest.mark.asyncio
async def test_nothing_found_means_no_answer_and_no_call(found, model, monkeypatch):
    """«Не нашёл» — законный ответ, и он не стоит ни одного запроса."""
    monkeypatch.setattr(qa.kb, "available", lambda: True)

    async def empty(_query, _k=0):
        return []

    monkeypatch.setattr(qa.kb, "search", empty)
    calls = model(_ok("что-то"))

    result = await qa.answer("сколько стоит биткоин")
    assert result.kind == qa.NOT_FOUND
    assert calls == []


@pytest.mark.asyncio
async def test_spent_quota_shows_the_fragments(found, model, monkeypatch):
    found([_chunk()])
    calls = model(_ok("что-то"))
    monkeypatch.setattr(qa.qa_log, "quota_spent", lambda: True)

    result = await qa.answer("когда прикладывать к груди")
    assert result.kind == qa.EXTRACT
    assert result.reason == llm.QUOTA
    assert calls == []


@pytest.mark.asyncio
async def test_without_keys_the_fragments_are_the_answer(found, model):
    """Пустой `.env` — рабочий режим, а не поломка.

    Цитата из клинических рекомендаций с названием документа отвечает на
    вопрос сама. Ровно этим бот и отличается от чата с моделью.
    """
    found([_chunk()])
    model(llm.Answer(reason=llm.NO_KEY))

    result = await qa.answer("когда прикладывать к груди")
    assert result.kind == qa.EXTRACT
    assert result.chunks


@pytest.mark.asyncio
async def test_empty_answer_falls_back_to_the_fragments(found, model):
    """Модель честно сказала «во фрагментах этого нет» — показываем их."""
    found([_chunk()])
    model(_ok("   "))
    assert (await qa.answer("вопрос про грудь")).kind == qa.EXTRACT


@pytest.mark.asyncio
async def test_a_good_answer_is_passed_through(found, model):
    found([_chunk()])
    model(_ok("Прикладывают в первые 30 минут."))
    result = await qa.answer("когда прикладывать к груди")
    assert result.kind == qa.ANSWERED
    assert result.answer == "Прикладывают в первые 30 минут."


# --- Сторож чисел -----------------------------------------------------------


@pytest.mark.asyncio
async def test_invented_number_throws_the_answer_away(found, model):
    """Придуманная дозировка дороже любого удобства.

    В источнике сказано «в первые 30 минут», модель пишет «в первые 2 часа» —
    текст гладкий, и отличить его от правды, не глядя в документ, нельзя.
    """
    found([_chunk()])
    model(_ok("Прикладывают в первые 2 часа после рождения."))

    result = await qa.answer("когда прикладывать к груди")
    assert result.kind == qa.EXTRACT
    assert result.reason == "invented_numbers"


@pytest.mark.asyncio
async def test_a_number_from_the_question_is_allowed(found, model):
    """Человек сам назвал число — повторить его не значит выдумать."""
    found([_chunk()])
    model(_ok("При 37.5 действуют так же."))
    assert (await qa.answer("а если 37.5")).kind == qa.ANSWERED


def test_decimal_zero_is_the_same_number():
    assert qa.invented_numbers("норма 6,0 месяцев", "не ранее 6 месяцев") == set()


def test_trailing_zero_of_an_integer_is_not_stripped():
    """«10» не должно превращаться в «1».

    Иначе сторож пропускал бы ровно то, ради чего стоит: ответ про 10 мл при
    источнике про 1 мл выглядел бы проверенным.
    """
    assert qa.invented_numbers("дают 10 мл", "дают 1 мл") == {"10"}


def test_numbers_inside_a_range_are_seen():
    assert qa.invented_numbers("схема 0-1-6", "по схеме 0-1-6") == set()


# --- Из чего собран ответ ---------------------------------------------------


def test_answer_carries_the_source_and_the_disclaimer():
    text = texts.qa_answer("Прикладывают рано.", (_chunk(),))
    assert "https://example.invalid/kr" in text
    assert texts.QA_DISCLAIMER in text


def test_answer_shows_the_fragment_next_to_itself():
    """Придуманное видно только рядом с настоящим текстом."""
    text = texts.qa_answer("Прикладывают рано.", (_chunk(),))
    assert "первые 30 минут" in text


def test_extract_carries_the_source_and_the_disclaimer():
    text = texts.qa_extract((_chunk(),))
    assert "https://example.invalid/kr" in text
    assert texts.QA_DISCLAIMER in text


def test_sources_are_listed_once_per_document():
    text = texts.qa_sources((_chunk(), _chunk(heading="Другой раздел")))
    assert text.count("https://example.invalid/kr") == 1


def test_angle_brackets_from_the_document_are_escaped():
    """В приказах сноски пишут как «<1>», и Telegram на этом молча отказывает.

    Сообщение с неразобранной разметкой не уходит вовсе — то есть ответ
    исчезает, а в логе остаётся только BadRequest.
    """
    text = texts.qa_extract((_chunk(text="Осмотр врачом <1> обязателен."),))
    assert "&lt;1&gt;" in text
    assert "<1>" not in text


# --- Квота ------------------------------------------------------------------


def test_quota_counts_requests_not_lines(monkeypatch):
    """Один вопрос стоит до девяти запросов: цепочка перебирает модели."""
    monkeypatch.setattr(qa_log.settings, "llm_daily_limit", 5)
    qa_log.write(event="qa", calls=3)
    assert qa_log.calls_today() == 3
    assert qa_log.quota_spent() is False
    qa_log.write(event="qa", calls=2)
    assert qa_log.quota_spent() is True


def test_zero_limit_turns_the_check_off(monkeypatch):
    monkeypatch.setattr(qa_log.settings, "llm_daily_limit", 0)
    qa_log.write(event="qa", calls=1000)
    assert qa_log.quota_spent() is False


def test_a_broken_log_does_not_break_the_answer(monkeypatch):
    """Лог — побочная вещь, пункт 3 контракта ошибок проекта.

    Упавшая запись не должна стоить человеку ответа, поэтому исключение
    глотается целиком — включая открытие файла.
    """

    def boom(*args, **kwargs):
        raise OSError("диск полон")

    monkeypatch.setattr(type(qa_log.PATH), "open", boom)
    qa_log.write(event="qa", calls=1)
    # Счётчик при этом вырос: он в памяти и от диска не зависит
    assert qa_log.calls_today() == 1


# --- Главное: до модели доходит не всё --------------------------------------


class RecordingSession(BaseSession):
    """Сеть не трогаем: складываем отправленное в список."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Any, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        payload = method.model_dump(exclude_none=True)
        if "text" in payload:
            self.sent.append(payload["text"])
        return Message(message_id=999, date=NOW, chat=CHAT, from_user=AUTHOR, text="")

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def wired(session, family, child, mother, monkeypatch):
    """Диспетчер с настоящими роутерами в настоящем порядке.

    Middleware подставляет то же, что `FamilyMiddleware` в бою. Смысл теста —
    именно порядок: ни один из проверяемых тут инвариантов не написан в коде
    хендлеров, оба заданы списком в `handlers/__init__.py`.
    """
    session_bot = RecordingSession()
    bot = Bot(token="42:TEST", session=session_bot)
    dp = Dispatcher()

    async def inject(handler, event, data):
        data.update(session=session, family=family, child=child, member=mother)
        return await handler(event, data)

    dp.update.outer_middleware(inject)
    order = (alarm.router, qa_handler.router, measure.router)
    for router in order:
        dp.include_router(router)

    asked: list[str] = []

    async def answer(question: str) -> qa.Result:
        asked.append(question)
        return qa.Result(qa.NOT_FOUND, "", ())

    monkeypatch.setattr(qa_handler.qa, "answer", answer)
    yield dp, bot, session_bot, asked

    # Роутеры — синглтоны уровня модуля, и aiogram запоминает, к какому
    # диспетчеру их подключили. Без отвязки второй тест в файле падает на
    # «Router is already attached»
    for router in order:
        router._parent_router = None


async def _feed(dp, bot, text, message_id=1):
    message = Message(
        message_id=message_id, date=NOW, chat=CHAT, from_user=AUTHOR, text=text
    )
    await dp.feed_update(bot, Update(update_id=message_id, message=message))


@pytest.mark.asyncio
async def test_a_red_flag_never_reaches_the_model(wired):
    """Граница безопасности, ради которой этап 4 делался раньше шестого.

    Человек нажал «Вопрос» и написал про судороги. Ответить обязан триаж —
    фиксированным текстом из файла, — а не модель. Обеспечивает это не проверка
    внутри Q&A, а порядок роутеров: `alarm` стоит выше и забирает апдейт себе.
    """
    dp, bot, session_bot, asked = wired
    pending.remember(CHAT.id, AUTHOR.id, pending.QUESTION)

    await _feed(dp, bot, "у неё судороги, что делать")

    assert asked == [], "вопрос с красным флагом до модели доходить не должен"
    assert any("103" in text for text in session_bot.sent)


@pytest.mark.asyncio
async def test_a_question_reaches_the_model(wired):
    dp, bot, session_bot, asked = wired
    pending.remember(CHAT.id, AUTHOR.id, pending.QUESTION)

    await _feed(dp, bot, "когда вводить прикорм")

    assert asked == ["когда вводить прикорм"]


@pytest.mark.asyncio
async def test_without_waiting_a_question_is_not_a_question(wired):
    """Родители переписываются в этом же чате. Молчим, если не спрашивали."""
    dp, bot, session_bot, asked = wired

    await _feed(dp, bot, "когда вводить прикорм")

    assert asked == []
    assert session_bot.sent == []


@pytest.mark.asyncio
async def test_a_bare_number_still_reaches_measure(wired):
    """Ввод веса числом не сломан — это и есть цена за место в списке.

    Ожидание вопроса сделано фильтром именно поэтому: хендлер, проверяющий
    ожидание внутри себя, забрал бы апдейт независимо от результата, и число
    молча пропало бы по дороге к `measure`.
    """
    dp, bot, session_bot, asked = wired
    pending.remember(CHAT.id, AUTHOR.id, pending.WEIGHT_G)

    await _feed(dp, bot, "3560")

    assert asked == []
    assert any("3560" in text or "3 560" in text.replace(" ", " ") for text in session_bot.sent)


@pytest.mark.asyncio
async def test_the_button_starts_waiting(wired):
    dp, bot, session_bot, asked = wired
    call = CallbackQuery(
        id="1",
        from_user=AUTHOR,
        chat_instance="x",
        data=keyboards.AskCB().pack(),
        message=Message(message_id=5, date=NOW, chat=CHAT, from_user=AUTHOR, text="панель"),
    )
    await dp.feed_update(bot, Update(update_id=7, callback_query=call))

    ask = pending.peek(CHAT.id, AUTHOR.id)
    assert ask is not None and ask.what == pending.QUESTION
    assert texts.ASK_QUESTION in session_bot.sent


@pytest.mark.asyncio
async def test_the_refusal_path_makes_no_network_calls(found, model, monkeypatch):
    """Отказ собирается офлайн — как и экстренный ответ триажа.

    Тест физический: рвём сокеты. Если в путь «базы нет» или «не нашёл»
    когда-нибудь заползёт обращение к модели, упадёт здесь.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("отказ не имеет права ходить в сеть")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    found([])
    model(_ok("не должно быть вызвано"))
    assert (await qa.answer("что угодно")).kind == qa.NO_KB
