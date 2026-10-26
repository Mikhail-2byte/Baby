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
from datetime import date, datetime, timedelta

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from matplotlib import image as mpimg
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import MEASURE_KINDS, Child, Event, Family, Measurement
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.keyboards import ExportCB
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
    message: Message, session: AsyncSession, family: Family, child: Child | None,
    command: CommandObject | None = None,
) -> None:
    if child is None:
        await message.answer(texts.NO_CHILD, reply_markup=keyboards.home())
        return

    if command is not None and command.args:
        try:
            first, last = parse_dates(command.args, tu.local_today(family.tz))
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await _send_export(message, session, family, child, first, last)
        return
    labels = [(1, "Сегодня"), (-1, "Вчера"), (7, "7 дней"), (30, "30 дней")]
    buttons = [InlineKeyboardButton(text=label, callback_data=ExportCB(days=days).pack())
               for days, label in labels]
    await message.answer(
        "За какой период подготовить выгрузку для врача?\n"
        "CSV, сводка и графики будут за одни и те же даты.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            buttons[:2], buttons[2:],
            [InlineKeyboardButton(text="Другие даты", callback_data=ExportCB(days=0).pack())],
        ]),
    )


def parse_dates(value: str, today: date) -> tuple[date, date]:
    """Одна дата или две включительно; принимаем русский и ISO-формат."""
    parts = value.split()
    if len(parts) not in {1, 2}:
        raise ValueError("Укажите даты: /export 20.10.2026 25.10.2026")
    dates = []
    for part in parts:
        try:
            parsed = (datetime.strptime(part, "%d.%m.%Y").date()
                      if "." in part else date.fromisoformat(part))
        except ValueError as exc:
            raise ValueError("Неверная дата. Пример: /export 20.10.2026 25.10.2026") from exc
        dates.append(parsed)
    first, last = dates[0], dates[-1]
    if first > last:
        raise ValueError("Начало периода должно быть не позже окончания.")
    if last > today:
        raise ValueError("Выгрузка доступна по сегодняшний день включительно.")
    if (last - first).days >= WINDOW_DAYS:
        raise ValueError(f"Выберите не больше {WINDOW_DAYS} дней за одну выгрузку.")
    return first, last


@router.callback_query(IN_GROUP_CB, ExportCB.filter())
async def choose_period(
    call: CallbackQuery, callback_data: ExportCB, session: AsyncSession,
    family: Family, child: Child | None,
) -> None:
    if child is None:
        await call.answer(texts.NO_CHILD, show_alert=True)
        return
    if not isinstance(call.message, Message):
        await call.answer()
        return
    await call.answer()
    if callback_data.days == 0:
        await call.message.answer(
            "Отправьте команду с датами начала и окончания включительно:\n"
            "<code>/export 20.10.2026 25.10.2026</code>\n"
            "За один день: <code>/export 25.10.2026</code>\n"
            f"Даты — по времени семьи, максимум {WINDOW_DAYS} дней."
        )
        return
    if callback_data.days not in {-1, 1, 7, 30}:
        return
    today = tu.local_today(family.tz)
    last = today - timedelta(days=1) if callback_data.days == -1 else today
    first = last - timedelta(days=max(callback_data.days, 1) - 1)
    await _send_export(call.message, session, family, child, first, last)


async def _send_export(
    message: Message, session: AsyncSession, family: Family, child: Child,
    first: date, last: date,
) -> None:
    now = tu.now_utc()
    since, _ = tu.day_bounds(first, family.tz)
    _, until = tu.day_bounds(last, family.tz)
    period = f"{first:%d.%m.%Y} - {last:%d.%m.%Y}"
    events = [event for event in await repo.events_between(session, child.id, since, until)
              if event.started_at <= now]
    measures: list[Measurement] = []
    for kind in MEASURE_KINDS:
        # Тем же окном, что и события: врачу нужна одна хронология, а не
        # таблица, где события за год, а взвешивания за всю жизнь
        measures.extend(
            m
            for m in await repo.measurements(session, child.id, kind)
            if since <= m.taken_at < until and m.taken_at <= now
        )

    if not events and not measures:
        await message.answer(f"За {period} записей нет.", reply_markup=keyboards.home())
        return

    stamp = f"{first:%Y-%m-%d}_{last:%Y-%m-%d}"
    # В поток: на четырёхстах днях это тысячи строк плюс сортировка, а
    # блокировать event loop ради выгрузки незачем — тот же довод, что у графиков
    payload = await asyncio.to_thread(
        _csv, child, family.tz, events, measures, since=since, until=min(until, now)
    )
    await message.answer_document(
        BufferedInputFile(payload, f"{_slug(child.name)}-{stamp}.csv"),
        caption=f"Период: {period}\n" + texts.EXPORT_CAPTION.format(
            events=len(events), measures=len(measures)
        ),
    )

    try:
        report = await _pdf(session, child, family, now, first=first, last=last)
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
    child: Child, tz: str, events: list[Event], measures: list[Measurement], *,
    since: datetime | None = None, until: datetime | None = None,
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
        began = max(event.started_at, since) if since else event.started_at
        ended = min(event.ended_at, until) if event.ended_at and until else event.ended_at
        local = tu.to_local(began, tz)
        notes = [event.note] if event.note else []
        if began != event.started_at:
            original = tu.to_local(event.started_at, tz)
            notes.append(f"Начало вне периода: {original:%d.%m.%Y %H:%M}")
        if event.ended_at is not None and ended != event.ended_at:
            original = tu.to_local(event.ended_at, tz)
            notes.append(f"Окончание вне периода: {original:%d.%m.%Y %H:%M}")
        minutes = ""
        if ended is not None:
            minutes = str(int((ended - began).total_seconds() // 60))
            if event.estimated:
                # Пометка обязана доехать до врача: разница между «спала два
                # часа» и «таймер висел с вечера» для него существенна
                minutes += " прибл."
        rows.append(
            (
                began,
                [
                    f"{local:%d.%m.%Y}", f"{local:%H:%M}",
                    growth.age_days(child, began),
                    texts.KIND_NAMES.get(event.kind, event.kind),
                    event.subtype or "",
                    minutes,
                    f"{event.value_num:g}" if event.value_num is not None else "",
                    texts.SIDE_NAMES.get(event.side or "", ""),
                    event.author.display_name if event.author else "",
                    "; ".join(notes),
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
    session: AsyncSession, child: Child, family: Family, now: datetime, *,
    first: date | None = None, last: date | None = None,
) -> bytes:
    """Сводка и графики выбранного периода; ритм разбит на недели."""
    last = last or tu.local_today(family.tz, now)
    first = first or last - timedelta(days=6)
    since, _ = tu.day_bounds(first, family.tz)
    _, until = tu.day_bounds(last, family.tz)
    period = f"{first:%d.%m.%Y} - {last:%d.%m.%Y}"
    total = await stats.collect(
        session, child, since, min(until, now + timedelta(microseconds=1)), now
    )
    summary = _plain(texts.day_summary_from_span(child, total, "за выбранный период"))
    header = _plain(texts.baby_card(child, now, family.tz))

    weight_data = await charts.collect_weight(
        session, child, since=since, until=min(until, now + timedelta(microseconds=1))
    )
    rhythm_data = await charts.collect_rhythm(
        session, child, family.tz, now, days=(last - first).days + 1, ending_on=last,
    )

    def build() -> bytes:
        buffer = io.BytesIO()
        with PdfPages(buffer) as pdf:
            pdf.savefig(_cover(header, summary, now, period))
            points, bands, evaluated = weight_data
            if points:
                pdf.savefig(
                    _figure_from_png(
                        charts.draw_weight(child.name, points, bands, evaluated), period,
                    )
                )
            if any(entry.has_data() for entry in rhythm_data):
                for offset in range(0, len(rhythm_data), 7):
                    chunk = rhythm_data[offset:offset + 7]
                    pdf.savefig(_figure_from_png(charts.draw_rhythm(
                        child.name, chunk, partial_day=tu.local_today(family.tz, now)
                    ), period))
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


def _cover(header: str, summary: str, now: datetime, period: str = "") -> Figure:
    """Титульная страница: ребёнок, выбранные даты и итоги периода."""
    figure = Figure(figsize=(8.27, 11.69), dpi=150, facecolor="white")  # A4
    figure.text(0.08, 0.94, header, fontsize=14, va="top", family="DejaVu Sans")
    figure.text(0.08, 0.79, f"Период: {period}", fontsize=12, va="top", family="DejaVu Sans")
    figure.text(0.08, 0.73, summary, fontsize=11, va="top", family="DejaVu Sans")
    figure.text(
        0.08, 0.05,
        f"Выгрузка из семейного трекера, {now:%d.%m.%Y}. "
        "Длительности с пометкой «прибл.» посчитаны автоматически.",
        fontsize=8, color="#52514e", va="bottom", family="DejaVu Sans",
    )
    return figure


def _figure_from_png(payload: bytes, period: str = "") -> Figure:
    """Готовый PNG графика — отдельной страницей PDF.

    Перерисовывать графики во второй раз под другой размер холста незачем:
    они уже собраны в нужной палитре и с нужными подписями, а страница A4 в
    альбомной ориентации повторяет их пропорции почти точно.
    """
    figure = Figure(figsize=(11.69, 8.27), dpi=150, facecolor="white")
    if period:
        figure.text(0.04, 0.97, f"Период: {period}", fontsize=10,
                    va="top", family="DejaVu Sans")
    axes = figure.add_axes((0.04, 0.04, 0.92, 0.88 if period else 0.92))
    axes.imshow(mpimg.imread(io.BytesIO(payload), format="png"))
    axes.axis("off")
    return figure
