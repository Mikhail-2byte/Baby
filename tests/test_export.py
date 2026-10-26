"""Выгрузка для педиатра: CSV и PDF.

Проверяем то, что ломается молча и обнаруживается уже у врача: кодировку,
разделитель, пометку приблизительных длительностей и то, что из PDF не уехали
подписи вместе с вырезанными значками.
"""

import csv
import re
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from bot.db import repo
from bot.db.models import FEED_BOTTLE, FEED_BREAST, SLEEP, WEIGHT
from bot.handlers import export
from bot.keyboards import ExportCB
from bot.services import charts
from bot.services import timeutil as tu
from tests.test_record_edit import _feed, _tap
from tests.test_record_edit import wired as wired


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
async def test_pdf_includes_rhythm_with_only_bottle_records(session, child, mother, family, now):
    await repo.add_event(session, child.id, mother.id, FEED_BOTTLE, started_at=now)
    payload = await export._pdf(session, child, family, now)
    assert _pages(payload) == 2, "обложка и шкала с точкой кормления без длительности"


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

    last = tu.local_today(family.tz)
    await export._send_export(
        Reply(), session, family, child, last - timedelta(days=export.WINDOW_DAYS - 1), last
    )

    rows = _decode(sent[0]).splitlines()[1:]
    assert len(rows) == 1, "старое взвешивание в выгрузку попасть не должно"
    assert "5100" in rows[0]


@pytest.mark.parametrize("value, expected", [
    ("20.10.2026 25.10.2026", (date(2026, 10, 20), date(2026, 10, 25))),
    ("2026-10-20 2026-10-25", (date(2026, 10, 20), date(2026, 10, 25))),
    ("25.10.2026", (date(2026, 10, 25), date(2026, 10, 25))),
])
def test_export_dates_are_inclusive(value, expected):
    assert export.parse_dates(value, date(2026, 10, 26)) == expected


@pytest.mark.parametrize("value", [
    "31.02.2026", "26.10.2026 25.10.2026", "27.10.2026",
    "01.01.2020 25.10.2026", "20.10.2026 21.10.2026 22.10.2026",
])
def test_export_rejects_invalid_reversed_future_or_oversized_period(value):
    with pytest.raises(ValueError):
        export.parse_dates(value, date(2026, 10, 26))


@pytest.mark.asyncio
async def test_export_command_offers_period_before_sending_files(wired, monkeypatch):
    dp, bot, talk, _ = wired
    send = AsyncMock()
    monkeypatch.setattr(export, "_send_export", send)
    await _feed(dp, bot, "/export")
    send.assert_not_awaited()
    labels = [button.text for row in talk.markups[-1].inline_keyboard for button in row]
    assert labels == ["Сегодня", "Вчера", "7 дней", "30 дней", "Другие даты"]


@pytest.mark.parametrize("days", [-1, 1, 7, 30])
@pytest.mark.asyncio
async def test_export_buttons_use_family_calendar_days(wired, family, monkeypatch, days):
    dp, bot, _, _ = wired
    send = AsyncMock()
    monkeypatch.setattr(export, "_send_export", send)
    # Полночь следующего дня по Москве, ещё предыдущий день в UTC.
    moment = tu.to_utc(datetime(2026, 10, 26, 0, 15), family.tz)
    monkeypatch.setattr(tu, "now_utc", lambda: moment)
    await _tap(dp, bot, ExportCB(days=days))
    last = date(2026, 10, 25) if days == -1 else date(2026, 10, 26)
    assert send.await_args.args[-2:] == (last - timedelta(days=max(days, 1) - 1), last)


@pytest.mark.asyncio
async def test_custom_export_dates_reach_handler(wired, monkeypatch):
    dp, bot, _, _ = wired
    send = AsyncMock()
    monkeypatch.setattr(export, "_send_export", send)
    await _feed(dp, bot, "/export 20.09.2026 25.09.2026")
    assert send.await_args.args[-2:] == (date(2026, 9, 20), date(2026, 9, 25))


@pytest.mark.asyncio
async def test_invalid_custom_dates_explain_error_without_export(wired, monkeypatch):
    dp, bot, talk, _ = wired
    send = AsyncMock()
    monkeypatch.setattr(export, "_send_export", send)
    await _feed(dp, bot, "/export 31.02.2026")
    send.assert_not_awaited()
    assert "Неверная дата" in talk.sent[-1]


@pytest.mark.parametrize("days", [0, 999])
@pytest.mark.asyncio
async def test_custom_or_forged_button_does_not_export_immediately(wired, monkeypatch, days):
    dp, bot, talk, _ = wired
    send = AsyncMock()
    monkeypatch.setattr(export, "_send_export", send)
    await _tap(dp, bot, ExportCB(days=days))
    send.assert_not_awaited()
    if days == 0:
        assert "/export 20.10.2026 25.10.2026" in talk.sent[-1]


@pytest.mark.asyncio
async def test_export_clips_crossing_events_and_bounds_measurements(
    session, child, mother, family, now, monkeypatch
):
    last = tu.local_today(family.tz, now) - timedelta(days=1)
    first = last - timedelta(days=1)
    start, _ = tu.day_bounds(first, family.tz)
    _, end = tu.day_bounds(last, family.tz)
    for moment in (start - timedelta(microseconds=1), start, end - timedelta(microseconds=1), end):
        await repo.add_event(session, child.id, mother.id, FEED_BOTTLE, started_at=moment)
        await repo.add_measurement(
            session, child.id, mother.id, WEIGHT, taken_at=moment, value=4000,
        )
    crossing = await repo.add_event(
        session, child.id, mother.id, FEED_BREAST,
        started_at=start - timedelta(minutes=10), ended_at=start + timedelta(minutes=10),
    )
    await repo.add_event(
        session, child.id, mother.id, FEED_BREAST,
        started_at=end - timedelta(minutes=10), ended_at=end + timedelta(minutes=10),
    )
    original_start = crossing.started_at
    monkeypatch.setattr(tu, "now_utc", lambda: now)
    pdf = AsyncMock(return_value=b"%PDF-test")
    monkeypatch.setattr(export, "_pdf", pdf)

    class Reply:
        sent = []

        async def answer_document(self, document, **kwargs):
            self.sent.append(document)

    reply = Reply()
    await export._send_export(reply, session, family, child, first, last)
    rows = list(csv.reader(_decode(reply.sent[0].data).splitlines(), delimiter=";"))[1:]
    assert len(rows) == 6, "два мгновенных события, два пересекающих границы и два измерения"
    assert all(row[0] in {f"{first:%d.%m.%Y}", f"{last:%d.%m.%Y}"} for row in rows)
    clipped = next(row for row in rows if "Начало вне периода" in row[-1])
    assert clipped[1] == "00:00" and clipped[5] == "10"
    clipped_end = next(row for row in rows if "Окончание вне периода" in row[-1])
    assert clipped_end[1] == "23:50" and clipped_end[5] == "10"
    assert crossing.started_at == original_start, "CSV не должен менять запись в БД"
    assert f"{first:%Y-%m-%d}_{last:%Y-%m-%d}" in reply.sent[0].filename
    assert pdf.await_args.kwargs == {"first": first, "last": last}


@pytest.mark.asyncio
async def test_pdf_uses_selected_period_for_summary_weight_and_paginated_rhythm(
    session, child, mother, family, now, monkeypatch
):
    report_now = now + timedelta(days=20)
    last = tu.local_today(family.tz, report_now) - timedelta(days=1)
    first = last - timedelta(days=7)
    start, _ = tu.day_bounds(first, family.tz)
    _, end = tu.day_bounds(last, family.tz)
    for moment, grams in ((child.born_at, 3420), (start, 4100), (end, 4300)):
        await repo.add_measurement(
            session, child.id, mother.id, WEIGHT, taken_at=moment, value=grams,
        )
        await repo.add_event(session, child.id, mother.id, FEED_BOTTLE, started_at=moment)
    await repo.add_event(
        session, child.id, mother.id, SLEEP,
        started_at=start - timedelta(hours=1), ended_at=start + timedelta(hours=1),
    )
    pages, weights, covers = [], [], []
    draw_rhythm, draw_weight, cover = charts.draw_rhythm, charts.draw_weight, export._cover

    def capture_rhythm(name, rhythm, **kwargs):
        pages.append([entry.day for entry in rhythm])
        return draw_rhythm(name, rhythm, **kwargs)

    def capture_weight(name, points, *args):
        weights.extend(points)
        return draw_weight(name, points, *args)

    def capture_cover(header, summary, stamp, period):
        covers.append((summary, period))
        return cover(header, summary, stamp, period)

    monkeypatch.setattr(charts, "draw_rhythm", capture_rhythm)
    monkeypatch.setattr(charts, "draw_weight", capture_weight)
    monkeypatch.setattr(export, "_cover", capture_cover)
    payload = await export._pdf(session, child, family, report_now, first=first, last=last)
    assert _pages(payload) == 4, "обложка, вес, семь суток и последние сутки"
    assert [point[1] for point in weights] == [4100]
    assert pages == [[first + timedelta(days=i) for i in range(7)], [last]]
    assert "Кормления: 1 раз" in covers[0][0]
    assert "Сон: 1 ч (начался до периода)" in covers[0][0]
    assert covers[0][1] == f"{first:%d.%m.%Y} - {last:%d.%m.%Y}"
