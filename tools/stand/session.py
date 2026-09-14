"""Сессия стенда: настоящая сеть, перехвачен ровно один метод.

Стенд подаёт апдейты сам, а значит `callback_query.id` у него выдуманный.
Telegram такой id отвергает, и это не мелочь: в `handlers/track.py`
`call.answer()` стоит после записи в базу, но ДО подтверждения в чат и до
`panel.schedule` (строки 124, 145, 179). Исключение из `answer()` съело бы
вторую половину каждого тапа — запись в базе появилась бы, а сообщения и
перерисовки панели не было бы вовсе. Протокол при этом выглядел бы зелёным.

Поэтому `answerCallbackQuery` замкнут на себя, а всё остальное — sendMessage,
sendPhoto, sendDocument, editMessageText, pinChatMessage — уходит в настоящий
Telegram настоящей группе. Побочная выгода: тексты всплывашек («Пошло время»,
«Записал», «Таймер уже закрыт») попадают в протокол, тогда как в живом чате их
не увидел бы никто.

Здесь же запоминаются `message_id` всего отправленного: без них тап по кнопке
отмены пришлось бы адресовать выдуманному сообщению, и `edit_reply_markup`
ушёл бы в никуда.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, TelegramMethod
from aiogram.types import Message

log = logging.getLogger("stand.session")

# Методы, которые несут файл, а не текст: в протоколе их видно по имени файла
FILE_METHODS = frozenset({"SendPhoto", "SendDocument", "SendVideo", "SendAudio"})

# Telegram пускает в одну группу около двадцати сообщений в минуту. Живая
# семья в этот потолок не упирается никогда, а стенд упёрся на двадцать
# четвёртом шаге: прогон оборвался на `TelegramRetryAfter`. Темп — свойство
# стенда, а не бота, поэтому и живёт здесь, а не в `sending.py`
SEND_INTERVAL = 3.4
OTHER_INTERVAL = 0.8
RETRY_ATTEMPTS = 3


@dataclass
class Outgoing:
    """Один исходящий вызов API — то, что стенд предъявляет как доказательство."""

    method: str
    text: str = ""
    message_id: int | None = None
    kind: str = "message"  # message | alert | file | edit | service
    extra: dict[str, Any] = field(default_factory=dict)


class StandSession(AiohttpSession):
    def __init__(self, proxy: str = "") -> None:
        super().__init__(proxy=proxy or None)
        self.records: list[Outgoing] = []
        # message_id последнего отправленного сообщения — по нему стенд
        # адресует тап «↩️ Отменить» той самой кнопке под тем самым текстом
        self.last_message_id: int | None = None
        self.waited_for_flood = 0.0
        # Пока счётчик поднят, отправка — это эхо реплики человека, а не
        # речь бота: в протокол оно не идёт и проверку «ждали тишины» не рвёт
        self.echo_depth = 0
        self._pace = asyncio.Lock()
        self._last_call = 0.0

    async def _slow_down(self, name: str) -> None:
        """Выдержать паузу перед вызовом.

        Замок нужен потому, что панель перерисовывается из своей задачи с
        дебаунсом, то есть вызовы приходят из разных корутин одновременно.
        """
        interval = SEND_INTERVAL if name.startswith("Send") else OTHER_INTERVAL
        async with self._pace:
            pause = interval - (time.monotonic() - self._last_call)
            if pause > 0:
                await asyncio.sleep(pause)
            self._last_call = time.monotonic()

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if isinstance(method, AnswerCallbackQuery):
            self.records.append(
                Outgoing(
                    method="answerCallbackQuery",
                    text=method.text or "",
                    kind="alert",
                    extra={"show_alert": bool(method.show_alert)},
                )
            )
            # Telegram на этот метод отвечает true; ничего, кроме «часиков» на
            # кнопке, от него не зависит, а показывать всплывашку здесь некому
            return True

        name = type(method).__name__
        for attempt in range(RETRY_ATTEMPTS):
            await self._slow_down(name)
            try:
                result = await super().make_request(bot, method, timeout)
            except TelegramRetryAfter as exc:
                # Пережидаем, а не сдаёмся: упавший вызов означал бы дыру в
                # протоколе, которую легко принять за поломку бота
                if attempt == RETRY_ATTEMPTS - 1:
                    raise
                delay = exc.retry_after + 1
                self.waited_for_flood += delay
                log.warning("Флуд-контроль на %s: ждём %s с", name, delay)
                await asyncio.sleep(delay)
                continue
            if self.echo_depth > 0:
                if isinstance(result, Message):
                    self.last_message_id = result.message_id
            else:
                self._remember(method, result)
            return result
        raise RuntimeError("недостижимо")

    def _remember(self, method: TelegramMethod[Any], result: Any) -> None:
        name = type(method).__name__
        payload = method.model_dump(exclude_none=True)
        text = payload.get("text") or payload.get("caption") or ""

        message_id: int | None = None
        if isinstance(result, Message):
            message_id = result.message_id
            # Только у отправки, но не у правки: `editMessageText` возвращает
            # изменённое сообщение, то есть панель, и «последнее отправленное»
            # стало бы указывать на неё. Тап «↩️ Отменить» уехал бы на панель,
            # а протокол показал бы исправную работу
            if name.startswith("Send"):
                self.last_message_id = message_id

        if name in FILE_METHODS:
            kind = "file"
        elif name.startswith("Edit"):
            kind = "edit"
        elif name == "SendMessage":
            kind = "message"
        else:
            kind = "service"

        extra: dict[str, Any] = {}
        for key in ("photo", "document"):
            value = payload.get(key)
            filename = getattr(value, "filename", None)
            if filename:
                extra["file"] = filename
        if name.startswith("Edit") and "message_id" in payload:
            extra["target"] = payload["message_id"]

        self.records.append(
            Outgoing(
                method=name, text=text, message_id=message_id, kind=kind, extra=extra
            )
        )

    def drain(self) -> list[Outgoing]:
        """Забрать накопленное и обнулить — протокол пишется по шагам."""
        taken = self.records
        self.records = []
        return taken
