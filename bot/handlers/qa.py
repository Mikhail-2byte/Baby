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
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from bot import keyboards, texts
from bot.db.models import Family
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.services import pending, qa, qa_log

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
    result = await qa.answer(question, chat=message.chat.id)

    if result.kind == qa.NO_KB:
        text = texts.QA_NO_KB
    elif result.kind == qa.NOT_FOUND:
        text = texts.QA_NOT_FOUND
    elif result.kind == qa.ANSWERED:
        text = texts.qa_answer(result.answer, result.chunks)
    else:
        text = texts.qa_extract(result.chunks)

    # Единственное сообщение проекта, которое носит inline-разметку вместо
    # `keyboards.home()`, и размен назван вслух. Одно сообщение обе разметки не
    # носит, а без 👍/👎 верность ответа не измеряется ничем: `outcome` в
    # `qa.log` говорит, что бот **сделал**, а не был ли он прав.
    #
    # Носителя клавиатуры это не отнимает ни у кого: до ответа человек
    # обязательно получил либо «Напишите вопрос» (`start_question` выше), либо
    # эхо расшифровки (`handlers/voice.py`), и оба несут `home()`.
    #
    # Пустой `qid` означает, что перед нами заглушка, а не боевой путь: кнопка
    # `qav:up:` склеилась бы не с чем. Тогда ведём себя как прежде
    markup = keyboards.qa_verdict(result.qid) if result.qid else keyboards.home()
    await message.reply(text, reply_markup=markup)


@router.message(IN_GROUP, F.text, AwaitsQuestion())
async def reply(message: Message, family: Family) -> None:
    """Вопрос пришёл."""
    pending.take(family.chat_id, message.from_user.id)
    await answer_and_reply(message, (message.text or "").strip())


@router.callback_query(IN_GROUP_CB, keyboards.QaCB.filter())
async def verdict(call: CallbackQuery, callback_data: keyboards.QaCB) -> None:
    """Человек сказал, помог ответ или нет.

    Единственный источник правды о верности во всём проекте. Всё остальное,
    что мы знаем про Q&A, — это поведение бота: ответил, показал фрагменты,
    промолчал. Прав ли он был, не выводится из этого никак, а модель-судья в
    контуре была бы судьёй самой себе.

    **Кнопки гасятся сразу.** Без этого на один `qid` ляжет сколько угодно
    строк: передумавший человек, второй родитель, случайный тап по вчерашнему
    ответу. Сняв разметку, мы делаем «один вопрос — один вердикт» свойством
    экрана, а не правилом, которое отчёту пришлось бы применять задним числом.

    Строку в `usage` пишет `UsageMiddleware` сама, и `qid` она достаёт из
    `callback_data` — хендлеру передавать его некуда. Здесь пишется вторая
    половина, в `qa.log`: там лежит текст вопроса, и склейка идёт по `qid`.

    Вердикт под ответом, чей вопрос уже выпал из ротации `qa.log`, принимается
    молча: спорить с человеком, который нажал кнопку, не о чем, а склеивать
    такую строку отчёт и так умеет — он печатает «вердиктов без вопроса: N».
    """
    qa_log.write(
        event="qa_verdict",
        chat=call.message.chat.id if isinstance(call.message, Message) else None,
        qid=callback_data.qid,
        verdict=callback_data.action,
        calls=0,
    )
    await call.answer(texts.QA_THANKS if callback_data.qid else texts.QA_VERDICT_STALE)
    if isinstance(call.message, Message):
        with suppress(TelegramBadRequest):
            # Сообщение могло уехать за горизонт правки или уже лишиться
            # кнопок вторым родителем — ни то, ни другое не повод ругаться
            await call.message.edit_reply_markup(reply_markup=None)
