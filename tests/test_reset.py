"""Сброс боевой базы: что обязано умереть, что обязано выжить и что обязано
пережить сброс СНАРУЖИ базы.

Третий тест здесь главный. Первые два проверяют то, что видно глазами в тот же
день: пустые таблицы и целые таблицы ВОЗ. Третий — защиту, поломка которой
видна только на следующем прогоне стенда и выглядит как тестовый ребёнок в
семейном чате.

Тесты зовут `reset.reset`, а не `main`: подтверждение словом и замок — это
интерфейс для человека, и подменять `input` ради них значит проверять подмену.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import create_engine

from bot.db.models import Base
from tools import forget, prod_chats, reset, wipe
from tools.wipe import WIPE_ORDER

CHAT_ID = -100500


def _build(path: Path) -> None:
    """Схема плюс `alembic_version` — её ведёт alembic, в `models.py` её нет."""
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.execute("INSERT INTO alembic_version VALUES ('5c986e072e69')")
        conn.commit()
    finally:
        conn.close()


def _fill(path: Path, family_id: int = 1, chat_id: int = CHAT_ID) -> None:
    """Семья со всем, что на неё ссылается, включая строку календаря.

    Номер семьи — параметр ради `tools/forget.py`: тому нужна вторая семья
    в той же базе, и заполнять её вторым, почти таким же куском значило бы
    проверять чистку одной семьи на данных, устроенных иначе.
    """
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(" ")
    fid = family_id
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO families (id, chat_id, title, tz, digest_dow, digest_time, "
            f"created_at, checkin_hours, panel_message_id) VALUES ({fid}, {chat_id}, "
            f"'Дочь', 'Asia/Yekaterinburg', 0, '10:00', '{now}', 0, 1501)"
        )
        conn.execute(
            "INSERT INTO members (id, family_id, tg_user_id, display_name, "
            f"joined_at) VALUES ({fid}, {fid}, 42, 'Мама', '{now}')"
        )
        conn.execute(
            "INSERT INTO children (id, family_id, name, sex, born_at, archived, "
            f"created_at) VALUES ({fid}, {fid}, 'Вера', 'f', '{now}', 0, '{now}')"
        )
        # Та самая строка: напоминание календаря знает ребёнка, и из-за неё
        # порядок «дети раньше напоминаний» падает с FOREIGN KEY constraint
        conn.execute(
            "INSERT INTO reminders (id, family_id, child_id, text, fire_at, "
            "active, preset, created_by, created_at) VALUES "
            f"({fid}, {fid}, {fid}, 'АКДС', '{now}', 1, 'vaccine', {fid}, '{now}')"
        )
        conn.execute(
            "INSERT INTO events (id, child_id, author_id, kind, started_at, "
            f"estimated, created_at) VALUES ({fid}, {fid}, {fid}, 'sleep', '{now}', 0, '{now}')"
        )
        conn.execute(
            "INSERT INTO measurements (id, child_id, author_id, kind, taken_at, "
            f"value, created_at) VALUES ({fid}, {fid}, {fid}, 'weight', '{now}', 3356.0, '{now}')"
        )
        conn.execute(
            "INSERT INTO alerts (id, child_id, flag_id, raised_on) VALUES "
            f"({fid}, {fid}, 'few_wet_diapers', '{date.today()}')"
        )
        # Нажатия ссылаются и на семью, и на участника. Структурный сторож
        # новую таблицу переживёт, а этот — нет: без чистки `usage` сброс
        # падает на `DELETE FROM members` с FOREIGN KEY constraint failed
        conn.execute(
            "INSERT INTO usage (id, at, family_id, member_id, kind, action, "
            f"handled, ms) VALUES ({fid}, '{now}', {fid}, {fid}, 'button', 'pnl:refresh', 1, 12)"
        )
        conn.execute(
            "INSERT INTO processed_messages(chat_id, message_id) VALUES (?, 1)", (chat_id,)
        )
        conn.execute(
            "INSERT INTO record_revisions(child_id, actor_id, what, row_id, "
            "before_json, after_json, undo_actor_id) VALUES (?, ?, 'e', ?, '{}', '{}', ?)",
            (fid, fid, fid, fid),
        )
        conn.execute(
            "INSERT OR IGNORE INTO who_lms (sex, indicator, age_days, l, m, s) "
            "VALUES ('f', 'weight', 0, 0.3809, 3.2322, 0.14171)"
        )
        conn.commit()
    finally:
        conn.close()


def test_reset_empties_the_family_and_keeps_the_who_tables(tmp_path: Path) -> None:
    """Семейное умирает целиком, справочное остаётся.

    Потеря `who_lms` не видна в день сброса: бот работает, вес записывается, а
    вместо центиля молча показывается процент от веса при рождении. Потеря
    `alembic_version` того хуже — база выглядит рабочей, пока следующий
    `run.cmd` не попробует накатить всю историю миграций поверх готовой схемы.
    """
    db = tmp_path / "baby.db"
    _build(db)
    _fill(db)

    wiped = reset.reset(db)

    assert wiped["reminders"] == 1, "строка календаря обязана удалиться раньше ребёнка"
    assert wiped["children"] == 1
    assert wiped["families"] == 1

    conn = sqlite3.connect(db)
    try:
        for table in WIPE_ORDER:
            left = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
            assert left == 0, f"в базе остались строки: {table}"
        assert conn.execute("SELECT count(*) FROM who_lms").fetchone()[0] == 1
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version == ("5c986e072e69",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_reset_shrinks_the_file(tmp_path: Path) -> None:
    """`VACUUM` действительно зовётся.

    Без него `DELETE` оставляет страницы в списке свободных: удалённые записи
    физически остаются в файле, который потом уедет в чат по `/backup`. Файл
    при этом выглядит вычищенным любым запросом — поймать можно только размером.
    """
    db = tmp_path / "baby.db"
    _build(db)
    _fill(db)
    # Мусора должно быть заметно больше страницы, иначе VACUUM нечего срезать
    conn = sqlite3.connect(db)
    try:
        now = datetime.now(UTC).replace(tzinfo=None).isoformat(" ")
        conn.executemany(
            "INSERT INTO events (child_id, author_id, kind, started_at, estimated, "
            "created_at, note) VALUES (1, 1, 'sleep', ?, 0, ?, ?)",
            [(now, now, "x" * 400) for _ in range(500)],
        )
        conn.commit()
    finally:
        conn.close()
    before = db.stat().st_size

    reset.reset(db)

    assert db.stat().st_size < before, "файл не ужался — VACUUM не позвался"


def test_the_stand_guard_survives_the_reset(tmp_path: Path) -> None:
    """Сброс не разоружает сторож стенда.

    `run.is_production_chat` узнавал боевую группу по строке в `families`.
    Сброс эту строку удаляет — и стенд, запущенный следом тем же токеном, залил
    бы тестового ребёнка в семейный чат. Поломка молчит до того прогона.
    """
    db = tmp_path / "baby.db"
    _build(db)
    _fill(db)

    assert prod_chats.is_production(CHAT_ID, db) == "Дочь"

    reset.reset(db)

    assert prod_chats.is_production(CHAT_ID, db) == "Дочь", (
        "после сброса сторож перестал узнавать боевой чат"
    )
    assert prod_chats.is_production(-999, db) is None, "чужой чат стал боевым"


def test_the_report_survives_an_emoji_in_the_chat_title(capsys, tmp_path: Path) -> None:
    """Эмодзи в названии чата не роняет отчёт.

    Боевая группа называется «Дочь🍼», а вывод, ушедший не в консоль, а в пайп
    или в файл, кодируется системной ANSI. Отчёт падал там с
    `UnicodeEncodeError` — до удаления, то есть безопасно, но инструмент,
    который нельзя записать в лог, сломан. Проверяется печатью в поток cp1251:
    в нём эмодзи нет заведомо, а кириллица есть — то есть ловится именно то.
    """
    import io
    import sys

    db = tmp_path / "baby.db"
    # Поток строгий намеренно: смягчать обязан код, а не эта строка — иначе
    # тест проверял бы собственную настройку
    buffer = io.TextIOWrapper(io.BytesIO(), encoding="cp1251")
    stdout, sys.stdout = sys.stdout, buffer
    try:
        reset.say_before(
            db,
            {"families": 1},
            [(1, CHAT_ID, "Дочь🍼", 1501)],
            [("checkup", 16)],
        )
    finally:
        sys.stdout = stdout
    buffer.flush()
    printed = buffer.buffer.getvalue().decode("cp1251")  # type: ignore[attr-defined]

    assert "Дочь" in printed
    assert "1501" in printed, "про закреплённую панель обязано быть сказано"


def test_a_confirmation_typed_by_hand_is_accepted() -> None:
    """Подтверждение не зависит от того, каким путём пришёл ввод.

    PowerShell 5.1 подмешивает в пайп BOM, и правильно набранное слово не
    сходилось с константой. Поймано на боевом сбросе — до удаления, но выглядело
    как отказ без причины.
    """
    assert reset.confirmed("СБРОСИТЬ")
    assert reset.confirmed("  сбросить \n")
    assert reset.confirmed("\ufeffRESET")
    assert not reset.confirmed("нет")
    assert not reset.confirmed(""), "пустой ввод — это не подтверждение"


def test_a_family_without_a_title_is_still_remembered(tmp_path: Path) -> None:
    """Семья без названия остаётся узнаваемой.

    `title` у семьи nullable, и пустая строка в памяти читается как «не
    боевой», если сравнивать её с `None` невнимательно. Узнаёт сторож по
    `chat_id`, название — только для человека в отказе.
    """
    db = tmp_path / "baby.db"
    _build(db)
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(" ")
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO families (id, chat_id, tz, digest_dow, digest_time, "
            f"created_at, checkin_hours) VALUES (1, {CHAT_ID}, 'Asia/Yekaterinburg', "
            f"0, '10:00', '{now}', 0)"
        )
        conn.commit()
    finally:
        conn.close()

    reset.reset(db)

    assert prod_chats.is_production(CHAT_ID, db) is not None


# --- забыть одну семью -----------------------------------------------------


def test_the_scope_covers_every_table_of_the_order() -> None:
    """У каждой таблицы порядка есть условие, и лишних условий нет.

    Половину этого сторожит сам `wipe._check` на импорте — забытая таблица
    означала бы семью, вычищенную наполовину. Тест нужен ради второй половины
    и ради того, что проверкой множеств не ловится вовсе: условие обязано
    **работать**, то есть находить строки той семьи, а не соседней.
    """
    assert set(wipe.SCOPE) == set(WIPE_ORDER)


def test_forget_takes_one_family_and_leaves_the_neighbour(tmp_path: Path) -> None:
    """Забываем чужой чат — свой остаётся целым до последней строки.

    Инструмент завёлся, когда бота отдали второй семье: пробная группа, чат,
    из которого ушли, семья, попросившая себя удалить. До него на это был один
    ответ — сброс всей базы, то есть вместе с дневником своего ребёнка.

    `foreign_key_check` здесь не формальность: каскадов в схеме нет нигде, и
    строка `usage` соседа, потерявшая семью, — это ровно та поломка, на которой
    падает подготовка стенда.
    """
    db = tmp_path / "baby.db"
    _build(db)
    _fill(db)
    _fill(db, family_id=2, chat_id=-777)

    wiped = forget.forget(db, -777)

    assert wiped["families"] == 1
    assert wiped["children"] == 1
    assert wiped["usage"] == 1, "нажатия чужой семьи остались бы висячим ключом"

    conn = sqlite3.connect(db)
    try:
        for table in WIPE_ORDER:
            left = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
            assert left == 1, f"у соседа не осталось строк: {table}"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT count(*) FROM who_lms").fetchone()[0] == 1
    finally:
        conn.close()


def test_forgetting_an_unknown_chat_changes_nothing(tmp_path: Path) -> None:
    """Опечатка в `chat_id` не должна ничего стоить."""
    db = tmp_path / "baby.db"
    _build(db)
    _fill(db)

    assert forget.forget(db, -123) == {}

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM families").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_stand_guard_survives_forgetting(tmp_path: Path) -> None:
    """Забытый чат остаётся боевым для стенда.

    Та же поломка, что у сброса, и ровно поэтому тест такой же: строка
    `families` — единственное, по чему стенд узнаёт боевую группу. Забыв её
    молча, мы разоружаем сторож, написанный против прогона стенда по живому
    семейному чату.
    """
    db = tmp_path / "baby.db"
    _build(db)
    _fill(db)
    _fill(db, family_id=2, chat_id=-777)

    forget.forget(db, -777)

    assert prod_chats.is_production(-777, db) == "Дочь", (
        "забытый чат перестал узнаваться, и стенд зальёт в него тестового ребёнка"
    )
    assert prod_chats.is_production(CHAT_ID, db) == "Дочь", "живой чат забыт заодно"
