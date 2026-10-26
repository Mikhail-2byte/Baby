"""Сброс боевой базы: семья, участники, ребёнок, записи — всё, кроме ВОЗ.

    .venv\\Scripts\\python -m tools.reset

Почему файл, а не команда в консоли. Порядок удаления не произвольный (см.
`tools/wipe.py`), и набранный по памяти он стал бы третьей, никем не сторожимой
копией. Печатать, что именно удалено, обязан кто-то один — правило «ничего не
сохраняется молча» касается разрушительных операций в первую очередь. И
главное: только код может проверить, что бот действительно остановлен.

Проверка эта — тот же замок, которым бот стережёт себя от второго экземпляра
(`bot/services/lock.py`). Замок ОС снимается вместе с процессом, поэтому
«взялся» значит «бота нет», без разбора «а жив ли ещё тот PID». Правка базы
из-под работающего бота в WAL не падает с ошибкой — она расходится с тем, что
бот уже держит в памяти, то есть ломается тихо. Именно этого мы и не хотим.

Бэкап здесь не снимается намеренно: это решение человека, а не забытый шаг.
Раз в сутки снимок делает сам бот (`data/backups/`), и `/backup` кладёт копию
в чат — оба пути остаются доступными до сброса, а не после.

Чат записывается в `tools/prod_chats.py` ДО удаления: иначе сброс разоружает
сторож стенда `run.is_production_chat` — почему это опасно, сказано там же.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

from bot.config import settings
from bot.services import lock
from tools import prod_chats
from tools.wipe import KEPT, WIPE_ORDER, wipe

# Набирается руками. Флага `--yes` здесь нет намеренно: подтверждение, которое
# можно поставить в скрипт, перестаёт быть подтверждением.
#
# Слов два, и второе не для красоты: интерактивная консоль Windows отдаёт ввод
# через UTF-16 и кириллицу принимает верно, а вот `python -m tools.reset` из Git
# Bash или с подставленным stdin читает его системной ANSI — и правильно
# набранное слово не сходится с этой константой. Отказ безопасен, но выглядит
# как поломка, а повторять его человек будет ровно в тот вечер, когда спешит
CONFIRM = "СБРОСИТЬ"
ACCEPTED = (CONFIRM, "RESET")


def confirmed(raw: str, accepted: tuple[str, ...] = ACCEPTED) -> bool:
    """Сошлось ли подтверждение.

    Отдельной функцией ради одного символа: PowerShell 5.1 подмешивает в пайп
    BOM, и `input()` возвращает `'\ufeffRESET'`. `strip()` пробельным его не
    считает, поэтому правильно набранное слово не сходилось, а человек видел
    «Отменено, база не тронута» и не понимал, чем не угодил. Отказ безопасен,
    но повторять его будут ровно в тот вечер, когда спешат.

    `accepted` — ради `tools/forget.py`: слово там своё, а знание про BOM
    обязано быть одно на проект. Вторая копия этой строки разошлась бы
    молча и ровно в ту сторону, где отказ выглядит как поломка.
    """
    return raw.strip().strip("\ufeff").strip().upper() in accepted

# `alembic_version` в отчёте показывается своим значением, а не счётчиком строк
COUNTED = (*WIPE_ORDER, "who_lms")


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
        for table in COUNTED
    }


# id, chat_id, название, id закреплённой панели
Family = tuple[int, int, str | None, int | None]


def _families(conn: sqlite3.Connection) -> list[Family]:
    return conn.execute(
        "SELECT id, chat_id, title, panel_message_id FROM families ORDER BY id"
    ).fetchall()


def _read_only(db: Path) -> sqlite3.Connection:
    """Открыть на чтение. `mode=ro` ещё и не создаст файл при опечатке в пути."""
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def reset(db: Path, *, today: date | None = None) -> dict[str, int]:
    """Вычистить базу и ужать файл. Без вопросов и без замка — это делает `main`.

    `VACUUM` не косметика: `DELETE` оставляет страницы в списке свободных, то
    есть удалённые записи физически остаются в файле, который потом уедет в чат
    по `/backup`. `wal_checkpoint(TRUNCATE)` — чтобы те же страницы не пережили
    чистку в `baby.db-wal`.
    """
    conn = sqlite3.connect(db)
    try:
        prod_chats.remember(
            {chat_id: title for _, chat_id, title, _ in _families(conn)},
            on=today or date.today(),
        )
        wiped = wipe(conn)
        # Только после коммита внутри `wipe`: VACUUM внутри транзакции
        # отказывается работать вовсе
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return wiped


def _before(db: Path) -> tuple[dict[str, int], list[Family], list[tuple[str, int]]]:
    conn = _read_only(db)
    try:
        return (
            _counts(conn),
            _families(conn),
            conn.execute(
                "SELECT preset, count(*) FROM reminders WHERE preset IS NOT NULL "
                "GROUP BY preset ORDER BY preset"
            ).fetchall(),
        )
    finally:
        conn.close()


def _after(db: Path) -> None:
    conn = _read_only(db)
    try:
        print("\nОсталось:")
        for table, count in _counts(conn).items():
            print(f"  {table}: {count}")
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        print(f"  alembic_version: {version[0] if version else 'ПОТЕРЯНА'}")
        print(f"  integrity_check: {conn.execute('PRAGMA integrity_check').fetchone()[0]}")
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        print(f"  foreign_key_check: {broken or 'чисто'}")
        print(f"  journal_mode: {conn.execute('PRAGMA journal_mode').fetchone()[0]}")
    finally:
        conn.close()


def say_before(
    db: Path,
    counts: dict[str, int],
    families: list[Family],
    presets: list[tuple[str, int]],
) -> None:
    """Напечатать, что в базе лежит сейчас.

    Отдельной функцией ради сторожа: в названии семейного чата стоит эмодзи
    («Дочь🍼»), а вывод, ушедший не в консоль, а в пайп или в файл, кодируется
    системной ANSI — и отчёт падал с `UnicodeEncodeError` ровно на строке про
    семью. Падал безопасно, до удаления, но инструмент, который нельзя записать
    в лог, — сломанный инструмент.

    Кодировку потока не трогаем — кириллица в cp866 читается как надо; смягчаем
    только то, что в неё не лезет. Смягчение стоит здесь, а не в `main`, чтобы
    его нельзя было проверить моком: тест печатает сюда поток в cp1251.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")

    print(f"База: {db.resolve()}")
    for table, count in counts.items():
        print(f"  {table}: {count}")
    for family_id, chat_id, title, panel in families:
        print(f"\nСемья #{family_id}: чат {chat_id} «{title or 'без названия'}»")
        if panel is not None:
            print(
                f"  закреплённая панель #{panel} останется в чате — после сброса "
                "бот про неё не знает, откройте её и открепите руками"
            )
    if presets:
        print("\nПридётся включить заново через /remind:")
        for preset, count in presets:
            print(f"  {preset}: {count}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="tools.reset")
    parser.add_argument("--db", type=Path, default=settings.db_path)
    db: Path = parser.parse_args().db

    if not db.exists():
        print(f"Нет базы {db}")
        return 1

    # Замок берётся от самой базы, а не из `lock.PATH`: с `--db
    # data/live/baby.db` сброс обязан спорить со стендом, а не с боевым ботом
    guard = lock.SingleInstance(db.parent / "bot.lock")
    if not guard.acquire(timeout=0):
        print(f"ОТКАЗ: замок {db.parent / 'bot.lock'} занят — бот работает.")
        print("Остановите задачу BabyBot и процесс python, потом повторите.")
        return 1

    try:
        before, families, presets = _before(db)
        say_before(db, before, families, presets)

        if not any(before[table] for table in WIPE_ORDER):
            print("\nЧистить нечего.")
            return 0

        print(f"\nУдаляю всё, кроме {', '.join(KEPT)}. Бэкап не снимается.")
        if not confirmed(input(f"Наберите {CONFIRM}, чтобы продолжить: ")):
            print("Отменено, база не тронута.")
            return 1

        wiped = reset(db)
        print("\nУдалено:")
        for table, count in wiped.items():
            print(f"  {table}: {count}")
        _after(db)

        print(f"\nБоевые чаты записаны в {prod_chats.PATH} — сторож стенда цел.")
        print("Дальше: запустить бота, написать в группу любую реплику, /baby.")
        return 0
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
