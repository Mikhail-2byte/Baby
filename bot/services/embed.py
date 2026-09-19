"""Векторы смысла: один запрос к провайдеру эмбеддингов.

Плотный поиск отклонялся 15.09.2026 не за бесполезность, а за цену: у
`multilingual-e5-large` 2.24 ГБ модели и около 2.5 ГБ у процесса, который
работает круглосуточно рядом с Family_planer. Ради пяти промахов из сорока
семи — дорого, и решение было правильным.

Здесь вычисление вынесено к провайдеру, и та цена исчезает целиком: в процессе
остаётся матрица 2104×1024 чисел, то есть около восьми мегабайт. Это не отмена
решения, а другая цена того же выигрыша. Планка, поднятая тогда же («обойти не
0.83, а словарь плюс починенный сторож»), остаётся в силе и меряется
`kb/eval_kb.py`.

**Наружу исключений не выпускает.** При любом сбое — нет ключа, отказ
провайдера, неразобранный ответ — возвращает `None`. У `None` ровно одно
значение: плотной выдачи в этот раз нет, поиск работает как работал до неё.
Причин, как у `llm.Answer`, здесь нет намеренно: там их двенадцать и каждая
меняет текст в чате, а тут исход один и человеку не показывается никогда.

**Векторы возвращаются нормированными.** Тогда косинус — это одно скалярное
произведение, и деления на норму в горячем пути нет вовсе. Нормировка стоит
здесь, а не у двух вызывающих, по общему правилу проекта: нормализация одна на
проект, иначе сборка и запрос однажды разойдутся, и разойдутся молча —
выдача останется осмысленной на вид.

**Квоты `llm_daily_limit` этот модуль не касается.** Та квота заведена против
счёта за модели ответов и считает обращения, у которых цена ошибки —
медицинская. Запрос вектора стоит тысячные доли цента и ничего не утверждает.
"""

import logging
import math

import httpx

from bot.config import settings
from bot.services import http_retry

log = logging.getLogger(__name__)

# Совместим с OpenAI, поэтому тело запроса и разбор ответа те же, что у любого
# клиента. Вендор один, в отличие от `llm`: цепочка там существует ради
# независимости отказов, а здесь второй вендор означал бы вторую модель, то
# есть несравнимые векторы и базу, которую надо пересобирать при каждом
# переключении. Отказ провайдера тут обратим и стоит одного поиска
URL = "https://openrouter.ai/api/v1/embeddings"

TIMEOUT = http_retry.TIMEOUT
ATTEMPTS = http_retry.ATTEMPTS

# Потолок ожидания на пути ЧЕЛОВЕКА, и он куда жёстче общего. Общая политика
# (три попытки, до 90 секунд) написана для сборки: там пачка из шестидесяти
# четырёх фрагментов, и повторить её дешевле, чем потерять.
#
# У вопроса в чате цена другая. Вектор — не ответ, а всего лишь вторая выдача:
# не пришёл — поиск остаётся лексическим, то есть таким, каким был до
# 19.09.2026. Ждать ради него полторы минуты значит отдать человеку худший
# исход из двух возможных: он получил бы ту же лексическую выдачу, только
# через полторы минуты, и ещё полторы ждал бы модель ответа.
#
# Живой замер: вектор приходит за доли секунды. Десять — это уже «провайдер
# лёг», и дальше ждать нечего
QUERY_BUDGET = 10.0
QUERY_ATTEMPTS = 2


def model() -> str:
    return settings.embed_model.strip()


def _key() -> str:
    """Свой ключ, иначе ключ ответов: аккаунт у OpenRouter один.

    Отдельная строка в `.env` заведена не ради второго секрета, а ради
    возможности выключить плотный поиск, не трогая Q&A.
    """
    return settings.embed_key or settings.openrouter_key


def available() -> bool:
    """Есть чем считать. Читается на каждый вызов: тесты подменяют `settings`."""
    return bool(model() and _key())


def normalise(raw: list[float]) -> list[float] | None:
    """Вектор единичной длины, или `None`, если вектора нет.

    Нулевой вектор — не ошибка провайдера, а вырожденный ответ, и делить на
    его длину нельзя. Такой отбрасывается целиком: фрагмент без направления в
    косинусе даёт ноль со всем на свете и занимал бы место в выдаче молча.
    """
    length = math.sqrt(sum(value * value for value in raw))
    if not length:
        return None
    return [value / length for value in raw]


async def vectors(
    texts: list[str],
    *,
    attempts: int = ATTEMPTS,
    budget: float = http_retry.TOTAL_BUDGET,
) -> list[list[float]] | None:
    """Векторы для пачки текстов, в том же порядке. `None` — не сложилось.

    Пачкой, а не по одному: при сборке базы текстов восемь сотен, и восемьсот
    запросов подряд упрутся в частотный лимит там, где хватает тринадцати.
    """
    if not texts:
        return []
    if not available():
        log.warning("Плотный поиск выключен: нет ключа или имени модели")
        return None

    size = max(1, settings.embed_batch)
    collected: list[list[float]] = []
    proxy = settings.embed_proxy or settings.openrouter_proxy
    async with httpx.AsyncClient(proxy=proxy or None) as client:
        for start in range(0, len(texts), size):
            batch = texts[start : start + size]

            async def attempt(batch: list[str] = batch) -> list[list[float]] | None:
                return await _try_once(client, batch)

            got = await http_retry.with_retries(
                attempt, what=f"эмбеддинги {model()}", attempts=attempts, budget=budget
            )
            if got is None or len(got) != len(batch):
                log.warning("Эмбеддинги: пачка с %d не сложилась", start)
                return None
            collected.extend(got)
    return collected


async def vector(text: str) -> list[float] | None:
    """Вектор одного текста — путь запроса человека, и потому нетерпеливый.

    Короткий бюджет здесь не оптимизация, а правильный отказ: см. `QUERY_BUDGET`.
    """
    got = await vectors([text], attempts=QUERY_ATTEMPTS, budget=QUERY_BUDGET)
    return got[0] if got else None


async def _try_once(client: httpx.AsyncClient, batch: list[str]) -> list[list[float]] | None:
    """Пачка векторов, либо `None` — «имеет смысл повторить».

    Разделения «повторить» и «отказаться» здесь меньше, чем в `llm`: любой
    неуспех кончается одинаково — плотной выдачи нет. Повтор всё же разведён,
    чтобы 429 на сборке базы не ронял всю сборку из-за одной пачки.
    """
    try:
        response = await client.post(
            URL,
            headers={"Authorization": f"Bearer {_key()}"},
            json={"model": model(), "input": batch, "encoding_format": "float"},
            timeout=TIMEOUT,
        )
    except Exception:
        # До провайдера могли не дойти вовсе — повторяем
        log.warning("Эмбеддинги: запрос не ушёл", exc_info=True)
        return None

    if response.status_code in http_retry.RETRY_CODES:
        log.warning("Эмбеддинги: %s, повторяем", response.status_code)
        return None
    if response.status_code != 200:
        log.warning("Эмбеддинги: %s %.160s", response.status_code, response.text)
        return []  # не `None`: повторять 401 и 402 бессмысленно

    try:
        body = response.json()
        rows = sorted(body["data"], key=lambda row: row.get("index", 0))
        ready = [normalise([float(value) for value in row["embedding"]]) for row in rows]
    except Exception:
        log.warning("Эмбеддинги: ответ не разобран", exc_info=True)
        return []

    # Порядок — единственное, на чём этот разбор может сломаться тихо:
    # провайдер вправе вернуть пачку как угодно, а `index` есть в схеме именно
    # затем. Перепутанные векторы дали бы поиск, отвечающий чужим фрагментом
    if any(item is None for item in ready):
        log.warning("Эмбеддинги: в пачке вырожденный вектор")
        return []
    return [item for item in ready if item is not None]
