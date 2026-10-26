"""Забыть одну семью: чат, участников, ребёнка, записи.

    .venv\\Scripts\\python -m tools.forget -1003900409468

Младший брат `tools/reset.py`. Тот чистит базу целиком и нужен раз в жизни
проекта; этот нужен каждый раз, когда бот побывал в чужом чате — пробная
группа, чат, из которого ушли, семья, попросившая себя удалить. Пока бот был
на одну семью, такой операции не существовало вовсе.

Порядок удаления не свой: `tools/wipe.py`, та же константа `WIPE_ORDER`, из
которой полная чистка берёт свой. Вторая копия порядка была бы не дублем, а
расхождением с отложенным сроком.

Чат записывается в `tools/prod_chats.py` ДО удаления — по той же причине, что
и в `reset.py`: строка `families` это единственное, по чему стенд узнаёт
боевую группу, и, удалив её молча, мы разоружаем сторож, написанный ровно
против прогона стенда по живому чату.

`VACUUM` здесь не делается намеренно, и это отличие от сброса названо вслух:
одна семья — это десятки строк, а `VACUUM` переписывает файл целиком и требует
места под вторую копию базы. Освободившиеся страницы переиспользует сам SQLite.
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
from tools.reset import _read_only, confirmed, say_before
from tools.wipe import SCOPE, WIPE_ORDER, wipe_family

# Набирается руками, как и в `reset.py`, и по той же причине: подтверждение,
# которое можно поставить в скрипт, перестаёт быть подтверждением. Второе слово
# латиницей — для консоли, отдающей ввод в системной ANSI
CONFIRM = "ЗАБЫТЬ"
ACCEPTED = (CONFIRM, "FORGET")


def _family_of(conn: sqlite3.Connection, chat_id: int) -> tuple[int, str | None] | None:
    row = conn.execute(
        "SELECT id, title FROM families WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    return (row[0], row[1]) if row else None


def _counts(conn: sqlite3.Connection, family_id: int) -> dict[str, int]:
    """Сколько строк семьи в каждой таблице — тем же условием, что и удаление.

    Условие берётся из `wipe.SCOPE`, а не пишется заново: отчёт, считающий не
    то, что удаляет чистка, хуже отсутствующего — он обещает человеку одно
    число, а забирает другое.
    """
    return {
        table: conn.execute(
            f"SELECT count(*) FROM {table} WHERE {SCOPE[table]}",  # noqa: S608
            (family_id,),
        ).fetchone()[0]
        for table in WIPE_ORDER
    }


def _children(conn: sqlite3.Connection, family_id: int) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT name, born_at FROM children WHERE family_id = ? ORDER BY id",
        (family_id,),
    ).fetchall()


def forget(db: Path, chat_id: int, *, today: date | None = None) -> dict[str, int]:
    """Вычистить семью этого чата. Без вопросов и без замка — это делает `main`."""
    conn = sqlite3.connect(db)
    try:
        found = _family_of(conn, chat_id)
        if found is None:
            return {}
        family_id, title = found
        prod_chats.remember({chat_id: title}, on=today or date.today())
        return wipe_family(conn, family_id)
    finally:
        conn.close()


def _after(db: Path, chat_id: int) -> None:
    conn = _read_only(db)
    try:
        left = conn.execute(
            "SELECT count(*) FROM families WHERE chat_id = ?", (chat_id,)
        ).fetchone()[0]
        print(f"\n  строк на чат {chat_id}: {left}")
        print(f"  семей осталось: {conn.execute('SELECT count(*) FROM families').fetchone()[0]}")
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        print(f"  foreign_key_check: {broken or 'чисто'}")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="tools.forget")
    parser.add_argument("chat_id", type=int, help="chat_id группы, которую забываем")
    parser.add_argument("--db", type=Path, default=settings.db_path)
    args = parser.parse_args()
    db: Path = args.db
    chat_id: int = args.chat_id

    if not db.exists():
        print(f"Нет базы {db}")
        return 1

    # Замок от самой базы, а не из `lock.PATH` — тот же довод, что в `reset.py`
    guard = lock.SingleInstance(db.parent / "bot.lock")
    if not guard.acquire(timeout=0):
        print(f"ОТКАЗ: замок {db.parent / 'bot.lock'} занят — бот работает.")
        print("Остановите задачу BabyBot и процесс python, потом повторите.")
        return 1

    try:
        conn = _read_only(db)
        try:
            found = _family_of(conn, chat_id)
            if found is None:
                print(f"Семьи на чат {chat_id} в базе нет — забывать нечего.")
                return 0
            family_id, title = found
            counts = _counts(conn, family_id)
            children = _children(conn, family_id)
        finally:
            conn.close()

        # Печатаем тем же смягчением, что и сброс: в названии чата стоит эмодзи,
        # а вывод, ушедший в файл, кодируется системной ANSI
        say_before(db, {}, [(family_id, chat_id, title, None)], [])
        for name, born in children:
            print(f"  ребёнок: {name}, {born}")
        for table, count in counts.items():
            print(f"  {table}: {count}")

        print("\nУдаляю только эту семью. Бэкап не снимается.")
        print("Бот из чата не выходит: написав туда, семья заведётся заново.")
        if not confirmed(input(f"Наберите {CONFIRM}, чтобы продолжить: "), ACCEPTED):
            print("Отменено, база не тронута.")
            return 1

        wiped = forget(db, chat_id)
        print("\nУдалено:")
        for table, count in wiped.items():
            print(f"  {table}: {count}")
        _after(db, chat_id)
        print(f"\nЧат записан в {prod_chats.PATH} — сторож стенда цел.")
        return 0
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
