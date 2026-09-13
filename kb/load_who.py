"""Загрузка стандартов роста ВОЗ в таблицу `who_lms`.

Запускать из корня проекта, один раз:

    .venv\\Scripts\\python kb\\load_who.py

Скрипт скачивает шесть таблиц (вес, длина, окружность головы × девочки,
мальчики), достаёт из них коэффициенты L, M, S по дням и кладёт в базу бота.
Файлы сохраняются в `kb/raw/who/` — при повторном запуске берутся оттуда, сеть
больше не нужна. `--offline` запрещает скачивать вовсе.

**Самое важное здесь — не загрузка, а проверка.** В таблицах ВОЗ, кроме L, M, S,
лежат уже посчитанные значения на каждом стандартном отклонении (SD3neg…SD3).
Это позволяет сверить нашу реализацию формулы LMS с тем, что считает сама ВОЗ,
на каждой строке каждой таблицы — то есть примерно на сорока тысячах точек.
Если `services/growth.py` когда-нибудь разойдётся со стандартом, узнать об этом
лучше здесь, а не из странного центиля в панели.

Лицензия данных: CC BY-NC-SA 3.0 IGO. Для приватного семейного бота подходит.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

import openpyxl
from sqlalchemy import create_engine, delete, insert

# Скрипт лежит в `kb/`, а импортирует `bot.*` — добавляем корень в путь, чтобы
# его можно было запускать и как `python kb/load_who.py`, и из любого каталога
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import settings  # noqa: E402
from bot.db.models import HEAD, LENGTH, WEIGHT, WhoLms  # noqa: E402
from bot.services.growth import zscore  # noqa: E402

RAW = ROOT / "kb" / "raw" / "who"
BASE = (
    "https://cdn.who.int/media/docs/default-source/child-growth"
    "/child-growth-standards/indicators"
)

# Путь у длины — `expandable-tables`, а не `expanded-tables`, как у остальных
# двух. Это не опечатка ВОЗ в одном месте, а два разных написания на сайте;
# первая попытка загрузки по единому шаблону дала 404 ровно на этих двух файлах
SOURCES: list[tuple[str, str, str, str]] = [
    (WEIGHT, "f", "wfa-girls",
     f"{BASE}/weight-for-age/expanded-tables/wfa-girls-zscore-expanded-tables.xlsx"),
    (WEIGHT, "m", "wfa-boys",
     f"{BASE}/weight-for-age/expanded-tables/wfa-boys-zscore-expanded-tables.xlsx"),
    (LENGTH, "f", "lhfa-girls",
     f"{BASE}/length-height-for-age/expandable-tables/lhfa-girls-zscore-expanded-tables.xlsx"),
    (LENGTH, "m", "lhfa-boys",
     f"{BASE}/length-height-for-age/expandable-tables/lhfa-boys-zscore-expanded-tables.xlsx"),
    (HEAD, "f", "hcfa-girls",
     f"{BASE}/head-circumference-for-age/expanded-tables/hcfa-girls-zscore-expanded-tables.xlsx"),
    (HEAD, "m", "hcfa-boys",
     f"{BASE}/head-circumference-for-age/expanded-tables/hcfa-boys-zscore-expanded-tables.xlsx"),
]

# Колонки, по которым сверяем формулу. Крайние (SD4) не берём: за ±3 у весовых
# показателей действует линейная шкала стандарта, и прямая формула там неприменима
CHECK_COLUMNS = {"SD3neg": -3, "SD2neg": -2, "SD1neg": -1, "SD0": 0, "SD1": 1, "SD2": 2, "SD3": 3}

# Допуск сверки. Таблицы округлены до четырёх знаков, и ошибка округления
# входного значения умножается на крутизну z по величине: у веса на третьем
# отклонении это несколько тысячных
TOLERANCE = 0.01


def fetch(name: str, url: str, *, offline: bool) -> Path:
    """Взять файл из `kb/raw/who/`, при отсутствии — скачать."""
    dest = RAW / f"{name}.xlsx"
    if dest.exists():
        return dest
    if offline:
        raise SystemExit(f"Нет файла {dest}, а скачивать запрещено (--offline)")

    RAW.mkdir(parents=True, exist_ok=True)
    print(f"  качаю {name}…", end=" ", flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(
            f"\nНе скачалось: {exc}\n"
            f"Скачайте вручную в {dest} и запустите снова."
        ) from exc
    dest.write_bytes(payload)
    print(f"{len(payload) // 1024} КБ")
    return dest


def read_table(path: Path) -> tuple[list[dict], list[tuple[int, str, float]]]:
    """Строки L, M, S и контрольные точки SD из одного файла.

    Читаем в режиме `read_only`: таблицы на 1858 строк, и обычный режим
    openpyxl держит их в памяти целиком без всякой нужды.
    """
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]

    rows = sheet.iter_rows(values_only=True)
    header = [str(cell).strip() if cell is not None else "" for cell in next(rows)]
    index = {name: position for position, name in enumerate(header)}

    for required in ("Day", "L", "M", "S"):
        if required not in index:
            raise SystemExit(f"{path.name}: нет колонки {required}. Формат ВОЗ изменился?")

    lms: list[dict] = []
    checks: list[tuple[int, str, float]] = []
    for row in rows:
        if row[index["Day"]] is None:
            continue
        day = int(row[index["Day"]])
        lms.append(
            {
                "age_days": day,
                "l": float(row[index["L"]]),
                "m": float(row[index["M"]]),
                "s": float(row[index["S"]]),
            }
        )
        for column, _ in CHECK_COLUMNS.items():
            if column in index and row[index[column]] is not None:
                checks.append((day, column, float(row[index[column]])))

    workbook.close()
    return lms, checks


def verify(indicator: str, lms: list[dict], checks: list[tuple[int, str, float]]) -> int:
    """Сверить нашу формулу с готовыми значениями ВОЗ. Вернуть число расхождений.

    Проверка идёт в обратную сторону: берём значение, которое ВОЗ считает
    лежащим ровно на n-м отклонении, и требуем, чтобы наш `zscore` вернул n.
    """
    by_day = {row["age_days"]: row for row in lms}
    bad = 0
    for day, column, value in checks:
        row = by_day.get(day)
        if row is None:
            continue
        expected = CHECK_COLUMNS[column]
        got = zscore(value, row["l"], row["m"], row["s"], indicator)
        if abs(got - expected) > TOLERANCE:
            bad += 1
            if bad <= 3:  # первые три — в лог, остальные только счётчиком
                print(
                    f"    расхождение: день {day}, {column}: "
                    f"ждали {expected}, получили {got:.4f}"
                )
    return bad


def main() -> int:
    parser = argparse.ArgumentParser(description="Загрузить стандарты роста ВОЗ")
    parser.add_argument(
        "--offline", action="store_true", help="не скачивать, брать только локальные файлы"
    )
    parser.add_argument(
        "--skip-verify", action="store_true", help="не сверять формулу с таблицами ВОЗ"
    )
    args = parser.parse_args()

    print(f"База: {settings.db_path}")
    engine = create_engine(f"sqlite:///{settings.db_path}")

    total = 0
    mismatches = 0
    with engine.begin() as connection:
        # Полная перезагрузка, а не добавление: таблицы ВОЗ неизменны, и
        # единственный смысл повторного запуска — починить испорченную загрузку
        connection.execute(delete(WhoLms.__table__))

        for indicator, sex, name, url in SOURCES:
            path = fetch(name, url, offline=args.offline)
            lms, checks = read_table(path)

            if not args.skip_verify:
                bad = verify(indicator, lms, checks)
                mismatches += bad
                # Без галочек и крестиков: консоль Windows работает в cp1251 и
                # роняет скрипт на первом же символе вне кодировки
                mark = f"РАСХОЖДЕНИЙ {bad}" if bad else "сверка ok"
                print(f"  {name:11} {len(lms):>5} дней, {mark} ({len(checks)} точек)")
            else:
                print(f"  {name:11} {len(lms):>5} дней")

            connection.execute(
                insert(WhoLms.__table__),
                [{"sex": sex, "indicator": indicator, **row} for row in lms],
            )
            total += len(lms)

    print(f"\nЗагружено {total} строк.")
    if mismatches:
        # Не «предупреждение»: расхождение означает, что центили в панели
        # считаются не так, как считает ВОЗ, и доверять им нельзя
        print(f"ВНИМАНИЕ: {mismatches} расхождений с таблицами ВОЗ — центили неверны.")
        return 1
    if not args.skip_verify:
        print("Формула сверена с таблицами ВОЗ на всех контрольных точках.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
