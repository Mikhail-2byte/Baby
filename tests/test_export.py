"""Выгрузка для педиатра: CSV и PDF.

Проверяем то, что ломается молча и обнаруживается уже у врача: кодировку,
разделитель, пометку приблизительных длительностей и то, что из PDF не уехали
подписи вместе с вырезанными значками.
"""

import csv
import re
from datetime import timedelta

import pytest

from bot.db import repo
from bot.db.models import FEED_BOTTLE, SLEEP, WEIGHT
from bot.handlers import export
from bot.services import timeutil as tu


def _decode(payload: bytes) -> str:
    assert payload.startswith(b"\xef\xbb\xbf"), "русский Excel без BOM покажет кракозябры"
    return payload[3:].decode("utf-8")


@pytest.mark.asyncio
async def test_csv_is_semicolon_separated(session, child, mother, family, now):
    """Точка с запятой, а не запятая: с запятой русский Excel валит строку в ячейку."""
    await repo.add_event(
        session, child.id, mother.id, FEED_BOTTLE, started_at=now, value_num=60.0
    )
    events = await repo.events_between(
        session, child.id, now - timedelta(days=1), now + timedelta(minutes=1)
    )

    text = _decode(export._csv(child, family.tz, events, []))
    rows = list(csv.reader(text.splitlines(), delimiter=";"))
    # Сторожим совпадение шапки со строками, а не количество колонок числом:
    # число менялось (голосовой ввод добавил «уточнение»), и тест краснел на
    # верной правке, ничего не говоря о настоящем инварианте — врач читает
    # таблицу, в которой подписи и данные обязаны стоять друг под другом
    assert len(rows[0]) > 1, "шапка обязана разобраться по точке с запятой"
    assert all(len(row) == len(rows[0]) for row in rows[1:])


@pytest.mark.asyncio
async def test_csv_marks_estimated_durations(session, child, mother, family, now):
    """Пометка «прибл.» обязана доехать до врача.

    Разница между «спала два часа» и «таймер висел с вечера, я его закрыл сам»
    для педиатра существенна, а по числу она неотличима.
    """
    started = now - timedelta(hours=6)
    event = await repo.start_event(
        session, child.id, mother.id, SLEEP, started_at=started
    )
    await repo.close_event(session, event, started + timedelta(hours=4), estimated=True)

    events = await repo.events_between(
        session, child.id, started - timedelta(days=1), now
    )
    text = _decode(export._csv(child, family.tz, events, []))
    assert "240 прибл." in text


@pytest.mark.asyncio
async def test_csv_merges_events_and_measurements_in_order(
    session, child, mother, family, now
):
    """Одна хронология, а не две таблицы: врачу нужна лента, а не схема БД."""
    await repo.add_event(
        session, child.id, mother.id, FEED_BOTTLE, started_at=now - timedelta(hours=2)
    )
    await repo.add_measurement(
        session, child.id, mother.id, WEIGHT,
        taken_at=now - timedelta(hours=3), value=3500.0,
    )
    events = await repo.events_between(session, child.id, now - timedelta(days=1), now)
    measures = await repo.measurements(session, child.id, WEIGHT)

    rows = _decode(export._csv(child, family.tz, events, measures)).splitlines()[1:]
    assert len(rows) == 2
    # Взвешивание было раньше кормления — значит и в файле оно первое
    assert "вес" in rows[0]
    assert "бутылочка" in rows[1]


@pytest.mark.asyncio
async def test_csv_records_the_author(session, child, mother, family, now):
    """Кто записал — видно. В семье двое, и «кто её кормил» бывает вопросом."""
    await repo.add_event(session, child.id, mother.id, FEED_BOTTLE, started_at=now)
    events = await repo.events_between(
        session, child.id, now - timedelta(days=1), now + timedelta(minutes=1)
    )
    assert "Аня" in _decode(export._csv(child, family.tz, events, []))


def test_slug_falls_back_to_latin():
    """Имя файла латиницей: кириллицу половина почтовых клиентов покажет как =?utf-8?B?"""
    assert export._slug("Аня") == "baby"
    assert export._slug("Anna") == "anna"
    assert export._slug("Anna 2!") == "anna2"


def test_plain_drops_emoji_but_keeps_words():
    """Значки убираются, текст остаётся читаемым."""
    assert export._plain("🍼 Кормления: 10 раз") == "Кормления: 10 раз"
    assert export._plain("<b>Аня</b>") == "Аня"
    assert "⚠️" not in export._plain("52-й центиль ⚠️ ниже коридора")


def test_plain_keeps_labels_that_came_as_words():
    """Строка из `week_summary(plain=True)` переживает вырезание значков целой.

    Тот самый случай, ради которого `plain` и заведён: вырезание значков из
    чатовой версии оставило бы «5 · 5 ч · 0/0» — числа без подписей.
    """
    line = "пт 12 сен: кормл. 5 · сон 5 ч · подгуз. 0/0"
    assert export._plain(line) == line


# Объекты страниц. «(?![s])» отсекает узел дерева «/Type /Pages», который иначе
# считался бы ещё одной страницей.
#
# Отдельная функция появилась не от любви к аккуратности. Раньше здесь стояло
# «count(b"/Type /Page" + перевод строки) >= 3 or count(b"/Page") >= 3»: левая
# часть не находит ничего вообще (после «/Page» перевода строки нет), а правая
# даёт 3 уже на одностраничном PDF. Тест с именем «has_three_pages» не мог
# упасть никогда, и пропажа обоих графиков прошла бы мимо него.
_PAGE = re.compile(rb"/Type\s*/Page(?![s])")


def _pages(payload: bytes) -> int:
    return len(_PAGE.findall(payload))


@pytest.mark.asyncio
async def test_pdf_has_three_pages(session, child, mother, family, now):
    """Сводка, вес, ритм. Страницы без данных просто не появляются."""
    for day, grams in ((0, 3420.0), (5, 3500.0)):
        await repo.add_measurement(
            session, child.id, mother.id, WEIGHT,
            taken_at=child.born_at + timedelta(days=day), value=grams,
        )
    await repo.add_event(
        session, child.id, mother.id, SLEEP,
        started_at=now - timedelta(hours=3), ended_at=now - timedelta(hours=2),
    )

    payload = await export._pdf(session, child, family, now)
    assert payload.startswith(b"%PDF-")
    assert _pages(payload) == 3, "обложка + вес + ритм"


@pytest.mark.asyncio
async def test_pdf_survives_without_any_charts(session, child, family, now):
    """Нет ни взвешиваний, ни событий — остаётся одна обложка, и это нормально."""
    payload = await export._pdf(session, child, family, now)
    assert payload.startswith(b"%PDF-")
    assert _pages(payload) == 1, "только обложка"


@pytest.mark.asyncio
async def test_export_window_is_bounded(session, child, mother, family, now):
    """Старше года в выгрузку не попадает: файл на десятки тысяч строк не откроют."""
    ancient = now - timedelta(days=export.WINDOW_DAYS + 10)
    await repo.add_event(session, child.id, mother.id, FEED_BOTTLE, started_at=ancient)
    await repo.add_event(session, child.id, mother.id, FEED_BOTTLE, started_at=now)

    since = now - timedelta(days=export.WINDOW_DAYS)
    events = await repo.events_between(
        session, child.id, since, now + timedelta(minutes=1)
    )
    assert len(events) == 1
    assert tu.to_local(events[0].started_at, family.tz).date() == tu.local_today(
        family.tz, now
    )


@pytest.mark.asyncio
async def test_export_window_bounds_measurements_too(
    session, child, mother, family, now
):
    """Окно ограничивает и взвешивания, а не только события.

    `repo.measurements` отдаёт всё за всю жизнь, и в выгрузке получалась
    таблица, где события за год, а взвешивания с рождения. Тест на границы
    проверял только события и этого не видел.
    """
    ancient = now - timedelta(days=export.WINDOW_DAYS + 10)
    await repo.add_measurement(
        session, child.id, mother.id, WEIGHT, taken_at=ancient, value=3400.0
    )
    await repo.add_measurement(
        session, child.id, mother.id, WEIGHT, taken_at=now, value=5100.0
    )

    sent: list[bytes] = []

    class Reply:
        async def answer(self, *args, **kwargs):
            return None

        async def answer_document(self, document, **kwargs):
            sent.append(document.data)

    await export.export(Reply(), session, family, child)

    rows = _decode(sent[0]).splitlines()[1:]
    assert len(rows) == 1, "старое взвешивание в выгрузку попасть не должно"
    assert "5100" in rows[0]
