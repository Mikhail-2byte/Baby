"""Словарь синонимов для поиска по базе знаний.

Документ и семья говорят разными словами, и у пары часто нет ни одной общей
основы: документ пишет «вакцинация против туберкулёза», человек спрашивает
«БЦЖ». Стемминг тут не помогает — он приводит к одной основе словоформы
одного слова, а не разные слова.

Всё содержание лежит в `bot/content/synonyms.yaml`, в Python нет ни одной
пары. Файл должен читаться человеком без программирования — то же правило,
что у `redflags.yaml` и `weeks.yaml`.

**Расширяется запрос, а не индекс.** Расширенный индекс размножил бы мусор и
сломал таблицу `df`, на которой стоит вся значимость слов; и правка словаря
требовала бы пересборки базы.
"""

import logging
from pathlib import Path

import yaml

from bot.services import morph

log = logging.getLogger(__name__)

PATH = Path(__file__).resolve().parent.parent / "content" / "synonyms.yaml"

# Фраза — кортеж основ; все они обязаны найтись во фрагменте, чтобы фраза
# считалась встреченной. Группа — кортеж таких фраз
Phrase = tuple[str, ...]
Group = tuple[Phrase, ...]


def _load() -> tuple[Group, ...]:
    """Прочитать словарь. Падаем громко: молча потерянный словарь означает
    вернувшийся сторож, глушащий настоящие вопросы, а симптомом будет тишина.
    """
    raw = yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}
    groups: list[Group] = []
    for number, group in enumerate(raw.get("groups", []), start=1):
        phrases = group.get("phrases") or []
        if len(phrases) < 2:
            raise ValueError(f"synonyms.yaml: группа {number} короче двух написаний")
        if not str(group.get("why", "")).strip():
            raise ValueError(f"synonyms.yaml: у группы {number} не сказано why")
        parsed = tuple(dict.fromkeys(tuple(morph.stems(phrase)) for phrase in phrases))
        if any(not phrase for phrase in parsed):
            raise ValueError(f"synonyms.yaml: в группе {number} пустое написание")
        groups.append(parsed)
    return tuple(groups)


GROUPS = _load()

def _same(one: str, other: str) -> bool:
    """Одна ли это основа с точностью до беглой гласной.

    Сравниваем по началу в обе стороны, как это делает весь остальной поиск.
    Snowball оставляет у слов одного гнезда разные основы — «грудничка» даёт
    `грудничк`, а «грудничок» уже `грудничок`, — и сравнение на равенство
    промахивалось бы мимо словаря ровно на тех словах, ради которых он заведён.
    """
    return one.startswith(other) or other.startswith(one)


def _matches(phrase: Phrase, present: set[str]) -> bool:
    """Все ли слова написания есть в вопросе."""
    return all(any(_same(word, stem) for stem in present) for word in phrase)


def alternatives(query_stems: list[str], stem: str) -> tuple[Phrase, ...]:
    """Чем ещё может называться `stem` в документе.

    Однословное написание срабатывает по самой основе. Многословное — только
    если в вопросе есть все его слова: расширять «молоко» до «достаточности
    лактации» по одному слову значило бы подставлять её в любой вопрос про
    молоко. Возвращаются все написания группы, включая исходное.
    """
    found: list[Phrase] = []
    present = set(query_stems)
    for group in GROUPS:
        touched = any(
            _same(phrase[0], stem) if len(phrase) == 1 else (
                any(_same(word, stem) for word in phrase) and _matches(phrase, present)
            )
            for phrase in group
        )
        if touched:
            found.extend(group)
    return tuple(dict.fromkeys(found))
