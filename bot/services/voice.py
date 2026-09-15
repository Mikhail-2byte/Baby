"""Расшифровка голосовых сообщений (этап 7).

Транспорт и только транспорт: голос → текст. Что с этим текстом делать, знают
`handlers/voice.py` и `services/dictation.py`.

Наружу исключений не выпускает — при любом сбое возвращает `None`, как и
`services/llm.py`. Бот обязан работать без расшифровки: пустой `STT_KEY`
выключает голос целиком, остальное живёт как раньше.

Эндпоинт — Whisper-совместимый (по умолчанию Groq), а не мультимодальная модель
OpenRouter, и это даёт две вещи. Первая: `ogg/opus` Telegram принимается как
есть, поэтому `ffmpeg` проекту не нужен вовсе. Вторая: своя квота, не
пересекающаяся с лимитом OpenRouter, на котором держится разбор и ответы по базе
знаний.

Шестая копия из Family_planer, и политика повторов у неё общая с `llm.py` —
`http_retry`, который ради этого модуля и выносился. Здесь она особенно важна:
голосовое проходит через два таких вызова подряд (сначала расшифровка, потом
модель), и без потолка ожидание складывалось бы до шести минут.

**Просьба писать числа цифрами — подсказка, а не гарантия.** Whisper принимает
поле `prompt` (у Groq предел 224 токена) и обычно слушается, но «тридцать девять
и пять» всё равно случается. Поэтому расшифровка потом идёт через
`services/numerals`, и вот почему это не перестраховка: триаж ищет температуру
**цифрами**, и названная словами не поднимает ничего.
"""

import logging

import httpx

from bot.config import settings
from bot.services import http_retry

log = logging.getLogger(__name__)

# Имена оставлены модулю намеренно: на них смотрят тесты политики повторов, как
# и в `llm.py`
TIMEOUT = http_retry.TIMEOUT
ATTEMPTS = http_retry.ATTEMPTS
BACKOFF = http_retry.BACKOFF
RETRY_CODES = http_retry.RETRY_CODES

# Одной фразой, а не инструкцией: у поля предел 224 токена, и длинная просьба
# начинает влиять на стиль расшифровки, а не только на запись чисел
HINT = "Числа и время записывай цифрами."


def available() -> bool:
    """Включена ли расшифровка вообще.

    Спрашивать это **до** скачивания файла, а не полагаться на `None` из
    `transcribe`: тот возвращает отказ без запроса к провайдеру, но файл к тому
    моменту уже скачан. Голосовые в семейном чате идут постоянно, и живой
    `getFile` с загрузкой через прокси на каждую реплику — при выключенном
    голосе — это трафик ради выброшенных байтов.
    """
    return bool(settings.stt_key and settings.stt_model)


async def transcribe(audio: bytes, filename: str = "voice.ogg") -> str | None:
    """Текст записи или `None` при любом сбое."""
    if not settings.stt_key:
        return None  # без ключа голос выключен, и это не ошибка
    if not settings.stt_model:
        log.warning("STT_MODEL пуст — расшифровка выключена")
        return None

    # Клиент на вызов, а не на модуль: голосовых единицы в день, зато не нужно
    # закрывать живое соединение на остановке бота (то же решение, что в llm.py)
    async with httpx.AsyncClient(proxy=settings.stt_proxy or None) as client:
        outcome = await http_retry.with_retries(
            lambda: _try_once(client, audio, filename), what="Расшифровка"
        )
    return outcome or None  # пустая строка — тоже неудача


async def _try_once(
    client: httpx.AsyncClient, audio: bytes, filename: str
) -> str | None:
    """Текст расшифровки, либо `None` — «имеет смысл повторить»."""
    try:
        response = await client.post(
            settings.stt_url,
            headers={"Authorization": f"Bearer {settings.stt_key}"},
            files={"file": (filename, audio, "audio/ogg")},
            data={
                "model": settings.stt_model,
                # Язык задан явно: на коротких записях автоопределение
                # ошибается и отдаёт русскую фразу латиницей
                "language": "ru",
                "prompt": HINT,
                "response_format": "json",
                "temperature": "0",
            },
            timeout=TIMEOUT,
        )
    except Exception:
        log.warning("Эндпоинт расшифровки недоступен", exc_info=True)
        return None

    if response.status_code in RETRY_CODES:
        log.warning("Расшифровка ответила %s — повторим", response.status_code)
        return None
    if response.status_code != 200:
        log.error(
            "Расшифровка отказала: %s %.200s", response.status_code, response.text
        )
        return ""  # повтор бессмыслен: плохой ключ, нет денег, нет модели

    try:
        body = response.json()
    except ValueError:
        log.error("Расшифровка вернула не JSON: %.200s", response.text)
        return ""

    text = (body.get("text") or "").strip()
    if not text:
        log.warning("Расшифровка вернула пустой текст: %.200s", str(body))
        return ""  # тишина в записи повтором не лечится
    return text
