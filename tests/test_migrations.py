"""Миграции: прогон вперёд-назад и сверка схемы с моделями.

Единственное место, где `alembic/` вообще исполняется тестами. Все остальные
тесты строят схему через `Base.metadata.create_all` — так быстрее и так задумано,
но у этого есть цена, названная в `CLAUDE.md`:

    «Правка модели без новой ревизии Alembic даёт зелёные тесты и падение на
    боевом старте — после изменения `models.py` всегда делать
    `revision --autogenerate`».

«Всегда делать» держалось на памяти разработчика. Эти два теста заменяют память
на проверку: расхождение видно на прогоне, а не при старте бота на боевой машине,
где `alembic upgrade head` стоит в `CMD` контейнера и в `run.cmd`.

Работают на файловой БД во временном каталоге, а не в памяти: Alembic открывает
своё соединение, а `sqlite:///:memory:` у каждого соединения своя.
"""

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from alembic import command
from bot.db.models import Base

ROOT = Path(__file__).resolve().parent.parent


def _config(db_path: Path) -> Config:
    """Конфиг Alembic, нацеленный на временную базу.

    URL подменяется здесь, а не в `.env`: `alembic/env.py` берёт его из
    `settings.db_url` (один источник правды), и без подмены тест накатывал бы
    ревизии на боевую `data/baby.db`.
    """
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    # `env.py` перетирает URL своим, поэтому кладём его ещё и в атрибуты:
    # оттуда он не перетирается, и env.py его увидит первым
    config.attributes["sqlalchemy.url"] = f"sqlite:///{db_path}"
    return config


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    """Временная база, накатанная до head."""
    db_path = tmp_path / "baby.db"
    monkeypatch.setattr("bot.config.settings.db_path", db_path)
    command.upgrade(_config(db_path), "head")
    return db_path


def test_migrations_run_forward_and_back(tmp_path, monkeypatch):
    """`upgrade head` → `downgrade base`. Ни одна ревизия не исполнялась тестами."""
    db_path = tmp_path / "baby.db"
    monkeypatch.setattr("bot.config.settings.db_path", db_path)
    config = _config(db_path)

    command.upgrade(config, "head")
    assert db_path.exists()

    command.downgrade(config, "base")


def test_schema_matches_the_models(migrated):
    """`models.py` == alembic head.

    Тест, ради которого файл и заведён. Падает он ровно в одном случае: модель
    поправили, а `revision --autogenerate` не сделали — то есть в том самом,
    который до сих пор ловился только боевым стартом.

    Если тест покраснел, чинить его правкой сравнения нельзя. Правильный ответ:

        .venv/Scripts/alembic revision --autogenerate -m "описание"
    """
    engine = create_engine(f"sqlite:///{migrated}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                # То же, что в `alembic/env.py`: без batch-режима SQLite не
                # умеет ALTER, и сравнение здесь разошлось бы с автогенерацией
                opts={"render_as_batch": True, "compare_type": True},
            )
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], (
        "схема в миграциях разошлась с models.py — сделайте "
        "`alembic revision --autogenerate`. Расхождения: " + repr(diff)
    )


def test_timer_card_migration_keeps_history_and_family_preferences(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "old.db"
    monkeypatch.setattr("bot.config.settings.db_path", path)
    config = _config(path)
    command.upgrade(config, "1da3278e4719")
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            INSERT INTO families(id, chat_id, tz, digest_dow, digest_time,
                                 checkin_hours, checkin_last_at)
            VALUES (1, -1001, 'Asia/Yekaterinburg', 0, '10:00', 6, '2026-10-26 08:00:00');
            INSERT INTO children(id, family_id, name, sex, born_at, archived)
            VALUES (1, 1, 'Тест', 'f', '2026-09-01 00:00:00', 0);
            INSERT INTO members(id, family_id, tg_user_id, display_name)
            VALUES (1, 1, 222, 'Тест');
            INSERT INTO events(id, child_id, author_id, kind, started_at, ended_at, estimated, side)
            VALUES (1, 1, 1, 'feed_breast', '2026-10-26 08:00:00', '2026-10-26 08:20:00', 0, 'L');
            INSERT INTO measurements(id, child_id, author_id, kind, taken_at, value, note)
            VALUES (1, 1, 1, 'weight', '2026-10-25 08:00:00', 4300, 'Исходная запись');
        """)
        columns = ",".join(row[1] for row in connection.execute("PRAGMA table_info(events)"))
        family_columns = ",".join(
            row[1] for row in connection.execute("PRAGMA table_info(families)")
        )
        before = connection.execute("SELECT * FROM events").fetchall()
        preferences = connection.execute("SELECT * FROM families").fetchall()
        weights = connection.execute("SELECT * FROM measurements").fetchall()
    command.upgrade(config, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute(f"SELECT {columns} FROM events").fetchall() == before
        assert connection.execute("SELECT card_message_id, nudged_at FROM events").fetchall() == [
            (None, None)
        ]
        assert (
            connection.execute(f"SELECT {family_columns} FROM families").fetchall() == preferences
        )
        assert connection.execute("SELECT diapers_tracked FROM families").fetchone() == (1,)
        assert connection.execute("SELECT * FROM measurements").fetchall() == weights
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table in ("events", "measurements"):
            ddl = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            assert "AUTOINCREMENT" in ddl.upper()

    command.downgrade(config, "1da3278e4719")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM events").fetchall() == before
        assert connection.execute("SELECT * FROM measurements").fetchall() == weights
