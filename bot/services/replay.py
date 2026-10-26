"""Очередь Telegram сохраняется; накопившиеся нажатия не исполняются вслепую."""

import logging
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.types import Message

from bot import keyboards

log = logging.getLogger(__name__)


def message_moment(message: Message, now: datetime) -> datetime:
    moment = message.date
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return min(moment, now)


async def drain(bot: Bot, dp: Dispatcher) -> None:
    """Подтверждаем пачку только после обработки. При сбое очередь остаётся.

    У callback нет времени нажатия. После простоя их безопаснее предложить
    повторить, чем запустить таймер неизвестным временем. Текст обрабатывается
    по message.date; его изменения идемпотентны по (chat_id, message_id).
    """
    offset: int | None = None
    skipped_chats: set[int] = set()
    while True:
        updates = await bot.get_updates(
            offset=offset,
            limit=100,
            timeout=0,
            allowed_updates=dp.resolve_used_update_types(),
        )
        if not updates:
            break
        for update in updates:
            call = update.callback_query
            if call is not None:
                if isinstance(call.message, Message):
                    skipped_chats.add(call.message.chat.id)
            elif update.message is not None and (
                (update.message.text or "").lstrip().startswith("/")
                or update.message.text in keyboards.HOME_LABELS
            ):
                skipped_chats.add(update.message.chat.id)
            else:
                # Ошибка не проглатывается: неподтверждённая пачка придёт снова.
                await dp.feed_update(bot, update, replayed=True)
            offset = update.update_id + 1
    for chat_id in skipped_chats:
        try:
            await bot.send_message(
                chat_id,
                "Бот снова на связи. Накопившиеся нажатия и команды не применял: "
                "их контекст мог устареть. Откройте /panel и повторите нужное действие. "
                "Текстовые записи из очереди обработаны по времени сообщения.",
            )
        except Exception:
            log.warning("Не удалось сообщить о пропущенных кнопках чату %s", chat_id)
