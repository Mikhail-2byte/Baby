"""Снимок базы через `VACUUM INTO` и ротация копий.

Почему не копирование файла: у базы включён WAL, и часть данных в момент
копирования лежит не в `baby.db`, а в `baby.db-wal`. Скопированный файл
может оказаться битым, и узнать об этом получится только при восстановлении.
`VACUUM INTO` идёт через то же соединение, что и бот, поэтому видит всё
зафиксированное, включая WAL.

Модуль ничего не знает про aiogram и не выпускает исключений наружу:
сорвавшийся бэкап не должен стоить тика.

`engine` импортируется **по имени модуля** — это тот самый живой движок, что
держит базу; тест подменяет его через `monkeypatch.setattr(backup, "engine", …)`,
ровно как подменяет `ticker.Session`.
"""

import logging
import os
from datetime import date, datetime
from pathlib import Path

from bot.config import settings
from bot.db.session import engine
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

# Рядом с БД, а не в корне: в Docker `./data` смонтирована томом, и только
# оттуда копии переживут пересборку образа. Отдельной настройкой каталог не
# делаем — у `parse.log` он выводится от `db_path` точно так же
DIR = settings.db_path.parent / "backups"

PREFIX = "baby-"
SUFFIX = ".db"

# Единственное имя под недописанный снимок. Фиксированное, а не с датой: обрывок
# от упавшего `VACUUM` иначе копился бы в каталоге навсегда. Под маску ротации
# `baby-*.db` оно не подходит и в выдачу не попадёт
TMP = DIR / "snapshot.part"

# Сутки, в которые ежедневный снимок сорвался. Без этой отметки провал
# повторялся бы каждый тик: файла дня нет, значит «пора делать» — и при полном
# диске в лог уедет 1400 строк за сутки. Ровно та болезнь, что лечил
# `test_unsendable_panel_does_not_loop_every_tick` в 2п. Цена — случайный сбой
# стоит суток без копии; перезапуск бота отметку сбрасывает
_failed_on: date | None = None


def path_for(day: date) -> Path:
    return DIR / f"{PREFIX}{day.isoformat()}{SUFFIX}"


async def snapshot(dest: Path) -> None:
    """Снять копию базы в `dest`. Исключения пропускает наружу.

    Единственная функция этапа, которая не глотает ошибку: у неё два вызывающих
    с разной политикой — ежедневный молчит в лог, а `/backup` обязан сказать
    человеку, что файла не будет.

    Пишем во временное имя и только потом переименовываем: `VACUUM INTO`
    отказывается писать в существующий файл, а прерванный на середине снимок под
    боевым именем выглядел бы как готовая копия.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / TMP.name
    tmp.unlink(missing_ok=True)

    async with engine.connect() as conn:
        # VACUUM не выполняется внутри транзакции, а SQLAlchemy открывает её
        # неявно. Путь идёт связанным параметром — в нём бывают и кавычки
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.exec_driver_sql("VACUUM INTO ?", (str(tmp),))

    os.replace(tmp, dest)


async def run_daily(now: datetime | None = None) -> Path | None:
    """Снять копию за сегодня, если её ещё нет, и подчистить старые.

    Отметки «сегодня уже сделано» в БД нет намеренно: бэкап один на всю
    установку, а не на семью, и колонке в `families` пришлось бы отвечать на
    вопрос «чьи это сутки». Отметка — сам файл: он переживает перезапуск, чего
    модульная переменная не умеет.

    День считается в `settings.tz_default`: бэкап — вещь машинная, семьи у него
    нет.
    """
    global _failed_on

    if settings.backup_keep <= 0:
        return None

    today = tu.local_today(settings.tz_default, now)
    dest = path_for(today)
    if dest.exists() or _failed_on == today:
        return None

    try:
        await snapshot(dest)
    except Exception:
        _failed_on = today
        log.exception("Бэкап за %s не снят — до конца суток больше не пробуем", today)
        return None

    log.info("Бэкап: %s", dest.name)
    rotate(settings.backup_keep)
    return dest


def rotate(keep: int) -> None:
    """Оставить `keep` самых свежих копий. Исключений наружу не выпускает.

    Имена в ISO-датах, поэтому лексикографический порядок и есть хронологический
    — отдельного чтения времён файлов не нужно.
    """
    if keep <= 0:
        return
    try:
        found = sorted(DIR.glob(f"{PREFIX}*{SUFFIX}"))
        for stale in found[:-keep]:
            stale.unlink()
            log.info("Бэкап удалён по ротации: %s", stale.name)
    except Exception:
        log.exception("Ротация бэкапов не удалась")
