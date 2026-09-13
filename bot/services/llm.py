"""Клиент моделей и защитный разбор их ответа.

Шестая копия из Family_planer (после `lock`, `http_retry`, `timeutil`,
`db/sqlite`, `backup`, `sending`). Переносится почти дословно; расходится с
оригиналом в одном месте — цепочек здесь две, см. `answer_chain`.

**Наружу не выпускает исключений.** При любом сбое — сети, отказе провайдера,
пустом ключе — возвращает `Answer` с непустым `reason`. Бот обязан работать
без LLM: без ключей Q&A показывает найденные фрагменты, и это не заглушка, а
режим по умолчанию при пустом `.env`.

**Почему причина, а не `None`.** В Family_planer до этого двенадцать разных
бед схлопывались в один `None`, и в чате читалась одна строка — что при пустом
ключе, что при исчерпанном лимите, что при 401 от провайдера. Понять, сломан
ли бот вообще, было нельзя.

**Почему цепочка, и почему она межвендорная.** У бесплатной модели OpenRouter
провайдер обычно ровно один, и его 401 убивает разбор целиком — это случилось
01.09.2026 и стоило суток. Смысл списка не в качестве моделей, а в
**независимости отказов**, и разные вендоры дают её надёжнее, чем разные
провайдеры внутри одного OpenRouter: у последних общий фронт, общий биллинг и
общая страница статуса.

Запись цепочки — `вендор:модель`; без префикса подразумевается `openrouter`.

Вендоры отличаются не только адресом и ключом, но и телом запроса: OpenRouter
принимает `provider.data_collection=deny`, а Groq на это же поле отвечает
`400 property 'provider' is unsupported`. Отсюда `_Endpoint.extra`.

Разбор ответа вынесен отдельно и написан с оговорками: строгой гарантии схемы
у бесплатных моделей нет, только `response_format`. На отборе модели один
ответ из восьми пришёл завёрнутым в markdown-блок, так что обёртка вокруг
JSON — не теоретический случай.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field, replace

import httpx

from bot.config import settings
from bot.services import http_retry

log = logging.getLogger(__name__)

OPENROUTER = "openrouter"
GROQ = "groq"
VENDORS = frozenset({OPENROUTER, GROQ})
DEFAULT_VENDOR = OPENROUTER

URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Политика повторов общая с будущей расшифровкой голоса — см. `http_retry`.
# Имена оставлены модулю: на них смотрят тесты и читатель, пришедший сюда
TIMEOUT = http_retry.TIMEOUT
ATTEMPTS = http_retry.ATTEMPTS
BACKOFF = http_retry.BACKOFF
RETRY_CODES = http_retry.RETRY_CODES

# Потолок ожидания на ВСЮ цепочку, а не на модель, и равен потолку одиночного
# вызова намеренно: человек в чате ждёт ровно столько же, сколько ждал бы одну
# модель. Размен принят сознательно — быстрый отказ (401, 429, 5xx приходят за
# секунды) успевает перебрать все звенья, а молчание первого съедает бюджет
# целиком, и до запасных дело не дойдёт. Лечить это удлинением ожидания хуже,
# чем не лечить: в три часа ночи полторы минуты тишины и так слишком много
CHAIN_BUDGET = http_retry.TOTAL_BUDGET

# Меньше этого остатка следующую модель не начинаем: успеть она не успеет, а
# человеку добавит ожидания на пустом месте
MIN_SLICE = 20.0

# Причины, по которым ответа не случилось. Простые строки, а не `Enum`:
# перечислений в проекте нет ни одного, а строка вдобавок ложится в лог как
# есть и читается там глазами через полгода
UNAVAILABLE = "unavailable"  # сеть, таймаут, отказ провайдера, вся цепочка
BAD_JSON = "bad_json"  # модель ответила, но не JSON
NO_KEY = "no_key"  # ни у одного звена цепочки нет ключа
NO_MODEL = "no_model"  # цепочка пуста
# Единственная причина, которую ставит не этот модуль, а вызывающий: до
# обращения к модели при исчерпанной квоте мы не доходим. Живёт здесь, чтобы
# список причин был один
QUOTA = "quota"

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Answer:
    """Итог обращения к модели: либо разбор, либо причина отказа.

    `__bool__` не определён намеренно: `http_retry.with_retries` возит этот
    объект как результат и проверяет его через `is not None`. Собственная
    истинность, ложная при пустом разборе, однажды превратила бы «модель
    ответила пустым объектом» в «модель не ответила».
    """

    data: dict | None = None
    reason: str = ""  # пусто ровно тогда, когда разбор удался
    model: str = ""  # фактически ответившая (или последняя пробованная)
    tried: tuple[str, ...] = ()  # вся цепочка в порядке перебора
    detail: str = ""  # текст провайдера; только для лога, не для чата
    calls: int = 0  # сколько запросов реально ушло — на них потрачена квота
    elapsed: float = 0.0  # секунды

    @property
    def ok(self) -> bool:
        return self.data is not None


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """Куда идти за вендором и чем дополнить тело запроса."""

    vendor: str
    url: str
    key: str
    proxy: str
    extra: dict = field(default_factory=dict)


def endpoint(vendor: str) -> _Endpoint | None:
    """Адрес, ключ и добавка к payload. Неизвестный вендор — `None`.

    Читается на каждый вызов, а не складывается в таблицу на импорте:
    `settings` — синглтон, и тесты подменяют в нём поля.
    """
    if vendor == GROQ:
        # Ключ и прокси у аккаунта одни на расшифровку и на ответы; `STT_*`
        # просто появились раньше. Вторая строка с тем же секретом развела бы
        # их со временем
        return _Endpoint(
            GROQ,
            GROQ_URL,
            settings.groq_key or settings.stt_key,
            settings.groq_proxy or settings.stt_proxy,
        )
    if vendor == OPENROUTER:
        return _Endpoint(
            OPENROUTER,
            URL,
            settings.openrouter_key,
            settings.openrouter_proxy,
            # Дублирует настройку аккаунта: приватность не должна зависеть от
            # галки, которую можно вернуть или потерять со сменой ключа. А
            # здесь в запрос уходят вопросы про здоровье ребёнка. Groq это поле
            # не принимает вовсе — см. докстроку модуля
            {"provider": {"data_collection": "deny"}},
        )
    return None


def split_entry(entry: str) -> tuple[str, str]:
    """`groq:openai/gpt-oss-120b` → `("groq", "openai/gpt-oss-120b")`.

    Префиксом считается часть до первого двоеточия, **если в ней нет слэша**.
    Это не придирчивость: у моделей OpenRouter двоеточие есть в самом имени —
    `minimax/minimax-m3:free`. Разрезав по первому попавшемуся, мы получили бы
    вендора «minimax/minimax-m3» и модель «free». Слэш и разводит эти два
    случая: имя модели там всегда `организация/модель`, а имя вендора — одно
    слово.

    Неизвестное слово в префиксе возвращается как есть, а не подменяется на
    `openrouter`. Иначе опечатка (`gorq:`, `Groq:`) молча превращалась бы в имя
    несуществующей модели OpenRouter: звено тратило бы запрос, получало 404 и
    уходило дальше — то есть цепочка работала бы, а `.env` врал.
    """
    vendor, sep, model = entry.partition(":")
    if not sep or "/" in vendor:
        return DEFAULT_VENDOR, entry.strip()
    return vendor.strip().lower(), model.strip()


def _chain(raw: str) -> list[str]:
    chain: list[str] = []
    for name in raw.split(","):
        name = name.strip()
        if name and name not in chain:
            chain.append(name)
    return chain


def model_chain() -> list[str]:
    """Общая цепочка: разбор текста, проверки связи, всё бытовое."""
    return _chain(settings.llm_models)


def answer_chain() -> list[str]:
    """Цепочка для ответов по базе знаний.

    Отдельная настройка потому, что задачи разные по цене ошибки: разобрать
    «покормили 20 минут» может кто угодно, а отвечать на вопрос о ребёнке —
    нет. Пусто — берём общую: одна настроенная цепочка лучше, чем выключенный
    Q&A из-за незаполненной второй строки в `.env`.
    """
    return _chain(settings.llm_answer_models) or model_chain()


def _links(chain: list[str]) -> list[tuple[str, str, _Endpoint]]:
    """Звенья, к которым есть смысл идти: известный вендор и непустой ключ.

    Отсев делается **до** перебора, а не по ходу, и это не украшение. От длины
    пригодного списка зависит, кто получит полную политику повторов; считая по
    исходной цепочке, мы отнимали бы попытку у последней живой модели ровно
    тогда, когда уходить с неё некуда.
    """
    links: list[tuple[str, str, _Endpoint]] = []
    for entry in chain:
        vendor, model = split_entry(entry)
        point = endpoint(vendor)
        if point is None:
            # Опечатка в `.env` не должна выключать ответы целиком
            log.warning("Неизвестный вендор в цепочке: %s", entry)
            continue
        if not point.key:
            # Ключа нет — звено пропускаем, а не роняем цепочку: одного
            # настроенного вендора хватает
            log.warning("Ключ %s не задан — пропускаем %s", vendor, entry)
            continue
        links.append((entry, model, point))
    return links


async def ask(system: str, user: str, *, models: list[str] | None = None) -> Answer:
    """Спросить модель (и запасные) и вернуть разбор либо причину отказа."""
    chain = models if models is not None else model_chain()
    if not chain:
        log.warning("Цепочка моделей пуста — ответы через модель выключены")
        return Answer(reason=NO_MODEL)

    links = _links(chain)
    if not links:
        log.warning("Ни у одного звена цепочки нет ключа — ответы выключены")
        return Answer(reason=NO_KEY, tried=tuple(chain))

    started = time.monotonic()
    outcome = Answer(reason=UNAVAILABLE)
    spent = 0
    try:
        for number, (entry, model, point) in enumerate(links):
            left = CHAIN_BUDGET - (time.monotonic() - started)
            if number and left < MIN_SLICE:
                log.warning("Цепочка: на %s не осталось времени (%.0f с)", entry, left)
                break
            # Последнему звену достаётся полная политика повторов, всем
            # остальным — на попытку меньше: пока есть куда уйти, третий заход
            # в моргнувшего провайдера стоит человеку паузы и не даёт ничего
            attempts = ATTEMPTS if number == len(links) - 1 else ATTEMPTS - 1
            outcome = await _one_model(
                point,
                system,
                user,
                entry,
                model,
                attempts=attempts,
                budget=min(left, http_retry.TOTAL_BUDGET),
            )
            spent += outcome.calls
            if outcome.ok:
                break
            log.warning(
                "Модель %s не ответила (%s: %.120s) — идём к следующей",
                entry,
                outcome.reason,
                outcome.detail,
            )
    except Exception:
        # Контракт «наружу исключений не выпускает» абсолютен, поэтому ловим
        # весь блок, а не только конструктор клиента. `CancelledError` сюда не
        # попадёт: он наследуется от `BaseException`, а не от `Exception`
        log.warning("Не удалось обратиться к модели", exc_info=True)
        outcome = Answer(reason=UNAVAILABLE, detail="сбой до ответа")

    return replace(
        outcome,
        tried=tuple(entry for entry, _, _ in links),
        calls=spent,
        elapsed=time.monotonic() - started,
    )


async def _one_model(
    point: _Endpoint,
    system: str,
    user: str,
    entry: str,
    model: str,
    *,
    attempts: int,
    budget: float,
) -> Answer:
    """Одна модель со всеми её повторами.

    Клиент на модель, а не на цепочку: у вендоров разные прокси. Живой
    `AsyncClient` на модуль пришлось бы закрывать на остановке бота, а запросов
    здесь единицы в день.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        **point.extra,
    }

    # Потратить мы могли несколько попыток, а `with_retries` отдаёт только
    # последнюю — поэтому счётчик снаружи цикла. Список, а не число: замыкание
    # должно уметь его править
    tally = [0]
    async with httpx.AsyncClient(proxy=point.proxy or None) as client:
        outcome = await http_retry.with_retries(
            lambda: _try_once(client, point, payload, tally),
            what=entry,
            attempts=attempts,
            budget=budget,
        )
    if outcome is None:  # попытки или бюджет исчерпаны
        return Answer(reason=UNAVAILABLE, model=entry, calls=tally[0], detail="нет ответа")
    return replace(outcome, model=entry, calls=tally[0])


async def _try_once(
    client: httpx.AsyncClient, point: _Endpoint, payload: dict, tally: list[int]
) -> Answer | None:
    """Итог по этой модели, либо `None` — «имеет смысл повторить»."""
    try:
        response = await client.post(
            point.url,
            headers={"Authorization": f"Bearer {point.key}"},
            json=payload,
            timeout=TIMEOUT,
        )
    except Exception:
        # Попытку не считаем: до провайдера мы могли не дойти вовсе (DNS,
        # прокси, оборванная сеть). Засчитывать её значило бы при лежащем
        # интернете выесть суточный лимит, не отправив ни одного запроса
        log.warning("%s недоступен", point.vendor, exc_info=True)
        return None

    # Любой HTTP-ответ — потраченный запрос аккаунта, включая 401 и 429.
    # Считая только удачные, защита от лимита не срабатывала бы ровно в
    # сценарии 429, ради которого написана
    tally[0] += 1

    if response.status_code in RETRY_CODES:
        log.warning("%s ответил %s — повторим", point.vendor, response.status_code)
        return None
    if response.status_code != 200:
        log.error("%s отказал: %s %.200s", point.vendor, response.status_code, response.text)
        # Повторять этой моделью бессмысленно, но у цепочки есть следующая:
        # 401 бывает свойством провайдера, а не нашего ключа
        return Answer(
            reason=UNAVAILABLE,
            detail=f"{response.status_code} {response.text[:160]}",
        )

    try:
        body = response.json()
    except ValueError:
        log.error("%s вернул не JSON: %.200s", point.vendor, response.text)
        return Answer(reason=UNAVAILABLE, detail="ответ не JSON")

    # Ошибка провайдера приезжает и с кодом 200 — с полем error в теле
    if body.get("error"):
        log.warning("Провайдер отказал: %.200s", str(body["error"]))
        return None

    choices = body.get("choices") or []
    if not choices:
        log.error("%s вернул ответ без choices: %.200s", point.vendor, str(body))
        return Answer(reason=UNAVAILABLE, detail="ответ без choices")
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        # Рассуждающие модели умеют обрываться, оставив content пустым
        log.warning("Пустой ответ, finish_reason=%s", choices[0].get("finish_reason"))
        return None

    # Разбор JSON здесь, а не этажом выше, — и в этом смысл цепочки: модель,
    # ответившая мусором вместо JSON, передаёт ход следующей, а не роняет ответ
    parsed = extract_json(content)
    if parsed is None:
        log.warning("Модель вернула не JSON: %.200s", content)
        return Answer(reason=BAD_JSON, detail=content[:160])
    return Answer(data=parsed)


def extract_json(raw: str) -> dict | None:
    """Достать объект JSON из ответа модели. Мусор — `None`, не исключение.

    Три случая с отбора модели: чистый JSON; JSON внутри markdown-обёртки;
    текст, в котором JSON — только часть.
    """
    if not raw:
        return None
    text = raw.strip()

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        value = json.loads(text)
    except ValueError:
        pass
    else:
        # Разобралось — вердикт окончательный. Если это не объект (например
        # голый массив), вырезать из него внутренний `{...}` нельзя: получился
        # бы первый элемент, выданный за весь ответ. Честнее отказать
        return value if isinstance(value, dict) else None

    # Не разобралось целиком — последняя попытка: от первой скобки до
    # последней. Помогает, когда модель приписала «Вот ответ:» или хвост
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            value = json.loads(text[start : end + 1])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return None
