"""Семейный альбом: явный ввод, сохранность, экспорт и восстановление."""

import asyncio
import io
import json
import sqlite3
import subprocess
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Document, Message, PhotoSize, Update, User
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import func, select

from bot.db import repo
from bot.db.models import Moment
from bot.handlers import moments as handler
from bot.services import backup, moment_export, moment_files, moments, stats
from bot.services import timeutil as tu
from tests.conftest import AUTHOR, CHAT, ME, NOW
from tools import forget, reset
from tools.stand import seed


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(tu, "now_utc", lambda: datetime(2026, 11, 10, 20))


def message(mid=1, text=None, *, reply=None, photo=False, document=False, group=None, user=AUTHOR):
    return Message(
        message_id=mid, date=NOW, chat=CHAT, from_user=user, text=text,
        reply_to_message=reply, media_group_id=group,
        photo=[PhotoSize(file_id=f"file-{mid}", file_unique_id=f"unique-{mid}",
                         width=160, height=90)] if photo else None,
        document=Document(file_id=f"file-{mid}", file_unique_id=f"unique-{mid}",
                          file_name="оригинал.png", mime_type="image/png") if document else None,
    )


@pytest.fixture
def photo_bot():
    buffer = io.BytesIO()
    Image.new("RGB", (160, 90), "#41987a").save(buffer, "PNG")
    content = buffer.getvalue()

    async def download_file(remote, destination, **kwargs):
        Path(destination).write_bytes(content)

    return SimpleNamespace(
        get_file=AsyncMock(return_value=SimpleNamespace(
            file_path="photo.png", file_size=len(content),
        )),
        download_file=AsyncMock(side_effect=download_file), content=content,
    )


async def note(session, family, child, mother, text="Первый переворот"):
    draft, _ = await moments.start(session, family, child, mother)
    await moments.ingest(None, session, draft, message(10, text), "content", child, family)
    return await moments.save(session, draft, child, family)


@pytest.mark.asyncio
async def test_text_moment_is_shared_but_not_part_of_medical_stats(session, family, child, mother):
    row = await note(session, family, child, mother)
    assert row.description == "Первый переворот" and row.topic == "other"
    assert row.on_date == date(2026, 11, 10)
    found = await repo.list_moments(session, child.id)
    assert [m.id for m in found] == [row.id]
    report = await stats.day(session, child, family.tz, date(2026, 11, 10))
    assert report.feeds == 0 and report.sleep_count == 0 and report.wet == 0


@pytest.mark.asyncio
async def test_empty_or_title_only_cannot_be_saved(session, family, child, mother):
    draft, _ = await moments.start(session, family, child, mother)
    await moments.ingest(None, session, draft, message(1, "Заголовок"), "title", child, family)
    with pytest.raises(ValueError, match="описание"):
        await moments.save(session, draft, child, family)


@pytest.mark.asyncio
async def test_saved_draft_survives_restart_and_duplicate_save(session_maker, session,
                                                              family, child, mother):
    draft, _ = await moments.start(session, family, child, mother)
    await moments.ingest(None, session, draft, message(1, "История"), "content", child, family)
    async with session_maker() as fresh:
        resumed = await repo.moment_draft(fresh, family.id, mother.id)
        assert resumed.id == draft.id and moments.active(resumed)
        first = await moments.save(fresh, resumed, child, family)
        second = await moments.save(fresh, resumed, child, family)
        assert first.id == second.id
        assert await fresh.scalar(select(func.count()).select_from(Moment)) == 1


@pytest.mark.asyncio
async def test_text_delivery_is_idempotent(session, family, child, mother):
    draft, _ = await moments.start(session, family, child, mother)
    for _ in range(2):
        await moments.ingest(None, session, draft, message(8, "Встала"), "content", child, family)
    assert moments.unpack(draft)["description"] == "Встала"


@pytest.mark.asyncio
async def test_date_validation_and_family_timezone(session, family, child, mother):
    family.tz = "Asia/Yekaterinburg"
    draft, _ = await moments.start(session, family, child, mother)
    assert moments.unpack(draft)["date"] == "2026-11-11"
    for text in ["31.02.2026", "01.01.2020", "12.11.2026"]:
        with pytest.raises(ValueError):
            await moments.ingest(None, session, draft, message(2, text), "date", child, family)
    await moments.ingest(None, session, draft, message(3, "09.11.2026"), "date", child, family)
    assert moments.unpack(draft)["date"] == "2026-11-09"


@pytest.mark.asyncio
async def test_multiple_albums_originals_captions_order_and_duplicate_photo(
    session, family, child, mother, photo_bot,
):
    draft, _ = await moments.start(session, family, child, mother)
    for mid, group in [(1, "album-a"), (2, "album-a"), (3, "album-b")]:
        msg = message(mid, photo=True, group=group)
        await moments.ingest(photo_bot, session, draft, msg, "photos", child, family)
        await moments.ingest(photo_bot, session, draft, msg, "photos", child, family)
    await moments.ingest(
        photo_bot, session, draft, message(4, document=True), "photos", child, family,
    )
    data = moments.unpack(draft)
    key = data["photos"][1]["key"]
    await moments.ingest(None, session, draft, message(5, "Первая ложка"), f"caption:{key}",
                         child, family)
    row = await moments.save(session, draft, child, family)
    assert len(row.photos) == 4
    assert [p.position for p in row.photos] == [0, 1, 2, 3]
    assert [p.media_group_id for p in row.photos] == ["album-a", "album-a", "album-b", None]
    assert row.photos[1].caption == "Первая ложка"
    assert moment_files.resolve(row.photos[-1].local_path).read_bytes() == photo_bot.content
    assert photo_bot.download_file.await_count == 4


@pytest.mark.asyncio
async def test_failed_download_can_retry_and_blocks_save(session, family, child, mother, photo_bot):
    photo_bot.download_file.side_effect = OSError("offline")
    draft, _ = await moments.start(session, family, child, mother)
    await moments.ingest(photo_bot, session, draft, message(1, photo=True), "photos", child, family)
    data = moments.unpack(draft)
    assert data["photos"][0]["error"]
    with pytest.raises(ValueError, match="фото"):
        await moments.save(session, draft, child, family)

    async def download_file(remote, destination, **kwargs):
        Path(destination).write_bytes(photo_bot.content)

    photo_bot.download_file.side_effect = download_file
    await moments.download(photo_bot, session, draft, data, 0)
    row = await moments.save(session, draft, child, family)
    assert len(row.photos) == 1


@pytest.mark.asyncio
async def test_non_image_file_is_rejected_without_publishing(
    session, family, child, mother, photo_bot,
):
    async def download_file(remote, destination, **kwargs):
        Path(destination).write_bytes(b"not an image")

    photo_bot.download_file.side_effect = download_file
    draft, _ = await moments.start(session, family, child, mother)
    await moments.ingest(
        photo_bot, session, draft, message(1, document=True), "photos", child, family,
    )
    assert moments.unpack(draft)["photos"][0]["error"]
    assert not list(moment_files.root().rglob("*.upload"))
    assert not await repo.list_moments(session, child.id)


@pytest.mark.asyncio
async def test_cancel_edit_preserves_saved_photos_and_removes_only_new_files(
    session, family, child, mother, photo_bot,
):
    draft, _ = await moments.start(session, family, child, mother)
    await moments.ingest(photo_bot, session, draft, message(1, photo=True), "photos", child, family)
    row = await moments.save(session, draft, child, family)
    original = moment_files.resolve(row.photos[0].local_path)
    edit, _ = await moments.start(session, family, child, mother, moment=row)
    await moments.ingest(photo_bot, session, edit, message(2, photo=True), "photos", child, family)
    new = moment_files.resolve(moments.unpack(edit)["photos"][1]["path"])
    assert await moments.cancel(session, family.id, mother.id)
    assert original.exists() and not new.exists()
    assert await repo.moment_draft(session, family.id, mother.id) is None


@pytest.mark.asyncio
async def test_two_parents_edit_conflict_does_not_overwrite(session, family, child, mother):
    father = await repo.get_or_create_member(session, family.id, 333, "Папа")
    row = await note(session, family, child, mother)
    first, _ = await moments.start(session, family, child, mother, moment=row)
    second, _ = await moments.start(session, family, child, father, moment=row)
    await moments.ingest(None, session, first, message(21, "Мамина правка"), "description",
                         child, family)
    await moments.ingest(None, session, second, message(22, "Папина правка"), "description",
                         child, family)
    await moments.save(session, second, child, family)
    with pytest.raises(ValueError, match="изменена"):
        await moments.save(session, first, child, family)
    assert (await repo.get_moment(session, child.id, row.id)).description == "Папина правка"


@pytest.mark.asyncio
async def test_search_dates_topics_and_no_400_day_limit(session, family, child, mother):
    child.born_at = datetime(2024, 1, 1)
    await session.commit()
    row = await note(session, family, child, mother, "Впервые попробовала КАШУ")
    draft, _ = await moments.start(session, family, child, mother, moment=row)
    data = moments.unpack(draft)
    data.update(date="2024-01-02", topic="food")
    await repo.update_moment_draft(session, draft, data)
    await moments.save(session, draft, child, family)
    found = await repo.list_moments(session, child.id, query="кашу", topic="food",
                                    first=date(2024, 1, 1), last=date(2026, 11, 10))
    assert [m.id for m in found] == [row.id]
    assert not await repo.list_moments(session, child.id, topic="trip")


async def feed(wired, msg):
    dp, bot, _ = wired
    await dp.feed_update(bot, Update(update_id=msg.message_id, message=msg))


async def tap(wired, action, row=0, arg="", user=AUTHOR):
    dp, bot, _ = wired
    call = CallbackQuery(id="tap", from_user=user, chat_instance="family",
                         data=f"mo:{action}:{row}:{arg}", message=message(999))
    await dp.feed_update(bot, Update(update_id=100, callback_query=call))


def bot_prompt():
    return Message(message_id=999, date=NOW, chat=CHAT, from_user=ME, text="Ответьте")


@pytest.mark.asyncio
async def test_dispatcher_ignores_chat_but_accepts_explicit_reply(wired, session, family, mother):
    await feed(wired, message(1, "/moment"))
    await feed(wired, message(2, "Обычная переписка"))
    draft = await repo.moment_draft(session, family.id, mother.id)
    assert moments.unpack(draft)["description"] == ""
    await feed(wired, message(3, "Сегодня перевернулась", reply=bot_prompt()))
    draft = await repo.moment_draft(session, family.id, mother.id)
    assert moments.unpack(draft)["description"] == "Сегодня перевернулась"
    await tap(wired, "save", draft.id)
    await tap(wired, "save", draft.id)
    assert await session.scalar(select(func.count()).select_from(Moment)) == 1


@pytest.mark.asyncio
async def test_reply_command_imports_existing_message_and_cancel_is_durable(
    wired, session, family, mother,
):
    await feed(wired, message(2, "/moment", reply=message(1, "Первые шаги")))
    draft = await repo.moment_draft(session, family.id, mother.id)
    assert moments.unpack(draft)["description"] == "Первые шаги"
    await feed(wired, message(3, "/cancel"))
    assert await repo.moment_draft(session, family.id, mother.id) is None


@pytest.mark.asyncio
async def test_medical_triage_does_not_swallow_moment(wired, session, family, mother):
    await feed(wired, message(1, "/moment"))
    await feed(wired, message(2, "У ребёнка судороги", reply=bot_prompt()))
    draft = await repo.moment_draft(session, family.id, mother.id)
    assert "судороги" in moments.unpack(draft)["description"]
    assert any("103" in text for text in wired[2].sent)


@pytest.mark.asyncio
async def test_other_parent_cannot_use_draft_but_can_edit_shared_moment(
    wired, session, family, mother, child,
):
    row = await note(session, family, child, mother)
    draft, _ = await moments.start(session, family, child, mother, moment=row)
    father = User(id=333, is_bot=False, first_name="Папа")
    await tap(wired, "cancel", draft.id, user=father)
    assert moments.active(await repo.moment_draft(session, family.id, mother.id))
    await tap(wired, "edit", row.id, user=father)
    member = await repo.get_or_create_member(session, family.id, father.id, father.first_name)
    his = await repo.moment_draft(session, family.id, member.id)
    assert moments.unpack(his)["target"] == row.id


@pytest.mark.asyncio
async def test_foreign_family_callbacks_cannot_view_edit_delete_or_export(wired, session):
    family = await repo.get_or_create_family(session, -2002, "Другая семья")
    member = await repo.get_or_create_member(session, family.id, 444, "Другой родитель")
    child = await repo.create_child(session, family.id, name="Боря", sex="m",
                                    born_at=datetime(2026, 9, 1), birth_weight_g=3500)
    row = await note(session, family, child, member, "Чужая история")
    for action in ["card", "edit", "show_photos", "confirm_delete"]:
        await tap(wired, action, row.id)
    assert all("Чужая история" not in text for text in wired[2].sent)
    assert await repo.get_moment(session, child.id, row.id) is not None


@pytest.mark.asyncio
async def test_two_parents_simultaneously_create_independent_drafts(wired, session, family, mother):
    father = await repo.get_or_create_member(session, family.id, 333, "Папа")
    await asyncio.gather(
        feed(wired, message(1, "/moment")),
        feed(wired, message(2, "/moment", user=User(id=333, is_bot=False, first_name="Папа"))),
    )
    first = await repo.moment_draft(session, family.id, mother.id)
    second = await repo.moment_draft(session, family.id, father.id)
    assert first.id != second.id and moments.active(first) and moments.active(second)


@pytest.mark.asyncio
async def test_albums_can_arrive_out_of_order_and_chat_albums_are_ignored(
    wired, session, family, mother, photo_bot, monkeypatch,
):
    bot = wired[1]
    monkeypatch.setattr(bot, "get_file", photo_bot.get_file)
    monkeypatch.setattr(bot, "download_file", photo_bot.download_file)
    await feed(wired, message(1, "/moment"))
    await asyncio.gather(
        feed(wired, message(3, photo=True, group="explicit-album")),
        feed(wired, message(2, photo=True, group="explicit-album", reply=bot_prompt())),
    )
    draft = await repo.moment_draft(session, family.id, mother.id)
    assert len(moments.unpack(draft)["photos"]) == 2
    await feed(wired, message(4, photo=True, group="ordinary-chat-album"))
    current = await repo.moment_draft(session, family.id, mother.id)
    assert len(moments.unpack(current)["photos"]) == 2


def export_rows(base, count=2):
    base.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(count):
        path = base / f"photo-{i}.png"
        Image.new("RGB", (80 + i * 50, 120), (40, 100 + i * 20, 60)).save(path)
        rows.append({
            "id": i + 1, "date": f"2026-11-{i + 1:02d}", "title": "Первый переворот",
            "description": "Длинная история с кириллицей. " * 300,
            "topic": "achievement", "photos": [{"path": path.name, "caption": "На коврике"}],
        })
    return rows


def test_pdf_and_zip_have_dates_cyrillic_full_text_and_original_bytes(tmp_path):
    base = tmp_path / "images"
    rows = export_rows(base)
    paths, warnings = moment_export.build(
        "Аня", date(2026, 9, 1), rows, tmp_path / "out", base=base,
    )
    assert not warnings
    pdf = next(p for p in paths if p.suffix == ".pdf")
    reader = PdfReader(pdf)
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "Аня" in text and "01.11.2026" in text and "На коврике" in text
    assert len(reader.pages) > 2 and "Первый переворот" in text
    archive = next(p for p in paths if p.suffix == ".zip")
    with zipfile.ZipFile(archive) as data:
        manifest = json.loads(data.read("moments.json"))
        assert manifest["moments"][0]["description"] == rows[0]["description"]
        original = (base / "photo-0.png").read_bytes()
        assert data.read(manifest["moments"][0]["photos"][0]["file"]) == original
        assert "Аня" in data.read("index.html").decode()


def test_export_missing_corrupt_images_and_html_escaping(tmp_path):
    base = tmp_path / "images"
    rows = export_rows(base)
    (base / "photo-0.png").unlink()
    (base / "photo-1.png").write_bytes(b"broken")
    rows[0]["description"] = '<script>alert("x")</script>'
    paths, warnings = moment_export.build(
        "Аня", date(2026, 9, 1), rows, tmp_path / "out", base=base,
    )
    assert len(warnings) == 2
    with zipfile.ZipFile(next(p for p in paths if p.suffix == ".zip")) as data:
        manifest = json.loads(data.read("moments.json"))
        assert all(m["photos"][0]["missing"] for m in manifest["moments"])
        html = data.read("index.html").decode()
        assert "<script>" not in html and "&lt;script&gt;" in html


def test_pdf_zip_split_into_independently_readable_numbered_parts(tmp_path):
    base = tmp_path / "images"
    rows = export_rows(base, count=4)
    # Фотографии с шумом гарантируют, что тест реально пересечёт лимит.
    for i in range(4):
        path = base / f"photo-{i}.jpg"
        Image.effect_noise((700, 700), 100).convert("RGB").save(path, quality=85)
        rows[i]["photos"][0]["path"] = path.name
    paths, warnings = moment_export.build("Аня", date(2026, 9, 1), rows, tmp_path / "out",
                                         base=base, limit=800_000)
    assert not warnings and len(paths) > 2
    assert all(p.stat().st_size <= 800_000 and "-of-" in p.name for p in paths)
    ids = []
    for path in paths:
        if path.suffix == ".pdf":
            assert len(PdfReader(path).pages) > 0
        else:
            with zipfile.ZipFile(path) as data:
                ids.extend(m["id"] for m in json.loads(data.read("moments.json"))["moments"])
                assert "index.html" in data.namelist()
    assert ids == [1, 2, 3, 4]


def test_backup_parts_restore_database_and_photo_bytes(tmp_path):
    db = tmp_path / "source.db"
    reset_tests = __import__("tests.test_reset", fromlist=["_build", "_fill"])
    reset_tests._build(db)
    reset_tests._fill(db)
    base = tmp_path / "moments"
    (base / "1").mkdir(parents=True)
    original = b"original-photo-bytes" * 8000
    (base / "1" / "photo.jpg").write_bytes(original)
    limit = max(db.stat().st_size, len(original)) + 70_000
    parts = moment_export.backup_parts(db, base, tmp_path / "bundle", limit=limit)
    assert len(parts) > 1
    restored = tmp_path / "restored"
    for part in parts:
        with zipfile.ZipFile(part) as archive:
            archive.extractall(restored)
    with sqlite3.connect(restored / "baby.db") as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT description FROM moments").fetchone() == ("Первый переворот",)
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert (restored / "moments/1/photo.jpg").read_bytes() == original


def test_forget_reset_and_stand_do_not_leave_or_leak_photos(tmp_path):
    from tests.test_reset import _build, _fill

    db = tmp_path / "source" / "baby.db"
    db.parent.mkdir()
    _build(db)
    _fill(db)
    _fill(db, family_id=2, chat_id=-777)
    for child_id in (1, 2):
        directory = db.parent / "moments" / str(child_id)
        directory.mkdir(parents=True)
        (directory / "photo.jpg").write_bytes(b"photo")
    dest = tmp_path / "stand" / "baby.db"
    dest.parent.mkdir()
    seed.prepare(db, dest)
    assert (db.parent / "moments/1/photo.jpg").exists(), "Стенд не трогает исходные файлы"
    assert not (dest.parent / "moments").exists()
    forget.forget(db, -777)
    assert (db.parent / "moments/1/photo.jpg").exists()
    assert not (db.parent / "moments/2").exists()
    reset.reset(db)
    assert not (db.parent / "moments/1").exists()


@pytest.mark.asyncio
async def test_backup_daily_rotates_complete_bundles(session_maker, monkeypatch):
    engine = session_maker.kw["bind"]
    monkeypatch.setattr(backup, "engine", engine)
    first = await backup.run_daily(datetime(2026, 11, 8))
    assert first and first.with_suffix(".album").is_dir()
    assert await backup.run_daily(datetime(2026, 11, 8)) is None
    second = await backup.run_daily(datetime(2026, 11, 9))
    assert second
    backup.rotate(1)
    assert not first.exists() and not first.with_suffix(".album").exists()
    assert second.exists() and second.with_suffix(".album").exists()


def test_photo_path_cannot_escape_storage(tmp_path):
    with pytest.raises(ValueError):
        moment_files.resolve("../other-family/photo.jpg", tmp_path)


@pytest.mark.asyncio
async def test_ui_edit_date_topic_title_description_and_confirmed_deletion(
    wired, session, family, child, mother,
):
    row = await note(session, family, child, mother)
    await tap(wired, "edit", row.id)
    draft = await repo.moment_draft(session, family.id, mother.id)
    await tap(wired, "title", draft.id)
    await feed(wired, message(31, "Первый переворот!", reply=bot_prompt()))
    await tap(wired, "description", draft.id)
    await feed(wired, message(32, "С живота на спину", reply=bot_prompt()))
    await tap(wired, "set_topic", draft.id, "achievement")
    await tap(wired, "day", draft.id, "yesterday")
    await tap(wired, "save", draft.id)
    row = await repo.get_moment(session, child.id, row.id)
    assert (row.title, row.description, row.topic, row.on_date) == (
        "Первый переворот!", "С живота на спину", "achievement", date(2026, 11, 9),
    )
    await tap(wired, "delete", row.id)
    assert await repo.get_moment(session, child.id, row.id) is not None
    confirmation = wired[2].markups[-1].inline_keyboard[0][0].callback_data
    await tap(wired, "confirm_delete", row.id, confirmation.split(":", 3)[3])
    assert await repo.get_moment(session, child.id, row.id) is None


@pytest.mark.asyncio
async def test_filters_and_search_persist_and_do_not_change_open_draft(
    wired, session, family, child, mother,
):
    await note(session, family, child, mother, "Попробовала кашу")
    draft, _ = await moments.start(session, family, child, mother)
    await tap(wired, "query")
    await feed(wired, message(33, "КАШУ", reply=bot_prompt()))
    await tap(wired, "period")
    await feed(wired, message(34, "01.11.2026 10.11.2026", reply=bot_prompt()))
    current = await repo.moment_draft(session, family.id, mother.id)
    data = moments.unpack(current)
    assert data["description"] == "" and current.id == draft.id
    assert data["filters"] == {"query": "КАШУ", "first": "2026-11-01", "last": "2026-11-10"}
    assert any("Поиск: КАШУ" in text for text in wired[2].sent)
    await tap(wired, "clear_filters")
    assert moments.unpack(await repo.moment_draft(session, family.id, mother.id))["filters"] == {}


@pytest.mark.asyncio
async def test_failed_photo_can_be_removed_before_saving_text(
    wired, session, family, child, mother, photo_bot, monkeypatch,
):
    photo_bot.get_file.side_effect = OSError("unavailable")
    draft, _ = await moments.start(session, family, child, mother)
    await moments.ingest(photo_bot, session, draft, message(1, photo=True), "photos", child, family)
    await moments.ingest(None, session, draft, message(2, "Семейная прогулка"), "content",
                         child, family)
    photo_key = moments.unpack(draft)["photos"][0]["key"]
    await tap(wired, "remove_photo", draft.id, photo_key)
    await tap(wired, "save", draft.id)
    found = await repo.list_moments(session, child.id)
    assert len(found) == 1 and found[0].description == "Семейная прогулка"
    assert found[0].photos == []


@pytest.mark.asyncio
async def test_cloud_download_limit_is_checked_before_network(
    session, family, child, mother, photo_bot,
):
    draft, _ = await moments.start(session, family, child, mother)
    msg = message(1, document=True)
    msg = msg.model_copy(update={"document": msg.document.model_copy(
        update={"file_size": moments.MAX_PHOTO_BYTES + 1},
    )})
    with pytest.raises(ValueError, match="20 МБ"):
        await moments.ingest(photo_bot, session, draft, msg, "photos", child, family)
    photo_bot.get_file.assert_not_awaited()


def test_previews_fix_exif_and_extreme_dimensions_without_changing_originals(tmp_path):
    path = tmp_path / "original.jpg"
    image = Image.new("RGB", (300, 100), "green")
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, exif=exif)
    original = path.read_bytes()
    preview = moment_export.preview_image(path)
    with Image.open(preview) as rendered:
        assert rendered.size == (100, 300)
    assert path.read_bytes() == original
    Image.new("RGB", (9000, 1), "green").save(path)
    with Image.open(moment_export.preview_image(path)) as rendered:
        assert max(rendered.size) <= 1800
        assert rendered.width / rendered.height <= 20


@pytest.mark.asyncio
async def test_preview_upload_uses_small_copy_not_original(photo_bot, tmp_path):
    path = tmp_path / "original.png"
    path.write_bytes(photo_bot.content)
    message = SimpleNamespace(answer_photo=AsyncMock(), answer=AsyncMock())
    assert await handler.send_photo(message, path)
    uploaded = message.answer_photo.call_args.args[0]
    assert uploaded.filename == "moment.jpg"
    with Image.open(io.BytesIO(uploaded.data)) as image:
        assert image.format == "JPEG"
    assert path.read_bytes() == photo_bot.content


def test_single_moment_photos_split_without_losing_files(tmp_path):
    base = tmp_path / "images"
    rows = export_rows(base, 4)
    for i in range(4):
        path = base / f"photo-{i}.jpg"
        Image.effect_noise((700, 700), 100).convert("RGB").save(path, quality=85)
        rows[i]["photos"][0].update(path=path.name, caption=f"Снимок {i}")
    single = {**rows[0], "photos": [r["photos"][0] for r in rows]}
    paths, _ = moment_export.build("Аня", date(2026, 9, 1), [single], tmp_path / "out",
                                  base=base, limit=800_000)
    photos = []
    for path in paths:
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                data = json.loads(archive.read("moments.json"))
                photos.extend(p["caption"] for m in data["moments"] for p in m["photos"])
    assert photos == [f"Снимок {i}" for i in range(4)]


def test_oversized_backup_files_are_chunked_and_restored_with_checksum(tmp_path):
    from tests.test_reset import _build, _fill

    db = tmp_path / "source.db"
    _build(db)
    _fill(db)
    base = tmp_path / "photos"
    base.mkdir()
    (base / "large.jpg").write_bytes(b"original" * 35000)
    parts = moment_export.backup_parts(db, base, tmp_path / "backup", limit=160_000)
    restored = tmp_path / "restore"
    for part in parts:
        assert part.stat().st_size <= 160_000
        with zipfile.ZipFile(part) as archive:
            archive.extractall(restored)
    result = subprocess.run([sys.executable, "restore.py"], cwd=restored, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert (restored / "baby.db").read_bytes() == db.read_bytes()
    assert (restored / "moments/large.jpg").read_bytes() == (base / "large.jpg").read_bytes()
    result = subprocess.run([sys.executable, "restore.py"], cwd=restored, capture_output=True)
    assert result.returncode == 0, "Повторный запуск не портит уже восстановленные файлы"


@pytest.mark.asyncio
async def test_full_text_can_be_read_in_chat_without_truncation(
    wired, session, family, child, mother,
):
    text = "Памятный день, <история>! " * 300
    row = await note(session, family, child, mother, text)
    wired[2].sent.clear()
    await tap(wired, "text", row.id)
    displayed = "".join(wired[2].sent)
    assert displayed == text.strip().replace("<", "&lt;").replace(">", "&gt;")


@pytest.mark.asyncio
async def test_deleting_moment_removes_edit_drafts_and_their_new_photos(
    session, family, child, mother, photo_bot,
):
    row = await note(session, family, child, mother)
    closed = await repo.moment_draft(session, family.id, mother.id)
    assert moments.unpack(closed)["description"] == "", "Закрытый черновик не дублирует историю"
    father = await repo.get_or_create_member(session, family.id, 333, "Папа")
    draft, _ = await moments.start(session, family, child, father, moment=row)
    await moments.ingest(
        photo_bot, session, draft, message(50, photo=True), "photos", child, family,
    )
    paths = await repo.delete_moment(session, row)
    moment_files.remove(paths)
    assert await repo.moment_draft(session, family.id, father.id) is None
    assert await repo.moment_draft(session, family.id, mother.id) is None
    assert not any(p.is_file() for p in moment_files.root().rglob("*"))


@pytest.mark.asyncio
async def test_album_is_not_lost_while_storage_is_busy(
    wired, session, family, mother, photo_bot, monkeypatch,
):
    bot = wired[1]
    monkeypatch.setattr(bot, "get_file", photo_bot.get_file)
    monkeypatch.setattr(bot, "download_file", photo_bot.download_file)
    await feed(wired, message(1, "/moment"))
    async with moment_files.storage_lock:
        first = asyncio.create_task(feed(wired, message(
            2, photo=True, group="queued-album", reply=bot_prompt(),
        )))
        await asyncio.sleep(0.2)
        second = asyncio.create_task(feed(wired, message(3, photo=True, group="queued-album")))
        await asyncio.sleep(1.4)
    await asyncio.gather(first, second)
    draft = await repo.moment_draft(session, family.id, mother.id)
    assert len(moments.unpack(draft)["photos"]) == 2


@pytest.mark.asyncio
async def test_stale_delete_confirmation_preserves_other_parents_edit(
    wired, session, family, child, mother,
):
    row = await note(session, family, child, mother)
    await tap(wired, "delete", row.id)
    confirmation = wired[2].markups[-1].inline_keyboard[0][0].callback_data
    version = confirmation.split(":", 3)[3]
    father = await repo.get_or_create_member(session, family.id, 333, "Папа")
    draft, _ = await moments.start(session, family, child, father, moment=row)
    await moments.ingest(None, session, draft, message(5, "Новое воспоминание папы"),
                         "description", child, family)
    await moments.save(session, draft, child, family)
    await tap(wired, "confirm_delete", row.id, version)
    row = await repo.get_moment(session, child.id, row.id)
    assert row is not None and row.description == "Новое воспоминание папы"


@pytest.mark.asyncio
@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
async def test_cancel_after_interrupted_photo_commit_removes_download(
    session, session_maker, family, child, mother, photo_bot, monkeypatch, image_format,
):
    buffer = io.BytesIO()
    Image.new("RGB", (80, 120), "green").save(buffer, image_format)

    async def download_file(remote, destination, **kwargs):
        Path(destination).write_bytes(buffer.getvalue())

    photo_bot.download_file.side_effect = download_file
    draft, _ = await moments.start(session, family, child, mother)
    original_update = repo.update_moment_draft

    async def fail_final_update(session, draft, data):
        if data["photos"] and not data["photos"][0]["error"]:
            raise OSError("Прерывание после переименования файла перед коммитом")
        await original_update(session, draft, data)

    with monkeypatch.context() as patch:
        patch.setattr(repo, "update_moment_draft", fail_final_update)
        with pytest.raises(OSError):
            await moments.ingest(photo_bot, session, draft, message(1, photo=True),
                                 "photos", child, family)
    # Новый сеанс читает только то, что успело попасть в БД перед прерыванием.
    async with session_maker() as restarted:
        pending = await repo.moment_draft(restarted, family.id, mother.id)
        assert moments.unpack(pending)["photos"][0]["path"].endswith(".upload")
        await moments.cancel(restarted, family.id, mother.id)
    assert not any(p.is_file() for p in moment_files.root().rglob("*"))


@pytest.mark.asyncio
async def test_queued_album_finishes_before_save(
    wired, session, family, child, mother, photo_bot, monkeypatch,
):
    bot = wired[1]
    monkeypatch.setattr(bot, "get_file", photo_bot.get_file)
    monkeypatch.setattr(bot, "download_file", photo_bot.download_file)
    await feed(wired, message(1, "/moment"))
    draft = await repo.moment_draft(session, family.id, mother.id)
    async with moment_files.storage_lock:
        first = asyncio.create_task(feed(wired, message(
            2, photo=True, group="save-album", reply=bot_prompt(),
        )))
        await asyncio.sleep(0.2)
        second = asyncio.create_task(feed(wired, message(3, photo=True, group="save-album")))
        await asyncio.sleep(0.2)
        saving = asyncio.create_task(tap(wired, "save", draft.id))
        await asyncio.sleep(1.4)
    await asyncio.gather(first, second, saving)
    found = await repo.list_moments(session, child.id)
    assert len(found) == 1
    assert [p.file_id for p in found[0].photos] == ["file-2", "file-3"]


@pytest.mark.asyncio
async def test_legacy_delete_confirmation_requires_new_confirmation(
    wired, session, family, child, mother,
):
    row = await note(session, family, child, mother)
    await tap(wired, "confirm_delete", row.id)
    assert await repo.get_moment(session, child.id, row.id) is not None
    assert "подтвердите удаление заново" in wired[2].sent[-1]
    await tap(wired, "delete", row.id)
    confirmation = wired[2].markups[-1].inline_keyboard[0][0].callback_data
    await tap(wired, "confirm_delete", row.id, confirmation.split(":", 3)[3])
    assert await repo.get_moment(session, child.id, row.id) is None
