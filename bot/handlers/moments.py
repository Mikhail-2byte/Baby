"""Явный ввод воспоминаний и общий семейный альбом."""

import asyncio
import html
import logging
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ForceReply,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.db import repo
from bot.db.models import Child, Family, Member, Moment, MomentDraft
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.handlers import alarm
from bot.services import moment_export, moment_files, moments, pending
from bot.services import timeutil as tu

log = logging.getLogger(__name__)
router = Router(name="moments")
PAGE_SIZE = 5


def button(label: str, action: str, row: int = 0, arg: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=f"mo:{action}:{row}:{arg}")


def keyboard(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def home() -> InlineKeyboardMarkup:
    return keyboard(
        [button("➕ Добавить момент", "new"), button("📚 Посмотреть", "list")],
        [button("📦 Выгрузить", "export")],
    )


def summary(data: dict[str, Any]) -> str:
    escape = html.escape
    title = data['title'] or 'Новый момент'
    lines = [f"<b>{escape(title[:240])}{'…' if len(title) > 240 else ''}</b>",
             f"{date.fromisoformat(data['date']):%d.%m.%Y} · {moments.TOPICS[data['topic']]}"]
    if data["description"]:
        lines.append(escape(data["description"][:1700]))
        if len(data["description"]) > 1700:
            lines.append("… Полный текст сохранится и попадёт в выгрузку.")
    lines.append(f"Фотографий: {len(data['photos'])}")
    failed = [str(i + 1) for i, p in enumerate(data["photos"]) if p.get("error")]
    if failed:
        lines.append("⚠️ Не загружены фото: " + ", ".join(failed[:20])
                     + (f" и ещё {len(failed) - 20}" if len(failed) > 20 else ""))
    return "\n\n".join(lines)


async def preview(message: Message, draft: MomentDraft) -> None:
    if draft.saved_id:
        await message.answer("Этот момент уже сохранён.", reply_markup=keyboard(
            [button("Открыть запись", "card", draft.saved_id)],
        ))
        return
    data = moments.unpack(draft)
    i = draft.id
    await message.answer(summary(data), reply_markup=keyboard(
        [button("✏️ Заголовок", "title", i), button("📝 Описание", "description", i)],
        [button("📅 Дата", "date", i), button("🏷 Тема", "topic", i)],
        [button("📷 Добавить фото", "photos", i), button("Фото и подписи", "photo_list", i)],
        [button("✅ Сохранить", "save", i), button("Отмена", "cancel", i)],
    ))


async def prompt(
    message: Message, session: AsyncSession, draft: MomentDraft, kind: str, user_id: int,
) -> None:
    labels = {
        "content": ("Ответьте на это сообщение описанием или фотографиями. "
                    "Затем нажмите «Сохранить»."),
        "photos": "Ответьте на это сообщение фотографией, альбомом или изображением-файлом.",
        "title": "Ответьте заголовком; «-» — очистить.",
        "description": "Ответьте новым описанием целиком; «-» — очистить.",
        "date": "Ответьте датой события: ДД.ММ.ГГГГ.",
        "query": "Ответьте словом или фразой для поиска; «-» — убрать поиск.",
        "period": "Ответьте одной или двумя датами: 01.11.2026 10.11.2026; «-» — весь период.",
    }
    text = labels.get(kind, "Ответьте подписью к этому снимку; «-» — очистить.")
    target = await message.answer(
        f'<a href="tg://user?id={user_id}">Для вас</a>: {text}',
        reply_markup=ForceReply(selective=True),
    )
    data = moments.unpack(draft)
    data["prompts"][str(target.message_id)] = kind
    await repo.update_moment_draft(session, draft, data)


async def triage(message: Message, source: Message, child: Child, family: Family) -> None:
    hits, level = alarm.assess(source.text or source.caption or "", child)
    if hits:
        await alarm.respond(message, hits, level, family)


async def card(message: Message, moment: Moment) -> None:
    rows = [
        [button("📷 Фотографии", "show_photos", moment.id)],
        [button("✏️ Изменить", "edit", moment.id), button("🗑 Удалить", "delete", moment.id)],
        [button("К альбому", "list")],
    ]
    if len(moment.title) > 240 or len(moment.description) > 1700:
        rows.insert(0, [button("📝 Весь текст", "text", moment.id)])
    await message.answer(summary(moment_export.serialize(moment)), reply_markup=keyboard(*rows))


async def send_photo(message: Message, path: Path) -> bool:
    try:
        image = await asyncio.to_thread(moment_export.preview_image, path)
    except (OSError, ValueError):
        await message.answer("Это фото отсутствует или повреждено. Остальная запись сохранена.")
        return False
    await message.answer_photo(BufferedInputFile(image.getvalue(), filename="moment.jpg"))
    return True


async def listing(
    message: Message, session: AsyncSession, child: Child, draft: MomentDraft, page: int = 0,
) -> None:
    data = moments.unpack(draft)
    found = await repo.list_moments(session, child.id, **moments.selection(data),
                                    offset=page * PAGE_SIZE, limit=PAGE_SIZE + 1)
    rows = [[button(
        f"{m.on_date:%d.%m.%Y} · {(m.title or m.description or moments.TOPICS[m.topic])[:45]}",
        "card", m.id,
    )] for m in found[:PAGE_SIZE]]
    navigation = []
    if page:
        navigation.append(button("←", "list", page - 1))
    if len(found) > PAGE_SIZE:
        navigation.append(button("→", "list", page + 1))
    if navigation:
        rows.append(navigation)
    rows.extend([
        [button("🏷 Тема", "filter_topic"), button("📅 Период", "period")],
        [button("🔎 Поиск", "query"), button("Сбросить фильтры", "clear_filters")],
        [button("➕ Добавить", "new"), button("📦 Выгрузить выборку", "export")],
    ])
    selected = data.get("filters", {})
    label = "📖 Моменты" + (" · применены фильтры" if any(selected.values()) else "")
    if not found:
        label += "\nЗаписей пока нет или ничего не найдено."
    if selected.get("query"):
        label += "\nПоиск: " + html.escape(selected["query"])
    if selected.get("topic"):
        label += "\nТема: " + moments.TOPICS[selected["topic"]]
    if selected.get("first"):
        label += f"\nПериод: {selected['first']} — {selected['last']}"
    await message.answer(label, reply_markup=keyboard(*rows))


@router.message(IN_GROUP, Command("moments"))
async def menu(
    message: Message, session: AsyncSession, family: Family, child: Child | None, member: Member,
) -> None:
    if child is None:
        await message.answer(texts.NO_CHILD)
        return
    async with moment_files.storage_lock:
        draft = await moments.context(session, family, child, member)
        await message.answer("📖 Моменты — семейный альбом с датами, историями и фотографиями.",
                             reply_markup=home())
        if moments.active(draft):
            await message.answer("Есть незавершённый черновик. Можно продолжить или отменить его.")
            await preview(message, draft)


@router.message(IN_GROUP, Command("moment"))
async def add(
    message: Message, bot: Bot, session: AsyncSession,
    family: Family, child: Child | None, member: Member,
) -> None:
    if child is None:
        await message.answer(texts.NO_CHILD)
        return
    async with moment_files.storage_lock:
        pending.forget(family.chat_id, member.tg_user_id)
        draft, fresh = await moments.start(
            session, family, child, member, command_id=message.message_id,
        )
        if fresh and message.reply_to_message is not None:
            source = message.reply_to_message
            await triage(message, source, child, family)
            try:
                await moments.ingest(bot, session, draft, source, "content", child, family)
            except ValueError as exc:
                await message.answer(str(exc))
            if source.media_group_id:
                await message.answer("Добавлен выбранный снимок. Остальные фото старого альбома "
                                     "пришлите отдельно ответом на приглашение.")
        elif not fresh and moments.active(draft):
            await message.answer("Продолжаем ваш незавершённый черновик.")
        await preview(message, draft)
        if moments.active(draft):
            await prompt(message, session, draft, "content", member.tg_user_id)


class MomentReply(BaseFilter):
    async def __call__(
        self, message: Message, bot: Bot, session: AsyncSession,
        family: Family | None = None, member: Member | None = None,
    ) -> bool:
        if family is None or member is None or (message.text or "").startswith("/"):
            return False
        draft = await repo.moment_draft(session, family.id, member.id)
        if draft is None:
            return False
        reply = message.reply_to_message
        if reply is not None and (reply.from_user is None or reply.from_user.id != bot.id):
            return False
        data = moments.unpack(draft)
        kind = moments.input_kind(message, data, draft_id=draft.id)
        # Telegram не сообщает размер media_group; соседний снимок может попасть
        # в фильтр раньше первого, отвечающего на приглашение. Ждём регистрацию
        # именно этой группы, не принимая произвольные фото из чата.
        if kind is None and message.media_group_id and moments.active(draft):
            for _ in range(10):
                await asyncio.sleep(0.1)
                draft = await repo.moment_draft(session, family.id, member.id)
                if draft is None:
                    return False
                data = moments.unpack(draft)
                kind = moments.input_kind(message, data, draft_id=draft.id)
                if kind is not None:
                    break
        if (kind in {"content", "photos"} and message.media_group_id
                and message.media_group_id not in data["groups"] and moments.active(draft)):
            moments.remember_album(draft.id, message.media_group_id)
        return bool(kind and (moments.active(draft) or kind in {"query", "period"}))


@router.message(IN_GROUP, MomentReply())
async def receive(
    message: Message, bot: Bot, session: AsyncSession,
    family: Family, child: Child | None, member: Member,
) -> None:
    if child is None:
        return
    async with moment_files.storage_lock:
        draft = await repo.moment_draft(session, family.id, member.id)
        if draft is None or draft.child_id != child.id:
            return
        kind = moments.input_kind(message, moments.unpack(draft), draft_id=draft.id)
        if kind is None:
            return
        known_group = message.media_group_id in moments.unpack(draft)["groups"]
        await triage(message, message, child, family)
        try:
            changed = await moments.ingest(bot, session, draft, message, kind, child, family)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        if changed:
            if kind in {"query", "period"}:
                await listing(message, session, child, draft)
            elif not message.media_group_id:
                await preview(message, draft)
            elif not known_group:
                await message.answer("Добавляю альбом в черновик.", reply_markup=keyboard(
                    [button("Предпросмотр / Сохранить", "preview", draft.id)],
                ))


async def send_export(
    message: Message, session: AsyncSession, child: Child, family: Family, draft: MomentDraft,
) -> None:
    found = await repo.list_moments(session, child.id, **moments.selection(moments.unpack(draft)),
                                    ascending=True)
    if not found:
        await message.answer("Нет записей для выгрузки. Измените фильтры или добавьте момент.")
        return
    await message.answer("Собираю PDF-альбом и архив с фотографиями…")
    rows = [moment_export.serialize(m) for m in found]
    born = tu.to_local(child.born_at, family.tz).date()
    with tempfile.TemporaryDirectory(prefix="baby-moments-") as temp:
        paths, warnings = await asyncio.to_thread(moment_export.build, child.name, born, rows,
                                                  Path(temp))
        for path in paths:
            await message.answer_document(FSInputFile(path), caption=path.name)
        if warnings:
            report = Path(temp) / "missing-photos.txt"
            report.write_text("Недоступные фото:\n" + "\n".join(warnings), encoding="utf-8")
            await message.answer_document(FSInputFile(report), caption="Список недоступных фото")


@router.callback_query(IN_GROUP_CB, F.data.startswith("mo:"))
async def callback(
    call: CallbackQuery, bot: Bot, session: AsyncSession, family: Family,
    child: Child | None, member: Member, state: FSMContext,
) -> None:
    await call.answer()
    if not isinstance(call.message, Message):
        return
    message = call.message
    if child is None:
        await message.answer(texts.NO_CHILD)
        return
    try:
        _, action, raw_id, arg = (call.data or "").split(":", 3)
        row_id = int(raw_id)
        if row_id < 0:
            raise ValueError("Недопустимая кнопка.")
    except ValueError:
        return
    if action == "save":
        # Даём снимкам одной отправки дойти до обработчика перед фиксацией.
        await asyncio.sleep(1)
    async with moment_files.storage_lock:
        try:
            await dispatch(action, row_id, arg, message, bot, session, family, child, member, state)
        except ValueError as exc:
            await message.answer(str(exc))
        except Exception:
            await session.rollback()
            log.exception("Ошибка работы с семейным альбомом")
            await message.answer(
                "Не удалось завершить действие. Откройте /moments и попробуйте ещё раз."
            )


async def dispatch(
    action: str, row_id: int, arg: str, message: Message, bot: Bot, session: AsyncSession,
    family: Family, child: Child, member: Member, state: FSMContext,
) -> None:
    draft = await moments.context(session, family, child, member)
    if action in {"card", "text", "edit", "delete", "confirm_delete", "show_photos"}:
        moment = await repo.get_moment(session, child.id, row_id)
        if moment is None:
            raise ValueError("Запись уже удалена или недоступна в этой семье.")
        if action == "card":
            await card(message, moment)
        elif action == "text":
            text = "\n\n".join(filter(None, [moment.title, moment.description])) or "Без описания"
            for offset in range(0, len(text), 3500):
                await message.answer(html.escape(text[offset:offset + 3500]))
        elif action == "delete":
            await message.answer(
                "Удалить этот момент вместе с фотографиями?", reply_markup=keyboard(
                    [button("Да, удалить", "confirm_delete", row_id, str(moment.version)),
                     button("Оставить", "card", row_id)],
                ),
            )
        elif action == "confirm_delete":
            if arg != str(moment.version):
                raise ValueError("Запись изменилась. Откройте её и подтвердите удаление заново.")
            paths = await repo.delete_moment(session, moment)
            moment_files.remove(paths)
            await message.answer("Момент удалён.", reply_markup=home())
        elif action == "show_photos":
            if not moment.photos:
                await message.answer("В этой записи пока нет фотографий.")
            for saved_photo in moment.photos:
                path = moment_files.resolve(saved_photo.local_path)
                if path.is_file():
                    await send_photo(message, path)
                    if saved_photo.caption:
                        await message.answer(html.escape(saved_photo.caption))
                else:
                    await message.answer("Это фото недоступно на диске бота.")
        else:
            await state.clear()
            pending.forget(family.chat_id, member.tg_user_id)
            draft, fresh = await moments.start(session, family, child, member, moment=moment)
            if not fresh:
                await message.answer("Сначала завершите или отмените свой текущий черновик.")
            await preview(message, draft)
        return
    if action == "home":
        await message.answer("📖 Моменты", reply_markup=home())
        if moments.active(draft):
            await message.answer("Можно продолжить незавершённый черновик:")
            await preview(message, draft)
    elif action == "new":
        await state.clear()
        pending.forget(family.chat_id, member.tg_user_id)
        draft, _ = await moments.start(session, family, child, member)
        await preview(message, draft)
        await prompt(message, session, draft, "content", member.tg_user_id)
    elif action in {"list", "clear_filters", "filter_topic", "filter_set", "query", "period",
                    "export", "export_go"}:
        data = moments.unpack(draft)
        if action == "filter_topic":
            rows = [[button(label, "filter_set", arg=key)] for key, label in moments.TOPICS.items()]
            await message.answer("Тема для просмотра и выгрузки:", reply_markup=keyboard(
                *rows, [button("Все темы", "filter_set")],
            ))
        elif action in {"query", "period"}:
            await prompt(message, session, draft, action, member.tg_user_id)
        elif action == "export":
            await message.answer("Выгрузить альбом с текущими фильтрами? "
                                 "Для всего альбома сбросьте фильтры.", reply_markup=keyboard(
                [button("PDF и ZIP", "export_go"), button("Настроить выборку", "list")],
            ))
        elif action == "export_go":
            await send_export(message, session, child, family, draft)
        else:
            if action == "clear_filters":
                data["filters"] = {}
            elif action == "filter_set":
                if arg and arg not in moments.TOPICS:
                    raise ValueError("Неизвестная тема.")
                data.setdefault("filters", {})["topic"] = arg
            await repo.update_moment_draft(session, draft, data)
            await listing(message, session, child, draft, row_id if action == "list" else 0)
    else:
        if draft.id != row_id:
            raise ValueError("Это кнопка старого или чужого черновика. Откройте /moments.")
        if action == "save" and draft.saved_id is not None:
            await preview(message, draft)
            return
        if not moments.active(draft):
            raise ValueError("Этот черновик уже закрыт. Откройте /moments.")
        data = moments.unpack(draft)
        if action in {"title", "description", "photos"}:
            await prompt(message, session, draft, action, member.tg_user_id)
        elif action == "date":
            await message.answer("Когда это было?", reply_markup=keyboard(
                [button("Сегодня", "day", row_id, "today"),
                 button("Вчера", "day", row_id, "yesterday")],
                [button("Другая дата", "date_input", row_id)],
            ))
        elif action == "date_input":
            await prompt(message, session, draft, "date", member.tg_user_id)
        elif action == "day":
            on = moments.yesterday(family) if arg == "yesterday" else tu.local_today(family.tz)
            moments.valid_date(on, child, family)
            data["date"] = on.isoformat()
            await repo.update_moment_draft(session, draft, data)
            await preview(message, draft)
        elif action == "topic":
            await message.answer("Тема момента:", reply_markup=keyboard(*[
                [button(label, "set_topic", row_id, key)] for key, label in moments.TOPICS.items()
            ]))
        elif action == "set_topic":
            if arg not in moments.TOPICS:
                raise ValueError("Неизвестная тема.")
            data["topic"] = arg
            await repo.update_moment_draft(session, draft, data)
            await preview(message, draft)
        elif action == "photo_list":
            if not data["photos"]:
                await message.answer("Пока нет фотографий. Нажмите «Добавить фото».")
            for i, photo in enumerate(data["photos"]):
                path = moment_files.resolve(photo["path"]) if photo["path"] else None
                shown = False
                if path and path.is_file():
                    shown = await send_photo(message, path)
                if not shown and not photo.get("error"):
                    photo["error"] = "Фото отсутствует или повреждено. Повторите загрузку."
                rows = [[button("Подпись", "caption", row_id, photo["key"]),
                         button("Убрать", "remove_photo", row_id, photo["key"])]]
                if photo.get("error"):
                    rows.append([button("Повторить загрузку", "retry", row_id, photo["key"])])
                text = f"Фото {i + 1}\n" + (photo.get("error") or photo["caption"] or "Без подписи")
                await message.answer(html.escape(text[:3500]), reply_markup=keyboard(*rows))
            await repo.update_moment_draft(session, draft, data)
            await preview(message, draft)
        elif action in {"caption", "remove_photo", "retry"}:
            photo = next((p for p in data["photos"] if p["key"] == arg), None)
            if photo is None:
                raise ValueError("Это фото уже убрано из черновика.")
            if action == "caption":
                await prompt(message, session, draft, f"caption:{arg}", member.tg_user_id)
            elif action == "retry":
                if not photo.get("error"):
                    raise ValueError("Это фото уже загружено.")
                await moments.download(bot, session, draft, data, data["photos"].index(photo))
                await preview(message, draft)
            else:
                data["photos"].remove(photo)
                await repo.update_moment_draft(session, draft, data)
                if photo["path"] and photo["path"] not in data["original_paths"]:
                    moment_files.remove([photo["path"]])
                await preview(message, draft)
        elif action == "preview":
            await preview(message, draft)
        elif action == "save":
            saved = await moments.save(session, draft, child, family)
            await message.answer("✅ Момент сохранён.")
            await card(message, saved)
        elif action == "cancel":
            await moments.cancel(session, family.id, member.id)
            await message.answer("Черновик отменён.", reply_markup=home())
