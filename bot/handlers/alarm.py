"""Тревожные признаки: перехват сообщения и команда `/redflags`.

Роутер стоит сразу после `common` — раньше просмотров, мастера и обработчика
голого числа. Порядок здесь не стилистический: если мама пишет про симптом,
находясь посреди мастера `/baby`, ответить надо про симптом, а не записать его
как имя ребёнка.

Перехват устроен фильтром, а не хендлером с `SkipHandler`. Разница
существенная: фильтр, не нашедший признаков, пропускает сообщение дальше
нетронутым, и обычная переписка семьи этот роутер вообще не замечает.

**Ни одного обращения к модели на этом пути.** Q&A (этап 6) стоит ПОСЛЕ этого
роутера, и экстренный ответ остаётся фиксированным текстом именно потому, что
порядок роутеров задан так. Проверки красных флагов внутри Q&A нет и не должно
быть: вторая копия границы безопасности разойдётся с первой молча.
"""

import logging

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import Message

from bot import texts
from bot.db.models import Child, Family
from bot.filters import IN_GROUP
from bot.services import growth, pending, redflags
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

router = Router(name="alarm")


class HasRedFlag(BaseFilter):
    """Есть ли в сообщении названные словами тревожные признаки.

    Возвращает `hits` и `level` хендлеру. Возраст берётся из карточки ребёнка:
    от него зависит самый важный порог во всём боте — 38 °C у младше трёх
    месяцев. Без карточки возраст неизвестен, и тогда возрастные признаки
    срабатывают все: молчать из-за незаполненной анкеты нельзя.
    """

    async def __call__(self, message: Message, child: Child | None = None) -> dict | bool:
        text = message.text or message.caption or ""
        if not text or text.startswith("/"):
            # Команды разбирают свои роутеры. «/today» тревожных слов не
            # содержит, но проверять каждую команду незачем
            return False

        # Возраст СКОРРЕКТИРОВАННЫЙ — тот же, по которому считаются центили и
        # вехи, и тот, что обещан в шапке `redflags.yaml`. Раньше здесь стоял
        # хронологический, и у ребёнка, родившегося на 34-й неделе, окно
        # «младше трёх месяцев» закрывалось на три недели раньше срока: порог
        # 38.0 переставал работать тогда, когда он ещё нужен. Поправка идёт в
        # сторону лишней тревоги, и это единственная допустимая здесь сторона
        age = growth.corrected_age_days(child, tu.now_utc()) if child is not None else None
        hits = redflags.triage(text, age)
        if not hits:
            return False
        return {"hits": hits, "level": redflags.worst(hits)}


@router.message(IN_GROUP, F.text | F.caption, HasRedFlag())
async def alarm(message: Message, hits: list, level: str, family: Family) -> None:
    """Фиксированный ответ. Ни генерации, ни разбора — только текст из файла."""
    log.warning(
        "Триаж сработал (%s): %s",
        level,
        ", ".join(hit.flag.id for hit in hits),
    )
    # Если человек нажал «❓ Вопрос» и написал про судороги, ответили на это мы,
    # и ждать вопроса больше незачем. Иначе ожидание висит ещё десять минут, и
    # следующая его реплика — «звоню 103», «одеваемся, едем» — уедет в поиск по
    # базе знаний ровно в ту минуту, когда бот обязан молчать
    if message.from_user is not None:
        pending.forget(family.chat_id, message.from_user.id)

    # Отвечаем реплаем на исходное сообщение: в семейном чате в этот момент
    # может идти другая переписка, и ответ обязан быть привязан к тому, что его
    # вызвало
    await message.reply(texts.triage_alert(hits, level))


@router.message(IN_GROUP, Command("redflags", "sos"))
async def show_list(message: Message) -> None:
    """Весь список — чтобы прочитать спокойно, а не встретить впервые в панике."""
    await message.answer(texts.redflags_list(list(redflags.FLAGS)))
