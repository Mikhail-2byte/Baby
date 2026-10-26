"""Настройки мягких напоминаний и обратимые правки без новых постоянных кнопок."""

import html
import json
from datetime import datetime

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.db import repo
from bot.db.models import (
    TIMED_KINDS,
    WEIGHT,
    Child,
    Event,
    Family,
    Measurement,
    Member,
    RecordRevision,
)
from bot.filters import IN_GROUP_CB
from bot.keyboards import HistoryCB, NudgeCB
from bot.services import panel, records, revisions, tracker_guard
from bot.services import timeutil as tu

router = Router(name="journal")
CHOICES = {"feed": (0, 30, 45, 60, 90), "sleep": (0, 60, 120, 180, 240)}


def shown(value: object, key: str, tz: str) -> str:
    if key in revisions.TIME_FIELDS and isinstance(value, str):
        return tu.to_local(datetime.fromisoformat(value), tz).strftime("%d.%m %H:%M")
    return "—" if value is None else str(value)


def settings_screen(family: Family) -> tuple[str, InlineKeyboardMarkup]:
    def label(minutes: int) -> str:
        return f"через {minutes} мин" if minutes else "выключено"

    text = (
        "🕐 Напоминания о незавершённом таймере\n\n"
        f"Кормление: {label(family.feed_nudge_minutes)}\n"
        f"Сон: {label(family.sleep_nudge_minutes)}\n\n"
        "Один вопрос на запись. Таймер сам не завершается. "
        "Интервал — ваше удобство, не медицинская норма."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤱 Кормление", callback_data=NudgeCB(kind="feed").pack()
                ),
                InlineKeyboardButton(text="😴 Сон", callback_data=NudgeCB(kind="sleep").pack()),
            ]
        ]
    )
    return text, markup


async def show_settings(message: Message, family: Family) -> None:
    text, markup = settings_screen(family)
    await message.answer(text, reply_markup=markup)


@router.callback_query(IN_GROUP_CB, NudgeCB.filter())
async def settings_tap(
    call: CallbackQuery, callback_data: NudgeCB, session: AsyncSession, family: Family
) -> None:
    data = callback_data
    if not isinstance(call.message, Message):
        await call.answer()
        return
    if data.kind == "menu":
        await call.answer()
        await show_settings(call.message, family)
        return
    if data.kind not in CHOICES:
        await call.answer()
        return
    if data.minutes == -1:
        buttons = [
            InlineKeyboardButton(
                text="Выключить" if minutes == 0 else f"Через {minutes} мин",
                callback_data=NudgeCB(kind=data.kind, minutes=minutes).pack(),
            )
            for minutes in CHOICES[data.kind]
        ]
        markup = InlineKeyboardMarkup(
            inline_keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        )
        await call.answer()
        await call.message.edit_text("Когда один раз напомнить о таймере?", reply_markup=markup)
        return
    if data.minutes not in CHOICES[data.kind]:
        await call.answer("Выберите предложенный интервал", show_alert=True)
        return
    setattr(
        family, "feed_nudge_minutes" if data.kind == "feed" else "sleep_nudge_minutes", data.minutes
    )
    await session.commit()
    await call.answer("Сохранил для этого чата")
    text, markup = settings_screen(family)
    await call.message.edit_text(text, reply_markup=markup)


async def row_for(
    session: AsyncSession, child: Child, data: HistoryCB
) -> Event | Measurement | None:
    row: Event | Measurement | None
    if data.what == "m":
        row = await session.get(Measurement, data.row_id, populate_existing=True)
    elif data.what == "e":
        row = await session.get(Event, data.row_id, populate_existing=True)
    else:
        return None
    return row if row is not None and row.child_id == child.id else None


@router.callback_query(IN_GROUP_CB, HistoryCB.filter())
async def history_tap(
    call: CallbackQuery,
    callback_data: HistoryCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child | None,
) -> None:
    if child is None:
        await call.answer(texts.NO_CHILD, show_alert=True)
        return
    data = callback_data
    async with tracker_guard.for_child(child.id):
        row = await row_for(session, child, data)
        if row is None:
            await call.answer(texts.RECORD_GONE, show_alert=True)
            return
        query = (
            select(RecordRevision)
            .where(
                RecordRevision.child_id == child.id,
                RecordRevision.what == data.what,
                RecordRevision.row_id == row.id,
            )
            .order_by(RecordRevision.id.desc())
        )
        if data.action == "list":
            changes = list(await session.scalars(query.limit(5)))
            lines = ["🕘 Последние правки этой записи"]
            for change in changes:
                author = await session.get(Member, change.actor_id) if change.actor_id else None
                who = html.escape(author.display_name) if author else "Система"
                local = tu.to_local(change.created_at, family.tz)
                before, after = json.loads(change.before_json), json.loads(change.after_json)
                labels = {
                    "started_at": "начало",
                    "ended_at": "окончание",
                    "kind": "вид",
                    "side": "историческая сторона",
                    "value_num": "значение",
                    "estimated": "приблизительность",
                    "taken_at": "время",
                    "value": "значение",
                }
                differences = []
                for key in before:
                    if before[key] != after[key]:
                        differences.append(
                            f"{labels.get(key, key)}: {shown(before[key], key, family.tz)} → "
                            f"{shown(after[key], key, family.tz)}"
                        )
                status = " · отменена" if change.undone_at else ""
                lines.append(
                    f"{local:%d.%m %H:%M} · {who}{status}\n" + html.escape("; ".join(differences))
                )
            if not changes:
                lines.append("Правок пока нет. Исходная запись сохранена.")
            await call.answer()
            if isinstance(call.message, Message):
                await call.message.answer("\n\n".join(lines))
            return
        if data.action != "undo":
            await call.answer()
            return
        change = await session.scalar(query.where(RecordRevision.undone_at.is_(None)).limit(1))
        if change is None:
            await call.answer("Нет правки для отмены", show_alert=True)
            return
        if revisions.snapshot(row) != change.after_json:
            await call.answer(
                "Запись уже изменилась. Откройте актуальную карточку.", show_alert=True
            )
            return
        before = json.loads(change.before_json)
        now = tu.now_utc()
        if isinstance(row, Event):
            start = datetime.fromisoformat(before["started_at"])
            end = datetime.fromisoformat(before["ended_at"]) if before["ended_at"] else None
            if not records.fits(start, end, born_at=child.born_at, now=now):
                await call.answer("Прежнее время вне допустимых границ", show_alert=True)
                return
            if before["kind"] in TIMED_KINDS and end is None:
                running = await repo.any_open_event(session, child.id)
                if running is not None and running.id != row.id:
                    await call.answer("Уже идёт другой таймер. Его не изменял.", show_alert=True)
                    return
        elif row.kind == WEIGHT and row.taken_at == child.born_at:
            await call.answer(texts.RECORD_BIRTH_WEIGHT, show_alert=True)
            return
        else:
            moment = datetime.fromisoformat(before["taken_at"])
            if not child.born_at <= moment <= now:
                await call.answer("Прежнее время вне допустимых границ", show_alert=True)
                return
        revisions.restore(row, change.before_json)
        change.undone_at = now
        change.undo_actor_id = member.id
        await session.commit()
        panel.schedule(bot, family.id)
        await call.answer("Последнюю правку отменил")
        if isinstance(row, Event):
            from bot.handlers.timing import render_card

            await render_card(bot, family, row, session=session)
        if isinstance(call.message, Message):
            await call.message.answer(
                "Вернул предыдущее значение.\n" + texts.record_card(row, family.tz, now)
            )
