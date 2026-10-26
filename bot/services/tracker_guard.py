"""Короткие изменения дневника сериализованы по ребёнку внутри процесса.

Бот запускается одним процессом (существующий bot.lock).
"""
import asyncio

_locks: dict[int, asyncio.Lock] = {}


def for_child(child_id: int) -> asyncio.Lock:
    return _locks.setdefault(child_id, asyncio.Lock())
