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


@router.callback_query(IN_GROUP_CB, keyboards.AskCB.filter())
async def ask(call: CallbackQuery, family: Family) -> None:
    pending.remember(family.chat_id, call.from_user.id, pending.QUESTION)
    await call.answer()
    if isinstance(call.message, Message):
        await call.message.answer(texts.ASK_QUESTION)


@router.message(IN_GROUP, F.text, AwaitsQuestion())
async def reply(message: Message, family: Family) -> None:
    """Вопрос пришёл. Ответ уходит одним сообщением, каким бы он ни был."""
    pending.take(family.chat_id, message.from_user.id)
    question = (message.text or "").strip()

    # Печатает бот заметно дольше, чем отвечает на тап: цепочка моделей ждёт до
    # полутора минут. Без «печатает…» человек решит, что тап не сработал
    await message.bot.send_chat_action(message.chat.id, "typing")
    result = await qa.answer(question)

    if result.kind == qa.NO_KB:
        await message.reply(texts.QA_NO_KB)
        return
    if result.kind == qa.NOT_FOUND:
        await message.reply(texts.QA_NOT_FOUND)
        return
    if result.kind == qa.ANSWERED:
        await message.reply(texts.qa_answer(result.answer, result.chunks))
        return
    await message.reply(texts.qa_extract(result.chunks))
