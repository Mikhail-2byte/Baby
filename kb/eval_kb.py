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

Разделов молчания три, и печатаются они тремя числами без среднего по ним: у
них разная цена промаха. Важнее другое — **скрипт меряет только первый сторож
молчания, лексический**. Второй, семантический, живёт в `services/qa.py`:
модель, которой показали найденное, отвечает «во фрагментах ответа нет», и бот
молчит. В сеть скрипт не ходит и ходить не должен, значит непустая выдача на
`absent` и `overlapping` НЕ означает, что бот ответил мусором, — означает, что
ворота передали мусор дальше. Свалив это в общее число, мы обрушили бы
заглавную цифру и тем самым позвали подкрутить порог: набор своими руками
спровоцировал бы подгонку, против которой написан.

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

# Разделы молчания и их подписи. Порядок фиксирован: от самого лёгкого случая к
# самому тяжёлому, и читается он как шкала, а не как список
SILENCE_SECTIONS = (
    ("silence", "чужой домен"),
    ("absent", "нет в корпусе"),
    ("overlapping", "чужое общими словами"),
)


def _report_missing_sources() -> None:
    """Сказать вслух, каких объявленных источников в базе нет.

    `build_kb` такой список печатает, но сборка бывает раз в полгода, а замер —
    каждый раз, когда трогают поиск. Источник `aap_safe_sleep_2022` объявлен в
    манифесте с самого начала и не положен ни разу: тема безопасного сна в базе
    отсутствует целиком, а выглядит это как покрытая. Шаг, который должен
    выполнить человек, — это не шаг, а дыра, и дыру надо показывать.
    """
    import sqlite3

    from bot.config import settings
    from kb.build_kb import SOURCES  # локально: боту этот модуль не нужен

    connection = sqlite3.connect(f"file:{settings.kb_path}?mode=ro", uri=True)
    try:
        built = {row[0] for row in connection.execute("SELECT id FROM sources")}
    finally:
        connection.close()
    missing = [source.id for source in SOURCES if source.id not in built]
    if missing:
        print(
            f"Объявлено источников {len(SOURCES)}, в базе {len(built)}"
            f" — нет: {', '.join(missing)}"
        )
        print()


@dataclass(frozen=True)
class Question:
    text: str
    source: str
    contains: tuple[re.Pattern[str], ...]


def load(path: Path = EVAL) -> tuple[tuple[Question, ...], dict[str, tuple[str, ...]]]:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    questions = tuple(
        Question(
            text=item["q"],
            source=item["source"],
            contains=tuple(re.compile(pattern) for pattern in item["contains"]),
        )
        for item in raw["questions"]
    )
    sections = {key: tuple(raw.get(key) or ()) for key, _ in SILENCE_SECTIONS}
    return questions, sections


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


async def _measure_silence(
    label: str, texts: tuple[str, ...]
) -> list[tuple[str, str, kb.Chunk]]:
    """Замерить один раздел молчания и напечатать его число.

    Возвращает пробои для подробного разбора внизу. Покрытие у них печатается
    не для красоты: без него не отличить «ворота еле пропустили» от «пропустили
    уверенно», то есть нельзя судить, стоит ли вообще трогать пороги.
    """
    if not texts:
        return []
    noisy: list[tuple[str, str, kb.Chunk]] = []
    for text in texts:
        found = await kb.search(text, DEPTH)
        if found:
            noisy.append((label, text, found[0]))
    quiet = len(texts) - len(noisy)
    print(f"  молчание ({label}): {quiet / len(texts):.2f}  ({quiet} из {len(texts)})")
    return noisy


async def run() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not kb.available():
        print("Базы знаний нет. Соберите её: python kb/build_kb.py")
        return 1

    # Набор задаётся аргументом: отложенный набор меряется тем же кодом, что
    # основной, иначе числа «до/после» сравнивать не с чем
    chosen = Path(sys.argv[1]) if len(sys.argv) > 1 else EVAL
    if not chosen.exists():
        print(f"Набора нет: {chosen}")
        return 1
    questions, sections = load(chosen)
    _report_missing_sources()
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

    noisy: list[tuple[str, str, kb.Chunk]] = []
    for key, label in SILENCE_SECTIONS:
        noisy += await _measure_silence(label, sections.get(key, ()))

    if misses:
        print(f"\nНе нашлось ({len(misses)}):")
        for question, found in misses:
            print(f"  - {question.text}")
            if not found:
                print(f"      ждали {question.source}, выдача пуста")
                continue
            top = found[0]
            # Покрытие и счёт печатаются не для красоты: по ним видно, что
            # именно случилось. Низкое покрытие у верного документа означает,
            # что вопрос задан не теми словами; высокое у чужого — что нашлось
            # похожее, но не то, и надо смотреть на ранжирование
            print(
                f"      ждали {question.source}, выдача: {top.source_id}"
                f" (покрытие {top.coverage:.2f}, счёт {top.score:.4f})"
            )
            print(f"      {top.text[:80]}")
    if noisy:
        # Не «ответил»: после разведения исходов в `services/qa.py` на таком
        # вопросе ответит или промолчит уже модель. Отсюда видно ровно то, что
        # отсюда видно, — что пропустили ворота
        print(f"\nУшло в модель, хотя ответа в корпусе нет ({len(noisy)}):")
        for label, text, chunk in noisy:
            print(f"  - [{label}] {text}")
            print(
                f"      {chunk.source_id} (покрытие {chunk.coverage:.2f},"
                f" счёт {chunk.score:.4f}): {chunk.text[:70]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
