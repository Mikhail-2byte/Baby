"""Оценка модели ответов — вторая половина замера, которой нет в `eval_kb.py`.

    .venv\\Scripts\\python kb\\eval_qa.py snapshot
    .venv\\Scripts\\python kb\\eval_qa.py run groq:openai/gpt-oss-120b ...
    .venv\\Scripts\\python kb\\eval_qa.py report

`eval_kb.py` в сеть не ходит и меряет только поиск; там же про это сказано
прямо: непустая выдача на разделе `absent` означает «ворота передали мусор
дальше», а ответил ли бот мусором — решает модель, и этого замера не было
вовсе. Здесь меряется ровно она: **второй сторож молчания, семантический**.

Меряется боевым кодом. Промпт берётся из `qa.SYSTEM`, сообщение собирает
`qa.build_user`, вердикт выносит `qa.decide` — та самая функция, которая
судит ответы в чате. Своей копии здесь нет ни у одного шага: копия меряла бы
себя, а не бота, и расходилась бы с ним молча.

**Выдача фиксируется снимком, и это главное решение файла.** `snapshot` зовёт
боевой `kb.search` один раз и кладёт пятёрки фрагментов в файл; дальше все
модели судятся по одной и той же выдаче. Иначе сравнивалась бы не модель, а
сеть в момент прогона: плотный поиск ходит к провайдеру, и молчание
провайдера у одного кандидата превратилось бы в его «плохое качество».

**Правильным считается поведение, а не смысл сказанного.** Числа отвечают на
три вопроса: ответил ли, когда было чем; промолчал ли, когда нечем; не
выдумал ли числа. Верность пересказа документа и читаемость ночью одной рукой
ими не ловятся — для этого `report` пишет markdown со всеми ответами подряд,
и читает его человек. Модели-судьи в контуре нет намеренно: её ошибки были бы
неотличимы от ошибок подсудимого.

Набор вопросов — `kb/eval.yaml`, тот же, что у поиска, и загружается он
`eval_kb.load()`. Написан до настройки ретривера и правится только вместе с
содержимым базы.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import settings  # noqa: E402
from bot.services import kb, llm, qa, qa_log  # noqa: E402
from kb import eval_kb  # noqa: E402

OUT = ROOT / "data" / "eval"
SNAPSHOT = OUT / "qa_snapshot.json"
RESULTS = OUT / "qa_models.jsonl"

# Сколько вопросов «с ответом» берём в прогон. Все пятьдесят взять нельзя:
# кандидатов десяток, а бесплатные тиры считают запросы в сутки. Двадцать
# разбираются по источникам поровну — иначе набор перекосило бы в сторону
# самого крупного документа, и мерили бы умение читать именно его
ANSWERABLE_LIMIT = 20

# Пауза между вопросами по умолчанию. Ноль потому, что бесплатный тир
# OpenRouter считает запросы в сутки и паузы не требует; у Groq потолок в
# токенах на МИНУТУ (замерено по заголовкам: 8000 при запросе в 2500), и там
# без `--pause 25` две трети прогона превращаются в 429 — то есть в замер
# частоты, выданный за замер качества
PAUSE = 0.0


def _chunks_of(item: dict[str, Any]) -> tuple[kb.Chunk, ...]:
    return tuple(kb.Chunk(**raw) for raw in item["chunks"])


# --- Снимок выдачи ----------------------------------------------------------


def _spread(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Отобрать `limit` вопросов, разложив их по источникам поровну.

    Порядок внутри источника сохраняется, и никакой случайности здесь нет:
    прогон обязан повторяться числом в число, иначе «стало лучше» не отличить
    от другого набора.
    """
    by_source: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_source.setdefault(item["source"], []).append(item)
    picked: list[dict[str, Any]] = []
    while len(picked) < limit and any(by_source.values()):
        for queue in by_source.values():
            if queue and len(picked) < limit:
                picked.append(queue.pop(0))
    # Обратно в исходный порядок: читать отчёт удобнее по темам, а не по тому,
    # в каком порядке их раздавал отбор
    order = {item["question"]: number for number, item in enumerate(items)}
    return sorted(picked, key=lambda item: order[item["question"]])


async def snapshot(source_set: Path, limit: int) -> int:
    if not kb.available():
        print("Базы знаний нет. Соберите её: python kb/build_kb.py")
        return 1

    questions, sections = eval_kb.load(source_set)
    answerable: list[dict[str, Any]] = []
    missed = 0
    for question in questions:
        found = await kb.search(question.text, eval_kb.DEPTH)
        marks = eval_kb.hits(question, found)
        if not any(marks):
            # Вопрос, на котором промахнулся поиск, о модели ответов не
            # говорит ничего: ей нечего было показать. Это замер поиска, и
            # делается он `eval_kb.py`
            missed += 1
            continue
        answerable.append(
            {
                "group": "answer",
                "question": question.text,
                "source": question.source,
                # Номер верного фрагмента в том же счёте, в каком его видит
                # модель в промпте: с единицы
                "gold": marks.index(True) + 1,
                "chunks": [asdict(chunk) for chunk in found],
            }
        )

    silent: list[dict[str, Any]] = []
    for key, label in eval_kb.SILENCE_SECTIONS:
        for text in sections.get(key, ()):
            found = await kb.search(text, eval_kb.DEPTH)
            if not found:
                # Погашено лексическими воротами — до модели такой вопрос не
                # доходит вовсе, и мерить на нём нечего
                continue
            silent.append(
                {
                    "group": "silence",
                    "label": label,
                    "question": text,
                    "source": "",
                    "gold": 0,
                    "chunks": [asdict(chunk) for chunk in found],
                }
            )

    picked = _spread(answerable, limit)
    OUT.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(
        json.dumps(
            {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "set": str(source_set),
                "kb_path": str(settings.kb_path),
                "kb_mtime": settings.kb_path.stat().st_mtime_ns,
                "embed_model": settings.embed_model,
                "items": picked + silent,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"Снимок: {SNAPSHOT}")
    print(f"  с ответом: {len(picked)} из {len(answerable)} (поиск промахнулся на {missed})")
    print(f"  молчание:  {len(silent)} — столько строк ворота пропустили в модель")
    print(f"  смысл считан моделью: {settings.embed_model or 'не считался'}")
    return 0


# --- Прогон -----------------------------------------------------------------


def _load_snapshot() -> dict[str, Any]:
    if not SNAPSHOT.exists():
        raise SystemExit(f"Снимка нет: {SNAPSHOT}. Сначала `eval_qa.py snapshot`")
    data: dict[str, Any] = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return data


def _done() -> set[tuple[str, str]]:
    """Пары «модель + вопрос», которые уже прогнаны.

    Прогон обязан быть возобновляемым: у бесплатных тиров суточные потолки, и
    упереться в 429 на восьмой модели из десяти — обычный исход, а не сбой.
    """
    done: set[tuple[str, str]] = set()
    for record in _records(missing_ok=True):
        if record.get("llm_reason") in _NO_VERDICT:
            # Провайдер не ответил — модель ничего не сказала, и судить не о
            # чем. Засчитав такую строку сделанной, мы бы записали 429 в
            # качество модели: первый прогон по Groq так и вышел, две трети
            # строк были отказами по частоте
            continue
        done.add((record.get("model", ""), record.get("question", "")))
    return done


# Причины, при которых строка прогона ничего не говорит о модели: до неё
# дело не дошло. `BAD_JSON` сюда не входит — это как раз её работа
_NO_VERDICT = frozenset({llm.UNAVAILABLE, llm.NO_KEY, llm.NO_MODEL})


def _mark(record: dict[str, Any]) -> str:
    """Одним знаком: сделала модель то, что от неё требовалось, или нет."""
    if record["group"] == "answer":
        return "+" if record["outcome"] == qa.ANSWERED else "-"
    return "+" if record["outcome"] == qa.NOT_FOUND else "-"


async def run(models: list[str], pause: float = PAUSE) -> int:
    # Лог вопросов уводится в папку замера, и это не аккуратность. `qa.decide`
    # пишет в `qa_log` строку на каждое сработавшее правило чисел, а суточная
    # квота бота (`llm_daily_limit`) считается по тому же файлу: триста строк
    # замера боевой бот после перезапуска прочитал бы как «квота семьи
    # выедена» и молча перестал бы отвечать. Подмена стоит здесь, а не на
    # импорте: импортировать модуль замера (те же тесты) не должно значить
    # переставить боевому боту лог
    qa_log.PATH = OUT / "qa_eval.log"
    data = _load_snapshot()
    items = data["items"]
    done = _done()
    OUT.mkdir(parents=True, exist_ok=True)

    for model in models:
        left = [item for item in items if (model, item["question"]) not in done]
        print(f"\n{model}: вопросов {len(left)} из {len(items)}")
        for number, item in enumerate(left, start=1):
            if pause and number > 1:
                await asyncio.sleep(pause)
            chunks = _chunks_of(item)
            question = item["question"]
            reply = await llm.ask(qa.SYSTEM, qa.build_user(question, chunks), models=[model])
            verdict = qa.decide(question, chunks, reply)
            used = (reply.data or {}).get("used") if reply.ok else None
            record = {
                "model": model,
                "group": item["group"],
                "label": item.get("label", ""),
                "question": question,
                "gold": item["gold"],
                "outcome": verdict.kind,
                "reason": verdict.reason,
                "answer": verdict.answer,
                "used": used if isinstance(used, list) else None,
                "llm_reason": reply.reason,
                "detail": reply.detail[:200],
                "seconds": round(reply.elapsed, 1),
                "calls": reply.calls,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            with RESULTS.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            note = " ОТКАЗ, повторим в следующем прогоне" if reply.reason in _NO_VERDICT else ""
            print(
                f"  {number:>2}/{len(left)} {_mark(record)} {verdict.kind:<9}"
                f" {reply.elapsed:>4.1f}с  {question[:48]}{note}",
                flush=True,
            )
    return 0


# --- Отчёт ------------------------------------------------------------------


def _records(missing_ok: bool = False) -> list[dict[str, Any]]:
    if not RESULTS.exists():
        if missing_ok:
            return []
        raise SystemExit(f"Прогонов нет: {RESULTS}")
    out = []
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _latest(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """По одной строке на пару «модель + вопрос» — последняя.

    Повторный прогон дописывает строку, а не правит прежнюю: файл замера
    только растёт, и это правильно — по нему видно, что отказов было три, а
    ответ получен с четвёртой попытки. Но в числа такая пара обязана войти
    один раз, иначе один отказ провайдера считался бы дважды.
    """
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        by_pair[(record["model"], record["question"])] = record
    return list(by_pair.values())


def _share(part: int, whole: int) -> str:
    return f"{part / whole:.2f}" if whole else "—"


def report() -> int:
    records = _latest(_records())
    models: list[str] = []
    for record in records:
        if record["model"] not in models:
            models.append(record["model"])

    header = (
        f"{'модель':<44} {'ответил':>7} {'сослался':>8} {'промолчал':>9}"
        f" {'числа':>5} {'не JSON':>7} {'отказ':>5} {'сек':>5} {'знаков':>6}"
    )
    print(header)
    print("-" * len(header))
    for model in models:
        mine = [record for record in records if record["model"] == model]
        answers = [record for record in mine if record["group"] == "answer"]
        silence = [record for record in mine if record["group"] == "silence"]
        answered = [record for record in answers if record["outcome"] == qa.ANSWERED]
        cited = [
            record
            for record in answered
            if isinstance(record["used"], list) and record["gold"] in record["used"]
        ]
        quiet = [record for record in silence if record["outcome"] == qa.NOT_FOUND]
        invented = [record for record in mine if record["reason"] == qa.INVENTED]
        bad_json = [record for record in mine if record["llm_reason"] == llm.BAD_JSON]
        refused = [record for record in mine if record["llm_reason"] == llm.UNAVAILABLE]
        seconds = statistics.median([record["seconds"] for record in mine]) if mine else 0.0
        sizes = [len(record["answer"]) for record in answered]
        print(
            f"{model:<44} {_share(len(answered), len(answers)):>7}"
            f" {_share(len(cited), len(answered)):>8}"
            f" {_share(len(quiet), len(silence)):>9}"
            f" {len(invented):>5} {len(bad_json):>7} {len(refused):>5}"
            f" {seconds:>5.1f} {int(statistics.median(sizes)) if sizes else 0:>6}"
        )

    print(
        "\nответил   — ответил там, где верный фрагмент был в выдаче"
        "\nсослался  — и назвал его в `used` (доля от ответивших)"
        "\nпромолчал — вернул пустой ответ там, где ответа в корпусе нет"
        "\nчисла     — сколько раз боевой сторож `qa.invented_numbers` отбросил ответ"
        "\nотказ     — провайдер не ответил или отказал (код в `detail`)"
    )

    broken = [record for record in records if record["llm_reason"] == llm.UNAVAILABLE]
    if broken:
        print("\nОтказы провайдера (первый на модель):")
        seen = set()
        for record in broken:
            if record["model"] in seen:
                continue
            seen.add(record["model"])
            print(f"  {record['model']}: {record['detail'][:160]}")

    print(f"\nОтветы целиком: {_write_markdown(records)}")
    return 0


def _write_markdown(records: list[dict[str, Any]]) -> Path:
    """Всё, что числами не измеряется, — глазами.

    Вопрос, показанные фрагменты и ответы всех моделей подряд. Фрагменты
    печатаются целиком не для полноты: судить пересказ, не видя источника,
    нельзя, а это единственное, ради чего отчёт читают.
    """
    data = _load_snapshot()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"qa_models_{datetime.now(UTC):%Y-%m-%d}.md"
    lines = [
        "# Ответы моделей на оценочном наборе",
        "",
        f"Снимок выдачи: {data['at']}, смысл — {data['embed_model'] or 'не считался'}.",
        "Вопросы и ожидаемые источники — `kb/eval.yaml`, набор написан до настройки поиска.",
        "",
    ]
    for item in data["items"]:
        question = item["question"]
        mine = [record for record in records if record["question"] == question]
        if not mine:
            continue
        lines += [f"## {question}", ""]
        if item["group"] == "answer":
            lines += [f"Ждём ответ, верный фрагмент — [{item['gold']}] из `{item['source']}`.", ""]
        else:
            lines += [f"Ждём молчание ({item['label']}): ответа в корпусе нет.", ""]
        for record in mine:
            verdict = record["outcome"]
            if record["llm_reason"]:
                verdict += f" ({record['llm_reason']}: {record['detail'][:120]})"
            elif record["reason"]:
                verdict += f" ({record['reason']})"
            lines += [f"**{record['model']}** — {verdict}, `used` {record['used']}", ""]
            if record["answer"]:
                lines += ["> " + record["answer"].replace("\n", "\n> "), ""]
        lines += ["<details><summary>Фрагменты, которые видела модель</summary>", ""]
        for number, raw in enumerate(item["chunks"], start=1):
            head = f"{raw['title']}. {raw['heading']}" if raw["heading"] else raw["title"]
            lines += [f"**[{number}]** {head}", "", raw["text"], ""]
        lines += ["</details>", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # Консоль Windows работает в cp1251 и роняет скрипт на первом символе
        # вне кодировки, а сюда едут и вопросы, и ответы моделей
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Замер моделей ответов по базе знаний")
    sub = parser.add_subparsers(dest="command", required=True)
    shot = sub.add_parser("snapshot", help="зафиксировать выдачу поиска")
    shot.add_argument("--set", default=str(eval_kb.EVAL), help="оценочный набор")
    shot.add_argument("--limit", type=int, default=ANSWERABLE_LIMIT)
    runner = sub.add_parser("run", help="прогнать модели по снимку")
    runner.add_argument("models", nargs="+", help="записи вида вендор:модель")
    runner.add_argument(
        "--pause", type=float, default=PAUSE, help="секунд между вопросами (Groq: 25)"
    )
    sub.add_parser("report", help="свести числа и написать markdown")
    args = parser.parse_args()

    if args.command == "snapshot":
        return asyncio.run(snapshot(Path(args.set), args.limit))
    if args.command == "run":
        return asyncio.run(run(args.models, args.pause))
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
