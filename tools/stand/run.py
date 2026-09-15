"""Точка входа стенда.

Запускать только из корня проекта: `settings = Settings()` выполняется на
импорте `bot/config.py`, а путь к `.env` и к базе относительный.

    .venv\\Scripts\\python -m tools.stand.run prepare
    .venv\\Scripts\\python -m tools.stand.run chat
    .venv\\Scripts\\python -m tools.stand.run act1 --chat -1001234567890 --user 111
    .venv\\Scripts\\python -m tools.stand.run act2

`DB_PATH` выставляется здесь, до первого импорта чего-либо из `bot`: настройки
читаются на импорте, и переменная, поставленная позже, уже ничего не изменит.
Переменные окружения имеют приоритет над `.env`, поэтому боевая строка
`DB_PATH` (её там и нет) ничему не мешает. За `db_path.parent` переезжают и
замок, и лог, и heartbeat, и каталог бэкапов — то есть боевому боту стенд не
мешает ничем, даже работая одновременно.
"""

from __future__ import annotations

import os

os.environ.setdefault("DB_PATH", "data/live/baby.db")
os.environ.setdefault("TICK_SECONDS", "5")

import argparse  # noqa: E402 — только после DB_PATH
import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

LIVE = Path("data/live")
STATE = LIVE / "stand.json"
PROD_DB = Path("data/baby.db")


def _load_state() -> dict[str, Any]:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict[str, Any]) -> None:
    LIVE.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_prepare() -> int:
    from tools.stand import seed

    wiped = seed.prepare(PROD_DB, Path(os.environ["DB_PATH"]))
    print("База стенда готова:", os.environ["DB_PATH"])
    for table, count in wiped.items():
        print(f"  {table}: {count}")
    return 0


async def _chat() -> int:
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession

    from bot.config import settings

    session = (
        AiohttpSession(proxy=settings.telegram_proxy)
        if settings.telegram_proxy
        else None
    )
    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    try:
        me = await bot.get_me()
        print(f"Бот @{me.username} (id {me.id})")
        updates = await bot.get_updates(timeout=1)
        if not updates:
            print("Очередь пуста. Напишите в тестовой группе любое сообщение и повторите.")
            return 1
        seen: dict[int, tuple[str, int, str, str]] = {}
        for update in updates:
            message = update.message or update.edited_message
            if message is None or message.chat.type not in {"group", "supergroup"}:
                continue
            author = message.from_user
            seen[message.chat.id] = (
                message.chat.title or "",
                author.id if author else 0,
                author.full_name if author else "",
                message.chat.type,
            )
        if not seen:
            print("Групповых сообщений в очереди нет.")
            return 1
        for chat_id, (title, user_id, user_name, chat_type) in seen.items():
            mark = " — БОЕВАЯ ГРУППА" if is_production_chat(chat_id) else ""
            print(
                f"  чат {chat_id} · {title} · {chat_type} · "
                f"автор {user_id} · {user_name}{mark}"
            )
        fresh = {c: v for c, v in seen.items() if not is_production_chat(c)}
        if not fresh:
            print("Все чаты в очереди — боевые. Заведите отдельную тестовую группу.")
            return 1
        chat_id, (title, user_id, user_name, chat_type) = next(iter(fresh.items()))
        _save_state(
            {
                "chat_id": chat_id,
                "chat_title": title,
                "chat_type": chat_type,
                "user_id": user_id,
                "user_name": user_name,
            }
        )
        print(f"Запомнил в {STATE}")
        return 0
    finally:
        await bot.session.close()


def is_production_chat(chat_id: int) -> str | None:
    """Заведена ли уже такая семья в БОЕВОЙ базе.

    Сторож появился не из осторожности, а по факту: очередь Telegram вернула
    единственный чат, и это была боевая группа. Стенд, запущенный по ней,
    завёл бы тестового ребёнка там, где через несколько дней будет настоящий,
    и вычищать пришлось бы руками из-под работающего бота. Сверка идёт по
    боевой базе, а не по названию: название можно поменять, chat_id — нет.
    """
    if not PROD_DB.exists():
        return None
    import sqlite3

    conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT title FROM families WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


async def _act(
    name: str,
    chat_id: int,
    user_id: int,
    title: str,
    user_name: str,
    chat_type: str,
) -> int:
    from tools.stand.harness import Stand

    module = __import__(f"tools.stand.{name}", fromlist=["run"])
    stand = Stand(
        chat_id=chat_id,
        chat_title=title,
        chat_type=chat_type,
        user_id=user_id,
        user_name=user_name,
        protocol=LIVE / f"protocol-{name}.txt",
        run_ticker=name in {"act2", "act3"},
    )
    await stand.start()
    try:
        await module.run(stand)
    finally:
        await stand.stop()
    print(f"Протокол: {stand.protocol.path}")
    if stand.protocol.failures:
        print(f"НЕ СОШЛОСЬ: {len(stand.protocol.failures)}")
        for line in stand.protocol.failures:
            print("  ", line)
        return 2
    print("Расхождений, которые стенд ловит сам, нет.")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LIVE / "stand.log", encoding="utf-8")]
        if LIVE.exists()
        else None,
    )
    parser = argparse.ArgumentParser(prog="tools.stand.run")
    parser.add_argument("command", choices=("prepare", "chat", "act1", "act2", "act3", "act4"))
    parser.add_argument("--chat", type=int, default=None)
    parser.add_argument("--user", type=int, default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="прогон по чату, который уже заведён в боевой базе",
    )
    args = parser.parse_args()

    if args.command == "prepare":
        return cmd_prepare()
    if args.command == "chat":
        return asyncio.run(_chat())

    state: dict[str, Any] = _load_state()
    chat_id = args.chat or state.get("chat_id")
    user_id = args.user or state.get("user_id")
    if not chat_id or not user_id:
        print("Неизвестен чат или пользователь. Сначала: python -m tools.stand.run chat")
        return 1

    known = is_production_chat(int(chat_id))
    if known is not None and not args.force:
        print(f"ОТКАЗ: чат {chat_id} — это боевая группа «{known}» из data/baby.db.")
        print("Стенд пишет в чат по-настоящему. Заведите отдельную группу.")
        print("Если группа всё же тестовая, повторите с --force.")
        return 1

    return asyncio.run(
        _act(
            args.command,
            int(chat_id),
            int(user_id),
            str(state.get("chat_title") or "Стенд"),
            str(state.get("user_name") or "Стенд"),
            str(state.get("chat_type") or "supergroup"),
        )
    )


if __name__ == "__main__":
    sys.exit(main())
