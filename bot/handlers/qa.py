"""Вопрос по базе знаний: кнопка «❓ Вопрос» и ответ на следующую реплику.

**Где этот роутер стоит и почему.** После `alarm` — это граница безопасности,
заложенная ещё на этапе 4: сообщение с тревожным признаком перехватывается
выше и получает фиксированный текст из файла, до модели оно не доходит вовсе.
Проверять красные флаги ещё и здесь было бы второй копией границы, а копия
расходится с оригиналом молча.

Перед `measure` — потому что там висит голый `F.text`, обработчик числа, и
апдейт до нас просто не дошёл бы.

**Ожидание вопроса — фильтр, а не хендлер.** Это главное решение файла.
Хендлер, проверяющий ожидание внутри себя, забирает апдейт себе в любом
случае: aiogram останавливается на первом сошедшемся фильтре, и вернувшийся
`None` для него — тоже «обработано». То есть голое число в ответ на «сколько
мл?» уехало бы сюда и молча пропало. Фильтр же, не совпав, пропускает апдейт
дальше нетронутым.

Ожидание хранится в `services/pending` вместе с ожиданиями чисел: источник
правды о том, чего бот ждёт, должен быть один.
"""

import logging

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from bot import keyboards, texts
from bot.db.models import Family
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.services import pending, qa

log = logging.getLogger(__name__)

router = Router(name="qa")


class AwaitsQuestion(BaseFilter):
    """Ждём ли мы от этого человека в этом чате именно вопрос."""

    async def __call__(self, message: Message, family: Family | None = None) -> bool:
        if family is None or message.from_user is None or not message.text:
            return False
        ask = pending.peek(family.chat_id, message.from_user.id)
        return ask is not None and ask.what == pending.QUESTION


async def start_question(message: Message, family: Family, user_id: int) -> None:
    """Начать ждать вопрос и сказать об этом. Ничего не ищет.

    Вызывающих двое: кнопка «❓ Вопрос» на панели (колбэк ниже) и та же кнопка
    на постоянной клавиатуре (`handlers/home.py`). Та же причина, по которой
    разнят `alarm.assess`/`respond`: две копии «запомнить ожидание и позвать
    человека» разошлись бы молча, а расхождение здесь означает ожидание,
    которое поставили, но о котором не сказали.
    """
    pending.remember(family.chat_id, user_id, pending.QUESTION)
    await message.answer(texts.ASK_QUESTION, reply_markup=keyboards.home())


@router.callback_query(IN_GROUP_CB, keyboards.AskCB.filter())
async def ask(call: CallbackQuery, family: Family) -> None:
    await call.answer()
    if isinstance(call.message, Message):
        await start_question(call.message, family, call.from_user.id)


async def answer_and_reply(message: Message, question: str) -> None:
    """Задать вопрос базе знаний и ответить одним сообщением.

    Вызывающих двое: этот роутер (вопрос текстом после кнопки «❓ Вопрос») и
    голосовой хендлер (вопрос, сказанный вслух). Четыре ветки по `result.kind`
    в двух копиях однажды разойдутся, и разойдутся молча — отличить «не нашёл»
    от «базы нет» по виду сообщения человек не сможет.

    Ответ уходит одним сообщением, каким бы он ни был, — и отправка тоже одна.
    Четыре ветки собирают текст, но не шлют его: с четырьмя отправками забыть
    разметку в одной из них ничего не стоило бы, а видно это было бы только у
    того, кто получил именно эту ветку.

    `NOT_FOUND` приезжает сюда двумя дорогами — поиск не нашёл ничего и модель
    не нашла ответа в найденном, — и выглядит одинаково намеренно. Для
    человека это один и тот же факт и один и тот же следующий шаг; различать
    их он не смог бы, а действовать по различию — тем более. Разницу знает
    `Result.reason`, и нужна она логу, а не чату.
    """
    # Печатает бот заметно дольше, чем отвечает на тап: цепочка моделей ждёт до
    # полутора минут. Без «печатает…» человек решит, что тап не сработал
    await message.bot.send_chat_action(message.chat.id, "typing")
    result = await qa.answer(question)

    if result.kind == qa.NO_KB:
        text = texts.QA_NO_KB
    elif result.kind == qa.NOT_FOUND:
        text = texts.QA_NOT_FOUND
    elif result.kind == qa.ANSWERED:
        text = texts.qa_answer(result.answer, result.chunks)
    else:
        text = texts.qa_extract(result.chunks)
    await message.reply(text, reply_markup=keyboards.home())


@router.message(IN_GROUP, F.text, AwaitsQuestion())
async def reply(message: Message, family: Family) -> None:
    """Вопрос пришёл."""
    pending.take(family.chat_id, message.from_user.id)
    await answer_and_reply(message, (message.text or "").strip())
