"""Ответ на вопрос по базе знаний.

Собирает ответ из четырёх шагов, и каждый из них умеет закончиться отказом.
Порядок важен: до модели дело доходит последним, и только если есть что ей
показать.

1. базы нет — отвечать нечем;
2. поиск ничего не нашёл — «не нашёл в источниках». **Модель не зовётся**;
3. квота на сутки исчерпана или ключей нет — показываем найденные фрагменты;
4. модель отвечает — сшивает найденное в два-три предложения.

**Третий пункт не заглушка, а режим по умолчанию при пустом `.env`.** Цитата
из клинических рекомендаций с названием документа полезна сама по себе, и это
ровно то, чем бот отличается от чата с моделью: он не придумывает, он
показывает, где написано.

**Сторож чисел.** Каждое число из ответа модели обязано встречаться в
переданных фрагментах или в самом вопросе. Не встречается — ответ
отбрасывается целиком, показываются фрагменты, случай пишется в лог. Это
частный случай правила всего проекта «числа не через LLM»: придуманная
дозировка стоит дороже любого удобства, а заметить её в гладком тексте
невозможно.

**Красных флагов этот модуль не видит, и это правильно.** Их перехватывает
`handlers/alarm`, который стоит выше по списку роутеров: сообщение про судороги
получает фиксированный текст из файла и до Q&A не доходит вовсе. Вторая
проверка здесь была бы второй копией границы безопасности — а копия
расходится с оригиналом молча.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from bot.services import kb, llm, qa_log

log = logging.getLogger(__name__)

PROMPT = Path(__file__).resolve().parent.parent / "content" / "qa_prompt.md"

# Куски, без которых промпт перестаёт быть тем, что задумано. Проверяются на
# импорте: бот, потерявший запрет выдумывать числа, не должен стартовать
# молча — он выглядел бы работающим
REQUIRED = (
    "ТОЛЬКО",
    "Ни одного числа",
    "Не ставь диагнозов",
    '{"answer"',
)

# Исходы. Строки, а не Enum — как и причины в `llm`
NO_KB = "no_kb"  # базы знаний нет
NOT_FOUND = "not_found"  # не нашли ничего подходящего
EXTRACT = "extract"  # показываем найденное как есть
ANSWERED = "answered"  # модель сшила ответ

# Потолок на ответ модели. Под ним поедут фрагменты и источники, а у Telegram
# на сообщение 4096 знаков
MAX_ANSWER = 1200


def _load() -> str:
    """Системный промпт из `content/qa_prompt.md`, ниже разделителя.

    Файл читается человеком целиком, а модели уходит только вторая половина:
    первая объясняет, что здесь вообще лежит и что нельзя убирать.
    """
    text = PROMPT.read_text(encoding="utf-8")
    _, sep, body = text.partition("\n---\n")
    if not sep:
        raise ValueError(f"{PROMPT}: нет разделителя `---`, непонятно, что слать модели")
    body = body.strip()
    if not body:
        raise ValueError(f"{PROMPT}: после разделителя пусто")
    for required in REQUIRED:
        if required not in body:
            raise ValueError(f"{PROMPT}: из промпта пропало «{required}»")
    return body


SYSTEM = _load()


@dataclass(frozen=True)
class Result:
    """Чем закончился вопрос. Текст собирает `texts`, здесь только смысл."""

    kind: str
    answer: str  # пусто, если `kind` не `ANSWERED`
    chunks: tuple[kb.Chunk, ...]
    reason: str = ""  # техническая причина, для лога


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    """Числа текста в сравнимом виде.

    Запятая приводится к точке, хвостовые нули у дробных отбрасываются: «6,0»
    и «6.0» — это «6». У целых хвостовые нули не трогаем, иначе «10» стало бы
    «1» и сторож пропускал бы ровно то, ради чего стоит.
    """
    found = set()
    for raw in _NUMBER.findall(text):
        value = raw.replace(",", ".")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        found.add(value or "0")
    return found


def invented_numbers(answer: str, context: str) -> set[str]:
    """Числа ответа, которых нет ни в источниках, ни в вопросе."""
    return _numbers(answer) - _numbers(context)


def build_user(question: str, chunks: tuple[kb.Chunk, ...]) -> str:
    """Сообщение модели: вопрос и пронумерованные фрагменты."""
    parts = [f"Вопрос: {question}", "", "Фрагменты:"]
    for number, chunk in enumerate(chunks, start=1):
        head = f"{chunk.title}. {chunk.heading}" if chunk.heading else chunk.title
        parts += [f"[{number}] {head}", chunk.text, ""]
    return "\n".join(parts)


async def answer(question: str) -> Result:
    """Ответ на вопрос. Исключений наружу не выпускает."""
    if not kb.available():
        return Result(NO_KB, "", ())

    chunks = tuple(await kb.search(question))
    if not chunks:
        qa_log.write(event="qa", question=question, outcome=NOT_FOUND, calls=0)
        return Result(NOT_FOUND, "", ())

    if qa_log.quota_spent():
        log.warning("Суточная квота обращений к модели исчерпана")
        return _extract(question, chunks, llm.QUOTA)

    reply = await llm.ask(SYSTEM, build_user(question, chunks), models=llm.answer_chain())
    qa_log.write(
        event="qa",
        question=question,
        outcome=ANSWERED if reply.ok else EXTRACT,
        sources=[chunk.source_id for chunk in chunks],
        model=reply.model,
        reason=reply.reason,
        detail=reply.detail[:160],
        calls=reply.calls,
        seconds=round(reply.elapsed, 1),
    )
    if not reply.ok:
        return _extract(question, chunks, reply.reason, logged=True)

    text = str((reply.data or {}).get("answer") or "").strip()
    if not text:
        # Модель честно сказала, что во фрагментах ответа нет. Показываем
        # найденное: пусть человек решит сам, есть там ответ или нет
        return _extract(question, chunks, "empty", logged=True)

    context = " ".join(chunk.text for chunk in chunks) + " " + question
    invented = invented_numbers(text, context)
    if invented:
        log.warning("Числа не из источника: %s — показываем фрагменты", sorted(invented))
        qa_log.write(event="qa_numbers", question=question, invented=sorted(invented), calls=0)
        return _extract(question, chunks, "invented_numbers", logged=True)

    return Result(ANSWERED, text[:MAX_ANSWER], chunks)


def _extract(
    question: str, chunks: tuple[kb.Chunk, ...], reason: str, *, logged: bool = False
) -> Result:
    if not logged:
        qa_log.write(event="qa", question=question, outcome=EXTRACT, reason=reason, calls=0)
    return Result(EXTRACT, "", chunks, reason)
