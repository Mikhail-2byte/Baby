"""Боевые чаты, которые стенд обязан узнавать даже после сброса базы.

`run.is_production_chat` узнавал боевую группу по строке в `families` боевой
базы. `tools/reset.py` эту строку удаляет — то есть разоружает ровно ту защиту,
которая не даёт стенду залить тестового ребёнка в семейный чат тем же токеном.
Окно опасности приходится на худший час: очередь Telegram сразу после сброса
возвращает один чат, и это как раз боевой — ровно так сторож и понадобился
впервые.

Отсюда вторая память. Пустая база — не доказательство того, что чат чужой, и
списывать это на внимательность человека нельзя: шаг, который должен выполнить
человек, — это не шаг, а дыра.

Файл лежит в `data/`: `chat_id` конкретной семьи — это данные, а не код, и
`data/` в `.gitignore`. Запись накапливается, второй сброс не стирает первый.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

PATH = Path("data/prod-chats.json")


def _load() -> dict[str, dict[str, str]]:
    if not PATH.exists():
        return {}
    try:
        loaded = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Битый файл памяти не должен мешать снять новый снимок. Молчать
        # здесь можно: запись всё равно перепишет файл целиком, а чтение
        # ниже вернёт «чат незнакомый» — сторож останется при живой базе
        return {}
    return loaded if isinstance(loaded, dict) else {}


def remember(chats: dict[int, str | None], *, on: date) -> None:
    """Запомнить боевые чаты.

    Зовётся ДО удаления строк `families` — после запоминать будет нечего.
    """
    known = _load()
    for chat_id, title in chats.items():
        known[str(chat_id)] = {"title": title or "", "wiped_on": on.isoformat()}
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(known, ensure_ascii=False, indent=2), encoding="utf-8")


def is_production(chat_id: int, db: Path) -> str | None:
    """Название семьи, если чат боевой. `None` — чат незнакомый.

    Две памяти по очереди: живая база (семья, заведённая прямо сейчас) и файл
    (семья, которую сбросили).
    """
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT title FROM families WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            return row[0] or "без названия"

    entry = _load().get(str(chat_id))
    if entry is None:
        return None
    return entry.get("title") or f"сброшена {entry.get('wiped_on', '?')}"
