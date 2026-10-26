"""`/export` — выгрузка для педиатра.

Два файла. CSV — сырые данные, их можно открыть в чём угодно и посчитать что
угодно. PDF — то, что реально протягивают врачу через стол: сводка, график веса
с коридорами ВОЗ и ритм суток.

PDF собирается тем же matplotlib, что рисует графики. Это не изящно, зато не
тянет в проект ни reportlab, ни возню со встраиванием кириллических шрифтов —
а лишняя зависимость в боте, который должен молча работать год, стоит дороже
красоты вёрстки.

Разделитель в CSV — точка с запятой, кодировка UTF-8 с BOM. Оба решения
ради русского Excel: с запятой он сваливает строку в одну ячейку, без BOM
показывает кракозябры.
"""

import asyncio
import csv
import io
import logging
from datetime import datetime, timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from matplotlib import image as mpimg
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import MEASURE_KINDS, Child, Event, Family, Measurement
from bot.filters import IN_GROUP
from bot.services import charts, growth, stats
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

NEWLINE = chr(10)

router = Router(name="export")

# Сколько суток забирать в выгрузку. Год с запасом: больше врачу не нужно, а
# файл на десятки тысяч строк никто не откроет
WINDOW_DAYS = 400


@router.message(IN_GROUP, Command("export"))
async def export(
    message: Message, session: AsyncSession, family: Family, child: Child | None
) -> None:
    if child is None:
        await message.answer(texts.NO_CHILD, reply_markup=keyboards.home())
        return

    now = tu.now_utc()
    since = now - timedelta(days=WINDOW_DAYS)
    events = await repo.events_between(session, child.id, since, now)
    measures: list[Measurement] = []
    for kind in MEASURE_KINDS:
        # Тем же окном, что и события: врачу нужна одна хронология, а не
        # таблица, где события за год, а взвешивания за всю жизнь
        measures.extend(
            m
            for m in await repo.measurements(session, child.id, kind)
            if since <= m.taken_at <= now
        )

    if not events and not measures:
        await message.answer(texts.EXPORT_EMPTY, reply_markup=keyboards.home())
        return

    stamp = tu.to_local(now, family.tz).strftime("%Y-%m-%d")
    # В поток: на четырёхстах днях это тысячи строк плюс сортировка, а
    # блокировать event loop ради выгрузки незачем — тот же довод, что у графиков
    payload = await asyncio.to_thread(_csv, child, family.tz, events, measures)
    await message.answer_document(
        BufferedInputFile(payload, f"{_slug(child.name)}-{stamp}.csv"),
        caption=texts.EXPORT_CAPTION.format(
            events=len(events), measures=len(measures)
        ),
    )

    try:
        report = await _pdf(session, child, family, now)
    except Exception:
        # CSV уже ушёл, данные у человека есть — PDF это удобство, а не
        # содержание выгрузки
        log.exception("PDF выгрузки не собрался")
        return
    await message.answer_document(
        BufferedInputFile(report, f"{_slug(child.name)}-{stamp}.pdf")
    )


def _slug(name: str) -> str:
    """Имя файла из имени ребёнка: только латиница и цифры.

    Telegram кириллицу в именах файлов принимает, но письмо врачу с вложением
    `Аня-2026-09-13.pdf` половина почтовых клиентов покажет как `=?utf-8?B?`.
    """
    keep = [c for c in name.lower() if c.isascii() and (c.isalnum() or c == "-")]
    return "".join(keep) or "baby"


def _csv(
    child: Child, tz: str, events: list[Event], measures: list[Measurement]
) -> bytes:
    """Одна таблица на события и измерения: врачу нужна хронология, а не схема."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    # «Уточнение» — это `subtype`: название лекарства, вид прикорма. Колонка
    # появилась вместе с голосовым вводом, и не для красоты: до неё `subtype`
    # не читал ни один экран, то есть названное вслух «витамин D» ложилось в
    # базу и исчезало навсегда. Колонка, которую пишет модель и не читает
    # никто, хуже неиспользуемой
    writer.writerow(
        ["дата", "время", "возраст, дней", "что", "уточнение",
         "длительность, мин", "значение", "сторона", "кто записал",
         "примечание"]
    )

    rows: list[tuple[datetime, list]] = []

    for event in events:
        local = tu.to_local(event.started_at, tz)
        minutes = ""
        if event.ended_at is not None:
            minutes = str(int((event.ended_at - event.started_at).total_seconds() // 60))
            if event.estimated:
                # Пометка обязана доехать до врача: разница между «спала два
                # часа» и «таймер висел с вечера» для него существенна
                minutes += " прибл."
        rows.append(
            (
                event.started_at,
                [
                    f"{local:%d.%m.%Y}", f"{local:%H:%M}",
                    growth.age_days(child, event.started_at),
                    texts.KIND_NAMES.get(event.kind, event.kind),
                    event.subtype or "",
                    minutes,
                    f"{event.value_num:g}" if event.value_num is not None else "",
                    texts.SIDE_NAMES.get(event.side or "", ""),
                    event.author.display_name if event.author else "",
                    event.note or "",
                ],
            )
        )

    for measure in measures:
        local = tu.to_local(measure.taken_at, tz)
        rows.append(
            (
                measure.taken_at,
                [
                    f"{local:%d.%m.%Y}", f"{local:%H:%M}",
                    growth.age_days(child, measure.taken_at),
                    texts.MEASURE_NAMES.get(measure.kind, measure.kind),
                    "",
                    "",
                    f"{measure.value:g}",
                    "",
                    measure.author.display_name if measure.author else "",
                    measure.note or "",
                ],
            )
        )

    for _, row in sorted(rows, key=lambda pair: pair[0]):
        writer.writerow(row)

    # BOM вручную: `utf-8-sig` на StringIO не действует, кодируем сами
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


async def _pdf(
    session: AsyncSession, child: Child, family: Family, now: datetime
) -> bytes:
    """Три страницы: сводка, вес с коридорами, ритм суток."""
    total, days = await stats.week(session, child, family.tz, now=now)
    summary = _plain(texts.week_summary(child, total, days, plain=True))
    header = _plain(texts.baby_card(child, now, family.tz))

    weight_data = await charts.collect_weight(session, child)
    rhythm_data = await charts.collect_rhythm(session, child, family.tz, now)

    def build() -> bytes:
        buffer = io.BytesIO()
        with PdfPages(buffer) as pdf:
            pdf.savefig(_cover(header, summary, now))
            points, bands, evaluated = weight_data
            if points:
                pdf.savefig(
                    _figure_from_png(
                        charts.draw_weight(child.name, points, bands, evaluated)
                    )
                )
            if any(entry.has_data() for entry in rhythm_data):
                pdf.savefig(
                    _figure_from_png(charts.draw_rhythm(child.name, rhythm_data))
                )
        return buffer.getvalue()

    return await asyncio.to_thread(build)


# Значки, которых нет в DejaVu Sans. В PDF они превращаются в квадратики, и
# документ для врача сразу выглядит сломанным. Просто выбрасываем: там, где
# значок нёс смысл, подпись словом приходит из `texts.week_summary(plain=True)`
_EMOJI_RANGES = (
    (0x2300, 0x23FF),    # ⏱ ⏰
    (0x2600, 0x27BF),    # ⚖ ⚠
    (0x2B00, 0x2BFF),
    (0xFE00, 0xFE0F),    # селекторы начертания
    (0x1F300, 0x1FAFF),  # 👶 📊 🍼 💧 😴
)


def _plain(html: str) -> str:
    """Текст для PDF: без разметки Telegram и без значков, которых нет в шрифте."""
    for tag in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>"):
        html = html.replace(tag, "")

    kept = [
        char
        for char in html
        if not any(low <= ord(char) <= high for low, high in _EMOJI_RANGES)
    ]
    # Схлопываем пробелы, оставшиеся на месте вырезанных значков
    return NEWLINE.join(
        " ".join(line.split()) for line in "".join(kept).split(NEWLINE)
    )


def _cover(header: str, summary: str, now: datetime) -> Figure:
    """Титульная страница: кто, сколько лет и итоги последней недели."""
    figure = Figure(figsize=(8.27, 11.69), dpi=150, facecolor="white")  # A4
    figure.text(0.08, 0.94, header, fontsize=14, va="top", family="DejaVu Sans")
    figure.text(0.08, 0.80, summary, fontsize=11, va="top", family="DejaVu Sans")
    figure.text(
        0.08, 0.05,
        f"Выгрузка из семейного трекера, {now:%d.%m.%Y}. "
        "Длительности с пометкой «прибл.» посчитаны автоматически.",
        fontsize=8, color="#52514e", va="bottom", family="DejaVu Sans",
    )
    return figure


def _figure_from_png(payload: bytes) -> Figure:
    """Готовый PNG графика — отдельной страницей PDF.

    Перерисовывать графики во второй раз под другой размер холста незачем:
    они уже собраны в нужной палитре и с нужными подписями, а страница A4 в
    альбомной ориентации повторяет их пропорции почти точно.
    """
    figure = Figure(figsize=(11.69, 8.27), dpi=150, facecolor="white")
    axes = figure.add_axes((0.04, 0.04, 0.92, 0.92))
    axes.imshow(mpimg.imread(io.BytesIO(payload), format="png"))
    axes.axis("off")
    return figure
