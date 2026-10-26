"""Порядок чистки семейных данных — один на весь проект.

Список записан руками ровно один раз, и это принципиально. Вторая копия — не
дубль, а расхождение с отложенным сроком: структурный сторож
(`tests/test_stand_seed.py`) сверяет с `models.py` ту копию, которую
импортирует, а вторая тихо стареет. Пользователей у порядка два — снимок для
стенда (`tools/stand/seed.py`) и сброс боевой базы (`tools/reset.py`), — и оба
обязаны чистить одинаково.

Порядок обязателен: сначала то, что ссылается, потом то, на что ссылаются.
Каскадов в схеме нет нигде, а `PRAGMA foreign_keys=ON` стоит на каждом
соединении (`bot/db/sqlite.py`).

`reminders` стоит выше `children` не для красоты: у строки календаря есть
`child_id`, и база, где семья подписана на прививки, чистилась бы до первого
`DELETE FROM children` и падала на нём с `FOREIGN KEY constraint failed`. Год
порядок был неверным и молчал — пока в боевых `reminders` не появилось ни одной
строки с ребёнком.
"""

from __future__ import annotations

import sqlite3

WIPE_ORDER = (
    # `usage` первой: она ссылается и на `families`, и на `members`, а на неё
    # не ссылается никто. Чистить её обязательно — оставленная при удалённых
    # семьях, она дала бы висячие ключи, и `reset.py` честно напечатал бы их в
    # `foreign_key_check`.
    #
    # Цена названа вслух: сброс боевой базы уносит накопленную аналитику
    # целиком. Это правильно — семей, про которые она, больше нет, — но значит,
    # что отчёт перед сбросом надо выгрузить: `python -m tools.usage --csv`
    "usage",
    "alerts",
    "events",
    "measurements",
    "reminders",
    "children",
    "members",
    "families",
)

# Что переживает чистку намеренно.
#
# `who_lms` — 11 142 строки таблиц роста ВОЗ. Ради них снимок для стенда
# снимается с боевой базы, а не создаётся с нуля, и ради них же сброс — это
# `DELETE`, а не «удалить файл и накатить миграции»: на пустой `who_lms` вместо
# центиля показывается процент от веса при рождении (`measure._zscore_text`).
#
# `alembic_version` — отметка накатанных миграций. Её потеря выглядит как
# рабочая база, на которой `alembic upgrade head` при следующем `run.cmd`
# попробует накатить всю историю заново поверх готовой схемы. В `models.py` её
# нет — таблицу ведёт сам alembic, — поэтому в структурном стороже она не
# участвует, и списки там и здесь не обязаны совпадать.
KEPT = ("who_lms", "alembic_version")


# Как добраться до одной семьи в каждой таблице. Порядок по-прежнему один —
# `WIPE_ORDER`, — здесь только условие: вторым списком таблиц он разошёлся бы с
# первым молча. У событий, измерений и тревог своего `family_id` нет вовсе: они
# висят на ребёнке, и это единственная причина, по которой условия разные.
SCOPE = {
    "usage": "family_id = ?",
    "alerts": "child_id IN (SELECT id FROM children WHERE family_id = ?)",
    "events": "child_id IN (SELECT id FROM children WHERE family_id = ?)",
    "measurements": "child_id IN (SELECT id FROM children WHERE family_id = ?)",
    "reminders": "family_id = ?",
    "children": "family_id = ?",
    "members": "family_id = ?",
    "families": "id = ?",
}


def _check() -> None:
    """Условие обязано быть у каждой таблицы порядка, и лишних быть не должно.

    На импорте, а не в тесте: забытая таблица означает семью, вычищенную
    наполовину, и узнать об этом мы бы могли только по висячему ключу через
    месяц. Тест на это тоже есть — он сторожит от обратного, от условия к
    несуществующей таблице.
    """
    if set(SCOPE) != set(WIPE_ORDER):
        raise RuntimeError(
            f"SCOPE и WIPE_ORDER разошлись: {set(SCOPE) ^ set(WIPE_ORDER)}"
        )


_check()


def wipe(conn: sqlite3.Connection) -> dict[str, int]:
    """Удалить все семейные данные. Коммитит сам, соединение не закрывает."""
    conn.execute("PRAGMA foreign_keys = ON")
    wiped: dict[str, int] = {}
    for table in WIPE_ORDER:
        cursor = conn.execute(f"DELETE FROM {table}")  # noqa: S608 — имена свои
        wiped[table] = cursor.rowcount
    conn.commit()
    return wiped


def wipe_family(conn: sqlite3.Connection, family_id: int) -> dict[str, int]:
    """Удалить одну семью со всем, что на ней висит. Коммитит сам.

    Тот же порядок, что у полной чистки, и это главное: каскадов в схеме нет
    нигде, а `PRAGMA foreign_keys=ON` стоит на каждом соединении. Пойдя в
    обратном порядке, мы бы упёрлись в `FOREIGN KEY constraint failed` на
    первой же таблице, на которую кто-то ссылается.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    wiped: dict[str, int] = {}
    for table in WIPE_ORDER:
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE {SCOPE[table]}",  # noqa: S608 — условия свои
            (family_id,),
        )
        wiped[table] = cursor.rowcount
    conn.commit()
    return wiped
