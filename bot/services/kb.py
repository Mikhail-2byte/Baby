"""Поиск по базе знаний.

База лежит отдельным файлом (`data/kb.db`), собирается скриптом
`kb/build_kb.py` и читается только на чтение. Схемы в `models.py` нет и
миграций нет намеренно: это артефакт сборки, а не данные семьи.

**Отсутствие базы — не ошибка.** `available()` вернёт `False`, и бот будет
работать ровно как раньше, просто не отвечая на вопросы. Требовать собранную
базу для старта нельзя: она нужна не всем и весит десятки мегабайт.

**Почему поиск не «что похоже», а «что покрывает вопрос».** Запрос
разворачивается в ИЛИ по основам слов — иначе вопрос из семи слов нашёл бы
только фрагмент, где есть все семь. Но ИЛИ означает, что находится всегда
хоть что-нибудь: слово «сколько» есть в каждом документе. Поэтому найденное
проверяется на покрытие: значимые слова вопроса должны в нём встретиться.
Не встретились — ответа нет. «Не нашёл» — законный ответ, а пересказ
случайного абзаца — нет.

Значимость слова берётся из частот самой базы (таблица `df`), а не из списка
стоп-слов: список пришлось бы вести руками и он всё равно разошёлся бы с
содержимым. Слово, встречающееся в четверти фрагментов, ничего не выделяет,
чем бы оно ни было.

Исключение ровно одно, и оно вынужденное: вопросительные слова
(`morph.QUESTION_WORDS`). Частота по документам их не ловит — клинические
рекомендации не задают вопросов, они отвечают, и «сколько» встречается там
в двух фрагментах из восьмисот, то есть выглядит редчайшим термином.

**Слияние RRF написано на две выдачи, хотя пока их одна.** Плотный поиск по
эмбеддингам — отложенное решение: сначала числа оценочного набора, потом
модель. Когда он появится, меняется одна строка здесь, а не устройство.
"""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from bot.config import settings
from bot.services import morph, synonyms

log = logging.getLogger(__name__)

# Схема, которую понимает этот модуль. База, собранная другой версией
# `build_kb.py`, честно считается отсутствующей: отвечать по непонятной схеме
# хуже, чем не отвечать
SCHEMA_VERSION = "1"

# Сколько фрагментов тянем из FTS5 до фильтра по покрытию. Больше сорока
# смысла нет: bm25 к этому месту уже уехал в шум
CANDIDATES = 40

# Константа RRF из исходной статьи (Cormack et al., 2009). Смысл — сгладить
# разницу между первым и вторым местом: обе выдачи ошибаются, и доверять
# верхушке любой из них нельзя
RRF_K = 60

# Надбавка за тир. Порядок величины выбран так, чтобы она РАЗВЕ ЧТО разводила
# почти равных: соседние места в RRF отличаются примерно на 2.6e-4, и вся
# надбавка на лучшем тире (5e-5 * 3) втрое меньше этого зазора. Клинические
# рекомендации Минздрава обходят рекомендацию AAP при прочих равных — и только.
# Сторожит `test_tier_bonus_cannot_jump_a_place`: первое значение, 1e-4, этот
# тест и завалило
TIER_BONUS = 5e-5
TIER_ORDER = {"T1": 3, "T2": 2, "T3": 1, "T4": 0}

# Основа, встреченная в одном-двух фрагментах из сотен, — почти всегда не
# редкое слово, а осечка стеммера: «смотрит» даёт `смотр`, которого в
# документах нет ни разу, потому что там пишут «осмотр» и «наблюдение». В
# опорные такие не годятся: на них вопрос «сколько стоит биткоин» набирал
# половину покрытия случайным совпадением и выглядел отвеченным
MIN_DF = 3

# Меньше этой базы частотные суждения не работают вовсе — ни «слишком часто»,
# ни «слишком редко». В базе из шести фрагментов каждое слово встречается один
# раз, то есть одновременно и «редчайшее», и «в шестой части документов».
# Поэтому на маленькой базе оба порога просто выключаются: пусть отвечает
# как умеет, чем молчит на всё подряд
MIN_CORPUS = 20


@dataclass(frozen=True)
class Chunk:
    """Фрагмент документа вместе со всем, чем его подписывают."""

    id: int
    source_id: str
    tier: str
    tier_name: str
    title: str
    publisher: str
    url: str
    heading: str
    text: str
    page: int | None
    score: float
    coverage: float


_connection: sqlite3.Connection | None = None
_broken = False


def _open() -> sqlite3.Connection | None:
    """Соединение с базой знаний, или `None`, если её нет или она чужая."""
    global _connection, _broken
    if _connection is not None:
        return _connection
    if _broken:
        return None
    path = Path(settings.kb_path)
    if not path.exists():
        return None
    try:
        # `check_same_thread=False` обязателен: поиск идёт из `asyncio.to_thread`,
        # то есть каждый раз из другого потока пула
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        version = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if version is None or version[0] != SCHEMA_VERSION:
            log.warning("База знаний собрана схемой %s, а нужна %s", version, SCHEMA_VERSION)
            connection.close()
            _broken = True
            return None
    except sqlite3.Error:
        log.warning("Не удалось открыть базу знаний %s", path, exc_info=True)
        _broken = True
        return None
    # Один раз, на открытии: соединение живёт весь процесс, а `_search`
    # выполняется из пула потоков — менять на нём что-либо на каждый запрос
    # значит делать это из разных потоков без нужды
    connection.row_factory = sqlite3.Row
    _connection = connection
    return _connection


def available() -> bool:
    """Есть ли по чему искать."""
    connection = _open()
    if connection is None:
        return False
    try:
        return bool(connection.execute("SELECT 1 FROM chunks LIMIT 1").fetchone())
    except sqlite3.Error:
        return False


def close() -> None:
    """Закрыть соединение. Нужно тестам и остановке бота."""
    global _connection, _broken
    if _connection is not None:
        _connection.close()
    _connection = None
    _broken = False


def _frequency(connection: sqlite3.Connection, stem: str) -> int:
    """Во скольких фрагментах встречается основа или что-то, начинающееся с неё.

    Диапазоном по первичному ключу, а не `LIKE`: таблица `df` отсортирована по
    основе, и это обычный поиск по индексу. Сумма слегка завышена — фрагмент,
    где есть и «невролог», и «неврологический», посчитан дважды, — но число
    нужно как мера распространённости слова, а не как точный счёт.
    """
    upper = stem[:-1] + chr(ord(stem[-1]) + 1)
    row = connection.execute(
        "SELECT sum(docs) FROM df WHERE stem >= ? AND stem < ?", (stem, upper)
    ).fetchone()
    return int(row[0] or 0)


def _phrase_frequency(connection: sqlite3.Connection, phrase: synonyms.Phrase) -> int:
    """Во скольких фрагментах может встретиться написание целиком.

    Оценка сверху — минимум по словам: фрагмент, где есть все слова фразы,
    содержит и самое редкое из них. Точного счёта совместной встречаемости у
    нас нет, и он тут не нужен: число работает как мера распространённости.
    """
    return min(_frequency(connection, word) for word in phrase)


def _covers(phrases: synonyms.Group, found: list[str]) -> bool:
    """Назван ли термин во фрагменте хоть одним из своих написаний."""
    return any(
        all(any(word.startswith(part) for word in found) for part in phrase)
        for phrase in phrases
    )


def _significant(
    connection: sqlite3.Connection, stems: list[str]
) -> tuple[list[synonyms.Group], list[synonyms.Group], set[synonyms.Group]]:
    """Основы вопроса, по которым судим о попадании: знакомые, незнакомые, опорные.

    Выбрасываем одну категорию — слишком частые: «как», «ребёнок», «год» есть
    в трети фрагментов и не выделяют ничего.

    Незнакомые базе основы (`docs == 0`) **не выбрасываются**. Раньше
    выбрасывались, и это была поломка ровно того свойства, ради которого
    считается покрытие: в знаменателе оставались только слова, которые база
    знает, то есть слово, делающее вопрос чужим, не считалось вовсе. «Как
    поменять масло в машине» давало знаменатель из одного «масла» и покрытие
    1.0 — бот отвечал про растительное масло в салаты. Чем более чужим было
    слово, тем меньше оно весило.

    Опорные — знакомые, что вдобавок встречаются не реже `MIN_DF` раз. Хотя бы
    одна опорная основа обязана найтись во фрагменте, иначе весь ответ держится
    на осечке стеммера.
    """
    total = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
    if not total:
        return [], [], set()
    big = total >= MIN_CORPUS
    limit = total * settings.kb_stopword_df if big else float("inf")
    floor = MIN_DF if big else 1
    known: list[synonyms.Group] = []
    unknown: list[synonyms.Group] = []
    anchors: set[synonyms.Group] = set()
    for stem in dict.fromkeys(stems):
        if stem in morph.QUESTION_STEMS:
            continue
        # Термин — само слово плюс то, как его называет документ. Частота
        # берётся по самому распространённому написанию: «БЦЖ» встречается
        # однажды, «туберкулёз» — тринадцать раз, и незнакомой аббревиатуру
        # считать нельзя, раз документ про неё говорит
        phrases = synonyms.alternatives(stems, stem) or ((stem,),)
        # Группа синонимов задевается каждым своим словом, и «хватает молока»
        # пришло бы сюда дважды — по «хватает» и по «молока». В знаменателе
        # покрытия термин обязан стоять один раз: попадание считается через
        # множество, и дубль опускал бы покрытие вдвое на ровном месте
        if phrases in known or phrases in unknown:
            continue
        docs = max(_phrase_frequency(connection, phrase) for phrase in phrases)
        if docs == 0:
            unknown.append(phrases)
            continue
        if docs > limit:
            continue
        known.append(phrases)
        if docs >= floor:
            anchors.add(phrases)
    return known, unknown, anchors


def _rrf(rankings: list[list[int]]) -> dict[int, float]:
    """Reciprocal rank fusion: сумма 1/(k + место) по всем выдачам."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for place, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + place)
    return scores


def _lexical(connection: sqlite3.Connection, parts: list[str]) -> list[sqlite3.Row]:
    expression = morph.expression(parts)
    if not expression:
        return []
    try:
        return connection.execute(
            "SELECT c.id, c.source_id, c.heading, c.text, c.page, c.stems,"
            "       s.tier, s.tier_name, s.title, s.publisher, s.url"
            "  FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
            "  JOIN sources s ON s.id = c.source_id"
            " WHERE chunks_fts MATCH ?"
            " ORDER BY bm25(chunks_fts) LIMIT ?",
            (expression, CANDIDATES),
        ).fetchall()
    except sqlite3.Error:
        # Выражение собирается из своих токенов, так что синтаксической ошибки
        # быть не должно — но если она случится, вопрос не должен ронять бота
        log.warning("Поиск по базе знаний не удался: %r", expression, exc_info=True)
        return []


def _search(query: str, k: int) -> list[Chunk]:
    connection = _open()
    if connection is None:
        return []

    known, unknown, anchors = _significant(connection, morph.stems(query))
    if not anchors:
        return []
    # Вопрос, в котором незнакомых базе слов не меньше, чем знакомых, — вопрос
    # не про эту базу. Отвечать на него по остатку значит отвечать про масло
    # в салаты на вопрос про масло в машине. Счёт целыми, а не порогом с
    # плавающей точкой: пятая настроечная ручка — приглашение подогнать её
    # под оценочный набор, а это уже стоило проекту одиннадцати пунктов recall
    if len(unknown) >= len(known):
        return []
    significant = known + unknown

    # В запрос уходят и написания из словаря синонимов: без них фрагмент, где
    # написано «вакцинация против туберкулёза», по вопросу про БЦЖ не найдётся
    # вовсе, и проверять его покрытие будет нечего
    parts = morph.stems(query) + [
        word for term in significant for phrase in term for word in phrase
    ]
    rows = _lexical(connection, parts)
    if not rows:
        return []

    by_id = {row["id"]: row for row in rows}
    coverage: dict[int, float] = {}
    anchored: dict[int, bool] = {}
    for row in rows:
        found = row["stems"].split()
        hit = {term for term in significant if _covers(term, found)}
        coverage[row["id"]] = len(hit) / len(significant)
        anchored[row["id"]] = bool(hit & anchors)

    passed = [
        row["id"]
        for row in rows
        if anchored[row["id"]] and coverage[row["id"]] >= settings.kb_min_coverage
    ]
    if not passed:
        return []

    # Пока выдача одна — лексическая. Плотная встанет вторым списком сюда же
    scores = _rrf([passed])
    ranked = _order(scores, {chunk_id: by_id[chunk_id]["tier"] for chunk_id in passed})
    return [
        _chunk(by_id[chunk_id], scores[chunk_id], coverage[chunk_id])
        for chunk_id in ranked[:k]
    ]


def _order(scores: dict[int, float], tiers: dict[int, str]) -> list[int]:
    """Порядок выдачи: счёт RRF, а при почти равном счёте — тир источника.

    Надбавка за тир умышленно мала: она не должна переставлять выдачу, только
    разводить совпавших. Пока выдача одна, совпасть счётом могут лишь
    фрагменты с одного места, чего не бывает, — но со второй выдачей это
    обычное дело: RRF складывает места, и суммы совпадают часто.
    """
    return sorted(
        scores,
        key=lambda chunk_id: (
            -(scores[chunk_id] + TIER_BONUS * TIER_ORDER.get(tiers.get(chunk_id, ""), 0)),
            chunk_id,
        ),
    )


def _chunk(row: sqlite3.Row, score: float, coverage: float) -> Chunk:
    return Chunk(
        id=row["id"],
        source_id=row["source_id"],
        tier=row["tier"],
        tier_name=row["tier_name"],
        title=row["title"],
        publisher=row["publisher"],
        url=row["url"],
        heading=row["heading"],
        text=row["text"],
        page=row["page"],
        score=score,
        coverage=coverage,
    )


async def search(query: str, k: int = 0) -> list[Chunk]:
    """Фрагменты по вопросу, от лучшего к худшему. Пусто — значит не нашли.

    Уходит в поток по тому же правилу, что графики и PDF: event loop обслуживает
    панель и таймеры, и блокировать его на время чтения базы нельзя. Сейчас
    чтение быстрое, но сюда же встанет прогон модели эмбеддингов.
    """
    return await asyncio.to_thread(_search, query, k or settings.kb_top_k)
