"""Черновики семейного альбома. Содержимое принимается только по явному ответу."""

import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

from aiogram import Bot
from aiogram.types import Message
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Child, Family, Member, Moment, MomentDraft
from bot.services import moment_files
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

TOPICS = {
    "achievement": "Достижения", "food": "Еда", "trip": "Прогулки и поездки",
    "family": "Семья", "health": "Здоровье", "other": "Другое",
}
# Обычный облачный getFile: https://core.telegram.org/bots/api#getfile
MAX_PHOTO_BYTES = 20_000_000

# Явный ответ разрешает группу ещё в фильтре, до ожидания storage_lock.
# После записи группы в черновик это разрешение больше не нужно.
_album_hints: set[tuple[int, str]] = set()


def remember_album(draft_id: int, group_id: str) -> None:
    _album_hints.add((draft_id, group_id))


def forget_albums(draft_id: int) -> None:
    _album_hints.difference_update({key for key in _album_hints if key[0] == draft_id})


def unpack(draft: MomentDraft) -> dict[str, Any]:
    return json.loads(draft.payload)


def active(draft: MomentDraft) -> bool:
    return draft.saved_id is None and bool(unpack(draft).get("active"))


def blank(family: Family) -> dict[str, Any]:
    return {
        "active": False, "date": tu.local_today(family.tz).isoformat(),
        "title": "", "description": "", "topic": "other", "photos": [],
        "prompts": {}, "groups": [], "seen": [], "original_paths": [], "filters": {},
    }


async def context(
    session: AsyncSession, family: Family, child: Child, member: Member,
) -> MomentDraft:
    draft = await repo.moment_draft(session, family.id, member.id)
    if draft is None or draft.child_id != child.id:
        draft = await repo.put_moment_draft(
            session, family.id, child.id, member.id, blank(family),
        )
    return draft


async def start(
    session: AsyncSession, family: Family, child: Child, member: Member,
    *, command_id: int | None = None, moment: Moment | None = None,
) -> tuple[MomentDraft, bool]:
    old = await context(session, family, child, member)
    if active(old) or (command_id is not None and old.command_message_id == command_id):
        return old, False
    forget_albums(old.id)
    data = blank(family)
    data["active"] = True
    data["filters"] = unpack(old).get("filters", {})
    if moment is not None:
        data.update({
            "target": moment.id, "version": moment.version, "date": moment.on_date.isoformat(),
            "title": moment.title, "description": moment.description, "topic": moment.topic,
            "photos": [{
                "key": str(p.id),
                "path": p.local_path, "caption": p.caption, "file_id": p.file_id,
                "file_unique_id": p.file_unique_id, "group": p.media_group_id, "error": "",
            } for p in moment.photos],
            "original_paths": [p.local_path for p in moment.photos],
        })
    draft = await repo.put_moment_draft(
        session, family.id, child.id, member.id, data, command_id,
    )
    return draft, True


def parse_date(value: str) -> date:
    value = value.strip()
    try:
        if "." in value:
            day, month, year = value.split(".")
            return date(int(year), int(month), int(day))
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Нужна дата ДД.ММ.ГГГГ, например 10.11.2026.") from exc


def valid_date(value: date, child: Child, family: Family) -> None:
    born = tu.to_local(child.born_at, family.tz).date()
    if value < born or value > tu.local_today(family.tz):
        raise ValueError("Дата должна быть от дня рождения ребёнка до сегодняшнего дня.")


def period(value: str) -> dict[str, str]:
    if value.strip() in {"-", "всё", "все"}:
        return {}
    parts = value.split()
    if not 1 <= len(parts) <= 2:
        raise ValueError("Укажите одну или две даты: 01.11.2026 10.11.2026; «-» — всё.")
    first, last = parse_date(parts[0]), parse_date(parts[-1])
    if first > last:
        raise ValueError("Начало периода должно быть не позже окончания.")
    return {"first": first.isoformat(), "last": last.isoformat()}


def selection(data: dict[str, Any]) -> dict[str, Any]:
    filters = data.get("filters", {})
    return {
        "topic": filters.get("topic", ""), "query": filters.get("query", ""),
        "first": date.fromisoformat(filters["first"]) if filters.get("first") else None,
        "last": date.fromisoformat(filters["last"]) if filters.get("last") else None,
    }


def input_kind(
    message: Message, data: dict[str, Any], *, draft_id: int | None = None,
) -> str | None:
    if message.reply_to_message is not None:
        kind = data["prompts"].get(str(message.reply_to_message.message_id))
        if kind:
            return str(kind)
    group = message.media_group_id
    if group and (group in data["groups"] or (draft_id, group) in _album_hints):
        return "photos"
    return None


def inspect_image(path: str) -> str:
    with Image.open(path) as image:
        suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image.format or "")
        if suffix is None or getattr(image, "is_animated", False):
            raise ValueError("Поддерживаются фотографии JPG, PNG и неподвижные WebP.")
        if image.width * image.height > 50_000_000:
            raise ValueError("Изображение слишком большое: максимум 50 мегапикселей.")
        image.load()
    return suffix


async def download(
    bot: Bot, session: AsyncSession, draft: MomentDraft, data: dict[str, Any], index: int,
) -> None:
    photo = data["photos"][index]
    relative = photo.get("path") or f"{draft.child_id}/{uuid4().hex}.upload"
    path = moment_files.resolve(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    photo["path"] = relative
    await repo.update_moment_draft(session, draft, data)
    try:
        remote = await bot.get_file(photo["file_id"])
        if remote.file_size and remote.file_size > MAX_PHOTO_BYTES:
            raise ValueError("Telegram даёт скачать фото до 20 МБ. Пришлите уменьшенную копию.")
        if not remote.file_path:
            raise ValueError("Telegram не отдал файл. Попробуйте повторить загрузку.")
        await bot.download_file(remote.file_path, destination=path, timeout=90)
        if path.stat().st_size > MAX_PHOTO_BYTES:
            raise ValueError("Telegram даёт скачать фото до 20 МБ.")
        suffix = await asyncio.to_thread(inspect_image, str(path))
        final = path.with_suffix(suffix)
        path.replace(final)
        photo.update(path=final.relative_to(moment_files.root().resolve()).as_posix(), error="")
    except Exception as exc:
        path.unlink(missing_ok=True)
        photo["path"] = ""
        photo["error"] = str(exc) if isinstance(exc, ValueError) else (
            "Не удалось скачать фото. Повторите загрузку или уберите его."
        )
        log.warning("Не скачано фото черновика %s (%s)", draft.id, type(exc).__name__)
    await repo.update_moment_draft(session, draft, data)


async def ingest(
    bot: Bot, session: AsyncSession, draft: MomentDraft, message: Message, kind: str,
    child: Child, family: Family,
) -> bool:
    data = unpack(draft)
    source = f"{message.chat.id}:{message.message_id}"
    if source in data["seen"]:
        return False
    text = (message.text or message.caption or "").strip()
    if kind in {"query", "period"}:
        if not message.text:
            raise ValueError("Ответьте текстом на приглашение бота.")
        filters = data.setdefault("filters", {})
        if kind == "query":
            filters["query"] = "" if text == "-" else text[:200]
        else:
            filters.pop("first", None)
            filters.pop("last", None)
            filters.update(period(text))
    else:
        if not active(draft):
            raise ValueError("Этот черновик уже закрыт. Нажмите «Добавить момент».")
        if kind == "date":
            on = parse_date(text)
            valid_date(on, child, family)
            data["date"] = on.isoformat()
        elif kind in {"title", "description"}:
            if not message.text:
                raise ValueError("Для этого поля нужен текст.")
            data[kind] = "" if text == "-" else text
        elif kind.startswith("caption:"):
            key = kind.split(":")[1]
            photo = next((p for p in data["photos"] if p["key"] == key), None)
            if photo is None:
                raise ValueError("Список фотографий изменился. Откройте подпись заново.")
            photo["caption"] = "" if text == "-" else text
        elif kind in {"content", "photos"}:
            document = message.document
            if message.photo or document:
                attachment = message.photo[-1] if message.photo else document
                if attachment is None:
                    raise ValueError("Пришлите фотографию.")
                if attachment.file_size and attachment.file_size > MAX_PHOTO_BYTES:
                    raise ValueError("Telegram даёт скачать фото до 20 МБ.")
                key = uuid4().hex[:12]
                data["photos"].append({
                    "key": key, "source_message_id": message.message_id,
                    "file_id": attachment.file_id, "file_unique_id": attachment.file_unique_id,
                    "caption": text, "path": "", "group": message.media_group_id,
                    "error": "Загрузка прервана. Нажмите «Повторить».",
                })
                if message.media_group_id and message.media_group_id not in data["groups"]:
                    data["groups"].append(message.media_group_id)
                if message.media_group_id:
                    positions = [i for i, p in enumerate(data["photos"])
                                 if p.get("group") == message.media_group_id]
                    photos = sorted((data["photos"][i] for i in positions),
                                    key=lambda p: p.get("source_message_id", 0))
                    for i, photo in zip(positions, photos, strict=True):
                        data["photos"][i] = photo
                data["seen"].append(source)
                await repo.update_moment_draft(session, draft, data)
                if message.media_group_id:
                    _album_hints.discard((draft.id, message.media_group_id))
                index = next(i for i, p in enumerate(data["photos"]) if p["key"] == key)
                await download(bot, session, draft, data, index)
                return True
            if not message.text:
                raise ValueError("Можно добавить текст или фотографии JPG, PNG, WebP.")
            data["description"] = "\n\n".join(filter(None, [data["description"], text]))
    data["seen"].append(source)
    await repo.update_moment_draft(session, draft, data)
    return True


async def save(
    session: AsyncSession, draft: MomentDraft, child: Child, family: Family,
) -> Moment:
    if draft.saved_id is not None:
        return await repo.save_moment(session, draft, {})
    data = unpack(draft)
    valid_date(date.fromisoformat(data["date"]), child, family)
    if data["topic"] not in TOPICS:
        raise ValueError("Выберите тему записи.")
    if not data["description"].strip() and not data["photos"]:
        raise ValueError("Добавьте описание или хотя бы одну фотографию.")
    if any(p.get("error") or not p["path"] or not moment_files.resolve(p["path"]).is_file()
           for p in data["photos"]):
        raise ValueError("Не все фото загружены. Повторите загрузку или уберите их.")
    moment = await repo.save_moment(session, draft, data)
    forget_albums(draft.id)
    kept = {p["path"] for p in data["photos"]}
    moment_files.remove([p for p in data["original_paths"] if p not in kept])
    closed = blank(family)
    closed["filters"] = data.get("filters", {})
    await repo.update_moment_draft(session, draft, closed)
    return moment


async def cancel(session: AsyncSession, family_id: int, author_id: int) -> bool:
    draft = await repo.moment_draft(session, family_id, author_id)
    if draft is None:
        return False
    data = unpack(draft)
    paths = [p["path"] for p in data["photos"] if p["path"] not in data["original_paths"]]
    await repo.drop_moment_draft(session, draft)
    forget_albums(draft.id)
    if draft.saved_id is None:
        moment_files.remove([p for p in paths if p])
    return bool(data.get("active") or data["prompts"])


def yesterday(family: Family) -> date:
    return tu.local_today(family.tz) - timedelta(days=1)
