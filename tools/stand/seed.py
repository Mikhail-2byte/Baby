"""Подготовка базы стенда и материал для сводок.

Три разные вещи, и путать их нельзя.

**Копия базы.** `data/live/baby.db` делается снимком боевой и вычищается, а не
создаётся с нуля. Причина одна и весомая: в `who_lms` лежат 11 142 строки
таблиц ВОЗ, и без них вместо центиля показывается процент от веса при
рождении (`measure._zscore_text`) — то есть главный результат этапа 3 остался
бы непроверенным, а протокол этого бы не заметил. Копия заодно уже стоит на
`alembic head`. Снимок снимается `VACUUM INTO` — тем же способом, что и
боевой бэкап, то есть содержимое WAL в него попадает.

Чистка идёт в порядке, который не спорит с `PRAGMA foreign_keys=ON`: каскадов
в схеме нет нигде, и удаление семьи с живыми детьми упёрлось бы в
`IntegrityError`.

**История.** Две недели кормлений, снов и подгузников пишутся прямо через
`repo`. Это подготовка материала, а не подмена проверяемого: `/week`,
`/chart`, `/export` и дайджест на пустой неделе показали бы прочерки и не
доказали бы ничего. В протоколе шаг помечен отдельно, чтобы его не считали
живым вводом.

**Сдвиги для акта 2.** Догонка, автозакрытие и дайджест ускоряются сдвигом
данных, а не правкой порогов. Выкрученный в минуту `OPEN_TIMER_MAX_HOURS`
проверял бы не боевое значение, а открытый пять часов назад таймер при
боевых четырёх — ровно его.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import (
    DIAPER_DIRTY,
    DIAPER_WET,
    FEED_BOTTLE,
    FEED_BREAST,
    SLEEP,
    WEIGHT,
)
from bot.services import checkin

# Порядок обязателен: сначала то, что ссылается, потом то, на что ссылаются
WIPE_ORDER = (
    "alerts",
    "events",
    "measurements",
    "children",
    "reminders",
    "members",
    "families",
)


def prepare(src: Path, dst: Path) -> dict[str, int]:
    """Снять снимок боевой базы и вычистить из него все семьи."""
    if not src.exists():
        raise FileNotFoundError(f"Нет боевой базы {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    for leftover in (dst, Path(f"{dst}-wal"), Path(f"{dst}-shm")):
        leftover.unlink(missing_ok=True)

    source = sqlite3.connect(src)
    try:
        source.execute("VACUUM INTO ?", (str(dst),))
    finally:
        source.close()

    wiped: dict[str, int] = {}
    conn = sqlite3.connect(dst)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for table in WIPE_ORDER:
            cursor = conn.execute(f"DELETE FROM {table}")  # noqa: S608 — имена свои
            wiped[table] = cursor.rowcount
        conn.commit()
        kept = conn.execute("SELECT count(*) FROM who_lms").fetchone()[0]
        wiped["who_lms (оставлено)"] = kept
    finally:
        conn.close()
    return wiped


async def history(
    session: AsyncSession,
    child_id: int,
    member_id: int,
    *,
    now: datetime,
    days: int = 14,
) -> dict[str, int]:
    """Две недели обычной жизни: кормления, сны, подгузники, три взвешивания."""
    counts = {"feed_breast": 0, "sleep": 0, "diaper": 0, "bottle": 0, "weight": 0}
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    for back in range(days, 0, -1):
        day = midnight - timedelta(days=back)
        # Кормления каждые три часа, по 25 минут, стороны через одну
        for i, hour in enumerate((0, 3, 6, 9, 12, 15, 18, 21)):
            start = day + timedelta(hours=hour, minutes=10)
            await repo.add_event(
                session,
                child_id,
                member_id,
                FEED_BREAST,
                started_at=start,
                ended_at=start + timedelta(minutes=25),
                side="L" if i % 2 == 0 else "R",
            )
            counts["feed_breast"] += 1

        # Сны: длинный ночной и три дневных
        for hour, minutes in ((22, 400), (7, 90), (11, 75), (16, 100)):
            start = day + timedelta(hours=hour)
            await repo.add_event(
                session,
                child_id,
                member_id,
                SLEEP,
                started_at=start,
                ended_at=start + timedelta(minutes=minutes),
            )
            counts["sleep"] += 1

        for hour in (1, 5, 8, 12, 17, 20):
            await repo.add_event(
                session,
                child_id,
                member_id,
                DIAPER_WET,
                started_at=day + timedelta(hours=hour, minutes=5),
            )
            counts["diaper"] += 1
        for hour in (9, 19):
            await repo.add_event(
                session,
                child_id,
                member_id,
                DIAPER_DIRTY,
                started_at=day + timedelta(hours=hour, minutes=30),
            )
            counts["diaper"] += 1

        # Бутылочка не каждый день — так и бывает
        if back % 3 == 0:
            await repo.add_event(
                session,
                child_id,
                member_id,
                FEED_BOTTLE,
                started_at=day + timedelta(hours=14),
                value_num=90,
            )
            counts["bottle"] += 1

    # Прибавка около 190 г в неделю — то, что и ожидается на втором месяце
    for back, grams in ((18, 4700), (11, 4900), (4, 5050)):
        await repo.add_measurement(
            session,
            child_id,
            member_id,
            WEIGHT,
            taken_at=midnight - timedelta(days=back) + timedelta(hours=10),
            value=float(grams),
        )
        counts["weight"] += 1

    return counts


# --- сдвиги для акта 2 ------------------------------------------------------


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def stage_overdue_reminders(
    db: Path, family_id: int, member_id: int, now: datetime
) -> list[tuple[str, str]]:
    """Три просроченных напоминания — по одному на каждую ступень догонки.

    Пороги боевые: < 10 мин молча, < 12 ч с припиской, дальше сводкой
    (`late_silent_min`, `late_summary_hours`).
    """
    plan = [
        ("SILENT", timedelta(minutes=2), "Стенд: свежее напоминание"),
        ("LATE", timedelta(minutes=40), "Стенд: опоздало на сорок минут"),
        ("SUMMARY", timedelta(hours=20), "Стенд: опоздало на двадцать часов"),
    ]
    conn = _conn(db)
    try:
        for _, back, text in plan:
            conn.execute(
                "INSERT INTO reminders "
                "(family_id, created_by, text, fire_at, active, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (
                    family_id,
                    member_id,
                    text,
                    (now - back).isoformat(sep=" "),
                    now.isoformat(sep=" "),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return [(name, text) for name, _, text in plan]


def stage_stale_timer(db: Path, child_id: int, member_id: int, now: datetime) -> None:
    """Таймер, открытый пять часов назад, при боевом пороге в четыре."""
    started = now - timedelta(hours=5)
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO events (child_id, author_id, kind, started_at, estimated, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (child_id, member_id, SLEEP, started.isoformat(sep=" "), now.isoformat(sep=" ")),
        )
        conn.commit()
    finally:
        conn.close()


def stage_digest(db: Path, family_id: int, local_now: datetime) -> None:
    """Дайджест на сегодня и на уже прошедшее время; отметка снята."""
    conn = _conn(db)
    try:
        conn.execute(
            "UPDATE families SET digest_dow = ?, digest_time = ?, last_digest_on = NULL "
            "WHERE id = ?",
            (local_now.weekday(), (local_now - timedelta(minutes=5)).strftime("%H:%M"), family_id),
        )
        conn.commit()
    finally:
        conn.close()


def stage_few_wet_diapers(db: Path, child_id: int, now: datetime) -> int:
    """Оставить в скользящих сутках два мокрых подгузника вместо шести."""
    since = (now - timedelta(hours=24)).isoformat(sep=" ")
    conn = _conn(db)
    try:
        rows = conn.execute(
            "SELECT id FROM events WHERE child_id = ? AND kind = ? AND started_at >= ? "
            "ORDER BY started_at",
            (child_id, DIAPER_WET, since),
        ).fetchall()
        doomed = [r[0] for r in rows[2:]]
        conn.executemany("DELETE FROM events WHERE id = ?", [(i,) for i in doomed])
        conn.commit()
        return len(rows) - len(doomed)
    finally:
        conn.close()


def stage_weight_loss(
    db: Path, child_id: int, member_id: int, now: datetime, birth_weight_g: int
) -> int:
    """Свежее взвешивание ниже порога потери массы.

    Время строго «сейчас», а не «два часа назад». Сторож судит по ПОСЛЕДНЕМУ
    взвешиванию (`repo.last_measurement`), и подложенное назад во времени
    оказалось старше живого веса из акта 1 — тревога не поднялась, и это было
    правильное поведение бота при неправильной подготовке.
    """
    value = int(birth_weight_g * 0.88)
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO measurements (child_id, author_id, kind, taken_at, value, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                child_id,
                member_id,
                WEIGHT,
                now.isoformat(sep=" "),
                float(value),
                now.isoformat(sep=" "),
            ),
        )
        conn.commit()
        return value
    finally:
        conn.close()


def stage_checkin(db: Path, family_id: int, hours: int, now: datetime) -> None:
    """Опрос включён и уже созрел: отметка отодвинута на интервал плюс минуту.

    Приём тот же, что у `stage_digest`: расписание переставляется на «только
    что прошло». Ни один порог при этом не правится — сдвигаются данные.
    """
    stamp = now - timedelta(hours=hours, minutes=1)
    conn = _conn(db)
    try:
        conn.execute(
            "UPDATE families SET checkin_hours = ?, checkin_last_at = ? WHERE id = ?",
            (hours, stamp.isoformat(sep=" "), family_id),
        )
        conn.commit()
    finally:
        conn.close()


def stage_checkin_silent(db: Path, child_id: int, member_id: int, now: datetime) -> None:
    """Свежая запись внутри окна — опрос обязан промолчать."""
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO events (child_id, author_id, kind, started_at, estimated, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (
                child_id,
                member_id,
                DIAPER_WET,
                (now - timedelta(minutes=5)).isoformat(sep=" "),
                now.isoformat(sep=" "),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# Зоны на весь круг смещений, от UTC-11 до UTC+14. Список, а не арифметика со
# смещением: `in_quiet` судит по имени зоны, и подсовывать ей выдуманный сдвиг
# значило бы проверять не тот код, который работает в бою
_TIMEZONES = (
    "Pacific/Midway", "Pacific/Honolulu", "America/Anchorage", "America/Los_Angeles",
    "America/Denver", "America/Chicago", "America/New_York", "America/Halifax",
    "America/Sao_Paulo", "Atlantic/South_Georgia", "Atlantic/Azores", "UTC",
    "Europe/Berlin", "Europe/Kyiv", "Europe/Moscow", "Asia/Dubai", "Asia/Karachi",
    "Asia/Dhaka", "Asia/Bangkok", "Asia/Shanghai", "Asia/Tokyo",
    "Australia/Brisbane", "Pacific/Guadalcanal", "Pacific/Auckland",
    "Pacific/Kiritimati",
)


def stage_checkin_night(db: Path, family_id: int, now: datetime) -> str:
    """Подменить зону семьи на ту, где сейчас ночь. Возвращает прежнюю.

    Часы боту не подкрутишь, а ночное окно — локальное, поэтому ночь
    подставляется зоной. Зона именно ВЫЧИСЛЯЕТСЯ через боевую `in_quiet`, а не
    зашита: правка границ окна в файле не должна делать этот шаг
    ложнозелёным — при зашитой зоне он остался бы зелёным, проверяя не то.

    Прежнюю зону обязан вернуть вызывающий: от неё зависят ещё и панель, и
    дайджест, и все локальные времена в сводках.
    """
    row = _conn(db).execute("SELECT tz FROM families WHERE id = ?", (family_id,)).fetchone()
    previous = row[0] if row else "UTC"

    night = next((name for name in _TIMEZONES if checkin.in_quiet(name, now)), None)
    if night is None:  # окно шире нуля — значит какая-то зона в него попадает
        raise RuntimeError("Не нашлось зоны, где сейчас ночь по границам файла")

    set_timezone(db, family_id, night)
    return previous


def set_timezone(db: Path, family_id: int, tz: str) -> None:
    """Сменить зону семьи. Отдельно от `stage_checkin_night` — ею же и возвращают."""
    conn = _conn(db)
    try:
        conn.execute("UPDATE families SET tz = ? WHERE id = ?", (tz, family_id))
        conn.commit()
    finally:
        conn.close()


def stage_yesterday_panel(db: Path, family_id: int, today: date) -> None:
    """Пометить панель вчерашней, чтобы тикер перевыпустил её."""
    conn = _conn(db)
    try:
        conn.execute(
            "UPDATE families SET panel_day = ? WHERE id = ?",
            ((today - timedelta(days=1)).isoformat(), family_id),
        )
        conn.commit()
    finally:
        conn.close()
