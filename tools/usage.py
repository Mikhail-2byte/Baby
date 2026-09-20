"""Отчёт о том, чем семьи реально пользуются.

    .venv\\Scripts\\python -m tools.usage
    .venv\\Scripts\\python -m tools.usage --days 14
    .venv\\Scripts\\python -m tools.usage --db data\\live\\baby.db
    .venv\\Scripts\\python -m tools.usage --csv data\\usage-csv

Скрипт, а не команда в боте, и это не лень. Отчёт читают редко и целиком, он не
участвует в рантайме и сломаться в бою не может; команда в меню означала бы ещё
один текст, ещё одного сторожа и ещё одну строку, которую надо держать
согласованной с `texts.COMMANDS`.

Читает два источника, и это не небрежность, а разделение по сроку жизни:

* **таблица `usage`** — нажатия. Ни одного слова человека там нет, поэтому она
  живёт сколько угодно и показывать её целиком не страшно;
* **`data/qa.log`** — обращения к моделям и тексты вопросов. Ротируется в одно
  поколение именно потому, что в нём лежат вопросы семьи о здоровье ребёнка.

Отсюда следствие, которое отчёт обязан печатать, а не прятать: вердикт,
нажатый под ответом, чей вопрос уже выпал из ротации, склеить не с чем. Такие
считаются отдельной строкой.

База открывается **только на чтение и без замка**: скрипт ничего не пишет, а
WAL разводит читателя с работающим ботом. Это ровно обратное `tools/reset.py`,
и разница названа там же — тот правит базу, этот на неё смотрит.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiogram.filters.callback_data import CallbackData

from bot import keyboards
from bot.config import settings
from bot.texts import plural

# Сколько строк показывать в топах. Больше не помещается на экран, а отчёт
# читают глазами — для машинной обработки есть `--csv`
TOP = 25


def read_only(db: Path) -> sqlite3.Connection:
    """Открыть на чтение. `mode=ro` ещё и не создаст файл при опечатке в пути."""
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def prefixes() -> dict[str, str]:
    """Префикс → имя класса, по всем кнопкам проекта.

    Реестр берётся из самого `bot.keyboards`, а не переписывается сюда
    списком. Вторая копия — не дубль, а расхождение с отложенным сроком: новая
    кнопка появилась бы в боте и не появилась бы в разделе «чего не жмут», то
    есть отчёт молча сказал бы, что ею не пользуются.
    """
    found = {}
    for name in dir(keyboards):
        value = getattr(keyboards, name)
        if (
            isinstance(value, type)
            and issubclass(value, CallbackData)
            and value is not CallbackData
        ):
            found[str(value.__prefix__)] = name
    return found


# --- чтение ---------------------------------------------------------------

COLUMNS = (
    "at",
    "kind",
    "action",
    "detail",
    "handled",
    "ms",
    "family_id",
    "member_id",
    "family",
    "who",
)


def taps(conn: sqlite3.Connection, since: datetime | None) -> list[dict[str, Any]]:
    """Нажатия с именами семьи и человека.

    `LEFT JOIN`, а не обычный: у апдейта из лички обеих ссылок нет вовсе, и
    внутреннее соединение выбросило бы ровно тот случай, ради которого личку и
    считают, — «боту написал кто-то незнакомый».
    """
    where = "WHERE u.at >= ?" if since else ""
    args = [since.isoformat(" ")] if since else []
    rows = conn.execute(
        "SELECT u.at, u.kind, u.action, u.detail, u.handled, u.ms, "
        "       u.family_id, u.member_id, f.title, m.display_name "
        "  FROM usage u "
        "  LEFT JOIN families f ON f.id = u.family_id "
        "  LEFT JOIN members  m ON m.id = u.member_id "
        f" {where} "
        " ORDER BY u.at",
        args,
    ).fetchall()
    return [dict(zip(COLUMNS, row, strict=True)) for row in rows]


def model_log(path: Path, since: datetime | None) -> list[dict[str, Any]]:
    """Разобрать `qa.log` и предыдущее поколение.

    Порядок файлов важен: `.1` старше, значит читается первым — иначе отчёт
    показал бы историю задом наперёд. Битая строка пропускается молча: журнал
    дописывается работающим ботом, и последняя строка может оказаться
    оборванной ровно в момент чтения.
    """
    records: list[dict[str, Any]] = []
    for file in (path.with_suffix(path.suffix + ".1"), path):
        if not file.exists():
            continue
        with file.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if since and str(record.get("at", "")) < since.isoformat():
                    continue
                records.append(record)
    return records


# --- печать ---------------------------------------------------------------


def _head(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


def say_reach(rows: list[dict[str, Any]]) -> None:
    _head("Кто пользуется")
    if not rows:
        print("  нажатий не записано ни одного")
        return

    by_family: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_family[row["family_id"]].append(row)

    for family_id, own in sorted(by_family.items(), key=lambda pair: -len(pair[1])):
        days = {str(row["at"])[:10] for row in own}
        name = own[-1]["family"] or ("личка" if family_id is None else f"#{family_id}")
        taps_word = plural(len(own), "нажатие", "нажатия", "нажатий")
        days_word = plural(len(days), "день", "дня", "дней")
        print(f"  {name}: {len(own)} {taps_word}, {len(days)} {days_word}")
        print(f"      с {min(days)} по {max(days)}")
        who = Counter(row["who"] for row in own if row["who"])
        if who:
            print("      " + ", ".join(f"{n} — {c}" for n, c in who.most_common()))


def say_actions(rows: list[dict[str, Any]]) -> None:
    _head("Что жмут")
    counts = Counter(str(row["action"]) for row in rows)
    total = sum(counts.values()) or 1
    for action, count in counts.most_common(TOP):
        print(f"  {count:5}  {100 * count / total:4.1f}%  {action}")


def say_untouched(rows: list[dict[str, Any]]) -> None:
    """Кнопки, которых не коснулся никто. Главный раздел отчёта.

    Сверка идёт на уровне префиксов, а не отдельных действий, и это названная
    цена: перечислить все возможные `action` из кода нельзя, не заведя второй
    копии реестра. Префикс отвечает на вопрос «этим сценарием вообще
    пользовались» — а это и есть то, ради чего отчёт открывают.
    """
    _head("Чего не жмут")
    seen = {str(row["action"]).split(":")[0] for row in rows if row["kind"] == "button"}
    known = prefixes()
    cold = sorted(prefix for prefix in known if prefix not in seen)
    if not cold:
        print("  все сценарии кнопок хоть раз открывали")
        return
    for prefix in cold:
        print(f"  {prefix:6} {known[prefix]}")


def say_dead(rows: list[dict[str, Any]]) -> None:
    """Апдейты, которых не забрал ни один роутер.

    Кнопка без действия в чате молчит, а в коде выглядит целой — увидеть её
    больше нечем. Обычный текст сюда попадает штатно: реплика, не подошедшая
    ни под один фильтр, — это разговор семьи, а не поломка.
    """
    _head("Ничего не сделали")
    cold = Counter(
        f"{row['kind']}:{row['action']}" for row in rows if not row["handled"]
    )
    if not cold:
        print("  таких не было")
        return
    for what, count in cold.most_common(TOP):
        mark = "  <- кнопка без обработчика" if what.startswith("button:") else ""
        print(f"  {count:5}  {what}{mark}")


def say_slow(rows: list[dict[str, Any]]) -> None:
    """Медиана считается по `kind`, и одно общее число тут не значило бы ничего:
    у вопроса в `ms` лежит вся цепочка моделей, у тапа по подгузнику — двадцать
    миллисекунд.
    """
    _head("Сколько ждут")
    by_kind: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        by_kind[str(row["kind"])].append(int(row["ms"] or 0))
    order = sorted(by_kind.items(), key=lambda pair: -statistics.median(pair[1]))
    for kind, values in order:
        print(
            f"  {kind:8} медиана {statistics.median(values):6.0f} мс, "
            f"худшее {max(values):6} мс, {len(values)} шт"
        )


def say_models(records: list[dict[str, Any]]) -> None:
    _head("Обращения к моделям")
    answers = [r for r in records if r.get("event") in {"qa", "voice"}]
    stt = [r for r in records if r.get("event") == "stt"]
    if not answers and not stt:
        print("  обращений не записано")
        return

    calls = sum(int(r.get("calls") or 0) for r in answers)
    print(f"  вопросов и разборов: {len(answers)}, запросов к моделям: {calls}")
    quota = sum(1 for r in answers if r.get("reason") == "quota")
    if quota:
        print(f"  упёрлись в суточную квоту: {quota}")

    for day, count in sorted(Counter(str(r.get("at", ""))[:10] for r in answers).items()):
        spent = sum(
            int(r.get("calls") or 0)
            for r in answers
            if str(r.get("at", "")).startswith(day)
        )
        asked = plural(count, "обращение", "обращения", "обращений")
        made = plural(spent, "запрос", "запроса", "запросов")
        print(f"    {day}: {count} {asked}, {spent} {made}")

    print("  по моделям:")
    for model, count in Counter(str(r.get("model") or "-") for r in answers).most_common():
        print(f"    {count:4}  {model}")

    print("  исходы:")
    for outcome, count in Counter(
        str(r.get("outcome") or "-") for r in answers
    ).most_common():
        print(f"    {count:4}  {outcome}")

    if stt:
        failed = sum(1 for r in stt if not r.get("ok"))
        seconds = sum(float(r.get("secs") or 0) for r in stt)
        print(
            f"  расшифровок: {len(stt)}, неудачных {failed}, "
            f"звука {seconds / 60:.0f} мин"
        )


def say_verdicts(records: list[dict[str, Any]]) -> None:
    """Единственный раздел, который говорит о верности, а не о поведении.

    Всё остальное меряет, что бот **сделал**. Был ли он прав, знает только
    человек, нажавший 👍 или 👎, и другого источника у этого числа нет.
    """
    _head("Помогло или нет")
    verdicts = [r for r in records if r.get("event") == "qa_verdict"]
    if not verdicts:
        print("  никто ещё не оценивал")
        return

    # Последний вердикт по вопросу побеждает: человек вправе передумать, а
    # второй родитель — не согласиться. Кнопки после первого тапа гаснут, так
    # что пара строк на один `qid` означает именно это, а не двойной тап
    latest: dict[str, str] = {}
    for record in verdicts:
        qid = str(record.get("qid") or "")
        if qid:
            latest[qid] = str(record.get("verdict") or "")

    questions = {
        str(r.get("qid")): r for r in records if r.get("event") == "qa" and r.get("qid")
    }
    up = sum(1 for verdict in latest.values() if verdict == "up")
    down = sum(1 for verdict in latest.values() if verdict == "down")
    print(f"  помогло {up}, не то {down}")

    orphans = sum(1 for qid in latest if qid not in questions)
    if orphans:
        print(f"  вердиктов без вопроса: {orphans} (вопрос выпал из ротации qa.log)")

    by_outcome: Counter[str] = Counter()
    for qid, verdict in latest.items():
        record = questions.get(qid)
        if record:
            by_outcome[f"{record.get('outcome')}/{verdict}"] += 1
    if by_outcome:
        print("  по исходам:")
        for pair, count in by_outcome.most_common():
            print(f"    {count:4}  {pair}")

    bad = [
        questions[qid]
        for qid, verdict in latest.items()
        if verdict == "down" and qid in questions
    ]
    if bad:
        print("\n  Что не понравилось:")
        for record in bad[:TOP]:
            print(f"    [{record.get('outcome')}] {record.get('question')}")

    silent = [
        r for r in records if r.get("event") == "qa" and r.get("outcome") == "not_found"
    ]
    if silent:
        print(f"\n  Промолчал ({len(silent)}):")
        for record in silent[:TOP]:
            print(f"    {record.get('question')}")


def write_csv(out: Path, rows: list[dict[str, Any]], records: list[dict[str, Any]]) -> None:
    """Выгрузка для сводных таблиц — и для того, чтобы пережить сброс базы.

    `tools/reset.py` уносит `usage` вместе с семьями (иначе остались бы висячие
    ключи), то есть перед сбросом отчёт надо выгрузить. Сказано об этом и здесь,
    и в `README.md`, и в докстроке `tools/wipe.py`.

    `utf-8-sig` и точка с запятой — как в `/export`: без них русский Excel
    открывает файл одной колонкой в кракозябрах.
    """
    out.mkdir(parents=True, exist_ok=True)

    taps_file = out / "usage.csv"
    with taps_file.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)

    log_file = out / "models.csv"
    fields = sorted({key for record in records for key in record})
    with log_file.open("w", encoding="utf-8-sig", newline="") as handle:
        log_writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
        log_writer.writeheader()
        for record in records:
            log_writer.writerow({key: record.get(key, "") for key in fields})

    print(f"\nВыгружено: {taps_file} и {log_file}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="tools.usage")
    parser.add_argument("--db", type=Path, default=settings.db_path)
    parser.add_argument("--days", type=int, default=0, help="окно; 0 — за всё время")
    parser.add_argument("--csv", type=Path, default=None, help="куда выгрузить")
    args = parser.parse_args()
    db: Path = args.db

    if not db.exists():
        print(f"Нет базы {db}")
        return 1

    since = (
        datetime.now(UTC).replace(tzinfo=None) - timedelta(days=args.days)
        if args.days
        else None
    )

    conn = read_only(db)
    try:
        rows = taps(conn, since)
    except sqlite3.OperationalError:
        # Таблицы нет — значит миграция ещё не накатывалась. Это штатное
        # состояние ровно один раз: между `git pull` и ближайшим `run.cmd`,
        # который зовёт `alembic upgrade head`. Трассировка тут сказала бы
        # правду и не сказала бы, что делать
        print(f"\nВ {db} ещё нет таблицы usage — не накатана миграция.")
        print("Запустите бота (run.cmd накатит сам) или:")
        print("  .venv\\Scripts\\alembic upgrade head")
        return 1
    finally:
        conn.close()
    records = model_log(db.parent / "qa.log", since)

    window = (
        f"за {args.days} {plural(args.days, 'день', 'дня', 'дней')}"
        if args.days
        else "за всё время"
    )
    print(f"Использование {window}: {db}")

    say_reach(rows)
    say_actions(rows)
    say_untouched(rows)
    say_dead(rows)
    say_slow(rows)
    say_models(records)
    say_verdicts(records)

    if args.csv:
        write_csv(args.csv, rows, records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
