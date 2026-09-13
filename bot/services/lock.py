"""Замок одного экземпляра: два бота на одном токене — это тихая потеря данных.

Повод — вечер 01.09.2026. Бота подняли вручную поверх уже работавшего под
задачей Планировщика, и получилось два long polling на одном токене. Ни один из
них не упал и не сказал ни слова в чат: Telegram отдаёт апдейт кому-то одному,
второй получает `Conflict: terminated by other getUpdates request`, пишет строку
в `data/bot.log` и через пять секунд пробует снова. За двадцать минут набежало
семьдесят шесть таких строк, семья в это время теряла часть сообщений, а бот со
стороны выглядел живым.

Вред не только в конкуренции за апдейты. Второй экземпляр на старте делает
`delete_webhook(drop_pending_updates=True)` и **выбрасывает очередь Telegram**,
которую первый ещё не забрал. То есть лишний запуск стоит семье сообщений, даже
если сразу его закрыть.

Почему замок здесь, а не в `run.cmd`. Проверка в скрипте запуска стережёт ровно
один путь — свой. Мимо неё проходят двойной щелчок по `python -m bot`, запуск из
другой сессии, вторая копия контейнера на том же томе. Замок внутри процесса
стоит на всех путях сразу.

Почему блокировка средствами ОС, а не PID-файл. PID-файл переживает жёсткое
убийство процесса и остаётся протухшим — а именно так бот обычно и умирает:
`Stop-Process -Force`, выключение питания, `taskkill /f`. Никакой `finally` там
не отрабатывает (`__main__` ловит только `KeyboardInterrupt`). Замок ОС снимается
вместе с процессом, без нашего участия и без разбора «а жив ли ещё тот PID».
"""

import contextlib
import logging
import sys
import time
from pathlib import Path
from typing import IO

from bot.config import settings

log = logging.getLogger(__name__)

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# Рядом с базой: `data/` смонтирована томом в Docker и лежит в `.gitignore`.
# На VPS это даёт побочную выгоду — вторую копию контейнера на том же томе
# замок поймает так же, как вторую копию процесса дома
PATH = settings.db_path.parent / "bot.lock"

# Сколько ждать освобождения, прежде чем сдаться. Ноль не годится: штатный
# перезапуск выглядит как короткое наложение — старый экземпляр ещё держит
# `getUpdates`, пока новый уже стартует, и восемь `TelegramConflictError`
# рассасываются сами (README, «Проверка перезапуска 30.08»). Замок, отказывающий
# мгновенно, превратил бы это в «бот не поднялся, ждите пять минут до следующего
# тика сторожа»
WAIT_SECONDS = 60.0
POLL_SECONDS = 2.0


class SingleInstance:
    """Захваченный замок. Держите ссылку живой всё время работы бота.

    Замок держится **открытым файловым дескриптором**, а не содержимым файла.
    Закроется дескриптор — замок исчезнет, даже если файл на месте.
    """

    def __init__(self, path: Path = PATH) -> None:
        self.path = path
        self._fh: IO[bytes] | None = None

    def acquire(self, timeout: float = WAIT_SECONDS) -> bool:
        """Взять замок, подождав до `timeout` секунд. `False` — уже занят."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # `a+b`, а не `w`: `w` обрезает файл, а обрезать чужой замок — плохая
        # идея даже когда это безвредно
        handle = open(self.path, "a+b")  # noqa: SIM115 — закрываем в release
        deadline = time.monotonic() + timeout
        while True:
            if _take(handle):
                self._fh = handle
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                handle.close()
                return False
            time.sleep(min(POLL_SECONDS, left))

    def release(self) -> None:
        """Отдать замок. Исключений наружу не выпускает: это шаг остановки."""
        handle, self._fh = self._fh, None
        if handle is None:
            return
        try:
            _give_back(handle)
        except OSError:
            log.warning("Не удалось снять замок %s", self.path, exc_info=True)
        finally:
            with contextlib.suppress(OSError):
                handle.close()


def _take(handle: IO[bytes]) -> bool:
    """Попытка захвата без ожидания. Занято — `False`, а не исключение."""
    try:
        handle.seek(0)
        if sys.platform == "win32":
            # Блокируется один байт от текущей позиции, поэтому `seek(0)` выше
            # обязателен — и здесь, и при снятии
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _give_back(handle: IO[bytes]) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
