"""Обратимые снимки правок. Журнал и запись сохраняются одной транзакцией."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from hashlib import sha256

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Event, Measurement, RecordRevision

_actor: ContextVar[int | None] = ContextVar("journal_actor", default=None)
EVENT_FIELDS = (
    "kind",
    "started_at",
    "ended_at",
    "side",
    "value_num",
    "estimated",
    "subtype",
    "note",
)
MEASURE_FIELDS = ("taken_at", "value")
TIME_FIELDS = frozenset({"started_at", "ended_at", "taken_at"})


@contextmanager
def actor(member_id: int | None) -> Iterator[None]:
    token = _actor.set(member_id)
    try:
        yield
    finally:
        _actor.reset(token)


def snapshot(row: Event | Measurement) -> str:
    fields = EVENT_FIELDS if isinstance(row, Event) else MEASURE_FIELDS
    data = {}
    for key in fields:
        value = getattr(row, key)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif key in {"value", "value_num"} and value is not None:
            # SQLite returns FLOAT as float after reload, even if the input was int.
            value = float(value)
        data[key] = value
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def timer_stamp(row: Event) -> str:
    """Короткая версия снимка для ограниченных 64 байтами Telegram-кнопок."""
    return sha256(f"{row.id}:{snapshot(row)}".encode()).hexdigest()[:10]


def stage(session: AsyncSession, row: Event | Measurement, before: str) -> None:
    after = snapshot(row)
    if before == after:
        return
    session.add(
        RecordRevision(
            child_id=row.child_id,
            actor_id=_actor.get(),
            what="e" if isinstance(row, Event) else "m",
            row_id=row.id,
            before_json=before,
            after_json=after,
        )
    )


def restore(row: Event | Measurement, serialized: str) -> None:
    data = json.loads(serialized)
    fields = EVENT_FIELDS if isinstance(row, Event) else MEASURE_FIELDS
    for key in fields:
        value = data[key]
        if key in TIME_FIELDS and value is not None:
            value = datetime.fromisoformat(value)
        setattr(row, key, value)
