"""Словарь синонимов: содержание файла и его действие на поиск.

Половина тестов здесь — про сам файл, а не про код. `synonyms.yaml` правит
человек без программирования, и ошибка в нём не видна ничем: группа с одним
написанием или с многозначным словом выглядит в тексте так же аккуратно, как
правильная, а симптомом будет либо молчащий бот, либо ответ не по теме.
"""

import ast

import pytest
import yaml

from bot.services import kb as search
from bot.services import morph, synonyms
from tests.test_kb import _big_kb, _heading, _paragraph  # noqa: F401


def _raw() -> dict:
    return yaml.safe_load(synonyms.PATH.read_text(encoding="utf-8"))


# --- Файл как содержание -----------------------------------------------------


def test_every_group_says_why_it_exists():
    """Группа без объяснения — строка, смысл которой через год не восстановить.

    Словарь расширяет запрос, то есть смягчает сторож молчания; каждая такая
    поблажка обязана называть документ и слово, ради которых она стоит.
    """
    for number, group in enumerate(_raw()["groups"], start=1):
        assert str(group.get("why", "")).strip(), f"группа {number} без why"


def test_every_group_has_something_to_swap():
    """Группа из одного написания не делает ничего и вводит в заблуждение."""
    for number, group in enumerate(_raw()["groups"], start=1):
        assert len(group["phrases"]) >= 2, f"группа {number} короче двух написаний"


def test_the_file_is_the_only_place_with_pairs():
    """В Python нет ни одной пары синонимов — то же правило, что у redflags.

    Сканируем строковые литералы, а не файл целиком: докстрока объясняет
    устройство на примере «БЦЖ», и ловить её значило бы завести сторож,
    который держат выключенным списком исключений.
    """
    module = ast.parse(
        (synonyms.PATH.parent.parent / "services" / "synonyms.py").read_text(encoding="utf-8")
    )
    documentation = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(module)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
    }
    for node in ast.walk(module):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node.value in documentation:
            continue
        for word in ("бцж", "туберкул", "акдс", "лактац", "метеоризм"):
            assert word not in node.value.lower(), f"пара {word!r} уехала в код"


def test_a_fleeting_vowel_is_written_out_in_full():
    """Слово с беглой гласной обязано стоять обеими формами.

    Snowball даёт «грудничок» и «грудничка» разные основы, и ни одна не
    является началом другой. Одной формы не хватает, а заметить это глазами
    нельзя — в файле обе выглядят одинаково уместно.
    """
    for word in ("грудничка", "младенца", "какашки", "какашек"):
        assert synonyms.alternatives(morph.stems(word), morph.stems(word)[0])


# Слова семьи, чья основа совпадает с основой чужого слова, и потому в словарь
# не берутся. Второе в паре — форма, которой это чужое слово реально написано в
# документах: считать надо по корпусу, а не по словарной форме. «Сосок» даёт
# основу `сосок` и ни с чем не сталкивается, а вот «сосков», «соски», «сосках»
# — все дают `соск`, и в корпусе стоят именно они (97, 66 и 11 раз)
COLLIDING = (
    ("какать", "какао"),
    ("какает", "какао"),
    ("соска", "сосков"),
    ("соски", "сосках"),
)


@pytest.mark.parametrize("family, stranger", COLLIDING)
def test_an_ambiguous_word_never_enters_the_dictionary(family, stranger):
    """Отказ записан сторожем, иначе через полгода его отменят как забывчивость.

    Многозначность здесь не в словаре, а в стеммере, и считается она по формам,
    которыми написаны документы, — не по словарной форме. «Сосок» даёт основу
    `сосок` и ни с чем не сталкивается; но корпус пишет «сосков» (97 раз),
    «соски» (66) и «сосках» (11), а все они дают `соск` — ровно основу
    «соски»-пустышки. Для поиска это одно слово. «Какать» даёт `кака`, и её же
    даёт «какао» из глав про лечение рахита и фруктоземию.

    Обе пары выглядят в файле уместно: «соска» и «пустышка» — очевидные
    синонимы, «какать» — очевидная форма к «покакать». Поймать это можно только
    прогоном, поэтому проверка здесь, а не во внимательности.

    Первое утверждение теста важнее второго: если стеммер или корпус изменятся
    и пара перестанет сталкиваться, запрет надо пересмотреть, а не донашивать
    молча.
    """
    assert morph.stems(family)[0] == morph.stems(stranger)[0], (
        "основы разошлись — запрет пора пересмотреть, а не оставлять молча"
    )
    words = {
        word.lower()
        for group in _raw()["groups"]
        for phrase in group["phrases"]
        for word in str(phrase).split()
    }
    assert family not in words, f"{family!r} тянет за собой {stranger!r}"


# --- Действие на отбор -------------------------------------------------------


def test_a_multiword_phrase_needs_all_its_words():
    """«Молоко» само по себе не должно тянуть за собой «достаточность лактации».

    Иначе словарь подставлял бы лактацию в любой вопрос про молоко, а это
    ровно то смягчение покрытия, из-за которого сторож молчания перестаёт
    работать.
    """
    assert synonyms.alternatives(["молок"], "молок") == ()
    assert synonyms.alternatives(morph.stems("хватает молока"), "молок")


def test_an_abbreviation_is_not_an_unknown_word(tmp_path, monkeypatch):
    """Аббревиатура, которую документ пишет словами, обязана считаться знакомой.

    Это не удобство, а условие работоспособности сторожа молчания: он считает
    незнакомые слова доводом против релевантности, и «БЦЖ» без словаря
    утопила бы собственный вопрос.
    """
    _big_kb(
        tmp_path,
        monkeypatch,
        [
            block
            for number in range(4)
            for block in (
                _heading(f"Вакцинация — часть {number}"),
                _paragraph("Вакцинация против туберкулеза проводится в роддоме. " * 12),
            )
        ],
    )
    connection = search._open()
    assert connection is not None
    _, unknown, _ = search._significant(connection, morph.stems("бцж"))
    assert unknown == []


@pytest.mark.asyncio
async def test_the_document_is_found_by_the_family_word(tmp_path, monkeypatch):
    """Сквозная проверка: вопрос словом семьи находит текст словом документа."""
    _big_kb(
        tmp_path,
        monkeypatch,
        [
            block
            for number in range(4)
            for block in (
                _heading(f"Вакцинация — часть {number}"),
                _paragraph("Вакцинация против туберкулеза проводится в роддоме. " * 12),
            )
        ],
    )
    assert await search.search("когда делают бцж")


@pytest.mark.asyncio
async def test_the_dictionary_does_not_raise_an_off_topic_question(tmp_path, monkeypatch):
    """Расширение смягчает покрытие — но не настолько, чтобы поднять чужое.

    Вторая половина словаря: без неё его можно было бы наполнять чем угодно.
    """
    _big_kb(
        tmp_path,
        monkeypatch,
        [
            block
            for number in range(4)
            for block in (
                _heading(f"Питание — часть {number}"),
                _paragraph("Масло растительное добавляют в прикорм ребенка. " * 12),
            )
        ],
    )
    assert await search.search("как поменять масло в машине") == []
