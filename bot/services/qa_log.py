"""Лог вопросов и счётчик суточной квоты — `data/qa.log`.

Упрощённая копия `parse_log` из Family_planer: здесь нет карточек и вердиктов
человека, есть только строка на вопрос.

**Зачем файл, а не таблица.** Смотреть в него будут глазами и редко: «что
спрашивали», «сколько вызовов ушло», «почему ответа не было». Отдельная
таблица с миграцией ради этого не окупается, а база знаний и данные семьи и
так живут в разных файлах.

**Зачем счётчик.** У бесплатного тира OpenRouter суточный лимит считается
**на аккаунт**, а не на ключ, и этот бот ест ту же квоту, что Family_planer.
Провайдер о приближении к лимиту не предупреждает — он просто начинает
отвечать 429. Без своего счёта бот молча перестал бы отвечать, а человек решил
бы, что сломалась сеть.

Сутки считаем по UTC: там же граница у самого OpenRouter. Таймзона семьи тут
ни при чём — лимит не её.

Всё, что здесь происходит, — побочное. Исключения глотаются целиком: пункт 3
контракта ошибок проекта ровно про это. Упавшая запись в лог не должна стоить
человеку ответа.
"""

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

from bot.config import settings

log = logging.getLogger(__name__)

PATH = settings.db_path.parent / "qa.log"

# Ротация в одно поколение. Потолок нужен не ради места на диске, а ради того,
# что в файле лежит: вопросы семьи о здоровье ребёнка. Хранить их годами ради
# подсчёта вызовов незачем — тот же довод, по которому суточный бэкап не
# уходит в облако
MAX_BYTES = 2 * 1024 * 1024

_counted_day: date | None = None
_counted = 0


def _today() -> date:
    return datetime.now(UTC).date()


def write(**record: Any) -> None:
    """Дописать строку JSON. Исключений наружу не выпускает."""
    record["at"] = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    spent = _spent(record)
    if spent:
        _bump(spent)
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        _rotate()
        with PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("Не удалось записать %s", PATH)


def _rotate() -> None:
    """Переложить разросшийся лог в `.1`.

    `replace`, а не «дописать и обрезать»: переименование атомарно, и
    оборвавшись на нём, мы теряем в худшем случае одно поколение, а не файл.
    """
    if not PATH.exists() or PATH.stat().st_size < MAX_BYTES:
        return
    PATH.replace(PATH.with_suffix(PATH.suffix + ".1"))
    log.info("qa.log перевалил %s байт — переложен в .1", MAX_BYTES)


def _spent(record: dict[str, Any]) -> int:
    """Сколько запросов к модели стоила эта строка.

    Считаем запросы, а не строки: с цепочкой моделей один вопрос стоит до
    девяти обращений, и счёт по строкам разошёлся бы с правдой в разы. Число
    приносит `llm.Answer.calls`, и в нём уже учтены неудачные ответы —
    401 и 429 тратят квоту аккаунта ровно так же, как удачный разбор.
    """
    try:
        return max(0, int(record.get("calls", 0)))
    except (TypeError, ValueError):
        return 0


def _bump(spent: int) -> None:
    global _counted_day, _counted
    today = _today()
    if _counted_day != today:
        _counted_day, _counted = today, 0
    _counted += spent


def calls_today() -> int:
    """Сколько запросов к модели ушло сегодня.

    Первый вызов после старта читает файл, дальше счётчик живёт в памяти.
    Сбой чтения — это `0`, а не исключение: счётчик существует ради подсказки,
    и уронить из-за него ответ было бы хуже, чем ошибиться в подсказке.
    """
    global _counted_day, _counted
    today = _today()
    if _counted_day == today:
        return _counted

    _counted_day, _counted = today, 0
    stamp = today.isoformat()
    try:
        with PATH.open(encoding="utf-8") as handle:
            for line in handle:
                # Дешёвая отсечка до разбора JSON: строк за прошлые дни в файле
                # заведомо больше, чем за сегодня
                if stamp not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if str(record.get("at", "")).startswith(stamp):
                    _counted += _spent(record)
    except FileNotFoundError:
        pass
    except Exception:
        log.warning("Не удалось пересчитать %s", PATH, exc_info=True)
    return _counted


def quota_spent() -> bool:
    """Суточный лимит обращений к модели исчерпан?

    `0` в настройке выключает проверку: у платного ключа суточного потолка нет.
    """
    limit = settings.llm_daily_limit
    return bool(limit) and calls_today() >= limit


def reset() -> None:
    """Забыть счётчик. Нужно тестам: он живёт в модуле и течёт между ними."""
    global _counted_day, _counted
    _counted_day, _counted = None, 0
