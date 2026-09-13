"""Поиск по базе знаний: морфология, нарезка, отбор, слияние.

База в этих тестах собирается своя, маленькая, прямо из блоков — настоящие
PDF сюда не тянутся. Проверяется при этом боевой `services/kb.search`, а не
его упрощённая копия: копия расходится с оригиналом молча.
"""


import sqlite3

import pytest

from bot.config import settings
from bot.services import kb as search
from bot.services import morph
from kb import build_kb


@pytest.fixture(autouse=True)
def _closed():
    """Соединение с базой знаний кешируется в модуле и течёт между тестами."""
    search.close()
    yield
    search.close()


def _source(source_id: str = "doc", tier: str = "T1") -> build_kb.Source:
    return build_kb.Source(
        id=source_id,
        tier=tier,
        title="Пробный документ",
        publisher="Проверка",
        year=2026,
        url="https://example.invalid/doc",
        fetch="",
        license="проверка",
        manual=False,
        note="",
    )


def _tiny_kb(tmp_path, monkeypatch, blocks, source_id: str = "doc", tier: str = "T1") -> None:
    """Собрать базу знаний из блоков и подсунуть её боту."""
    source = _source(source_id, tier)
    chunks = build_kb.chunk_blocks(source, blocks)
    path = tmp_path / "kb.db"
    if path.exists():
        # Второй источник в ту же базу: дописываем, а не пересобираем
        existing = _read_back(path)
        build_kb.write_db(path, existing + [(source, chunks)])
    else:
        build_kb.write_db(path, [(source, chunks)])
    monkeypatch.setattr(settings, "kb_path", path)
    search.close()


def _read_back(path) -> list[tuple[build_kb.Source, list[build_kb.Chunk]]]:
    connection = sqlite3.connect(path)
    out = []
    for row in connection.execute("SELECT id, tier FROM sources").fetchall():
        source = _source(row[0], row[1])
        chunks = [
            build_kb.Chunk(row[0], heading, text, page)
            for heading, text, page in connection.execute(
                "SELECT heading, text, page FROM chunks WHERE source_id = ?", (row[0],)
            )
        ]
        out.append((source, chunks))
    connection.close()
    return out


def _paragraph(text: str, page: int = 1) -> build_kb.Block:
    return build_kb.Block("p", 0, text, page)


def _heading(text: str, level: int = 1, page: int = 1) -> build_kb.Block:
    return build_kb.Block("h", level, text, page)


# --- Морфология -------------------------------------------------------------


@pytest.mark.parametrize(
    "words",
    [
        ("кормление", "кормления", "кормлению", "кормлений"),
        ("желтуха", "желтухи", "желтухой"),
        ("новорожденный", "новорожденного", "новорожденным"),
        ("вскармливание", "вскармливания"),
    ],
)
def test_inflections_share_a_stem(words):
    """Склонение не должно мешать поиску: это половина русского текста."""
    assert len({morph.stems(word)[0] for word in words}) == 1


def test_yo_is_normalized_like_in_triage():
    """Нормализация одна на проект. Две разошлись бы, и молча."""
    assert morph.stems("ребёнок") == morph.stems("ребенок")


def test_short_words_are_dropped():
    assert morph.stems("я и в с ок") == morph.stems("ок")


def test_match_query_is_an_or_of_prefixes():
    """`OR`, а не пробел: пробел в FTS5 значит `AND`.

    И звёздочка: Snowball оставляет «невролог» и «неврологическ» разными
    основами, и без поиска по началу документ по вопросу не находится.
    """
    expression = morph.match_query("желтуха прикорм")
    assert " OR " in expression
    assert expression.count("*") == 2


def test_match_query_of_nothing_is_empty():
    assert morph.match_query("!!! ??") == ""


# --- Нарезка ----------------------------------------------------------------


def test_chunk_keeps_the_heading_path():
    blocks = [
        _heading("Глава 1. Вскармливание"),
        _heading("1.1 Прикорм", level=2),
        _paragraph("Текст про сроки. " * 20),
    ]
    chunks = build_kb.chunk_blocks(_source(), blocks)
    assert chunks[0].heading == f"Глава 1. Вскармливание{build_kb.CRUMB}1.1 Прикорм"


def test_chunk_does_not_cross_a_heading():
    """Склеенные куски двух разделов отвечают словами из соседнего."""
    blocks = [
        _heading("Раздел А"),
        _paragraph("Первый раздел про кормление. " * 10),
        _heading("Раздел Б"),
        _paragraph("Второй раздел про прогулки. " * 10),
    ]
    chunks = build_kb.chunk_blocks(_source(), blocks)
    assert len(chunks) == 2
    assert "прогулки" not in chunks[0].text


def test_long_paragraph_is_split_by_sentences():
    sentence = "Это предложение ровно про одно. "
    chunks = build_kb.chunk_blocks(_source(), [_paragraph(sentence * 100)])
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= build_kb.MAX_CHUNK
        # Ни один чанк не обрывается посреди слова
        assert chunk.text.strip().endswith(".")


def test_indexed_text_carries_the_document_title():
    """Contextual header prepending.

    Фрагмент «не ранее 6 месяцев» сам по себе не находится ни по «прикорм»,
    ни по «вскармливание»: этих слов в нём нет.
    """
    source = _source()
    chunk = build_kb.Chunk("doc", "Прикорм", "Не ранее 6 месяцев.", 1)
    text = build_kb.indexed(source, chunk)
    assert source.title in text
    assert "Прикорм" in text


# --- Склейка переносов ------------------------------------------------------


def test_wrapped_line_is_joined():
    blocks = build_kb._join_wraps(
        [_paragraph("Самостоятельное дыхание с постоянным давлением в"), _paragraph("конце выдоха")]
    )
    assert len(blocks) == 1
    assert blocks[0].text.endswith("давлением в конце выдоха")


def test_numbered_heading_is_not_swallowed():
    """Регрессия, стоившая всей структуры документа разом.

    Продолжение перенесённой строки опознаётся по строчной букве в начале.
    Стоило добавить в этот список цифры — и каждый нумерованный заголовок
    («1. Краткая информация») стал продолжением предыдущего абзаца, а вместе
    с ним исчезло и всё дерево разделов.
    """
    blocks = build_kb._join_wraps(
        [_paragraph("Конец предыдущего абзаца без точки"), _heading("1. Краткая информация")]
    )
    assert len(blocks) == 2
    assert blocks[1].kind == "h"


def test_abbreviation_list_is_not_joined():
    """«АД – артериальное давление» и «АЛТ – …» — разные строки, а не одна."""
    blocks = build_kb._join_wraps(
        [_paragraph("АД – артериальное давление"), _paragraph("АЛТ – аланинаминотрансфераза")]
    )
    assert len(blocks) == 2


# --- Поиск ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declension_finds_the_chunk(tmp_path, monkeypatch):
    """Человек спрашивает одной формой, документ написан другой."""
    _tiny_kb(
        tmp_path,
        monkeypatch,
        [
            _heading("Кормление"),
            _paragraph("Грудное кормление новорожденного ребенка. " * 12),
            _heading("Прогулки"),
            _paragraph("Прогулка на свежем воздухе полезна ребенку. " * 12),
        ],
    )
    found = await search.search("кормления новорожденных", 3)
    assert found
    assert "Кормление" in found[0].heading


@pytest.mark.asyncio
async def test_prefix_bridges_a_different_suffix(tmp_path, monkeypatch):
    """«невролог» и «неврологических» — разные основы у Snowball."""
    _tiny_kb(
        tmp_path,
        monkeypatch,
        [
            _heading("Осмотры"),
            _paragraph("Проводятся неврологические обследования младенца в поликлинике. " * 10),
        ],
    )
    assert await search.search("невролог младенца", 3)


@pytest.mark.asyncio
async def test_heading_words_are_searchable(tmp_path, monkeypatch):
    """Шапка уходит в индекс, поэтому слово из заголовка находит фрагмент."""
    _tiny_kb(
        tmp_path,
        monkeypatch,
        [
            _heading("Фототерапия при желтухе"),
            _paragraph("Процедуру назначают по уровню билирубина у младенца. " * 10),
        ],
    )
    found = await search.search("фототерапия билирубин", 3)
    assert found


@pytest.mark.asyncio
async def test_off_topic_question_finds_nothing(tmp_path, monkeypatch):
    """«Не нашёл» — законный ответ. Пересказ случайного абзаца — нет."""
    _tiny_kb(
        tmp_path,
        monkeypatch,
        [_heading("Кормление"), _paragraph("Грудное кормление новорожденного. " * 15)],
    )
    assert await search.search("сколько стоит биткоин", 3) == []


@pytest.mark.asyncio
async def test_fts_syntax_in_the_question_does_not_break_search(tmp_path, monkeypatch):
    """Кавычка, звёздочка и `AND` — это синтаксис FTS5, а не слова.

    Выражение собирается из своих токенов именно поэтому: подставленный текст
    человека уронил бы запрос на `sqlite3.OperationalError`.
    """
    _tiny_kb(
        tmp_path,
        monkeypatch,
        [_heading("Пустышка"), _paragraph("Пустышка и сосание у младенца. " * 15)],
    )
    assert await search.search('а "пустышка" AND это * NEAR нормально?', 3) is not None


@pytest.mark.asyncio
async def test_no_base_is_not_an_error(tmp_path, monkeypatch):
    """Бот без собранной базы работает, просто не отвечает на вопросы."""
    monkeypatch.setattr(settings, "kb_path", tmp_path / "nope.db")
    search.close()
    assert search.available() is False
    assert await search.search("желтуха", 3) == []


@pytest.mark.asyncio
async def test_foreign_schema_counts_as_no_base(tmp_path, monkeypatch):
    """База от прошлой версии сборщика — это отсутствие базы, а не выдача."""
    _tiny_kb(tmp_path, monkeypatch, [_heading("А"), _paragraph("Текст про младенца. " * 20)])
    connection = sqlite3.connect(tmp_path / "kb.db")
    connection.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()
    search.close()
    assert search.available() is False


@pytest.mark.asyncio
async def test_answer_carries_its_source(tmp_path, monkeypatch):
    """Ответ без источника показывать нельзя, поэтому источник едет с чанком."""
    _tiny_kb(
        tmp_path,
        monkeypatch,
        [_heading("Желтуха"), _paragraph("Желтуха новорожденных и билирубин. " * 12)],
    )
    chunk = (await search.search("желтуха билирубин", 1))[0]
    assert chunk.title and chunk.publisher and chunk.url
    assert chunk.tier in search.TIER_ORDER


# --- Слияние выдач ----------------------------------------------------------


def test_rrf_prefers_what_both_rankings_found():
    """Смысл RRF: обе выдачи ошибаются, согласие двух весомее первого места."""
    scores = search._rrf([[1, 2], [2, 3]])
    assert max(scores, key=lambda key: scores[key]) == 2


def test_rrf_of_one_ranking_keeps_its_order():
    scores = search._rrf([[7, 8, 9]])
    assert sorted(scores, key=lambda key: -scores[key]) == [7, 8, 9]


def test_tier_bonus_cannot_jump_a_place():
    """Надбавка за тир разводит почти равных, а не переставляет выдачу.

    Соседние места в RRF отличаются примерно на 2.6e-4. Надбавка обязана быть
    меньше этого зазора, иначе клинические рекомендации начнут всплывать над
    фрагментом, который действительно отвечает на вопрос.
    """
    gap = 1 / (search.RRF_K + 1) - 1 / (search.RRF_K + 2)
    assert search.TIER_BONUS * max(search.TIER_ORDER.values()) < gap


def test_tier_breaks_an_exact_tie():
    """При совпавшем счёте первым идёт Минздрав, а не зарубежное общество."""
    assert search._order({1: 0.5, 2: 0.5}, {1: "T3", 2: "T1"}) == [2, 1]


def test_tier_does_not_reorder_different_scores():
    """Тир разводит равных. Обогнать фрагмент, который лучше отвечает, — нет.

    Разница в одно место RRF заведомо больше всей надбавки, и это проверяется
    отдельно в `test_tier_bonus_cannot_jump_a_place`.
    """
    scores = {1: 1 / (search.RRF_K + 1), 2: 1 / (search.RRF_K + 2)}
    assert search._order(scores, {1: "T4", 2: "T1"}) == [1, 2]


# --- Отбор ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_question_words_do_not_count_as_matches(tmp_path, monkeypatch):
    """«Сколько» есть в двух фрагментах из восьмисот, то есть выглядит редким.

    Частотный отсев ловит служебные слова по документам, а вопросительных
    слов в клинических рекомендациях нет: они отвечают, а не спрашивают.
    Поэтому список вопросительных — единственный, который ведётся руками.
    """
    _tiny_kb(
        tmp_path,
        monkeypatch,
        [
            _heading("Прикорм"),
            _paragraph("Сколько давать прикорма, зависит от возраста ребенка. " * 10),
        ],
    )
    assert await search.search("сколько зачем как что", 3) == []


def test_question_words_reach_the_filter_as_stems():
    """Список ведётся словами, а сравнивается с основами.

    Написанные руками основы разошлись бы с тем, что выдаёт Snowball, и молча:
    «зачем» превращается в `зач`, «почему» — в `поч`, и ни одно из этих слов
    в списке из словоформ не сработало бы.
    """
    for word in morph.QUESTION_WORDS:
        assert morph.stems(word)[0] in morph.QUESTION_STEMS, word


@pytest.mark.asyncio
async def test_coverage_threshold_rejects_a_single_word_hit(tmp_path, monkeypatch):
    """Совпадение по одному слову из четырёх — это не ответ на вопрос.

    Слова берутся такие, что все они в базе есть, просто разбросаны по разным
    разделам: иначе проверялось бы не покрытие, а отсев незнакомых слов.
    """
    _tiny_kb(
        tmp_path,
        monkeypatch,
        [
            _heading("Прогулки"),
            _paragraph("Прогулка с младенцем на свежем воздухе. " * 15),
            _heading("Желтуха"),
            _paragraph("Билирубин, фототерапия и желтуха новорожденных. " * 15),
        ],
    )
    found = await search.search("прогулка билирубин фототерапия желтуха", 3)
    assert [chunk.heading for chunk in found] == ["Желтуха"]


@pytest.mark.asyncio
async def test_search_runs_off_the_event_loop(tmp_path, monkeypatch):
    """Инвариант проекта: тяжёлая работа не в event loop.

    Сейчас чтение быстрое, и соблазн звать `_search` напрямую велик. Но сюда
    же встанет прогон модели эмбеддингов, а блокировка цикла даёт ровно тот
    симптом, от которого заведён heartbeat: процесс жив, логов нет, бот молчит.

    Проверяем фактом, а не чтением исходника: подменяем `to_thread` и смотрим,
    прошёл ли поиск через него.
    """
    _tiny_kb(tmp_path, monkeypatch, [_heading("Сон"), _paragraph("Сон младенца ночью. " * 15)])

    calls = []
    original = search.asyncio.to_thread

    async def counted(function, *args):
        calls.append(function)
        return await original(function, *args)

    monkeypatch.setattr(search.asyncio, "to_thread", counted)
    await search.search("сон младенца", 2)
    assert calls == [search._search]


# --- Файл базы: кто его держит ----------------------------------------------


@pytest.mark.asyncio
async def test_an_open_base_cannot_be_replaced(tmp_path, monkeypatch):
    """Обещание README проверяется фактом, а не рассуждением.

    «Пересобирать при остановленном боте» — это не совет, а свойство Windows:
    открытый чужим процессом файл не подменить. Если однажды окажется, что
    подмена проходит, документацию надо будет править, а не гадать.
    """
    _tiny_kb(tmp_path, monkeypatch, [_heading("Сон"), _paragraph("Сон младенца ночью. " * 15)])
    assert await search.search("сон младенца", 1), "соединение должно открыться"

    path = tmp_path / "kb.db"
    fresh = tmp_path / "fresh.db"
    fresh.write_bytes(path.read_bytes())
    with pytest.raises(PermissionError):
        path.unlink()
        fresh.replace(path)


@pytest.mark.asyncio
async def test_close_releases_the_file(tmp_path, monkeypatch):
    """`close()` не украшение: соединение живёт весь процесс.

    До аудита эту функцию не звал никто — ровно тот случай, из-за которого
    когда-то год не работали PRAGMA. Теперь её зовёт порядок остановки бота,
    а проверяем мы то, ради чего она есть.
    """
    _tiny_kb(tmp_path, monkeypatch, [_heading("Сон"), _paragraph("Сон младенца ночью. " * 15)])
    await search.search("сон младенца", 1)

    search.close()

    path = tmp_path / "kb.db"
    fresh = tmp_path / "fresh.db"
    fresh.write_bytes(path.read_bytes())
    path.unlink()
    fresh.replace(path)  # не должно поднять PermissionError


def test_build_says_what_to_do_when_the_file_is_busy(tmp_path, monkeypatch):
    """Сборка при работающем боте обязана объяснить, а не сыпать трассировкой.

    Человек в этот момент не отлаживает скрипт, он пересобирает базу.
    """
    source = _source()
    chunks = build_kb.chunk_blocks(source, [_heading("А"), _paragraph("Текст про сон. " * 20)])
    path = tmp_path / "kb.db"
    build_kb.write_db(path, [(source, chunks)])

    holder = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with pytest.raises(SystemExit, match="Остановите бота"):
            build_kb.write_db(path, [(source, chunks)])
    finally:
        holder.close()
