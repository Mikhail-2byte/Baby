"""Чистка снимка боевой базы для стенда.

`wipe.WIPE_ORDER` — единственное место проекта, где порядок таблиц записан
руками, а не выведен из схемы. Жил он в `seed.py`, пока пользователь был один;
со сбросом боевой базы (`tools/reset.py`) их стало двое, и порядок переехал в
`tools/wipe.py` целиком — вторая копия разошлась бы с этим сторожем молча, ведь
сверяется с `models.py` ровно та, которую тест импортирует.

Год порядок был неверным и молчал: `reminders`
ссылается на `children`, а удалялись они в обратном порядке. Поймать это могла
только боевая база, в которой у семьи есть подписка на календарь прививок, —
у строки календаря `child_id` заполнен. Пока такой строки не было, чистка
проходила, и стенд готовился как ни в чём не бывало.

Поломка при этом выглядит как отказ подготовки целиком: `sqlite3.IntegrityError:
FOREIGN KEY constraint failed` на первом же `DELETE FROM children`, то есть
живая проверка не запускается вовсе. Симптом честный — но приходит он в тот
день, когда стенд нужен, а не в тот, когда порядок правили.

Два теста: структурный — порядок сверяется с настоящими внешними ключами
`models.py`, и поведенческий — снимок базы с подпиской на календарь обязан
вычиститься. Структурный переживёт новую таблицу, поведенческий — новую ссылку
в существующей.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine

from bot.db.models import Base
from tools.wipe import WIPE_ORDER

# Таблицы ВОЗ переживают чистку намеренно: ради них снимок и снимается с боевой
# базы, а не создаётся с нуля. На пустой `who_lms` вместо центиля показывался бы
# процент от веса при рождении, и этап 3 остался бы непроверенным.
#
# Это не `wipe.KEPT`, и брать его оттуда нельзя: там есть ещё `alembic_version`,
# которой в `Base.metadata` нет вовсе. Здесь перечислены таблицы `models.py`,
# которые чистку переживают, — а сверка идёт именно по `models.py`
KEPT = {"who_lms"}


def _build(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def test_wipe_order_agrees_with_the_foreign_keys() -> None:
    """Таблица удаляется раньше той, на которую ссылается.

    Сверка идёт с `models.py`, а не со списком в голове: новая ссылка между уже
    существующими таблицами покраснит этот тест, ничего больше не трогая.
    """
    position = {name: index for index, name in enumerate(WIPE_ORDER)}

    for table in Base.metadata.sorted_tables:
        if table.name in KEPT:
            continue
        assert table.name in position, (
            f"таблица {table.name} есть в models.py, но не чистится в стенде — "
            "её строки уедут в снимок вместе с боевой семьёй"
        )
        for key in table.foreign_keys:
            referred = key.column.table.name
            if referred in KEPT:
                continue
            assert position[table.name] < position[referred], (
                f"{table.name} ссылается на {referred}, значит удаляться обязана "
                f"раньше — иначе DELETE FROM {referred} падает с FOREIGN KEY "
                "constraint failed"
            )


def test_a_family_subscribed_to_the_calendar_is_wiped(tmp_path: Path) -> None:
    """Снимок с подпиской на прививки чистится целиком.

    Ровно тот случай, на котором подготовка стенда встала 15.09: строка
    календаря держит `child_id`, и порядок «дети раньше напоминаний» делал
    `prepare` невозможным.
    """
    from tools.stand import seed

    src = tmp_path / "baby.db"
    _build(src)

    now = datetime.now(UTC).replace(tzinfo=None).isoformat(" ")
    conn = sqlite3.connect(src)
    try:
        conn.execute(
            "INSERT INTO families (id, chat_id, tz, digest_dow, digest_time, "
            "created_at, checkin_hours) VALUES (1, -100, 'Asia/Yekaterinburg', "
            f"0, '10:00', '{now}', 0)"
        )
        conn.execute(
            "INSERT INTO members (id, family_id, tg_user_id, display_name, "
            f"joined_at) VALUES (1, 1, 42, 'Тест', '{now}')"
        )
        conn.execute(
            "INSERT INTO children (id, family_id, name, sex, born_at, archived, "
            f"created_at) VALUES (1, 1, 'Тест', 'f', '{now}', 0, '{now}')"
        )
        # Та самая строка: напоминание календаря знает ребёнка
        conn.execute(
            "INSERT INTO reminders (id, family_id, child_id, text, fire_at, "
            "active, preset, created_by, created_at) VALUES "
            f"(1, 1, 1, 'АКДС', '{now}', 1, 'vaccine', 1, '{now}')"
        )
        # Нажатия боевой семьи. В снимок для стенда им нельзя не потому, что
        # они секретны, а потому, что ссылаются на удаляемых: без чистки
        # `usage` подготовка падает на `DELETE FROM members`
        conn.execute(
            "INSERT INTO usage (id, at, family_id, member_id, kind, action, "
            f"handled, ms) VALUES (1, '{now}', 1, 1, 'button', 'pnl:refresh', 1, 12)"
        )
        conn.commit()
    finally:
        conn.close()

    wiped = seed.prepare(src, tmp_path / "live" / "baby.db")

    assert wiped["usage"] == 1
    assert wiped["reminders"] == 1
    assert wiped["children"] == 1
    assert wiped["families"] == 1

    conn = sqlite3.connect(tmp_path / "live" / "baby.db")
    try:
        for table in WIPE_ORDER:
            left = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
            assert left == 0, f"в снимке остались строки: {table}"
    finally:
        conn.close()
