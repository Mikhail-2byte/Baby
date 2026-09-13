"""Ожидание числа от человека — без FSM.

Зачем не FSM. Состояние aiogram блокирующее: пока оно висит, следующая реплика
человека достаётся шагу мастера, а не тому роутеру, которому предназначалась.
В сценарии «мама нажала ⚖️ Вес, отвлеклась на ребёнка, вернулась и написала
что-то другое» это значит, что её фраза молча уедет в поле веса. Таймаута у FSM
нет — висеть состояние будет до перезапуска бота.

Здесь наоборот: ожидание ничего не блокирует. Бот запомнил, что спросил число, и
если в ближайшие `TTL` минут тот же человек в том же чате пришлёт голое число —
оно пойдёт по назначению. Пришлёт что-то другое или промолчит — ожидание тихо
истечёт, и ни одна кнопка от этого не сломается.

Ключ — «чат + пользователь», а не «чат»: в семейном чате двое, и вес, введённый
мамой, не должен попасть в бутылочку, про которую спрашивали папу.

Тем же механизмом ждётся вопрос к базе знаний (`QUESTION`), и по той же
причине: мама нажала «Спросить», отвлеклась, вернулась и написала мужу про
ужин — эта реплика не должна уехать в поиск по клиническим рекомендациям.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from bot.services import timeutil as tu

log = logging.getLogger(__name__)

# Что именно ждём
BOTTLE_ML = "bottle_ml"
PUMP_ML = "pump_ml"
WEIGHT_G = "weight_g"
TEMP_C = "temp_c"
# Единственное ожидание не числа: вопрос к базе знаний. Живёт здесь, а не
# отдельным механизмом, ровно затем, чтобы источник правды о том, чего бот
# ждёт, остался один. На нём же держится порядок роутеров: обработчик вопроса
# висит фильтром по этому ожиданию и пропускает апдейт дальше, если ожидания
# нет, — иначе он съел бы голое число и ввод веса перестал бы работать молча
QUESTION = "question"

# Десять минут: достаточно, чтобы дойти до весов и вернуться, и мало, чтобы
# случайное число через час попало не туда
TTL = timedelta(minutes=10)

# Потолок на случай, если что-то пойдёт не так и словарь начнёт расти. Семья из
# двух человек столько не наберёт никогда — это страховка, а не режим работы
MAX_ENTRIES = 50


@dataclass
class Ask:
    what: str
    # Событие, которое надо дополнить. Для веса пусто — там создаётся новая
    # запись, а не правится существующая
    row_id: int | None
    asked_at: datetime


_asks: dict[tuple[int, int], Ask] = {}


def remember(chat_id: int, user_id: int, what: str, row_id: int | None = None) -> None:
    if len(_asks) >= MAX_ENTRIES:
        # Вытесняем самое старое, а не чистим всё: чужое незаконченное ожидание
        # не повод ронять текущее
        oldest = min(_asks, key=lambda key: _asks[key].asked_at)
        del _asks[oldest]
    _asks[(chat_id, user_id)] = Ask(what=what, row_id=row_id, asked_at=tu.now_utc())


def take(chat_id: int, user_id: int, now: datetime | None = None) -> Ask | None:
    """Забрать ожидание, если оно есть и не протухло. Забирается один раз."""
    ask = _asks.pop((chat_id, user_id), None)
    if ask is None:
        return None
    if (now or tu.now_utc()) - ask.asked_at > TTL:
        return None
    return ask


def peek(chat_id: int, user_id: int, now: datetime | None = None) -> Ask | None:
    """Посмотреть, не забирая — нужно фильтру, решающему, брать ли сообщение."""
    ask = _asks.get((chat_id, user_id))
    if ask is None:
        return None
    if (now or tu.now_utc()) - ask.asked_at > TTL:
        del _asks[(chat_id, user_id)]
        return None
    return ask


def forget(chat_id: int, user_id: int) -> None:
    _asks.pop((chat_id, user_id), None)


def clear() -> None:
    """Только для тестов: между прогонами словарь должен быть пуст."""
    _asks.clear()


def parse_number(text: str) -> float | None:
    """Голое число из реплики, или None.

    Намеренно строго: принимаем только число, возможно с запятой и единицей
    измерения. «Дал 60 мл где-то в полночь» сюда не попадёт — такие фразы разбирает
    LLM на этапе свободного ввода, и путать два механизма нельзя.
    """
    cleaned = (
        text.replace(",", ".")
        .replace("мл", "")
        .replace("ml", "")
        .replace("кг", "")
        .replace("гр", "")
        .replace("г", "")
        .strip()
    )
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None
