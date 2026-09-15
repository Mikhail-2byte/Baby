"""Стенд: боевой бот, которому апдейты подаёт скрипт, а не Telegram.

Собирается ровно так же, как `bot/__main__.py`, и отличается в двух местах:
апдейты приходят из `feed_update`, а не из long polling, и `answerCallbackQuery`
перехвачен (см. `session.py`). Всё остальное боевое — та же `FamilyMiddleware`,
тот же `drop_wizard_state`, тот же список роутеров целиком и тот же тикер.

Список роутеров берётся как `handlers.routers`, а не собирается здесь своей
тройкой. Это прямое следствие инварианта проекта: тест, составляющий свой
порядок, доказывает работу механизма и молчит о том, каков порядок на самом
деле — ровно так однажды и разъехались `qa` с `measure`.

База читается отсюда напрямую через `sqlite3`, в обход `repo`. Это сознательно
и только здесь: стенд обязан видеть то, чего `repo` наружу не отдаёт —
`estimated`, `alerts`, `panel_day`, — а правило «весь SQL в repo» защищает
боевой код от разъезжающихся запросов, а не диагностику от чтения.
"""

from __future__ import annotations

import asyncio
import html
import logging
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import handlers
from bot.config import settings
from bot.db.session import engine
from bot.middlewares import FamilyMiddleware, drop_wizard_state
from bot.services import kb, lock, panel, ticker
from tools.stand.session import Outgoing, StandSession

log = logging.getLogger("stand")

# Пауза после каждого шага. Дебаунс панели — 1.5 с (panel_debounce_seconds),
# и без ожидания протокол не увидел бы перерисовку, которая на самом деле есть
SETTLE_SECONDS = 3.0


class Protocol:
    """Протокол прогона: что ушло в Telegram и что стало в базе.

    Пишется построчно и со сбросом на диск после каждой строки: прогон может
    оборваться на середине, и уцелевшая часть протокола тогда — единственное,
    что осталось.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w", encoding="utf-8")
        self.path = path
        self.failures: list[str] = []

    def write(self, line: str = "") -> None:
        self._fh.write(line + "\n")
        self._fh.flush()

    def fail(self, step: str, why: str) -> None:
        self.failures.append(f"{step}: {why}")
        self.write(f"    [!] НЕ СОШЛОСЬ: {why}")

    def close(self) -> None:
        self._fh.close()


class Stand:
    def __init__(
        self,
        *,
        chat_id: int,
        chat_title: str,
        chat_type: str = "supergroup",
        user_id: int,
        user_name: str,
        protocol: Path,
        run_ticker: bool = False,
    ) -> None:
        # Тип берётся настоящий, а не «супергруппа» наугад: обычная группа и
        # супергруппа расходятся в закреплении и в переезде chat_id, и стенд,
        # объявивший чужой тип, проверял бы конфигурацию, которой нет
        self.chat = Chat(id=chat_id, type=chat_type, title=chat_title)
        self.user = User(id=user_id, is_bot=False, first_name=user_name)
        self.protocol = Protocol(protocol)
        self.run_ticker = run_ticker

        self.session = StandSession(settings.telegram_proxy)
        self.bot = Bot(
            token=settings.bot_token,
            session=self.session,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        self.dp = Dispatcher()
        self.dp.update.outer_middleware(FamilyMiddleware())
        self.dp.message.outer_middleware(drop_wizard_state)
        for router in handlers.routers:
            self.dp.include_router(router)

        self._guard = lock.SingleInstance()
        self._ticker: asyncio.Task[None] | None = None
        self._update_id = 1
        self._fallback_message_id = 1

    # --- жизненный цикл -------------------------------------------------

    async def start(self) -> None:
        if not self._guard.acquire():
            raise RuntimeError(f"Замок {lock.PATH} занят — стенд уже запущен")
        me = await self.bot.get_me()
        stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
        self.protocol.write(f"Стенд @{me.username} · чат {self.chat.id} · {stamp}")
        self.protocol.write(f"База {settings.db_path}")
        self.protocol.write("=" * 72)
        if self.run_ticker:
            self._ticker = asyncio.create_task(ticker.run(self.bot), name="ticker")
            self.protocol.write(f"Тикер запущен, период {settings.tick_seconds} с")

    async def stop(self) -> None:
        # Порядок дословно из bot/__main__.py:146-168
        if self._ticker is not None:
            self._ticker.cancel()
            with suppress(asyncio.CancelledError):
                await self._ticker
        for step in (panel.shutdown(), self.bot.session.close(), engine.dispose()):
            try:
                await step
            except Exception:
                log.exception("Ошибка при остановке")
        with suppress(Exception):
            kb.close()
        self._guard.release()
        self.protocol.write("")
        self.protocol.write("=" * 72)
        if self.session.waited_for_flood:
            self.protocol.write(
                f"Пережидали флуд-контроль Telegram: {self.session.waited_for_flood:.0f} с"
            )
        if self.protocol.failures:
            self.protocol.write(f"НЕ СОШЛОСЬ: {len(self.protocol.failures)}")
            for line in self.protocol.failures:
                self.protocol.write(f"  [!] {line}")
        else:
            self.protocol.write("Расхождений, которые стенд умеет ловить сам, нет.")
        self.protocol.close()

    # --- подача апдейтов ------------------------------------------------

    def _next_incoming_id(self) -> int:
        """message_id для нашего выдуманного входящего.

        Берётся от последнего РЕАЛЬНОГО сообщения в чате: по нему панель судит,
        далеко ли она уехала (`panel_max_messages`), и своя нумерация сломала бы
        эту проверку в обе стороны.
        """
        if self.session.last_message_id is not None:
            return self.session.last_message_id + 1
        self._fallback_message_id += 1
        return self._fallback_message_id

    def _incoming(self, text: str, *, private: bool = False) -> Message:
        chat = (
            Chat(id=self.user.id, type="private", first_name=self.user.first_name)
            if private
            else self.chat
        )
        return Message(
            message_id=self._next_incoming_id(),
            date=datetime.now(),
            chat=chat,
            from_user=self.user,
            text=text,
        )

    async def _feed(self, body: dict[str, Any]) -> None:
        self._update_id += 1
        await self.dp.feed_update(
            self.bot, Update(update_id=self._update_id, **body)
        )

    async def _echo(self, text: str) -> int | None:
        """Положить реплику человека в чат по-настоящему.

        Без этого `alarm` и `qa` не работают вовсе: они отвечают реплаем, а
        реплай на сообщение, которого в чате нет, Telegram отвергает —
        «message to be replied not found». То есть выдуманный message_id
        отнимал у стенда ровно те два роутера, ради которых всё и затевалось.
        Заодно лента читается как разговор, а не как монолог бота.
        """
        self.session.echo_depth += 1
        try:
            sent = await self.bot.send_message(
                self.chat.id, f"👤 {html.escape(text)}"
            )
        except Exception:
            log.warning("Эхо реплики не ушло: %r", text, exc_info=True)
            return None
        finally:
            self.session.echo_depth -= 1
        return sent.message_id

    async def say(self, text: str, *, private: bool = False) -> None:
        """Человек написал в чат."""
        if private:
            # В личке эхо не нужно и некуда: бот отвечает отказом, не реплаем
            await self._feed({"message": self._incoming(text, private=True)})
            return
        real_id = await self._echo(text)
        message = self._incoming(text)
        if real_id is not None:
            message = message.model_copy(update={"message_id": real_id})
        await self._feed({"message": message})

    async def tap(self, data: str, *, on: str | int = "panel") -> None:
        """Человек нажал кнопку.

        `on` — под каким сообщением кнопка: «panel» берёт живой
        `panel_message_id` из базы, «last» — последнее отправленное ботом.
        Выдуманный номер означал бы, что правка панели и снятие кнопки отмены
        уходят в никуда, — а в протоколе это выглядело бы как успех.
        """
        if on == "panel":
            target = self.panel_message_id()
        elif on == "last":
            target = self.session.last_message_id
        else:
            target = int(on)
        if target is None:
            raise RuntimeError("Не к чему прикрепить нажатие: сообщения ещё нет")

        call = CallbackQuery(
            id=f"stand-{self._update_id}",
            from_user=self.user,
            chat_instance=f"stand-{self.chat.id}",
            data=data,
            message=Message(
                message_id=target,
                date=datetime.now(),
                chat=self.chat,
                from_user=self.user,
                text="(панель)",
            ),
        )
        await self._feed({"callback_query": call})

    async def mark(self, text: str) -> None:
        """Отбивка в самом чате — чтобы владелец читал ленту как протокол.

        Украшение, и падать из-за него прогон не должен: отбивка, не
        дошедшая до чата, не меняет ни одного вывода о боте.
        """
        with suppress(Exception):
            await self.bot.send_message(self.chat.id, f"<i>— {text} —</i>")

    async def settle(self, seconds: float = SETTLE_SECONDS) -> None:
        await asyncio.sleep(seconds)

    # --- чтение базы ----------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        conn = sqlite3.connect(settings.db_path)
        try:
            return conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()

    def one(self, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def panel_message_id(self) -> int | None:
        row = self.one(
            "SELECT panel_message_id FROM families WHERE chat_id = ?", (self.chat.id,)
        )
        return row[0] if row else None

    def family_id(self) -> int | None:
        row = self.one("SELECT id FROM families WHERE chat_id = ?", (self.chat.id,))
        return row[0] if row else None

    def child_id(self) -> int | None:
        row = self.one(
            "SELECT c.id FROM children c JOIN families f ON f.id = c.family_id "
            "WHERE f.chat_id = ? AND c.archived = 0",
            (self.chat.id,),
        )
        return row[0] if row else None

    # --- шаг ------------------------------------------------------------

    @asynccontextmanager
    async def step(self, number: str, title: str) -> AsyncIterator[StepReport]:
        self.session.drain()
        self.protocol.write("")
        self.protocol.write(f"### шаг {number} · {title}")
        report = StepReport(self, number)
        try:
            yield report
        except Exception as exc:  # noqa: BLE001 — прогон не рвётся из-за одного шага
            self.protocol.fail(
                f"шаг {number}", f"исключение {type(exc).__name__}: {exc}"
            )
            log.exception("Шаг %s упал", number)
        await self.settle()
        for record in self.session.drain():
            report.log(record)
        report.verdict()


class StepReport:
    """Что стенд увидел на одном шаге и чего он от шага ждал."""

    def __init__(self, stand: Stand, number: str) -> None:
        self.stand = stand
        self.number = number
        self.seen: list[Outgoing] = []
        self._expect_text: list[str] = []
        self._expect_absent: list[str] = []
        self._expect_silence = False
        self._expect_kinds: list[str] = []

    def expect(self, *fragments: str) -> None:
        """Подстроки, которые обязаны встретиться в отправленном."""
        self._expect_text.extend(fragments)

    def expect_absent(self, *fragments: str) -> None:
        self._expect_absent.extend(fragments)

    def expect_silence(self) -> None:
        self._expect_silence = True

    def expect_kind(self, *kinds: str) -> None:
        self._expect_kinds.extend(kinds)

    def note(self, line: str) -> None:
        self.stand.protocol.write(f"    · {line}")

    def show(self, title: str, sql: str, params: Sequence[Any] = ()) -> None:
        rows = self.stand.query(sql, params)
        if not rows:
            self.stand.protocol.write(f"    БД {title}: пусто")
            return
        for row in rows:
            self.stand.protocol.write(f"    БД {title}: {row}")

    def log(self, record: Outgoing) -> None:
        self.seen.append(record)
        text = record.text.replace("\n", " / ")
        if len(text) > 400:
            text = text[:400] + "…"
        prefix = {
            "alert": "    (~) всплывашка",
            "file": "    (f) файл",
            "edit": "    (e) правка",
            "delete": "    (x) убрал",
            "message": "    ->  в чат",
        }.get(record.kind, f"    ... {record.method}")
        tail = f" #{record.message_id}" if record.message_id else ""
        extra = f" [{record.extra}]" if record.extra else ""
        self.stand.protocol.write(f"{prefix}{tail}{extra}: {text}")

    def verdict(self) -> None:
        blob = "\n".join(r.text for r in self.seen)
        step = f"шаг {self.number}"
        if self._expect_silence:
            speech = [r for r in self.seen if r.kind in {"message", "file"}]
            if speech:
                self.stand.protocol.fail(
                    step, f"ждали тишины, а ушло сообщений: {len(speech)}"
                )
        for fragment in self._expect_text:
            if fragment not in blob:
                self.stand.protocol.fail(step, f"в отправленном нет {fragment!r}")
        for fragment in self._expect_absent:
            if fragment in blob:
                self.stand.protocol.fail(step, f"в отправленном есть лишнее {fragment!r}")
        for kind in self._expect_kinds:
            if not any(r.kind == kind for r in self.seen):
                self.stand.protocol.fail(
                    step, f"не было ни одного исходящего вида {kind!r}"
                )
