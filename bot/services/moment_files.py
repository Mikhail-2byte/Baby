"""Локальные вложения: пути всегда относительно каталога рядом с рабочей БД."""

import asyncio
from pathlib import Path

from bot.config import settings

# Бот работает одним процессом. Общий замок также даёт согласованный бэкап БД+фото.
storage_lock = asyncio.Lock()


def root() -> Path:
    return settings.db_path.parent / "moments"


def resolve(relative: str, base: Path | None = None) -> Path:
    base = (base or root()).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base) or path == base:
        raise ValueError("Недопустимый путь фотографии")
    return path


def remove(paths: list[str], base: Path | None = None) -> None:
    for relative in paths:
        path = resolve(relative, base)
        candidates = [path]
        if path.suffix == ".upload":
            # При обрыве между rename и коммитом БД знает ещё временное имя.
            # UUID-имя принадлежит только этой загрузке; старые фото не затрагиваем.
            candidates.extend(path.with_suffix(suffix) for suffix in (".jpg", ".png", ".webp"))
        for candidate in candidates:
            candidate.unlink(missing_ok=True)
