"""Клиент моделей: цепочка, причины отказа, защитный разбор ответа.

Перенесён из Family_planer вместе с самим модулем — это готовая сетка,
оплаченная чужими граблями. Отличий два: здесь нет legacy-поля
`OPENROUTER_MODEL`, зато есть вторая цепочка для ответов по базе знаний.

Сети в тестах нет: httpx подменяется `MockTransport`, а сон между ретраями —
monkeypatch'ем, иначе тест ждал бы шесть секунд.
"""

import json

import httpx
import pytest

from bot.services import http_retry, llm


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Паузы между ретраями живут в `http_retry` — там их и гасим.

    Раньше подменялся `asyncio.sleep` в самом модуле клиента; с выносом общей
    политики повторов спать стало некому, и тест падал на отсутствии атрибута.
    """

    async def instant(_seconds):
        return None

    monkeypatch.setattr(http_retry.asyncio, "sleep", instant)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Настройки под тест, включая пустую цепочку.

    `settings` — синглтон, прочитанный из живого `.env`, поэтому запасные модели
    обязательно гасим: иначе тесты вели бы себя по-разному у владельца, у
    которого цепочка заполнена, и в CI, где `.env` нет.
    """
    monkeypatch.setattr(llm.settings, "openrouter_key", "sk-test")
    monkeypatch.setattr(llm.settings, "llm_models", "test/model")
    monkeypatch.setattr(llm.settings, "llm_answer_models", "")
    monkeypatch.setattr(llm.settings, "openrouter_proxy", "")
    monkeypatch.setattr(llm.settings, "groq_key", "gsk-test")
    monkeypatch.setattr(llm.settings, "groq_proxy", "")


def _client_returning(responses, calls=None):
    """Подменить httpx.AsyncClient транспортом, отдающим `responses` по очереди."""
    sequence = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        item = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler)


@pytest.fixture
def patched(monkeypatch):
    def apply(responses, calls=None):
        transport = _client_returning(responses, calls)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs.pop("proxy", None)
            return original(*args, transport=transport, **kwargs)

        monkeypatch.setattr(llm.httpx, "AsyncClient", factory)

    return apply


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.mark.asyncio
async def test_returns_parsed_json(patched):
    patched([_ok('{"intent": "create", "items": []}')])
    answer = await llm.ask("system", "купить хлеб")
    assert answer.data == {"intent": "create", "items": []}
    assert answer.reason == ""
    assert answer.model == "test/model"


@pytest.mark.asyncio
async def test_request_denies_data_collection(patched):
    """Приватность не должна зависеть только от галки в аккаунте (шаг 3a.0)."""
    calls = []
    patched([_ok("{}")], calls)
    await llm.ask("system", "текст")
    assert calls[0]["provider"] == {"data_collection": "deny"}
    assert calls[0]["model"] == "test/model"


@pytest.mark.asyncio
async def test_network_failure_returns_a_reason_not_an_exception(patched):
    """Главное требование шага: сетевая ошибка не выходит наружу."""
    patched([httpx.ConnectError("сеть отвалилась")])
    answer = await llm.ask("system", "текст")
    assert answer.data is None
    assert answer.reason == llm.UNAVAILABLE


@pytest.mark.asyncio
async def test_a_network_failure_costs_no_quota(patched):
    """До провайдера мы могли не дойти вовсе — списывать за это нечего.

    Иначе при лежащем интернете бот выел бы суточный лимит, не отправив ни
    одного запроса, и разбор остался бы выключен до полуночи уже своими руками.
    """
    patched([httpx.ConnectError("сеть отвалилась")])
    assert (await llm.ask("system", "текст")).calls == 0


@pytest.mark.asyncio
async def test_retries_then_succeeds(patched):
    calls = []
    patched(
        [httpx.Response(429, json={}), httpx.Response(503, json={}), _ok('{"ok": 1}')],
        calls,
    )
    answer = await llm.ask("system", "текст")
    assert answer.data == {"ok": 1}
    assert len(calls) == 3, "две неудачи должны были дать два ретрая"
    assert answer.calls == 3, "неудачные попытки тоже потрачены"


@pytest.mark.asyncio
async def test_gives_up_after_three_attempts(patched):
    calls = []
    patched([httpx.Response(429, json={})], calls)
    answer = await llm.ask("system", "текст")
    assert answer.data is None
    assert answer.reason == llm.UNAVAILABLE
    assert len(calls) == llm.ATTEMPTS


@pytest.mark.asyncio
async def test_bad_key_is_not_retried(patched):
    """401 повторять бессмысленно — ключ от повтора не починится."""
    calls = []
    patched([httpx.Response(401, json={"error": {"message": "no auth"}})], calls)
    answer = await llm.ask("system", "текст")
    assert answer.data is None
    assert answer.reason == llm.UNAVAILABLE
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_reason_carries_the_provider_text(patched):
    """01.09.2026 весь диагноз был в теле ответа — до `/ai` он не доезжал."""
    patched(
        [
            httpx.Response(
                401,
                json={"error": {"message": "Provider returned error", "code": 401}},
            )
        ]
    )
    answer = await llm.ask("system", "текст")
    assert answer.detail.startswith("401 ")
    assert "Provider returned error" in answer.detail


@pytest.mark.asyncio
async def test_provider_error_inside_200_is_retried(patched):
    """Отказ провайдера приезжает и с кодом 200 — с полем error в теле."""
    calls = []
    patched(
        [
            httpx.Response(200, json={"error": {"message": "rate-limited upstream"}}),
            _ok('{"ok": 1}'),
        ],
        calls,
    )
    assert (await llm.ask("system", "текст")).data == {"ok": 1}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_empty_content_is_retried(patched):
    """Рассуждающая модель умеет оборваться, оставив content пустым."""
    calls = []
    patched(
        [
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": ""}, "finish_reason": "error"}]},
            ),
            _ok('{"ok": 1}'),
        ],
        calls,
    )
    assert (await llm.ask("system", "текст")).data == {"ok": 1}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_markdown_wrapped_answer_is_understood(patched):
    patched([_ok('```json\n{"intent": "create"}\n```')])
    assert (await llm.ask("system", "текст")).data == {"intent": "create"}


@pytest.mark.asyncio
async def test_garbage_instead_of_json_is_its_own_reason(patched):
    """«Модель ответила мусором» и «модель не ответила» — разные беды."""
    patched([_ok("Вот разбор: сейчас посмотрю")])
    answer = await llm.ask("system", "текст")
    assert answer.data is None
    assert answer.reason == llm.BAD_JSON


@pytest.mark.asyncio
async def test_no_key_means_no_call(patched, monkeypatch):
    """Без ключа бот обязан работать — просто без разбора текста."""
    monkeypatch.setattr(llm.settings, "openrouter_key", "")
    calls = []
    patched([_ok("{}")], calls)
    answer = await llm.ask("system", "текст")
    assert answer.reason == llm.NO_KEY
    assert calls == []


@pytest.mark.asyncio
async def test_no_model_means_no_call(patched, monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_models", "")
    calls = []
    patched([_ok("{}")], calls)
    answer = await llm.ask("system", "текст")
    assert answer.reason == llm.NO_MODEL
    assert calls == []


@pytest.mark.asyncio
async def test_no_key_and_no_model_are_told_apart(patched, monkeypatch):
    """Обе беды чинятся правкой `.env`, но разными строками в нём."""
    monkeypatch.setattr(llm.settings, "openrouter_key", "")
    patched([_ok("{}")])
    assert (await llm.ask("s", "t")).reason == llm.NO_KEY

    monkeypatch.setattr(llm.settings, "openrouter_key", "sk-test")
    monkeypatch.setattr(llm.settings, "llm_models", "")
    assert (await llm.ask("s", "t")).reason == llm.NO_MODEL


# --- Цепочка моделей (этап 12) -----------------------------------------------


@pytest.fixture
def chained(monkeypatch):
    """Запасная модель у другого провайдера."""
    monkeypatch.setattr(llm.settings, "llm_models", "test/model,spare/model")


@pytest.mark.asyncio
async def test_second_model_answers_when_the_first_refuses(patched, chained):
    """Ровно случай 01.09.2026: единственный провайдер основной модели моргнул."""
    calls = []
    patched([httpx.Response(401, json={"error": "провайдер"}), _ok('{"ok": 1}')], calls)
    answer = await llm.ask("system", "текст")
    assert answer.data == {"ok": 1}
    assert answer.model == "spare/model", "ответила запасная — её и надо назвать"
    assert answer.tried == ("test/model", "spare/model")
    assert [call["model"] for call in calls] == ["test/model", "spare/model"]


@pytest.mark.asyncio
async def test_chain_stops_on_the_first_success(patched, chained):
    """Запасная — на случай беды, а не второе мнение на каждую фразу."""
    calls = []
    patched([_ok('{"ok": 1}')], calls)
    answer = await llm.ask("system", "текст")
    assert answer.model == "test/model"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_garbage_hands_over_to_the_next_model(patched, chained):
    """Разбор JSON внутри попытки затем и нужен, чтобы у мусора был запасной."""
    calls = []
    patched([_ok("не JSON вовсе"), _ok('{"ok": 1}')], calls)
    answer = await llm.ask("system", "текст")
    assert answer.data == {"ok": 1}
    assert answer.model == "spare/model"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_the_last_model_gets_one_attempt_more(patched, chained):
    """Пока есть куда уйти, третий заход в моргнувшего провайдера бесполезен."""
    calls = []
    patched([httpx.Response(429, json={})], calls)
    answer = await llm.ask("system", "текст")
    assert answer.data is None
    assert len(calls) == (llm.ATTEMPTS - 1) + llm.ATTEMPTS
    assert answer.calls == len(calls), "все попытки цепочки потрачены"


@pytest.mark.asyncio
async def test_chain_never_waits_longer_than_a_single_call():
    """Инвариант «ожидание внешнего вызова ограничено сверху» цепочка не отменяет.

    Три модели по три попытки по минуте — это девять минут молчания в чате.
    """
    assert llm.CHAIN_BUDGET == http_retry.TOTAL_BUDGET


@pytest.mark.asyncio
async def test_next_model_is_not_started_on_the_last_seconds(
    patched, chained, monkeypatch
):
    """Начать и не успеть — значит добавить человеку ожидания на пустом месте."""
    ticks = iter([0.0, 0.0, llm.CHAIN_BUDGET - 1.0, llm.CHAIN_BUDGET - 1.0])
    monkeypatch.setattr(llm.time, "monotonic", lambda: next(ticks, llm.CHAIN_BUDGET))
    calls = []
    patched([httpx.Response(401, json={})], calls)

    answer = await llm.ask("system", "текст")

    assert answer.data is None
    assert len(calls) == 1, "на запасную времени не осталось — не начинаем"


def test_model_chain_drops_blanks_and_duplicates(monkeypatch):
    """В `.env` пишут руками: лишняя запятая и повтор — норма."""
    monkeypatch.setattr(
        llm.settings, "llm_models", " main/model , ,spare/one, main/model "
    )
    assert llm.model_chain() == ["main/model", "spare/one"]


def test_model_chain_is_empty_without_models(monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_models", "")
    assert llm.model_chain() == []


def test_answer_chain_is_separate_when_set(monkeypatch):
    """Отвечать на вопрос о ребёнке и разбирать «покормили 20 минут» —
    задачи разной цены, и модель у них может быть разная."""
    monkeypatch.setattr(llm.settings, "llm_models", "cheap/model")
    monkeypatch.setattr(llm.settings, "llm_answer_models", "careful/model")
    assert llm.answer_chain() == ["careful/model"]


def test_answer_chain_falls_back_to_the_common_one(monkeypatch):
    """Одна настроенная цепочка лучше, чем Q&A, выключенный пустой строкой."""
    monkeypatch.setattr(llm.settings, "llm_models", "cheap/model")
    monkeypatch.setattr(llm.settings, "llm_answer_models", "")
    assert llm.answer_chain() == ["cheap/model"]


# --- Вендоры (этап 12) -------------------------------------------------------


@pytest.fixture
def recorded(monkeypatch):
    """Как `patched`, но записывает полный запрос: URL, заголовки, тело."""
    sent: list[httpx.Request] = []

    def apply(responses):
        sequence = list(responses)

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]

        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs.pop("proxy", None)
            return original(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(llm.httpx, "AsyncClient", factory)
        return sent

    return apply


@pytest.mark.asyncio
async def test_groq_goes_to_groq_with_the_groq_key(recorded, monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_models", "groq:openai/gpt-oss-120b")
    sent = recorded([_ok('{"ok": 1}')])

    answer = await llm.ask("system", "текст")

    assert answer.ok
    assert str(sent[0].url) == llm.GROQ_URL
    assert sent[0].headers["authorization"] == "Bearer gsk-test"
    assert json.loads(sent[0].content)["model"] == "openai/gpt-oss-120b"


@pytest.mark.asyncio
async def test_groq_gets_no_provider_field(recorded, monkeypatch):
    """Groq отвечает `400 property 'provider' is unsupported`.

    То есть лишнее поле не «игнорируется на всякий случай», а роняет запрос
    целиком — приватность OpenRouter'а нельзя слать всем подряд.
    """
    monkeypatch.setattr(llm.settings, "llm_models", "groq:openai/gpt-oss-120b")
    sent = recorded([_ok('{"ok": 1}')])

    await llm.ask("system", "текст")

    assert "provider" not in json.loads(sent[0].content)


@pytest.mark.asyncio
async def test_openrouter_still_gets_the_privacy_field(recorded, monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_models", "openrouter:some/model")
    sent = recorded([_ok('{"ok": 1}')])

    await llm.ask("system", "текст")

    body = json.loads(sent[0].content)
    assert str(sent[0].url) == llm.URL
    assert body["provider"] == {"data_collection": "deny"}


@pytest.mark.asyncio
async def test_groq_key_falls_back_to_the_stt_one(monkeypatch):
    """Аккаунт один; вторая строка с тем же секретом развела бы их со временем."""
    monkeypatch.setattr(llm.settings, "groq_key", "")
    monkeypatch.setattr(llm.settings, "stt_key", "gsk-from-stt")

    point = llm.endpoint(llm.GROQ)

    assert point is not None
    assert point.key == "gsk-from-stt"


@pytest.mark.asyncio
async def test_a_vendor_without_a_key_is_skipped_not_fatal(recorded, monkeypatch):
    """Одного настроенного вендора хватает, чтобы бот разбирал."""
    monkeypatch.setattr(llm.settings, "groq_key", "")
    monkeypatch.setattr(llm.settings, "stt_key", "")
    monkeypatch.setattr(
        llm.settings, "llm_models", "groq:openai/gpt-oss-120b,openrouter:some/model"
    )
    sent = recorded([_ok('{"ok": 1}')])

    answer = await llm.ask("system", "текст")

    assert answer.ok
    assert len(sent) == 1, "к Groq без ключа ходить незачем"
    assert str(sent[0].url) == llm.URL


@pytest.mark.asyncio
async def test_no_key_anywhere_is_no_key(recorded, monkeypatch):
    monkeypatch.setattr(llm.settings, "groq_key", "")
    monkeypatch.setattr(llm.settings, "stt_key", "")
    monkeypatch.setattr(llm.settings, "openrouter_key", "")
    monkeypatch.setattr(
        llm.settings, "llm_models", "groq:openai/gpt-oss-120b,openrouter:some/model"
    )
    sent = recorded([_ok('{"ok": 1}')])

    assert (await llm.ask("system", "текст")).reason == llm.NO_KEY
    assert sent == []


@pytest.mark.asyncio
async def test_an_unknown_vendor_is_skipped_not_sent_as_a_model_name(
    recorded, monkeypatch
):
    """Опечатка в префиксе не должна ни выключать разбор, ни уходить в сеть.

    До правки `gorq:` молча становился именем несуществующей модели OpenRouter:
    звено тратило запрос, получало 404 и уходило дальше — цепочка работала, а
    `.env` врал.
    """
    monkeypatch.setattr(
        llm.settings, "llm_models", "gorq:openai/gpt-oss-120b,openrouter:some/model"
    )
    sent = recorded([_ok('{"ok": 1}')])

    answer = await llm.ask("system", "текст")

    assert answer.ok
    assert len(sent) == 1, "к выдуманному вендору ходить некуда"
    assert json.loads(sent[0].content)["model"] == "some/model"


@pytest.mark.asyncio
async def test_vendor_prefix_is_case_insensitive(recorded, monkeypatch):
    """`.env` пишут руками, и заглавная буква не повод молча уйти к другому."""
    monkeypatch.setattr(llm.settings, "llm_models", "GROQ:openai/gpt-oss-120b")
    sent = recorded([_ok('{"ok": 1}')])

    assert (await llm.ask("system", "текст")).ok
    assert str(sent[0].url) == llm.GROQ_URL


def test_a_colon_in_the_model_name_is_not_a_vendor():
    """`minimax/minimax-m3:free` работает с этапа 3a — разрезать его нельзя."""
    assert llm.split_entry("minimax/minimax-m3:free") == (
        llm.OPENROUTER,
        "minimax/minimax-m3:free",
    )
    assert llm.split_entry("openrouter:dots-studio/dots-3:free") == (
        llm.OPENROUTER,
        "dots-studio/dots-3:free",
    )
    assert llm.split_entry("groq:openai/gpt-oss-120b") == (
        llm.GROQ,
        "openai/gpt-oss-120b",
    )


def test_every_vendor_in_the_set_has_an_endpoint():
    """Сторож против «добавил имя в VENDORS и забыл про адрес»."""
    for vendor in llm.VENDORS:
        point = llm.endpoint(vendor)
        assert point is not None and point.url, vendor


@pytest.mark.asyncio
async def test_a_skipped_link_does_not_steal_the_last_ones_retries(
    recorded, monkeypatch
):
    """Пропущенное звено не должно отнимать попытку у живого.

    Если ключа нет только у второго вендора, то первый и есть последний — и
    уходить с него некуда, значит он обязан отработать полную политику повторов.
    Считая «последнего» по исходной цепочке, мы отнимали бы попытку ровно там,
    где запасного не осталось.
    """
    monkeypatch.setattr(llm.settings, "openrouter_key", "")
    monkeypatch.setattr(
        llm.settings, "llm_models", "groq:openai/gpt-oss-120b,openrouter:some/model"
    )
    sent = recorded([httpx.Response(429, json={})])

    answer = await llm.ask("system", "текст")

    assert answer.data is None
    assert len(sent) == llm.ATTEMPTS, "единственному живому звену — полная политика"
    assert answer.tried == ("groq:openai/gpt-oss-120b",), (
        "в отчёт не должна попадать модель, к которой не обращались"
    )


@pytest.mark.asyncio
async def test_tried_lists_only_the_links_actually_reachable(recorded, monkeypatch):
    """`/ai` заведена ради правды — «пробовал» обязано означать «обращался»."""
    monkeypatch.setattr(llm.settings, "groq_key", "")
    monkeypatch.setattr(llm.settings, "stt_key", "")
    monkeypatch.setattr(
        llm.settings,
        "llm_models",
        "gorq:опечатка,groq:openai/gpt-oss-120b,openrouter:some/model",
    )
    recorded([_ok('{"ok": 1}')])

    answer = await llm.ask("system", "текст")

    assert answer.tried == ("openrouter:some/model",)
