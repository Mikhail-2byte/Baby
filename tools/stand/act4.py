"""Акт 4 — голос (этап 7).

Первый акт стенда, который **тратит деньги у внешнего провайдера**: до сих пор
настоящим был только Telegram, а расшифровка и разбор идут через Groq и
OpenRouter и едят ту же суточную квоту, что Q&A. Поэтому акт отдельный, а не
дописан к первому: его запускают осознанно.

Записи надиктовываются руками и лежат в `tools/stand/fixtures/`. Синтезировать
их нечем — TTS в зависимостях нет, — а подсунуть тишину значило бы проверять
расшифровку на пустом файле. Файлов нет или ключа нет — акт **отказывается**, а
не выдаёт зелёный протокол на молчании: зелёный прогон на невыполненной работе
в этом проекте уже трижды оказывался главной находкой.

Шаг с красным флагом голосом — главный. Он проверяет то, чего не проверяет ни
один другой шаг стенда: `alarm` висит на `F.text | F.caption`, голосового не
видит вовсе, и граница безопасности на этом канале держится вызовом внутри
хендлера. Сверять надо не только текст ответа, но и `data/qa.log`: строка с
`calls > 0` означала бы, что расшифровка дошла до модели.
"""

from __future__ import annotations

import json
from pathlib import Path

from bot import keyboards as kb
from bot.config import settings
from tools.stand.harness import Stand

FIXTURES = Path("tools/stand/fixtures")

# Что должно быть надиктовано и что примерно сказано. Имя файла — ключ шага;
# текст в скобках здесь для того, кто будет записывать
SCRIPT = {
    "feed.ogg": "покормила двадцать минут левой в три сорок",
    "diaper.ogg": "подгузник мокрый",
    "med.ogg": "дала витамин д, одну каплю",
    "temp.ogg": "температура тридцать шесть и восемь",
    "note.ogg": "сегодня первый раз улыбнулась",
    "sleep.ogg": "уснула",
    "weight.ogg": "вес три тысячи двести",
    "question.ogg": "сколько должен спать ребёнок в два месяца",
    "redflag.ogg": "у неё судороги, что делать",
    "long.ogg": "любая короткая запись, длительность подменяется в апдейте",
}


def _missing() -> list[str]:
    return [name for name in SCRIPT if not (FIXTURES / name).exists()]


def _qa_log_calls() -> int:
    """Сколько обращений к модели записано в логе. Для шага про красный флаг."""
    path = Path(settings.db_path).parent / "qa.log"
    if not path.exists():
        return 0
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("event") == "voice":
            total += int(record.get("calls") or 0)
    return total


async def run(stand: Stand) -> None:
    if not settings.stt_key:
        raise RuntimeError(
            "STT_KEY пуст — расшифровки не будет, и акт проверил бы только молчание. "
            "Заполните ключ или не запускайте этот акт."
        )
    if not (settings.llm_models or settings.llm_answer_models):
        raise RuntimeError(
            "LLM_MODELS пуст — разбирать расшифровку нечем. Акт проверил бы "
            "только путь отказа, а он и так покрыт тестами."
        )
    absent = _missing()
    if absent:
        raise RuntimeError(
            "Не надиктованы записи: "
            + ", ".join(absent)
            + f". Положите их в {FIXTURES} — что говорить, написано в SCRIPT."
        )
    if not stand.child_id():
        raise RuntimeError("Нет карточки ребёнка — сначала акт 1")

    await stand.mark("Стенд: акт 4, голос")

    async with stand.step("60", "кормление голосом, время задним числом") as r:
        r.note(SCRIPT["feed.ogg"])
        await stand.speak(FIXTURES / "feed.ogg")
        r.expect("Услышал", "Записать?")
        r.note("числительные обязаны стать цифрами: «двадцать» → 20")

    async with stand.step("61", "подтверждение карточки") as r:
        await stand.tap(kb.VoiceCB(action="save").pack(), on="last")
        r.expect("Записал")
        r.show(
            "последнее событие",
            "SELECT kind, started_at, ended_at, side FROM events "
            "ORDER BY id DESC LIMIT 1",
        )
        r.note("конец обязан быть посчитан кодом: начало плюс 20 минут")

    async with stand.step("62", "то, чего нет ни на одной кнопке: лекарство") as r:
        r.note(SCRIPT["med.ogg"])
        await stand.speak(FIXTURES / "med.ogg")
        r.expect("Записать?")
        await stand.tap(kb.VoiceCB(action="save").pack(), on="last")
        r.show(
            "лекарство",
            "SELECT kind, subtype, value_num FROM events WHERE kind = 'med' "
            "ORDER BY id DESC LIMIT 1",
        )

    async with stand.step("63", "температура ниже порога — запись, а не тревога") as r:
        r.note(SCRIPT["temp.ogg"] + " — 36.8 порогов не задевает")
        await stand.speak(FIXTURES / "temp.ogg")
        r.expect("Записать?")
        r.expect_absent("103")
        await stand.tap(kb.VoiceCB(action="save").pack(), on="last")
        r.show(
            "температура",
            "SELECT kind, value_num FROM events WHERE kind = 'temp' "
            "ORDER BY id DESC LIMIT 1",
        )

    async with stand.step("64", "заметка голосом") as r:
        r.note(SCRIPT["note.ogg"])
        await stand.speak(FIXTURES / "note.ogg")
        r.expect("Записать?")
        await stand.tap(kb.VoiceCB(action="save").pack(), on="last")
        r.show(
            "заметка",
            "SELECT kind, note FROM events WHERE kind = 'note' ORDER BY id DESC LIMIT 1",
        )

    async with stand.step("65", "«Не то» не пишет ничего") as r:
        before = stand.one("SELECT COUNT(*) FROM events")
        await stand.speak(FIXTURES / "diaper.ogg")
        r.expect("Записать?")
        await stand.tap(kb.VoiceCB(action="drop").pack(), on="last")
        r.expect("Не записал")
        after = stand.one("SELECT COUNT(*) FROM events")
        r.note(f"событий было {before}, стало {after}")
        if before != after:
            r.stand.protocol.fail("шаг 65", "«Не то», а запись появилась")

    async with stand.step("66", "открытый таймер голосом поверх идущего") as r:
        await stand.tap(
            kb.TrackCB(action="start", kind="feed_breast", side="L").pack()
        )
        r.note("кнопкой открыт таймер кормления")
        await stand.speak(FIXTURES / "sleep.ogg")
        await stand.tap(kb.VoiceCB(action="save").pack(), on="last")
        r.expect("Закрыл предыдущее")
        r.show(
            "идущие таймеры",
            "SELECT kind, started_at, ended_at, estimated FROM events "
            "WHERE ended_at IS NULL AND kind IN ('feed_breast','sleep','pump')",
        )
        r.note("идущим обязан остаться ровно один, и это сон")

    async with stand.step("67", "вес голосом — в measurements, с центилем") as r:
        await stand.speak(FIXTURES / "weight.ogg")
        r.expect("Записать?")
        await stand.tap(kb.VoiceCB(action="save").pack(), on="last")
        r.show(
            "взвешивания",
            "SELECT taken_at, value FROM measurements ORDER BY id DESC LIMIT 2",
        )
        r.note("в ответе обязаны быть центиль и прибавка — те же, что у ввода числом")

    async with stand.step("68", "вопрос голосом уходит в базу знаний") as r:
        r.note(SCRIPT["question.ogg"])
        await stand.speak(FIXTURES / "question.ogg")
        r.expect_absent("Записать?")
        r.note("ответ обязан выглядеть как у кнопки «❓ Вопрос»")

    async with stand.step("69", "красный флаг голосом — до модели не доходит") as r:
        spent_before = _qa_log_calls()
        r.note(SCRIPT["redflag.ogg"])
        await stand.speak(FIXTURES / "redflag.ogg")
        r.expect("103")
        r.expect_absent("Записать?", "Услышал")
        spent_after = _qa_log_calls()
        r.note(f"обращений к модели в qa.log: было {spent_before}, стало {spent_after}")
        if spent_after != spent_before:
            r.stand.protocol.fail(
                "шаг 69", "расшифровка с красным флагом дошла до модели"
            )

    async with stand.step("70", "слишком длинная запись — отказ до скачивания") as r:
        limit = settings.voice_max_seconds
        await stand.speak(FIXTURES / "long.ogg", duration=limit + 60)
        r.expect(str(limit))
        r.expect_absent("Услышал")

    await stand.mark("Акт 4 закончен")
