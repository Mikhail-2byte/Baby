from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Импорт ради побочного эффекта, и он обязателен. `sqlite.py` вешает слушатель
# на событие `connect` класса Engine **на импорте модуля**; без этой строки
# слушатель не регистрируется, и база молча работает без WAL, без
# `busy_timeout` и без `PRAGMA foreign_keys=ON` — то есть ровно так, как
# описано в докстрингах трёх модулей, что она НЕ работает.
#
# Ровно это и случилось: боевая `data/baby.db` жила в `journal_mode=delete`,
# пока сюда не дописали строку. Если однажды `ruff --fix` или чужая рука
# соберётся снести «неиспользуемый импорт» — не сносите, а прочитайте
# `bot/db/sqlite.py`.
from bot.config import settings
from bot.db import sqlite  # noqa: F401

settings.db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(settings.db_url, echo=False)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
