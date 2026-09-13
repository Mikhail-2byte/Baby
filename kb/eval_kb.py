"""Оценка поиска по базе знаний.

    .venv\\Scripts\\python kb\\eval_kb.py

Меряет то, что бот делает на самом деле: скрипт импортирует боевой
`bot.services.kb` и зовёт ту же `search`, которую позовёт хендлер. Своей копии
поиска здесь нет намеренно — копия меряла бы себя, а не бота, и расходилась бы
с ним молча.

Две половины оценки, и вторая не менее важна:

* **recall** — нашёлся ли нужный фрагмент среди первых k;
* **молчание** — промолчал ли поиск там, где ответа в базе нет. Ретривер,
  отвечающий на «сколько стоит биткоин» абзацем из клинических рекомендаций,
  опаснее молчащего: он выглядит знающим.

Числа отсюда идут в летопись `docs/TASKS.md`. По ним же принимается отложенное
решение про эмбеддинги: плотный поиск стоит двух гигабайт оперативной памяти
на машине, которая работает круглосуточно, и покупать их вслепую незачем.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.services import kb  # noqa: E402
from bot.services.redflags import normalize  # noqa: E402

EVAL = ROOT / "kb" / "eval.yaml"

# Глубина, на которой считаем попадание. Пять — столько же, сколько уходит в
# ответ (`kb_top_k`): мерить глубже, чем показываем, бессмысленно
DEPTH = 5


@dataclass(frozen=True)
class Question:
    text: str
    source: str
    contains: tuple[re.Pattern[str], ...]


def load() -> tuple[tuple[Question, ...], tuple[str, ...]]:
    raw: Any = yaml.safe_load(EVAL.read_text(encoding="utf-8"))
    questions = tuple(
        Question(
            text=item["q"],
            source=item["source"],
            contains=tuple(re.compile(pattern) for pattern in item["contains"]),
        )
        for item in raw["questions"]
    )
    return questions, tuple(raw.get("silence") or ())


def hits(question: Question, found: list[kb.Chunk]) -> list[bool]:
    """По каждому месту выдачи: это тот фрагмент, который искали?"""
    out = []
    for chunk in found:
        text = normalize(chunk.text)
        out.append(
            chunk.source_id == question.source
            and any(pattern.search(text) for pattern in question.contains)
        )
    return out


async def run() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not kb.available():
        print("Базы знаний нет. Соберите её: python kb/build_kb.py")
        return 1

    questions, silence = load()
    at = {1: 0, 3: 0, 5: 0}
    reciprocal = 0.0
    misses: list[tuple[Question, list[kb.Chunk]]] = []

    for question in questions:
        found = await kb.search(question.text, DEPTH)
        marks = hits(question, found)
        place = marks.index(True) + 1 if any(marks) else 0
        if place:
            reciprocal += 1 / place
            for depth in at:
                if place <= depth:
                    at[depth] += 1
        else:
            misses.append((question, found))

    total = len(questions)
    print(f"Вопросов: {total}, глубина {DEPTH}")
    for depth in sorted(at):
        print(f"  recall@{depth}: {at[depth] / total:.2f}  ({at[depth]} из {total})")
    print(f"  MRR@{DEPTH}:    {reciprocal / total:.2f}")

    quiet = 0
    noisy: list[tuple[str, kb.Chunk]] = []
    for text in silence:
        found = await kb.search(text, DEPTH)
        if found:
            noisy.append((text, found[0]))
        else:
            quiet += 1
    if silence:
        print(f"  молчание:   {quiet / len(silence):.2f}  ({quiet} из {len(silence)})")

    if misses:
        print(f"\nНе нашлось ({len(misses)}):")
        for question, found in misses:
            got = f"{found[0].source_id}: {found[0].text[:70]}" if found else "пусто"
            print(f"  - {question.text}")
            print(f"      ждали {question.source}, выдача: {got}")
    if noisy:
        print(f"\nОтветил там, где ответа нет ({len(noisy)}):")
        for text, chunk in noisy:
            print(f"  - {text}")
            print(f"      {chunk.source_id}: {chunk.text[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
