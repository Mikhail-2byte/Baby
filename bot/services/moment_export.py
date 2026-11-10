"""Автономные PDF/ZIP-альбомы. Генератор вызывается в рабочем потоке."""

import hashlib
import html
import io
import json
import shutil
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

from matplotlib import get_data_path
from PIL import Image, ImageOps
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image as PDFImage,
)
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from bot.db.models import Moment
from bot.services import moment_files
from bot.services.moments import TOPICS

PART_BYTES = 40 * 1024 * 1024


def serialize(moment: Moment) -> dict[str, Any]:
    return {
        "id": moment.id, "date": moment.on_date.isoformat(), "title": moment.title,
        "description": moment.description, "topic": moment.topic,
        "photos": [{"path": p.local_path, "caption": p.caption} for p in moment.photos],
    }


def _paragraphs(text: str, style: ParagraphStyle) -> list[Paragraph]:
    # Делим по границам слов: технический размер блока не разрывает слово.
    result = []
    while text:
        end = len(text)
        if end > 2000:
            boundary = max(text.rfind(" ", 0, 2000), text.rfind("\n", 0, 2000))
            end = boundary + 1 if boundary >= 0 else 2000
        result.append(Paragraph(html.escape(text[:end]).replace("\n", "<br/>"), style))
        text = text[end:]
    return result


def _font() -> None:
    if "MomentSans" not in pdfmetrics.getRegisteredFontNames():
        fonts = Path(get_data_path()) / "fonts" / "ttf"
        pdfmetrics.registerFont(TTFont("MomentSans", str(fonts / "DejaVuSans.ttf")))


def preview_image(path: Path) -> io.BytesIO:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((1800, 1800))
        # sendPhoto ограничивает отношение сторон; исходник при этом не меняем.
        width, height = image.size
        padded = (max(width, (height + 19) // 20), max(height, (width + 19) // 20))
        if padded != image.size:
            image = ImageOps.pad(image, padded, color="white")
        data = io.BytesIO()
        image.save(data, format="JPEG", quality=85)
        data.seek(0)
        return data


def _pdf(
    dest: Path, name: str, born: date, records: list[dict[str, Any]], base: Path,
) -> None:
    _font()
    body = ParagraphStyle("body", fontName="MomentSans", fontSize=11, leading=16,
                          spaceAfter=8, splitLongWords=True)
    heading = ParagraphStyle("heading", parent=body, fontSize=18, leading=24,
                             textColor=colors.HexColor("#31564A"), spaceAfter=14)
    story: list[Any] = _paragraphs(f"Моменты · {name}", heading)
    for i, record in enumerate(records):
        if i:
            story.append(PageBreak())
        day = date.fromisoformat(record["date"])
        title = record["title"] or TOPICS[record["topic"]]
        story.extend(_paragraphs(title, heading))
        story.extend(_paragraphs(
            f"{day:%d.%m.%Y} · Возраст: {(day - born).days} дн. · {TOPICS[record['topic']]}", body,
        ))
        story.extend(_paragraphs(record["description"], body))
        for photo in record["photos"]:
            story.append(Spacer(1, 4 * mm))
            if photo.get("missing"):
                story.extend(_paragraphs(f"Фото недоступно: {photo['path']}", body))
            else:
                image = PDFImage(preview_image(moment_files.resolve(photo["path"], base)))
                scale = min(170 * mm / image.imageWidth, 185 * mm / image.imageHeight)
                image.drawWidth = image.imageWidth * scale
                image.drawHeight = image.imageHeight * scale
                story.append(image)
            story.extend(_paragraphs(photo["caption"], body))

    def footer(canvas: Any, doc: Any) -> None:
        canvas.setFont("MomentSans", 9)
        canvas.drawRightString(190 * mm, 12 * mm, str(doc.page))

    SimpleDocTemplate(
        str(dest), pagesize=(210 * mm, 297 * mm), leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=20 * mm, title=f"Моменты — {name}",
    ).build(story, onFirstPage=footer, onLaterPages=footer)


def _zip(
    dest: Path, name: str, born: date, records: list[dict[str, Any]], base: Path,
) -> None:
    entries = []
    sections = []
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for record in records:
            entry = {**record, "photos": []}
            figures = []
            for index, photo in enumerate(record["photos"]):
                path = moment_files.resolve(photo["path"], base) if photo["path"] else None
                member = f"photos/{record['id']}-{index + 1}{path.suffix if path else '.jpg'}"
                missing = bool(photo.get("missing"))
                entry["photos"].append({
                    "file": None if missing else member, "caption": photo["caption"],
                    "missing": missing,
                })
                if not missing and path is not None:
                    archive.write(path, member)
                    figures.append(f'<figure><img src="{member}" alt="">'
                                   f'<figcaption>{html.escape(photo["caption"])}</figcaption></figure>')
                else:
                    figures.append('<p class="missing">Фото недоступно</p>')
            entries.append(entry)
            day = date.fromisoformat(record["date"])
            sections.append(
                f'<article><h2>{html.escape(record["title"] or TOPICS[record["topic"]])}</h2>'
                f'<p>{day:%d.%m.%Y} · Возраст: {(day - born).days} дн. · '
                f'{TOPICS[record["topic"]]}</p>'
                f'<div class="text">{html.escape(record["description"])}</div>'
                f'{"".join(figures)}</article>'
            )
        manifest = {"format_version": 1, "child": name, "born_on": born.isoformat(),
                    "moments": entries}
        archive.writestr("moments.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("index.html", (
            '<!doctype html><html lang="ru"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Моменты</title><style>body{max-width:850px;margin:40px auto;padding:0 18px;'
            'font:17px/1.6 sans-serif;color:#263d35;background:#faf8f2}'
            'article{border-top:1px solid #bdc9bf;margin-top:35px;padding-top:15px}'
            'img{max-width:100%;max-height:85vh}figure{margin:20px 0}.text,figcaption{'
            'white-space:pre-wrap;overflow-wrap:anywhere}.missing{color:#974b38}</style>'
            f'<h1>Моменты · {html.escape(name)}</h1>{"".join(sections)}</html>'
        ))


def _halves(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(records) > 1:
        half = len(records) // 2
        return records[:half], records[half:]
    record = records[0]
    photos = record["photos"]
    if len(photos) > 1:
        half = len(photos) // 2
        return [{**record, "photos": photos[:half]}], [{**record, "photos": photos[half:]}]
    raise ValueError("Одна запись превышает размер части. Уменьшите её фото или текст.")


def build(
    name: str, born: date, records: list[dict[str, Any]], directory: Path,
    *, base: Path | None = None, limit: int = PART_BYTES,
) -> tuple[list[Path], list[str]]:
    base = base or moment_files.root()
    directory.mkdir(parents=True, exist_ok=True)
    warnings = []
    # Не меняем переданные структуры: они могут использоваться повторной выгрузкой.
    records = json.loads(json.dumps(records))
    for record in records:
        for index, photo in enumerate(record["photos"]):
            try:
                path = moment_files.resolve(photo["path"], base)
                with Image.open(path) as image:
                    image.load()
            except (OSError, ValueError, Image.DecompressionBombError):
                photo["missing"] = True
                warnings.append(f"{record['date']}, запись {record['id']}, фото {index + 1}")
    paths = []
    for extension, writer in (("pdf", _pdf), ("zip", _zip)):
        pending = [records]
        parts: list[Path] = []
        while pending:
            batch = pending.pop(0)
            candidate = directory / f"album-{len(parts) + 1:03d}.{extension}"
            writer(candidate, name, born, batch, base)
            if candidate.stat().st_size > limit:
                candidate.unlink()
                left, right = _halves(batch)
                pending[0:0] = [left, right]
            else:
                parts.append(candidate)
        for index, path in enumerate(parts, 1):
            final = path.with_name(f"moments-{index:03d}-of-{len(parts):03d}.{extension}")
            path.rename(final)
            paths.append(final)
    return paths, warnings


def backup_parts(database: Path, base: Path, prefix: Path, limit: int = PART_BYTES) -> list[Path]:
    """Каждая часть — обычный ZIP. Для восстановления распаковать все в один каталог."""
    files = [(database, "baby.db")]
    if base.exists():
        files.extend((p, f"moments/{p.relative_to(base).as_posix()}")
                     for p in sorted(base.rglob("*")) if p.is_file() and p.suffix != ".upload")
    budget = limit - 65536
    if budget <= 0:
        raise ValueError("Размер части слишком мал.")
    segments = []
    restore = []
    for path, member in files:
        length = path.stat().st_size
        if length <= budget:
            segments.append((path, member, 0, length))
        else:
            pieces: list[str] = []
            for offset in range(0, length, budget):
                name = f"{member}.part-{len(pieces) + 1:06d}"
                segments.append((path, name, offset, min(budget, length - offset)))
                pieces.append(name)
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            restore.append({"target": member, "parts": pieces, "size": length, "sha256": digest})
    batches: list[list[tuple[Path, str, int, int]]] = [[]]
    size = 0
    for path, member, offset, length in segments:
        if size + length > budget and batches[-1]:
            batches.append([])
            size = 0
        batches[-1].append((path, member, offset, length))
        size += length
    # Каталог публикуется целиком после завершения, незаконченный комплект не виден.
    stage = prefix.with_suffix(".part")
    parent = prefix.parent.resolve()
    if stage.resolve().parent != parent or prefix.resolve().parent != parent:
        raise ValueError("Каталог резервной копии выходит за пределы назначения")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        for index, batch in enumerate(batches, 1):
            target = stage / f"backup-{index:03d}-of-{len(batches):03d}.zip"
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
                for path, member, offset, length in batch:
                    with path.open("rb") as source, archive.open(member, "w") as output_stream:
                        source.seek(offset)
                        remaining = length
                        while remaining:
                            block = source.read(min(remaining, 1024 * 1024))
                            if not block:
                                raise OSError("Исходный файл изменился при резервном копировании")
                            output_stream.write(block)
                            remaining -= len(block)
                if restore:
                    archive.writestr("restore.json", json.dumps(restore, ensure_ascii=False))
                    archive.writestr("restore.py", RESTORE_SCRIPT)
                archive.writestr("RESTORE.txt", (
                    "Остановите бота. Распакуйте ВСЕ части в один пустой каталог.\n"
                    "Если есть restore.py — выполните python restore.py в этом каталоге.\n"
                    "Скопируйте baby.db и папку moments в каталог рабочей базы.\n"
                    "Не объединяйте восстановленную базу с оставшимися WAL/SHM файлами.\n"
                    "Запустите прежним способом: миграции выполняются при запуске.\n"
                ).encode())
            if target.stat().st_size > limit:
                raise ValueError("Часть резервной копии превышает допустимый размер.")
        if prefix.exists():
            shutil.rmtree(prefix)
        stage.replace(prefix)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return sorted(prefix.glob("*.zip"))


RESTORE_SCRIPT = '''"""Run after extracting ALL backup ZIP parts into an empty directory."""
import hashlib
import json
import shutil
from pathlib import Path

base = Path(__file__).resolve().parent
for entry in json.loads((base / "restore.json").read_text(encoding="utf-8")):
    target = (base / entry["target"]).resolve()
    parts = [(base / name).resolve() for name in entry["parts"]]
    if any(p == base or not p.is_relative_to(base) for p in [target, *parts]):
        raise SystemExit("Unsafe restore path")
    if target.exists():
        with target.open("rb") as source:
            matches = hashlib.file_digest(source, "sha256").hexdigest() == entry["sha256"]
        if matches:
            continue
        raise SystemExit(f"Destination already exists and differs: {target}")
    if any(not p.is_file() for p in parts):
        raise SystemExit("Missing backup parts; extract ALL ZIPs first")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".restoring")
    with temporary.open("wb") as output:
        for part in parts:
            with part.open("rb") as source:
                shutil.copyfileobj(source, output)
    with temporary.open("rb") as source:
        valid = hashlib.file_digest(source, "sha256").hexdigest() == entry["sha256"]
    if temporary.stat().st_size != entry["size"] or not valid:
        temporary.unlink()
        raise SystemExit("Restored file size mismatch")
    temporary.replace(target)
    for part in parts:
        part.unlink()
    print(f"Restored: {target}")
'''
