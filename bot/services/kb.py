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

**Выдач две, и они судят разное.** Лексическая отвечает «в этом фрагменте
есть слова вопроса», плотная — «этот фрагмент про то же самое». Обе ошибаются
по-разному, и RRF складывает места, а не счета, ровно поэтому: доверять
верхушке любой из них нельзя.

Плотная появилась 19.09.2026 и закрывает разрыв, которого лексика не
закрывает ничем: верный фрагмент лежал в сорока кандидатах у 94% вопросов, а
первым местом выходил у 60%. Четыре лексические переранжировки дали в лучшем
случае четыре пункта из тридцати четырёх.

**Плотная выдача ничего не гасит.** Она не умеет молчать: ближайший по смыслу
кусок найдётся всегда, даже для вопроса про биткоин. Поэтому ворота, судящие
сам вопрос (нет опорных основ; незнакомых больше, чем знакомых), стоят
**до** обеих выдач и остаются единственным местом, где поиск отказывается
искать. Ворота, судившие фрагмент (порог покрытия), к плотной выдаче не
применяются вовсе: они отказали бы ровно там, где она и нужна — вопрос про
ланолин 18.09 нашёл верный абзац и был отброшен с покрытием 0.40.

**Без ключа и без векторов всё работает как до 19.09.2026.** Это тот же
контракт, что у пустого `.env` в Q&A: плотной выдачи нет, остаётся
лексическая, и человек получает то же, что получал.
"""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.config import settings
from bot.services import embed, morph, synonyms

log = logging.getLogger(__name__)

# Схема, которую понимает этот модуль. База, собранная другой версией
# `build_kb.py`, честно считается отсутствующей: отвечать по непонятной схеме
# хуже, чем не отвечать
SCHEMA_VERSION = "2"

# Сколько фрагментов тянем из FTS5 до фильтра по покрытию. Больше сорока
# смысла нет: bm25 к этому месту уже уехал в шум
CANDIDATES = 40

# Столько же берём из плотной выдачи. Равенство не совпадение: RRF складывает
# МЕСТА, и список вдвое длиннее второго дал бы своим хвостам места, которых у
# того нет вовсе, — то есть перевесил бы молча, самим размером
DENSE_CANDIDATES = 40

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
    # Назвал ли этот фрагмент смысл. Нужно не отбору, а логу: без него по
    # `qa.log` не отличить «плотный поиск промолчал» от «его не было вовсе»,
    # а это ровно та разница, ради которой поиск и переделывали
    by_meaning: bool = False


_connection: sqlite3.Connection | None = None
_broken = False
# Матрица векторов и параллельный ей список идентификаторов. Грузятся один раз
# на процесс: 2104 фрагмента по 1024 числа — около восьми мегабайт. Именно за
# эти восемь мегабайт решение 15.09 и переигралось: локальная модель стоила
# 2.5 ГБ у процесса, который работает круглосуточно
_matrix = None
_matrix_ids: list[int] = []
_dense_off = False


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
    global _connection, _broken, _matrix, _matrix_ids, _dense_off
    if _connection is not None:
        _connection.close()
    _connection = None
    _broken = False
    _matrix = None
    _matrix_ids = []
    _dense_off = False


def _vectors(connection: sqlite3.Connection) -> tuple[Any, list[int]]:
    """Матрица векторов и их идентификаторы, или `(None, [])`.

    Ленивая и однократная: файл открыт на чтение весь процесс, и перечитывать
    три мегабайта на каждый вопрос незачем.

    Сверка модели здесь, а не при сборке, и она обязательна. Векторы двух
    разных моделей лежат в разных пространствах: косинус между ними считается
    без всякой ошибки и даёт осмысленно выглядящий мусор. Это тот же довод, по
    которому база чужой схемы считается отсутствующей, только страшнее —
    чужая схема роняет запрос, а чужая модель тихо меняет ответы.
    """
    global _matrix, _matrix_ids, _dense_off
    if _dense_off:
        return None, []
    if _matrix is not None:
        return _matrix, _matrix_ids
    try:
        import numpy
    except ImportError:  # pragma: no cover - numpy стоит в requirements
        log.warning("Плотный поиск выключен: нет numpy")
        _dense_off = True
        return None, []
    try:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        built_by = meta.get("embed_model", "")
        if not built_by:
            log.info("В базе знаний нет векторов — поиск только по словам")
            _dense_off = True
            return None, []
        if built_by != embed.model():
            log.warning(
                "База знаний посчитана моделью %r, а настроена %r — "
                "плотный поиск выключен, пересоберите базу",
                built_by,
                embed.model(),
            )
            _dense_off = True
            return None, []
        rows = connection.execute(
            "SELECT chunk_id, vector FROM vectors ORDER BY chunk_id"
        ).fetchall()
    except sqlite3.Error:
        log.warning("Не удалось прочитать векторы", exc_info=True)
        _dense_off = True
        return None, []
    if not rows:
        _dense_off = True
        return None, []
    _matrix_ids = [row[0] for row in rows]
    _matrix = numpy.frombuffer(
        b"".join(row[1] for row in rows), dtype=numpy.float32
    ).reshape(len(rows), -1)
    return _matrix, _matrix_ids


def _dense(connection: sqlite3.Connection, query: list[float] | None) -> list[int]:
    """Лучшие по смыслу фрагменты. Пусто — плотной выдачи в этот раз нет.

    Векторы нормированы при записи (`embed.normalise`), поэтому косинус — это
    одно умножение матрицы на вектор, без единого деления.
    """
    if not query:
        return []
    matrix, ids = _vectors(connection)
    if matrix is None:
        return []
    import numpy

    vector = numpy.asarray(query, dtype=numpy.float32)
    if vector.shape[0] != matrix.shape[1]:
        log.warning(
            "Вектор вопроса длиной %d против %d в базе — плотный поиск пропущен",
            vector.shape[0],
            matrix.shape[1],
        )
        return []
    similarity = matrix @ vector
    floor = settings.kb_min_similarity
    order = numpy.argsort(-similarity)[:DENSE_CANDIDATES]
    return [ids[int(place)] for place in order if similarity[int(place)] >= floor]


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


def _search(query: str, k: int, dense_query: list[float] | None = None) -> list[Chunk]:
    connection = _open()
    if connection is None:
        return []

    known, unknown, anchors = _significant(connection, morph.stems(query))
    if not anchors:
        return []
    # Вопрос, в котором незнакомых базе слов БОЛЬШЕ, чем знакомых, — вопрос не
    # про эту базу. Отвечать на него по остатку значит отвечать про масло в
    # салаты на вопрос про масло в машине. Счёт целыми, а не порогом с
    # плавающей точкой: пятая настроечная ручка — приглашение подогнать её
    # под оценочный набор, а это уже стоило проекту одиннадцати пунктов recall.
    #
    # Неравенство было нестрогим и стало строгим 19.09.2026, когда корпус
    # вырос с 790 фрагментов до 2104. Причина не в плотном поиске, а в
    # арифметике: «новорождённый» перевалил `kb_stopword_df` (1178 из 2104,
    # то есть 56%) и выбыл из знакомых как служебное слово. У вопроса «почему
    # у новорождённого желтеет кожа» после отсева вопросительного «почему» и
    # служебного «новорождённый» осталось одно знакомое слово против одного
    # незнакомого — и ворота гасили его ДО всякого поиска. То же случилось с
    # «зачем новорождённому колют витамин K». Оба вопроса базовые, и оба
    # молчали.
    #
    # Замерено на 55 вопросах: нестрогое против строгого — recall@1 0.69→0.71,
    # @3 0.84→0.87, @5 0.89→0.91, MRR 0.77→0.79; молчание «чужой домен»
    # 5/9→4/9, «нет в корпусе» 1/4→0/4. Размен сознательный и стоит на
    # правиле, записанном 17.09: ложное молчание ворот необратимо, а ложный
    # пропуск обратим и ограничен — мусор доезжает до модели, та молчит,
    # цена вопроса один запрос и полторы секунды
    if len(unknown) > len(known):
        return []
    significant = known + unknown

    # В запрос уходят и написания из словаря синонимов: без них фрагмент, где
    # написано «вакцинация против туберкулёза», по вопросу про БЦЖ не найдётся
    # вовсе, и проверять его покрытие будет нечего
    parts = morph.stems(query) + [
        word for term in significant for phrase in term for word in phrase
    ]
    rows = _lexical(connection, parts)
    dense = _dense(connection, dense_query)
    if not rows and not dense:
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

    # Порог покрытия судит фрагмент и к плотной выдаче не применяется: он
    # отказал бы ровно там, где она нужна. Фрагмент, пришедший только по
    # смыслу, надо дочитать из базы — в `rows` его может не быть вовсе
    missing = [chunk_id for chunk_id in dense if chunk_id not in by_id]
    if missing:
        for row in _by_ids(connection, missing):
            by_id[row["id"]] = row
            found = row["stems"].split()
            hit = {term for term in significant if _covers(term, found)}
            coverage[row["id"]] = len(hit) / len(significant)
    dense = [chunk_id for chunk_id in dense if chunk_id in by_id]

    if not passed and not dense:
        return []

    # Две выдачи, ради которых слияние и написано: RRF складывает места, а не
    # счета. Пустой список места никому не раздаёт, поэтому отсутствие любой
    # из выдач сводит слияние к другой, а не ломает его
    scores = _rrf([passed, dense])
    ranked = _order(scores, {chunk_id: by_id[chunk_id]["tier"] for chunk_id in scores})
    named_by_meaning = set(dense)
    return [
        _chunk(
            by_id[chunk_id],
            scores[chunk_id],
            coverage.get(chunk_id, 0.0),
            chunk_id in named_by_meaning,
        )
        for chunk_id in ranked[:k]
    ]


def _by_ids(connection: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    """Фрагменты по идентификаторам — для тех, что пришли только по смыслу."""
    marks = ",".join("?" * len(ids))
    try:
        return connection.execute(
            "SELECT c.id, c.source_id, c.heading, c.text, c.page, c.stems,"
            "       s.tier, s.tier_name, s.title, s.publisher, s.url"
            "  FROM chunks c JOIN sources s ON s.id = c.source_id"
            f" WHERE c.id IN ({marks})",
            ids,
        ).fetchall()
    except sqlite3.Error:
        log.warning("Не удалось дочитать фрагменты плотной выдачи", exc_info=True)
        return []


def _order(scores: dict[int, float], tiers: dict[int, str]) -> list[int]:
    """Порядок выдачи: счёт RRF, а при почти равном счёте — тир источника.

    Надбавка за тир умышленно мала: она не должна переставлять выдачу, только
    разводить совпавших. При одной выдаче совпасть счётом могли лишь фрагменты
    с одного места, чего не бывает, — то есть надбавка не срабатывала никогда.
    Со второй выдачей это обычное дело: RRF складывает места, и суммы
    совпадают часто. 19.09.2026 тир впервые начал работать.
    """
    return sorted(
        scores,
        key=lambda chunk_id: (
            -(scores[chunk_id] + TIER_BONUS * TIER_ORDER.get(tiers.get(chunk_id, ""), 0)),
            chunk_id,
        ),
    )


def _chunk(row: sqlite3.Row, score: float, coverage: float, by_meaning: bool = False) -> Chunk:
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
        by_meaning=by_meaning,
    )


async def search(query: str, k: int = 0) -> list[Chunk]:
    """Фрагменты по вопросу, от лучшего к худшему. Пусто — значит не нашли.

    Вектор вопроса берётся ДО ухода в поток: это сетевой запрос, и держать на
    нём поток из пула незачем. `None` от провайдера ничего не ломает — поиск
    остаётся лексическим, каким был до 19.09.2026.

    `_dense_off` в условии — не оптимизация, а деньги и секунды. Флаг ставится
    один раз, когда выяснилось, что плотного поиска в этой базе нет: векторов
    не положили, или её посчитали другой моделью — то есть модель сменили, а
    пересобрать забыли. Без него каждый следующий вопрос заказывал бы вектор,
    которого некуда приложить, и так до пересборки. Замерено: с флагом первый
    вопрос стоит одного запроса и 1.7 с, следующие — нуля запросов и 0.01 с.

    На опечатку в имени модели флаг не срабатывает, и это правильно: там
    проваливается сам запрос, вектора нет вовсе, и до сверки с базой дело не
    доходит. Стоит такая опечатка одного отказа провайдера на вопрос — она
    громко пишется в лог и не стоит денег, потому что 4xx не тарифицируется.

    Само чтение уходит в поток по тому же правилу, что графики и PDF: event
    loop обслуживает панель и таймеры, и блокировать его нельзя. Умножение
    матрицы 2104×1024 на вектор считается там же и стоит микросекунды.
    """
    wanted = embed.available() and not _dense_off
    dense_query = await embed.vector(query) if wanted else None
    return await asyncio.to_thread(_search, query, k or settings.kb_top_k, dense_query)
