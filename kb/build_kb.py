"""Сборка базы знаний: документы -> `data/kb.db`.

Запускать из корня проекта:

    .venv\\Scripts\\python kb\\build_kb.py

Четыре шага, и каждый оставляет после себя то, что можно посмотреть глазами:

1. **Скачать** по `kb/sources.yaml` в `kb/raw/docs/`. Повторный запуск сети не
   требует, `--offline` запрещает её вовсе — как в `load_who.py`.
2. **Нормализовать** PDF в markdown в `kb/normalized/`. Этот шаг существует не
   ради удобства следующего, а ради проверки: единственный способ убедиться,
   что бот читает то же, что написано в документе, — открыть оба и сравнить.
   На этапе 5 у скачанного PDF оказалась сбита таблица шрифта, и текст читался
   со сдвигом на 29 позиций; такое не ловится ничем, кроме чтения.
3. **Нарезать** по заголовкам. В индекс уходит не голый чанк, а чанк с
   названием документа и путём заголовков впереди (contextual header
   prepending): фрагмент «не ранее 6 месяцев» сам по себе не отвечает ни на
   один вопрос, а с шапкой «Программа оптимизации вскармливания. Прикорм.
   Сроки введения» — отвечает.
4. **Записать** `data/kb.db` голым `sqlite3`: схемы в `models.py` нет и не
   будет. База знаний — артефакт сборки, а не данные семьи. Её не жалко
   потерять, она не участвует в миграциях и не попадает в бэкапы.

Пишем во временный файл и подменяем готовый одним движением: оборванная на
середине сборка не должна оставлять боту полупустую базу.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pymupdf
import yaml

# Скрипт лежит в `kb/`, а импортирует `bot.*` — добавляем корень в путь, чтобы
# его можно было запускать и как `python kb/build_kb.py`, и из любого каталога
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import settings  # noqa: E402
from bot.services import embed, morph  # noqa: E402

MANIFEST = ROOT / "kb" / "sources.yaml"
RAW = ROOT / "kb" / "raw" / "docs"
NORMALIZED = ROOT / "kb" / "normalized"

# Версия схемы `kb.db`. Читается `services/kb.py` на открытии: база, собранная
# прошлой версией скрипта, должна честно считаться отсутствующей, а не
# отвечать на запросы по несуществующим колонкам
SCHEMA_VERSION = "2"

# Границы чанка в знаках. Верхняя — чтобы фрагмент влезал и в сообщение, и в
# окно будущей модели эмбеддингов; нижняя — чтобы «Приложение Б» из двух строк
# не занимало место в выдаче наравне с разделом
MIN_CHUNK = 400
MAX_CHUNK = 1200

# Разделитель пути заголовков. Внутри БД и markdown; в консоль не печатается —
# в cp1251 этого знака нет
CRUMB = " › "

TIER_KEYS = ("T1", "T2", "T3", "T4")


# --- Манифест ---------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    id: str
    tier: str
    title: str
    publisher: str
    year: int
    url: str
    fetch: str
    license: str
    manual: bool
    note: str

    @property
    def path(self) -> Path:
        return RAW / f"{self.id}.pdf"


def _load() -> tuple[dict[str, str], tuple[Source, ...]]:
    """Прочитать и проверить `sources.yaml`.

    Проверяем придирчиво и падаем громко — по тому же правилу, что в
    `redflags._load`: собранная наполовину база хуже отсутствующей, потому что
    выглядит рабочей.
    """
    raw: Any = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{MANIFEST}: ожидался словарь")

    tiers = raw.get("tiers") or {}
    if set(tiers) != set(TIER_KEYS):
        raise ValueError(f"{MANIFEST}: тиры должны быть ровно {TIER_KEYS}, а не {sorted(tiers)}")

    entries = raw.get("sources") or []
    if not entries:
        raise ValueError(f"{MANIFEST}: ни одного источника")

    sources: list[Source] = []
    seen: set[str] = set()
    for entry in entries:
        for field in ("id", "tier", "title", "publisher", "year", "url", "license"):
            if not entry.get(field):
                raise ValueError(f"{MANIFEST}: у источника {entry.get('id')} пусто поле {field}")
        if entry["tier"] not in tiers:
            raise ValueError(f"{MANIFEST}: неизвестный тир {entry['tier']} у {entry['id']}")
        if entry["id"] in seen:
            raise ValueError(f"{MANIFEST}: повторяющийся id {entry['id']}")
        seen.add(entry["id"])
        manual = bool(entry.get("manual"))
        if not manual and not entry.get("fetch"):
            raise ValueError(f"{MANIFEST}: у {entry['id']} нет ни fetch, ни manual")
        sources.append(
            Source(
                id=entry["id"],
                tier=entry["tier"],
                title=" ".join(str(entry["title"]).split()),
                publisher=" ".join(str(entry["publisher"]).split()),
                year=int(entry["year"]),
                url=entry["url"],
                fetch=entry.get("fetch", ""),
                license=entry["license"],
                manual=manual,
                note=" ".join(str(entry.get("note", "")).split()),
            )
        )
    return dict(tiers), tuple(sources)


TIERS, SOURCES = _load()


# --- Шаг 1: скачать ---------------------------------------------------------


def fetch(source: Source, *, offline: bool) -> Path | None:
    """Файл источника на диске, или `None`, если его нет и взять неоткуда."""
    dest = source.path
    if dest.exists():
        return dest
    if source.manual:
        print(f"  {source.id}: файл кладётся руками. Скачайте {source.url}")
        print(f"     и сохраните в {dest}")
        return None
    if offline:
        print(f"  {source.id}: нет файла {dest}, а скачивать запрещено (--offline)")
        return None

    RAW.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(source.fetch, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print(f"  {source.id}: не скачалось ({error}).")
        print(f"     Скачайте {source.fetch} вручную в {dest} и запустите снова.")
        return None
    if not body.startswith(b"%PDF"):
        print(f"  {source.id}: по ссылке не PDF. Проверьте fetch в sources.yaml")
        return None
    dest.write_bytes(body)
    return dest


# --- Шаг 2: нормализовать ---------------------------------------------------


@dataclass(frozen=True)
class Block:
    kind: str  # "h" — заголовок, "p" — абзац
    level: int
    text: str
    page: int


# Строка-заголовок по нумерации: «1.2 Этиология», «3. Лечение». Хвостовая
# пунктуация исключает нумерованный пункт приказа («1. Утвердить:»), который
# заголовком не является.
#
# Заглавная буква после номера обязательна, и это не косметика. Без неё
# заголовком становился обрывок «1 раза в сутки. Фототерапия кожи может быть
# прекращена…» — предложение, разорванное границей страницы посреди числа, — и
# уезжал шапкой в каждый чанк раздела
_NUMBERED = re.compile(r"^\d+(\.\d+)*\.?\s+[А-ЯЁA-Z]")
_NAMED = re.compile(
    r"^(приложение|раздел|глава|часть|список сокращений|термины и определения"
    r"|оглавление|содержание|ключевые слова|критерии оценки)\b",
    re.IGNORECASE,
)
# Служебные разделы документа — не его содержание. Список литературы, состав
# рабочей группы, методология разработки, благодарности: сотни фамилий и
# канцелярита, которые не отвечают ни на один родительский вопрос, зато
# находятся по любому. Проверено отложенным набором: на «сколько весит взрослый
# слон» бот выдавал текст лицензии ВОЗ.
#
# Приложения А1 и А2 — именно про это (рабочая группа и методология), а вот
# А3 «Справочные материалы» содержит дозировки, и его трогать нельзя
_SERVICE = re.compile(
    r"^(список литературы|литература|references"
    r"|выражение признательности|благодарности"
    r"|конфликт интересов|авторский коллектив"
    r"|методология разработки"
    r"|состав рабочей группы"
    r"|рабочая группа|экспертный совет|координаторы программы"
    r"|коллектив авторов|под редакцией|рецензент|составител"
    r"|приложение\s+а\s*[12][.\s)])",
    re.IGNORECASE,
)

# Разделы учебной методички — не содержание документа, а план занятия для
# того, кто его ведёт. Курс ВОЗ/ЮНИСЕФ 1993 года наполовину состоит из них:
# из 188 его фрагментов 105 это упражнения, ролевые задачи, демонстрации и
# ответы к письменным заданиям.
#
# Стоит это правило против случая, пойманного 22.09.2026 в боевом чате. На
# вопрос «нормально ли, что у ребёнка на третий день жизни стул жёлтый и в
# крапинку» бот ответил «да, считается нормой», опершись на единственный
# фрагмент — задачу из раздела «УПРАЖНЕНИЯ ПО ФОРМИРОВАНИЮ УВЕРЕННОСТИ»:
# «История Б. Ирина очень обеспокоена… Стул мягкий, желтовато-коричневый.
# а. Не надо так беспокоиться — у детей это вполне нормальное явление». Ребёнку
# в задаче несколько недель, речь о частоте, а не о виде, и сама фраза — вариант
# ответа из листка для тренера. Сторож чисел это пропустил (чисел в ответе нет),
# а семья поставила 👍. То есть ни один механизм проекта такую ошибку не ловит —
# кроме отсутствия методички в индексе.
#
# Правило опознаёт ГЛАГОЛЫ плана занятия, а не римскую нумерацию, которой эти
# заголовки набраны. Нумерация в сегодняшнем корпусе встречается только здесь,
# но «II. Диагностика» в будущих клинических рекомендациях — обычный раздел, и
# отсев по ней выбросил бы его молча
_TRAINING = re.compile(
    r"^(?:[IVX]+\.\s*)?("
    r"упражнени"
    r"|(практическое|клиническое|групповое)\s+занятие"
    r"|демонстрация"
    r"|оказание помощи (в выполнении|слушателям)"
    r"|объяснение, как"
    r"|чтение и обсуждение"
    r"|резюме темы"
    r")",
    re.IGNORECASE,
)

# Заголовок, в котором нет ни одного слова ни на одном из алфавитов, которыми
# набран индекс. Так выглядит подпись к диаграмме, набранная нестандартным
# шрифтом: PDF отдаёт её байтами cp1251, а читаются они как латиница-1, и
# «килограммы» приезжает как «êèëî ãðàì ì û». Заголовок печатается человеку над
# цитатой и уезжает в шапку чанка, то есть портит и вид, и поиск
_REAL_WORD = re.compile(r"[А-Яа-яЁё]{3,}|[A-Za-z]{3,}")

# Признаки записи из списка литературы. Ловим по тексту, а не по разделу:
# заголовок «Список литературы» в PDF нередко теряется вовсе, а сама запись
# узнаётся всегда — у неё есть либо двойной слэш, либо «et al.», либо том со
# страницами, либо год с точкой с запятой
_CITATION = re.compile(
    r"//|et al\.|\bdoi\b|\bvol\.|\bс\.\s*\d+[-–]|\b\d{4};\d|^©|\bP\.\s?\d",
    re.IGNORECASE,
)

# Строка из состава авторов: «6. Зубков Виктор Васильевич — доктор мед. наук».
# Отчество обязательно — без него под правило попал бы «2. Ведение беременных»
_PERSON = re.compile(
    r"^\d+\.\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]*?(ович|евич|овна|евна|ична)\b"
)

# Латинская запись без «//» и «et al.»: «23. Mak C.M., Lee H.H., Chan A.Y.»
# Одна такая пара бывает в клиническом тексте («шкала Апгар (Apgar V., 1953)»),
# две подряд — только в списке литературы
_LATIN_INITIALS = re.compile(r"[A-Z][a-z]+ [A-Z]\.[A-Z]?\.")

# Инициалы рядом с фамилией: «Никитюк Д. Б.». Одна такая пара в клиническом
# абзаце законна («шкала Апгар (Apgar V., 1953)»), а несколько подряд бывают
# только в библиографии и в составе рабочей группы
_INITIALS = re.compile(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]?\.")

# Выходные данные издания. Заголовка у них обычно нет вовсе — они стоят на
# обороте титула, — поэтому ловим по тексту, и только по тем меткам, которых в
# клиническом тексте не бывает ни при каких обстоятельствах
_IMPRINT = re.compile(r"(^|\s)(УДК|ББК|ISBN)\b|условиями лицензии|Все права защищены")

# Строка оглавления: у неё точечное заполнение до номера страницы. Точки
# бывают и подряд, и через пробел — PDF расставляет их как придётся
_TOC_LINE = re.compile(r"\.(\s?\.){3,}")

# Колонтитул-номер: «12», «Страница 3 из 12», «- 4 -»
_PAGE_NUMBER = re.compile(r"^[\s\-–—]*(страница\s+)?\d+(\s+из\s+\d+)?[\s\-–—]*$", re.IGNORECASE)

# Переносы и невидимые пробелы, которыми PDF засыпает текст
_SOFT = str.maketrans({" ": " ", " ": " ", " ": " ", "­": ""})
_HYPHEN_WRAP = re.compile(r"(\w)[-‐‑]\s+(?=[а-яa-z])")


def _clean(text: str) -> str:
    text = text.translate(_SOFT)
    text = _HYPHEN_WRAP.sub(r"\1", text)
    return " ".join(text.split())


@dataclass
class _Line:
    page: int
    text: str
    size: float
    bold: bool
    lines: int


def _raw_blocks(document: pymupdf.Document) -> list[_Line]:
    """Блоки PDF с их кеглем и жирностью, в порядке чтения."""
    out: list[_Line] = []
    for number in range(document.page_count):
        page = document[number]
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            pieces: list[str] = []
            size = 0.0
            bold = True
            for line in block["lines"]:
                spans = line.get("spans") or []
                if not spans:
                    continue
                pieces.append("".join(span["text"] for span in spans))
                size = max(size, max(float(span["size"]) for span in spans))
                bold = bold and all(_is_bold(span) for span in spans)
            text = _clean(" ".join(pieces))
            if text:
                out.append(_Line(number + 1, text, size, bold, len(pieces)))
    return out


def _is_bold(span: dict) -> bool:
    # У pymupdf жирность лежит и во флагах (бит 4), и в имени шрифта. Второе
    # надёжнее: встречаются шрифты, объявленные жирными только именем
    return bool(int(span.get("flags", 0)) & 16) or "bold" in str(span.get("font", "")).lower()


def _body_size(lines: list[_Line]) -> float:
    """Кегль основного текста — тот, которым набрано больше всего знаков."""
    weights: Counter[int] = Counter()
    for line in lines:
        weights[round(line.size)] += len(line.text)
    return float(weights.most_common(1)[0][0]) if weights else 10.0


def _toc_pages(lines: list[_Line]) -> set[int]:
    """Страницы оглавления — целиком.

    Строку оглавления видно по точечному заполнению, но её продолжение,
    переехавшее на следующую строку, точек уже не содержит: «1. Краткая
    информация по заболеванию или состоянию (группе заболеваний или» — и
    дальше отдельной строкой «состояний)». Выброшенные поштучно, такие обрубки
    остаются в тексте и, что хуже, попадают в заголовки. Поэтому выбрасываем
    страницу целиком: ничего, кроме оглавления, на ней не бывает.
    """
    counts: Counter[int] = Counter()
    for line in lines:
        if _TOC_LINE.search(line.text):
            counts[line.page] += 1
    return {page for page, count in counts.items() if count >= 3}


def _boilerplate(lines: list[_Line], pages: int) -> set[str]:
    """Строки, повторяющиеся из страницы в страницу, — колонтитулы.

    Порог по числу страниц, а не по числу повторов: фраза, встретившаяся
    четыре раза на одной странице, — это текст, а та же фраза на четырёх
    страницах подряд — шапка.
    """
    seen: dict[str, set[int]] = {}
    for line in lines:
        if len(line.text) > 120:
            continue
        seen.setdefault(line.text, set()).add(line.page)
    limit = max(3, int(pages * 0.4))
    return {text for text, where in seen.items() if len(where) >= limit}


def _is_heading(line: _Line, body: float) -> bool:
    if line.lines > 2 or len(line.text) > 140:
        return False
    # Заголовок из трёх букв не бывает, а вот обрывок подписи к таблице («%»,
    # «мл/кг») — сплошь и рядом, и он уезжает шапкой в каждый чанк раздела
    if sum(character.isalpha() for character in line.text) < 3:
        return False
    if not _REAL_WORD.search(line.text):
        return False
    # Запись списка литературы и строка состава авторов начинаются с номера и
    # заглавной буквы, то есть проходят под `_NUMBERED`. Отсюда та самая
    # поломка, что стоила базе сотни мусорных фрагментов: `classify` снимает
    # пропуск служебного раздела на каждом заголовке, и первая же запись
    # «2. Lunze K., … // BMC Pregnancy Childbirth. 2015» открывала библиотеку
    # обратно. В латинице `_NUMBERED` не сомневается — там `A-Z`
    if _CITATION.search(line.text) or _PERSON.match(line.text):
        return False
    if len(_LATIN_INITIALS.findall(line.text)) >= 2:
        return False
    # Строка оглавления с точечным заполнением. Целые страницы оглавления
    # выбрасываются раньше, но одиночная строка доживала до заголовка
    if _TOC_LINE.search(line.text):
        return False
    if _NAMED.match(line.text) or _SERVICE.match(line.text):
        return True
    # Заголовок мельче основного текста не бывает, а подпись к таблице —
    # сплошь и рядом, и она бывает жирной. Замерено: в «Программе оптимизации
    # вскармливания» основной текст 9.0, а «Возраст, мес» и «© University of
    # Oxford Villar et al.» — жирные 7.5 и 8.0
    if line.size < body:
        return False
    if line.size >= body * 1.12:
        return True
    tail = line.text[-1]
    if line.bold and tail not in ".;,":
        return True
    return bool(_NUMBERED.match(line.text)) and tail not in ".;:,"


def _level(text: str) -> int:
    match = _NUMBERED.match(text)
    if match:
        return text.split()[0].rstrip(".").count(".") + 1
    return 1


def read_pdf(path: Path) -> list[Block]:
    """PDF -> последовательность заголовков и абзацев.

    Три вещи выбрасываются здесь, а не позже: колонтитулы, оглавление и список
    литературы. Первое зашумляет каждый чанк, второе состоит из строк вида
    «Лечение ........ 19» и ищется по любому запросу, третье — сотни фамилий,
    которые сбивают частоты слов и ни на что не отвечают.
    """
    document = pymupdf.open(path)
    try:
        lines = _raw_blocks(document)
        pages = document.page_count
    finally:
        document.close()

    return classify(
        lines,
        body=_body_size(lines),
        junk=_boilerplate(lines, pages),
        contents=_toc_pages(lines),
    )


def classify(
    lines: list[_Line], *, body: float, junk: set[str], contents: set[int]
) -> list[Block]:
    """Строки PDF -> заголовки и абзацы, без мусора и служебных разделов.

    Вынесено из `read_pdf` отдельной функцией, чтобы проверялось без PDF:
    отсев служебных разделов — правило про содержание, и оно должно быть
    проверяемо само по себе, а не вместе с разбором шрифтов.
    """
    blocks: list[Block] = []
    skipping = False
    for line in lines:
        if line.page in contents or line.text in junk or _PAGE_NUMBER.match(line.text):
            continue
        heading = _is_heading(line, body)
        if skipping and not heading:
            continue
        if heading:
            # Два разных повода пропустить раздел, и в одно выражение они не
            # сливаются намеренно: служебный раздел — это обложка документа,
            # учебный — его половина, написанная не для читателя
            skipping = bool(_SERVICE.match(line.text) or _TRAINING.match(line.text))
            if skipping:
                continue
            if _NAMED.match(line.text) and not line.text.lower().startswith(
                ("приложение", "раздел", "глава", "часть")
            ):
                # «Оглавление», «Ключевые слова» — служебные разделы. Сам
                # заголовок пропускаем, содержимое пусть живёт без него
                continue
            blocks.append(Block("h", _level(line.text), line.text, line.page))
        else:
            blocks.append(Block("p", 0, line.text, line.page))
    return _join_wraps(blocks)


# Продолжение перенесённой строки: начинается со строчной буквы или закрывающей
# скобки. Заглавная буква в начале — почти всегда новая мысль, и список
# сокращений («АД – …», «АЛТ – …») склеивать нельзя.
#
# Цифры в этот список не входят, и это важно: с цифры начинается каждый
# нумерованный заголовок. Взяв их сюда, мы склеили «1. Краткая информация» с
# предыдущим абзацем и потеряли всю нумерованную структуру документа разом
_WRAP = re.compile(r"^[а-яa-z)\]]")

# Потолок на заголовок ПОСЛЕ склейки переносов. Сама склейка границ не знает:
# если абзац по ошибке принят за заголовок, к нему пристраивается вся его
# продолжающаяся часть, и в путь заголовков уезжает полстраницы текста —
# в выдаче это выглядит как шапка на шесть строк над цитатой в одну.
# Настоящие двухстрочные заголовки в этих документах доходят до двухсот знаков
# («Приложение А3. Справочные материалы, включая соответствие показаний…»),
# поэтому порог заметно выше, чем у `_is_heading`
MAX_HEADING = 260


def _join_wraps(blocks: list[Block]) -> list[Block]:
    """Склеить абзацы, разорванные переносом строки.

    В этих PDF почти каждая строка — отдельный блок, и без склейки чанк
    получается лесенкой из обрубков. Для поиска это ещё терпимо, а вот
    цитата, которую бот покажет человеку, обрывается на середине слова.
    """
    out: list[Block] = []
    for block in blocks:
        if not _WRAP.match(block.text):
            out.append(block)
            continue
        previous = out[-1] if out else None
        # Со строчной буквы не начинается ни один заголовок. Значит либо это
        # продолжение предыдущего блока, либо мы ошиблись, посчитав строку
        # заголовком, — и тогда это обычный абзац
        if previous is None or (previous.kind == "p" and previous.text[-1] in ".!?"):
            out.append(Block("p", 0, block.text, block.page))
            continue
        out.pop()
        out.append(
            Block(previous.kind, previous.level, f"{previous.text} {block.text}", previous.page)
        )
    # Разросшийся заголовок заголовком не был: разжалуем в абзац
    return [
        block if block.kind == "p" or len(block.text) <= MAX_HEADING
        else Block("p", 0, block.text, block.page)
        for block in out
    ]


def to_markdown(source: Source, blocks: list[Block]) -> str:
    """Нормализованный текст для чтения глазами."""
    out = [f"# {source.title}", "", f"{source.publisher}, {source.year}", f"<{source.url}>", ""]
    for block in blocks:
        if block.kind == "h":
            out += ["", "#" * min(block.level + 1, 6) + f" {block.text}", ""]
        else:
            out += [block.text, ""]
    return "\n".join(out) + "\n"


# --- Шаг 3: нарезать --------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    source_id: str
    heading: str
    text: str
    page: int


_SENTENCE = re.compile(r"(?<=[.!?;])\s+")


def _split_long(text: str) -> list[str]:
    """Абзац длиннее `MAX_CHUNK` — по предложениям, а не по знакам."""
    parts: list[str] = []
    current = ""
    for sentence in _SENTENCE.split(text):
        if current and len(current) + len(sentence) + 1 > MAX_CHUNK:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def chunk_blocks(source: Source, blocks: list[Block]) -> list[Chunk]:
    """Блоки -> чанки, по одному разделу за раз.

    Чанк не пересекает границу заголовка намеренно. Склеенные куски двух
    разделов отвечают на вопрос словами из соседнего — ровно та ошибка,
    которую потом нельзя объяснить, глядя на выдачу.
    """
    chunks: list[Chunk] = []
    crumbs: list[tuple[int, str]] = []
    buffer: list[str] = []
    length = 0
    page = 1

    def heading() -> str:
        return CRUMB.join(text for _, text in crumbs)

    def flush() -> None:
        nonlocal buffer, length
        if not buffer:
            return
        text = "\n".join(buffer)
        # Короткий хвост подклеиваем к предыдущему чанку того же раздела:
        # «Порядок такой.» отдельной записью в выдаче бесполезен
        if (
            len(text) < MIN_CHUNK
            and chunks
            and chunks[-1].heading == heading()
            and len(chunks[-1].text) + len(text) <= MAX_CHUNK + MIN_CHUNK
        ):
            previous = chunks.pop()
            text = f"{previous.text}\n{text}"
            chunks.append(Chunk(source.id, previous.heading, text, previous.page))
        else:
            chunks.append(Chunk(source.id, heading(), text, page))
        buffer, length = [], 0

    for block in blocks:
        if block.kind == "h":
            flush()
            while crumbs and crumbs[-1][0] >= block.level:
                crumbs.pop()
            crumbs.append((block.level, block.text))
            page = block.page
            continue
        for piece in _split_long(block.text) if len(block.text) > MAX_CHUNK else [block.text]:
            if length and length + len(piece) > MAX_CHUNK:
                flush()
            if not buffer:
                page = block.page
            buffer.append(piece)
            length += len(piece) + 1
    flush()
    return [
        chunk
        for chunk in chunks
        if len(chunk.text) >= MIN_CHUNK // 4
        and not _IMPRINT.search(chunk.text)
        and not _is_credits(chunk.text)
    ]


def _is_credits(text: str) -> bool:
    """Сплошные фамилии с инициалами — библиография или состав рабочей группы.

    Второй эшелон к отсеву по заголовку: в части документов служебный
    заголовок теряется при извлечении, и раздел доезжает до чанков без него.
    Судим по плотности, а не по факту: одна пара инициалов в клиническом
    абзаце законна — «шкала Апгар (Apgar V., 1953)», — а три на четыреста
    знаков бывают только в перечне людей.
    """
    return len(_INITIALS.findall(text)) > max(1, len(text) // MIN_CHUNK)


def indexed(source: Source, chunk: Chunk) -> str:
    """Текст, который уходит в индекс: чанк с шапкой из заголовков.

    Contextual header prepending. Без него фрагмент «не ранее 6 месяцев» не
    находится ни по «прикорм», ни по «вскармливание» — этих слов в нём нет.
    """
    return f"{source.title}. {chunk.heading}\n{chunk.text}"


# --- Шаг 4: записать --------------------------------------------------------

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE sources(
    id        TEXT PRIMARY KEY,
    tier      TEXT NOT NULL,
    tier_name TEXT NOT NULL,
    title     TEXT NOT NULL,
    publisher TEXT NOT NULL,
    year      INTEGER NOT NULL,
    url       TEXT NOT NULL,
    license   TEXT NOT NULL
);

CREATE TABLE chunks(
    id        INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    heading   TEXT NOT NULL,
    text      TEXT NOT NULL,
    stems     TEXT NOT NULL,
    page      INTEGER,
    chars     INTEGER NOT NULL
);
CREATE INDEX ix_chunks_source ON chunks(source_id);

-- content='chunks' — внешнее содержимое: FTS5 не хранит вторую копию текста,
-- а берёт колонку из `chunks` по rowid. База собирается одним заходом и после
-- этого только читается, так что рассинхронизации взяться неоткуда
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    stems, content='chunks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

-- Сколько фрагментов содержат основу. По этой таблице на запросе отбрасываются
-- служебные слова: «сколько» и «можно» есть почти везде и значат почти ничего
CREATE TABLE df(stem TEXT PRIMARY KEY, docs INTEGER NOT NULL);

-- Вектор смысла фрагмента: `float32`, уже нормированный. Нормировка стоит при
-- записи, а не на запросе, ровно затем, чтобы косинус в горячем пути был одним
-- скалярным произведением. Таблица отдельная, а не колонка в `chunks`: база
-- собирается и без ключа, и тогда её просто нет — а пустая колонка на каждой
-- строке выглядела бы как «векторы есть, но битые»
CREATE TABLE vectors(
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id),
    vector   BLOB NOT NULL);
"""


def embed_all(prepared: list[tuple[Source, list[Chunk]]]) -> list[list[float]] | None:
    """Векторы всех фрагментов в порядке обхода, или `None`, если не вышло.

    Векторизуется тот же текст, что уходит в `stems`, — с названием документа
    и путём заголовков. Contextual header prepending помогает смыслу ровно так
    же, как словам: фрагмент «не ранее 6 месяцев» без шапки не про прикорм не
    только по словам, но и по смыслу.

    Порядок обхода здесь и в `write_db` обязан совпадать, и это единственное,
    на чём шаг может сломаться тихо: перепутанные векторы дадут поиск, который
    уверенно отвечает чужим фрагментом. Поэтому оба обхода — один и тот же
    двойной цикл по `prepared`, и списком, а не словарём: словарь по `chunk.id`
    завести нельзя, идентификаторы раздаёт база при вставке.
    """
    if not embed.available():
        return None
    texts = [indexed(source, chunk) for source, chunks in prepared for chunk in chunks]
    print(f"  считаю смысл: {len(texts)} фрагментов моделью {embed.model()}")
    return asyncio.run(embed.vectors(texts))


def write_db(
    path: Path,
    prepared: list[tuple[Source, list[Chunk]]],
    vectors: list[list[float]] | None = None,
) -> None:
    tmp = path.with_suffix(".part")
    tmp.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(tmp)
    try:
        connection.executescript(SCHEMA)
        document_frequency: Counter[str] = Counter()
        total = 0
        dims = len(vectors[0]) if vectors else 0
        for source, chunks in prepared:
            connection.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source.id,
                    source.tier,
                    TIERS[source.tier],
                    source.title,
                    source.publisher,
                    source.year,
                    source.url,
                    source.license,
                ),
            )
            for chunk in chunks:
                stems = morph.index_text(indexed(source, chunk))
                cursor = connection.execute(
                    "INSERT INTO chunks(source_id, heading, text, stems, page, chars)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        chunk.source_id, chunk.heading, chunk.text,
                        stems, chunk.page, len(chunk.text),
                    ),
                )
                if vectors is not None:
                    connection.execute(
                        "INSERT INTO vectors VALUES (?, ?)",
                        (cursor.lastrowid, array.array("f", vectors[total]).tobytes()),
                    )
                document_frequency.update(set(stems.split()))
                total += 1

        connection.executemany(
            "INSERT INTO df VALUES (?, ?)", sorted(document_frequency.items())
        )
        connection.execute("INSERT INTO chunks_fts(rowid, stems) SELECT id, stems FROM chunks")
        connection.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [
                ("schema_version", SCHEMA_VERSION),
                ("built_at", datetime.now(UTC).isoformat(timespec="seconds")),
                ("chunks", str(total)),
                ("sources", str(len(prepared))),
                ("min_chunk", str(MIN_CHUNK)),
                ("max_chunk", str(MAX_CHUNK)),
                # Чем посчитан смысл. Сверяется на запросе: векторы двух
                # моделей несравнимы, и база, прочитанная не той, что её
                # считала, даёт не ошибку, а осмысленно выглядящую случайную
                # выдачу. Пусто = плотного поиска в этой базе нет
                ("embed_model", embed.model() if vectors is not None else ""),
                ("embed_dims", str(dims)),
            ],
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    try:
        path.unlink(missing_ok=True)
        tmp.replace(path)
    except PermissionError:
        # Windows не даёт подменить файл, открытый чужим процессом. Собранная
        # база остаётся рядом — её не жалко, но сказать надо внятно
        raise SystemExit(
            f"Не могу заменить {path}: файл занят. "
            f"Остановите бота и запустите снова. Готовая база лежит в {tmp}"
        ) from None


# --- Точка входа ------------------------------------------------------------


def build(*, offline: bool, only: str = "") -> list[tuple[Source, list[Chunk]]]:
    prepared: list[tuple[Source, list[Chunk]]] = []
    NORMALIZED.mkdir(parents=True, exist_ok=True)
    for source in SOURCES:
        if only and source.id != only:
            continue
        path = fetch(source, offline=offline)
        if path is None:
            continue
        blocks = read_pdf(path)
        (NORMALIZED / f"{source.id}.md").write_text(
            to_markdown(source, blocks), encoding="utf-8"
        )
        chunks = chunk_blocks(source, blocks)
        if not chunks:
            print(f"  {source.id}: текста не нашлось. Скорее всего это скан без текстового слоя")
            continue
        prepared.append((source, chunks))
        sizes = sorted(len(chunk.text) for chunk in chunks)
        print(
            f"  {source.id}: {len(chunks)} фрагментов, "
            f"{sum(sizes)} знаков, медиана {sizes[len(sizes) // 2]}"
        )
    return prepared


def swap(path: Path) -> int:
    """Подменить готовую `.part` на боевую базу. Отдельная команда — ради простоя.

    Сборка идёт при работающем боте: она качает, разбирает и считает смысл
    минутами, а бот всё это время отвечает по старой базе. Подмена файла —
    единственный шаг, который требует остановленного бота, и занимает он
    секунду. Вместе это превращает «остановите бота на время сборки» в
    «остановите на время одной команды».

    Проверяем перед подменой, а не после: битую базу лучше не подсунуть вовсе,
    чем подсунуть и узнать об этом из тишины в чате.
    """
    tmp = path.with_suffix(".part")
    if not tmp.exists():
        print(f"Нечего подменять: {tmp} нет. Сначала запустите сборку")
        return 1
    try:
        probe = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            meta = dict(probe.execute("SELECT key, value FROM meta"))
            chunks = probe.execute("SELECT count(*) FROM chunks").fetchone()[0]
        finally:
            probe.close()
    except sqlite3.Error as error:
        print(f"{tmp} не читается как база: {error}")
        return 1
    if meta.get("schema_version") != SCHEMA_VERSION:
        print(f"{tmp} собрана схемой {meta.get('schema_version')!r}, а нужна {SCHEMA_VERSION!r}")
        return 1
    if not chunks:
        print(f"{tmp} пуста — подменять нечем")
        return 1

    try:
        path.unlink(missing_ok=True)
        tmp.replace(path)
    except PermissionError:
        print(f"Не могу заменить {path}: файл занят. Остановите бота и повторите")
        return 1
    print(
        f"Подменено: {path}, фрагментов {chunks}, "
        f"смысл — {meta.get('embed_model') or 'не считался'}"
    )
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # Консоль Windows работает в cp1251 и роняет скрипт на первом символе
        # вне кодировки. Здесь в вывод попадают названия документов, а в них
        # бывает что угодно
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Собрать базу знаний в data/kb.db")
    parser.add_argument("--offline", action="store_true", help="не скачивать, брать только kb/raw")
    parser.add_argument("--only", default="", help="собрать один источник по id")
    parser.add_argument(
        "--swap",
        action="store_true",
        help="только подменить готовую data/kb.part на data/kb.db",
    )
    args = parser.parse_args()

    if args.swap:
        return swap(settings.kb_path)

    print(f"Манифест: {len(SOURCES)} источников")
    prepared = build(offline=args.offline, only=args.only)
    if not prepared:
        print("Ни одного источника не собралось — база не тронута")
        return 1

    vectors = embed_all(prepared)
    total = sum(len(chunks) for _, chunks in prepared)
    if vectors is None:
        # Вслух и подробно. База без векторов работает — поиск в ней такой,
        # каким был до 19.09.2026, — но выглядит она точно так же, как полная,
        # и разницу видно только по качеству ответов через неделю
        print("  ВНИМАНИЕ: смысл не посчитан — база собирается только со словами.")
        print("  Замерено 19.09.2026 на этом же корпусе: верный фрагмент выходит")
        print("  первым в 45 случаях из 100 вместо 71, recall@5 0.66 вместо 0.91.")
        print(f"  Причина — пустой EMBED_MODEL или ключ. Модель: {embed.model()!r}")
    elif len(vectors) != total:
        print(f"  ВНИМАНИЕ: векторов {len(vectors)}, а фрагментов {total} — пишу без них")
        vectors = None

    write_db(settings.kb_path, prepared, vectors)
    print(f"Готово: {settings.kb_path}, источников {len(prepared)}, фрагментов {total}")
    missing = [s.id for s in SOURCES if not any(s.id == p.id for p, _ in prepared)]
    if missing:
        print("Не попали в базу: " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
