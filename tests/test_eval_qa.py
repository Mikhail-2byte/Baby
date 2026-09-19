"""Сторожа замера моделей ответов.

Скрипт `kb/eval_qa.py` в сеть ходит всегда, и проверить его прогоном нельзя.
Зато у него есть три решения, ошибка в которых не видна никак — числа выйдут
правдоподобные, — и проверяются здесь ровно они.
"""

import json

from bot.services import llm
from kb import eval_qa


def _record(model: str, question: str, **extra) -> dict:
    record = {
        "model": model,
        "question": question,
        "group": "answer",
        "gold": 1,
        "outcome": "answered",
        "reason": "",
        "answer": "текст",
        "used": [1],
        "llm_reason": "",
        "detail": "",
        "seconds": 1.0,
        "calls": 1,
    }
    record.update(extra)
    return record


def _write(path, records) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def test_a_provider_refusal_is_not_a_measurement(tmp_path, monkeypatch):
    """Отказ провайдера не считается сделанной работой.

    Первый же прогон по Groq уткнулся в потолок токенов на минуту, и две трети
    строк приехали отказом. Засчитав их сделанными, замер записал бы 429 в
    качество модели и больше никогда бы к этим вопросам не вернулся — то есть
    соврал бы тихо и навсегда.

    `bad_json` — противоположный случай и потому проверяется рядом: это ответ
    модели, просто негодный. Повторять его незачем, иначе кандидат, который
    систематически не умеет в JSON, крутился бы в прогоне вечно.
    """
    monkeypatch.setattr(eval_qa, "RESULTS", tmp_path / "qa_models.jsonl")
    _write(
        eval_qa.RESULTS,
        [
            _record("groq:a", "ответ был", llm_reason=""),
            _record("groq:a", "429 по частоте", llm_reason=llm.UNAVAILABLE, outcome="extract"),
            _record("groq:a", "мусор вместо json", llm_reason=llm.BAD_JSON, outcome="extract"),
            _record("groq:a", "ключа нет", llm_reason=llm.NO_KEY, outcome="extract"),
        ],
    )

    done = eval_qa._done()

    assert ("groq:a", "ответ был") in done
    assert ("groq:a", "мусор вместо json") in done
    assert ("groq:a", "429 по частоте") not in done
    assert ("groq:a", "ключа нет") not in done


def test_a_repeated_question_counts_once(tmp_path, monkeypatch):
    """В числа пара «модель плюс вопрос» входит один раз — последней строкой.

    Файл замера только растёт: по нему видно, что отказов было три, а ответ
    получен с четвёртой попытки. Но посчитав все четыре строки, отчёт показал
    бы три отказа там, где модель в итоге ответила.
    """
    monkeypatch.setattr(eval_qa, "RESULTS", tmp_path / "qa_models.jsonl")
    _write(
        eval_qa.RESULTS,
        [
            _record("groq:a", "вопрос", llm_reason=llm.UNAVAILABLE, outcome="extract"),
            _record("groq:a", "вопрос", answer="со второй попытки"),
            _record("groq:b", "вопрос"),
        ],
    )

    latest = eval_qa._latest(eval_qa._records())

    assert len(latest) == 2
    mine = [record for record in latest if record["model"] == "groq:a"]
    assert mine[0]["answer"] == "со второй попытки"


def test_the_set_is_spread_across_sources():
    """Отбор вопросов раскладывает их по источникам поровну.

    Взяв первые двадцать подряд, мы измерили бы умение читать самый крупный
    документ корпуса: вопросы в наборе идут темами, и одна тема заняла бы весь
    прогон. Порядок при этом обязан быть исходным и повторяемым — прогон
    сравнивают с прогоном, и перетасованный набор сравнивать не с чем.
    """
    items = [{"question": f"{source}-{number}", "source": source}
             for source in ("первый", "второй", "третий")
             for number in range(4)]

    picked = eval_qa._spread(items, 6)

    assert len(picked) == 6
    sources = [item["source"] for item in picked]
    assert sources.count("первый") == sources.count("второй") == sources.count("третий") == 2
    assert picked == sorted(picked, key=lambda item: items.index(item))
