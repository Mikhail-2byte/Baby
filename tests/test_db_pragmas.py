"""PRAGMA применяются к каждому соединению.

Единственный тест, который проверяет, что `bot/db/sqlite.py` вообще подключён.
Он заведён после того, как выяснилось, что модуль не импортировал никто:
слушатель вешается на событие `connect` **на импорте**, импорта не было, и
боевая база полгода жила в `journal_mode=delete` — при том что и `backup.py`,
и `README.md`, и `CLAUDE.md` рассуждали от включённого WAL.

Проверка идёт на файловой базе во временном каталоге, а не на `:memory:`:
у базы в памяти журнал всегда `memory`, и утверждение про WAL было бы
тавтологией — прошло бы и с выключённым слушателем.
"""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

# Импорт ради побочного эффекта: именно он и есть предмет проверки — что
# `bot.db.session` тянет за собой `bot.db.sqlite`. `bot.db.sqlite` здесь НЕ
# импортируется намеренно: импорт зарегистрировал бы слушатель сам, и тест
# стал бы тавтологией — проверено, без него он проходил и со снятой строкой
# в `session.py`. Это уже третья тавтология в тестах проекта; ищите её первой,
# когда тест кажется зелёным без причины.
import bot.db.session  # noqa: F401

# Дублируется из `bot/db/sqlite.py` намеренно, см. выше: тест обязан знать
# ожидаемое число сам, а не спрашивать его у проверяемого модуля
BUSY_TIMEOUT_MS = 5000


@pytest.mark.asyncio
async def test_every_connection_gets_its_pragmas(tmp_path):
    path = (tmp_path / "probe.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.connect() as conn:
            journal = await conn.exec_driver_sql("PRAGMA journal_mode")
            busy = await conn.exec_driver_sql("PRAGMA busy_timeout")
            keys = await conn.exec_driver_sql("PRAGMA foreign_keys")

            # WAL разводит писателя и читателей: без него тикер, отправляющий
            # напоминание, блокирует хендлер, отвечающий на команду
            assert journal.scalar() == "wal"
            # Без таймаута конкурентная запись падает `database is locked`
            # сразу, а не ждёт. В базу пишут три независимые сессии:
            # хендлер, тикер и панель
            assert busy.scalar() == BUSY_TIMEOUT_MS
            # SQLite по умолчанию внешние ключи не проверяет, и без этого
            # все `ForeignKey` в models.py — декларация, а не защита
            assert keys.scalar() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_lower_unicode_knows_cyrillic(tmp_path):
    """Встроенный `lower()` умеет только ASCII — ради этого функция и заведена."""
    path = (tmp_path / "probe.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.connect() as conn:
            result = await conn.exec_driver_sql("SELECT lower_unicode('ОТПУСК')")
            assert result.scalar() == "отпуск"
            plain = await conn.exec_driver_sql("SELECT lower('ОТПУСК')")
            assert plain.scalar() == "ОТПУСК", "если это упало, SQLite научился юникоду"
    finally:
        await engine.dispose()
