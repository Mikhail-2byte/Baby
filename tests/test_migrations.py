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
