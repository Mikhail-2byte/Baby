"""Голос: расшифровка, числительные, разбор, карточка (этап 7).

Сети в тестах нет: `httpx` подменяется `MockTransport` (тот же приём, что в
`test_llm.py`), скачивание — методом `FakeBot.download`, а модель — подменой
`llm.ask`, как в `test_qa.py`.
"""

from datetime import datetime, timedelta

import httpx
import pytest

from bot import texts
from bot.db.models import (
    DIAPER_WET,
    FEED_BOTTLE,
    FEED_BREAST,
    FEED_SOLID,
    LEFT,
    MED,
    NOTE,
    PUMP,
    SLEEP,
    TEMP,
    WEIGHT,
)
from bot.handlers import measure
from bot.services import dictation, http_retry, numerals, qa_log
from bot.services import redflags as rf
from bot.services import voice as stt

STT_URL = "https://stt.test/v1/audio/transcriptions"

NOW = datetime(2026, 9, 14, 2, 0)


# --- Клиент расшифровки -----------------------------------------------------


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Паузы между ретраями живут в `http_retry` — там их и гасим.

    Не в `voice`: с выносом общей политики повторов спать в самом клиенте стало
    некому, и тест, подменяющий `voice.asyncio`, падал бы на отсутствии
    атрибута. Урок из Family_planer, перенесённый вместе с кодом.
    """

    async def instant(_seconds):
        return None

    monkeypatch.setattr(http_retry.asyncio, "sleep", instant)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(stt.settings, "stt_key", "gsk-test")
    monkeypatch.setattr(stt.settings, "stt_model", "whisper-test")
    monkeypatch.setattr(stt.settings, "stt_url", STT_URL)
    monkeypatch.setattr(stt.settings, "stt_proxy", "")


@pytest.fixture
def patched(monkeypatch):
    """Транспорт, отдающий заготовленные ответы; `calls` копит тела запросов."""

    def apply(responses, calls=None):
        sequence = list(responses)

        def handler_fn(request: httpx.Request) -> httpx.Response:
            if calls is not None:
                calls.append(request.content)
            item = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            if isinstance(item, Exception):
                raise item
            return item

        transport = httpx.MockTransport(handler_fn)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            # MockTransport не принимает proxy, а боевой код его передаёт
            kwargs.pop("proxy", None)
            return original(*args, transport=transport, **kwargs)

        monkeypatch.setattr(stt.httpx, "AsyncClient", factory)

    return apply


def _ok(text: str) -> httpx.Response:
    return httpx.Response(200, json={"text": text})


@pytest.mark.asyncio
async def test_transcribe_returns_text(patched):
    calls: list[bytes] = []
    patched([_ok("  покормила двадцать минут левой  ")], calls)

    assert await stt.transcribe(b"OggS") == "покормила двадцать минут левой"
    assert len(calls) == 1
    # Модель, язык и просьба про цифры уходят в multipart явно. Автоопределение
    # языка на коротких записях отдаёт русскую фразу латиницей
    assert b"whisper-test" in calls[0]
    assert b"ru" in calls[0]
    assert stt.HINT.encode() in calls[0]


@pytest.mark.asyncio
async def test_busy_quota_is_retried(patched):
    calls: list[bytes] = []
    patched([httpx.Response(429), _ok("подгузник мокрый")], calls)

    assert await stt.transcribe(b"OggS") == "подгузник мокрый"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_gives_up_after_attempts(patched):
    calls: list[bytes] = []
    patched([httpx.Response(503)], calls)

    assert await stt.transcribe(b"OggS") is None
    assert len(calls) == stt.ATTEMPTS


@pytest.mark.asyncio
async def test_bad_key_is_not_retried(patched):
    """401 повтором не лечится: ключ не станет хорошим со второй попытки."""
    calls: list[bytes] = []
    patched([httpx.Response(401, text="bad key")], calls)

    assert await stt.transcribe(b"OggS") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_network_failure_is_none(patched):
    patched([httpx.ConnectError("нет сети")])
    assert await stt.transcribe(b"OggS") is None


@pytest.mark.asyncio
async def test_silence_is_not_retried(patched):
    """Тишина в записи повтором не лечится — она и во второй раз тишина."""
    calls: list[bytes] = []
    patched([_ok("   ")], calls)

    assert await stt.transcribe(b"OggS") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_not_json_is_not_retried(patched):
    calls: list[bytes] = []
    patched([httpx.Response(200, text="<html>502 Bad Gateway</html>")], calls)

    assert await stt.transcribe(b"OggS") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_without_a_key_there_is_no_request(patched, monkeypatch):
    """Пустой `.env` — рабочий режим: голос просто выключен."""
    calls: list[bytes] = []
    patched([_ok("что-нибудь")], calls)
    monkeypatch.setattr(stt.settings, "stt_key", "")

    assert await stt.transcribe(b"OggS") is None
    assert calls == []


def test_available_is_false_without_a_key(monkeypatch):
    """Спрашивается ДО скачивания файла — см. докстроку `voice.available`."""
    assert stt.available()
    monkeypatch.setattr(stt.settings, "stt_key", "")
    assert not stt.available()
    monkeypatch.setattr(stt.settings, "stt_key", "gsk-test")
    monkeypatch.setattr(stt.settings, "stt_model", "")
    assert not stt.available()


# --- Числительные -----------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("температура тридцать девять", "температура 39"),
        ("тридцать семь и пять", "37.5"),
        ("покормила двадцать минут левой", "покормила 20 минут левой"),
        ("дала сорок мл", "дала 40 мл"),
        ("вес три тысячи восемьсот", "вес 3800"),
        ("сто двадцать пять миллилитров", "125 миллилитров"),
        # Сотни, десятки, единицы подряд — одно число
        ("двести пятьдесят", "250"),
    ],
)
def test_numerals_become_digits(said, expected):
    assert numerals.digits(said) == expected


def test_one_and_a_half_becomes_a_number():
    """«Полтора» — найдено на живой модели, а не придумано.

    На «поспала полтора часа» модель, не увидев в тексте ни одной цифры,
    вернула длительность `null` — то есть законченный сон превратился в идущий
    таймер. Дальше его закрыл бы тикер с пометкой «приблизительно», и в отчёте
    педиатру четырёхчасовой сон выглядел бы измеренным.

    С цифрой обе доступные модели отвечают 90 минутами и закрытым сном.
    """
    assert numerals.digits("поспала полтора часа") == "поспала 1.5 часа"
    assert numerals.digits("полторы минуты") == "1.5 минуты"


def test_one_and_a_half_does_not_join_its_neighbours():
    """Готовое число: ни с чем не складывается и ничем не продолжается.

    Иначе «полтора двадцать» дало бы 21.5, а «два полтора» — 3.5, то есть
    случайную длительность из двух не связанных слов.
    """
    assert numerals.digits("полтора двадцать") == "1.5 20"
    assert numerals.digits("два полтора") == "2 1.5"


def test_a_clock_time_is_not_summed():
    """«Три сорок» — не 43, а 3:40.

    По-русски «три сорок» числом не является: разряды идут по возрастанию, а
    так составные числа не строят. Складывать всё подряд было бы хуже всего —
    «в три сорок» дало бы 43, и запись уехала бы на несуществующее время.
    """
    assert numerals.digits("в три сорок") == "в 3 40"


def test_a_small_whole_is_not_a_decimal():
    """«Три и пять» — перечисление, а не 3.5.

    Порог у дробной части («слева не меньше десяти») про язык: десятичную долю
    словами называют у больших чисел, а у маленьких перечисляют.
    """
    assert numerals.digits("три и пять") == "3 и 5"


def test_text_without_numerals_is_untouched():
    said = "она хорошо поспала, всё в порядке!"
    assert numerals.digits(said) == said


def test_digits_already_written_are_untouched():
    said = "уже 39 и 5 градусов"
    assert numerals.digits(said) == said


def test_the_triage_hears_a_temperature_said_in_words():
    """Главный довод за существование `numerals`.

    `redflags.find_temperatures` ищет ЦИФРЫ. Whisper пишет «тридцать девять»
    словами, и без конвертации самый важный порог во всём боте не срабатывал бы
    ровно на голосовом канале — молча, при полностью исправном виде.
    """
    said = "температура тридцать девять"
    assert rf.triage(said, None) == []
    hits = rf.triage(numerals.digits(said), None)
    assert [hit.flag.id for hit in hits] == ["fever_young"]
    assert rf.worst(hits) == "emergency"


def test_a_whole_temperature_still_needs_the_word():
    """«У неё тридцать девять» молчит и после конвертации — как и в тексте.

    Целое число в диапазоне 34–43 триаж принимает за температуру только при
    слове о температуре: «39» в этом боте с равным успехом означает миллилитры
    в бутылочке. Голос ведёт себя точно так же, как клавиатура, то есть новой
    дыры здесь нет — и записано это затем, чтобы следующий читатель не счёл
    поведение поломкой.
    """
    assert numerals.digits("у неё тридцать девять") == "у неё 39"
    assert rf.triage("у неё 39", None) == []


# --- Промпт и границы модуля ------------------------------------------------


def test_the_prompt_keeps_its_required_rules(tmp_path, monkeypatch):
    """Промпт без любого из правил роняет импорт, а не тихо портит разбор."""
    original = dictation.PROMPT.read_text(encoding="utf-8")
    for required in dictation.REQUIRED:
        broken = tmp_path / "voice_prompt.md"
        broken.write_text(original.replace(required, "…"), encoding="utf-8")
        monkeypatch.setattr(dictation, "PROMPT", broken)
        with pytest.raises(ValueError, match="пропало"):
            dictation._load()


def test_the_prompt_needs_the_separator(tmp_path, monkeypatch):
    broken = tmp_path / "voice_prompt.md"
    broken.write_text("только шапка, без разделителя", encoding="utf-8")
    monkeypatch.setattr(dictation, "PROMPT", broken)
    with pytest.raises(ValueError, match="разделител"):
        dictation._load()


def test_the_prompt_body_is_not_empty(tmp_path, monkeypatch):
    broken = tmp_path / "voice_prompt.md"
    broken.write_text("шапка\n---\n   \n", encoding="utf-8")
    monkeypatch.setattr(dictation, "PROMPT", broken)
    with pytest.raises(ValueError, match="пусто"):
        dictation._load()


def test_the_prompt_offers_only_kinds_the_base_knows():
    """Вид, которого нет в схеме, разбор молча выбросит.

    Тогда голос перестал бы записывать целую категорию, оставаясь при этом
    полностью «работающим»: карточка просто не появлялась бы на таких фразах.
    Список в промпте и список в `models.py` обязаны совпадать, и сверять их
    глазами — не способ.
    """
    body = dictation.PROMPT.read_text(encoding="utf-8")
    _, _, listing = body.partition("Виды записей (поле kind):")
    block, _, _ = listing.partition("\n\nСхема ответа")
    offered = {
        line.split("`")[1]
        for line in block.splitlines()
        if line.startswith("- `")
    }
    assert offered, "список видов в промпте не нашёлся — тест смотрит не туда"
    assert offered == dictation.KINDS


def test_the_parser_does_not_know_about_red_flags():
    """`dictation` не импортирует ни `redflags`, ни `kb`, и это физически.

    Соблазн «проверить флаги ещё и здесь» — это вторая копия границы
    безопасности, а копия расходится с оригиналом молча. Триаж отрабатывает на
    расшифровке, до модели, и фраза про судороги сюда не доходит вовсе.

    Читается деревом разбора, а не поиском подстроки: «kb» — две буквы, и
    подстрочный поиск однажды сойдётся на слове внутри комментария или
    разойдётся на переносе строки.
    """
    import ast

    source = dictation.PROMPT.parent.parent / "services" / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)

    # Сначала убедимся, что дерево вообще прочиталось: без этой строки тест
    # прошёл бы и на пустом множестве, то есть на любой опечатке в пути
    assert "bot.services.llm" in imported
    assert not {name for name in imported if "redflags" in name}
    assert not {name for name in imported if name.endswith(("kb", ".kb"))}


def test_the_stamp_names_the_weekday_and_the_time():
    """Без «сейчас» фраза «в три сорок» не разбирается вовсе."""
    system = dictation.build_system(NOW, "Asia/Yekaterinburg", "Аня", "6 дней")
    assert "понедельник" in system
    assert "14 сентября" in system
    assert "02:00" in system
    assert "Asia/Yekaterinburg" in system
    assert "Аня" in system and "6 дней" in system


def test_the_plausibility_range_covers_what_the_triage_judges():
    """Диапазон температуры в разборе обязан быть шире триажного.

    Иначе голос отбросил бы показание, о котором у триажа есть что сказать, —
    и записи не появилось бы ровно в том случае, когда она нужнее всего. Тест
    сверяет два диапазона, а не перепечатывает числа: перепечатанные разошлись
    бы молча.
    """
    assert rf.THRESHOLDS["temp_min_c"] >= dictation.TEMP_MIN
    assert rf.THRESHOLDS["temp_max_c"] <= dictation.TEMP_MAX


# --- Приведение ответа модели -----------------------------------------------


def _raw(**fields):
    return {"intent": "record", "records": [{"kind": FEED_BREAST, **fields}]}


def test_a_full_record_survives():
    intent, records = dictation.normalize(
        _raw(at="2026-09-14T01:40:00", minutes=20, side="L"), NOW
    )
    assert intent == dictation.RECORD
    assert len(records) == 1
    one = records[0]
    assert one.kind == FEED_BREAST
    assert one.at == datetime(2026, 9, 14, 1, 40)
    assert one.minutes == 20
    assert one.side == LEFT
    assert not one.open_timer


@pytest.mark.parametrize("garbage", [None, [], "текст", 5, {"records": 5}])
def test_garbage_is_unclear_not_an_exception(garbage):
    assert dictation.normalize(garbage, NOW) == (dictation.UNCLEAR, [])


def test_an_unknown_kind_is_dropped_not_defaulted():
    """Вид не угадываем.

    У планировщика неизвестный тип становился задачей, и это было безобидно.
    Здесь подмена вида — порча данных о ребёнке: «прикорм» вместо «лекарства»
    уедет в выгрузку для педиатра и будет выглядеть измеренным.
    """
    intent, records = dictation.normalize(
        {"intent": "record", "records": [{"kind": "feeding"}]}, NOW
    )
    assert records == []
    # И намерение честно меняется: врать «записал» нечем
    assert intent == dictation.UNCLEAR


def test_year_one_never_reaches_the_conversion():
    """`0001-01-01` от модели — не бессмыслица, а падение хендлера.

    Проверяется обеими половинами: что `normalize` такую дату не пропускает, и
    что она действительно роняет `to_utc`. Без второй половины тест был бы
    тавтологией — он доказывал бы только то, что код делает то, что написано.
    """
    from bot.services import timeutil as tu

    _, records = dictation.normalize(_raw(at="0001-01-01T00:00:00"), NOW)
    assert records[0].at == NOW
    assert records[0].guessed_time

    with pytest.raises(OverflowError):
        tu.to_utc(datetime(1, 1, 1, 0, 0), "Asia/Yekaterinburg")


def test_a_named_time_in_the_future_is_yesterday():
    """«В три сорок», сказанное в два ночи, — это вчера.

    Названное вслух время суток всегда про прошлое: человек диктует о том, что
    было. Без сдвига запись уехала бы в будущее, и сутки её не увидели бы.
    """
    _, records = dictation.normalize(_raw(at="2026-09-14T03:40:00"), NOW)
    assert records[0].at == datetime(2026, 9, 13, 3, 40)
    assert not records[0].guessed_time


def test_a_time_just_ahead_of_now_is_not_moved_a_day():
    """Допуск нужен: «сейчас» модель узнала до вызова, а мы читаем часы после.

    Между этими двумя моментами лежит вся цепочка с повторами, до полутора
    минут. Без допуска «уснула», сказанное прямо сейчас, уехало бы на сутки
    назад.
    """
    ahead = (NOW + timedelta(seconds=30)).isoformat()
    _, records = dictation.normalize(_raw(at=ahead), NOW)
    assert records[0].at.date() == NOW.date()


def test_a_wildly_future_date_becomes_now_and_says_so():
    """Сутки вычитаются один раз, а не в цикле.

    Цикл превратил бы ошибку модели в дату месячной давности — и сделал бы это
    молча, а тут бот признаётся, что время не разобрал.
    """
    _, records = dictation.normalize(_raw(at="2026-10-20T10:00:00"), NOW)
    assert records[0].at == NOW
    assert records[0].guessed_time


def test_a_date_older_than_the_window_is_dropped():
    """Кормление, уехавшее на три недели назад, портит сводки незаметно."""
    _, records = dictation.normalize(_raw(at="2026-08-20T10:00:00"), NOW)
    assert records[0].at == NOW
    assert records[0].guessed_time


def test_no_time_at_all_is_not_a_guess():
    """«Уснула» без часов означает «сейчас», и говорить об этом незачем."""
    _, records = dictation.normalize(_raw(), NOW)
    assert records[0].at == NOW
    assert not records[0].guessed_time


def test_an_offset_from_the_model_is_ignored():
    """Смещению от модели доверять нельзя: неизвестно, из какой зоны она его взяла.

    А `to_utc` делает `replace(tzinfo=...)`, то есть выжившее смещение молча
    победило бы зону семьи.
    """
    _, records = dictation.normalize(_raw(at="2026-09-14T01:40:00+09:00"), NOW)
    assert records[0].at.tzinfo is None
    assert records[0].at == datetime(2026, 9, 14, 1, 40)


@pytest.mark.parametrize("minutes", [0, -5, 1e9, True, "двадцать", None])
def test_implausible_minutes_are_dropped(minutes):
    _, records = dictation.normalize(_raw(minutes=minutes), NOW)
    assert records[0].minutes is None


def test_a_timed_kind_without_minutes_is_an_open_timer():
    """«Уснула» — это старт таймера, ровно как кнопка «😴 Сон»."""
    intent, records = dictation.normalize(
        {"intent": "record", "records": [{"kind": SLEEP}]}, NOW
    )
    assert records[0].open_timer


def test_an_instant_kind_is_never_an_open_timer():
    """У подгузника `ended_at` пуст всегда: это момент, а не отрезок.

    Условие «нет конца — значит идёт», применённое ко всем видам, однажды
    затянуло в каждую выборку все мгновенные события за всю историю.
    """
    _, records = dictation.normalize(
        {"intent": "record", "records": [{"kind": DIAPER_WET, "minutes": 5}]}, NOW
    )
    assert not records[0].open_timer
    assert records[0].minutes is None


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        (FEED_BOTTLE, 120, 120.0),
        (FEED_BOTTLE, 600, None),
        (FEED_BOTTLE, 0, None),
        (PUMP, 80, 80.0),
        (TEMP, 37.5, 37.5),
        (TEMP, 400, None),
        (MED, 2, 2.0),
        (WEIGHT, 3200, 3200.0),
        (DIAPER_WET, 5, None),
        (FEED_BOTTLE, True, None),
        (FEED_BOTTLE, float("inf"), None),
    ],
)
def test_the_value_means_what_the_kind_says(kind, value, expected):
    _, records = dictation.normalize(
        {"intent": "record", "records": [{"kind": kind, "value": value}]}, NOW
    )
    assert records[0].value == expected


def test_the_side_belongs_only_to_the_breast_and_the_pump():
    _, records = dictation.normalize(_raw(side="l"), NOW)
    assert records[0].side == LEFT

    _, records = dictation.normalize(
        {"intent": "record", "records": [{"kind": DIAPER_WET, "side": "L"}]}, NOW
    )
    assert records[0].side is None

    _, records = dictation.normalize(_raw(side="левая"), NOW)
    assert records[0].side is None


def test_two_records_from_one_phrase_both_arrive():
    """«Покормила двадцать минут левой и подгузник мокрый» — две записи.

    Разбор, берущий первую половину и молча теряющий вторую, хуже отказа:
    потерю не видно ничем.
    """
    intent, records = dictation.normalize(
        {
            "intent": "record",
            "records": [
                {"kind": FEED_BREAST, "minutes": 20, "side": "L"},
                {"kind": DIAPER_WET},
            ],
        },
        NOW,
    )
    assert intent == dictation.RECORD
    assert [one.kind for one in records] == [FEED_BREAST, DIAPER_WET]


def test_a_looping_model_is_cut_at_the_ceiling():
    _, records = dictation.normalize(
        {"intent": "record", "records": [{"kind": DIAPER_WET}] * 9}, NOW
    )
    assert len(records) == dictation.MAX_RECORDS


def test_long_text_fields_are_cut_not_dropped():
    _, records = dictation.normalize(
        {
            "intent": "record",
            "records": [{"kind": MED, "subtype": "я" * 200, "note": "ю" * 900}],
        },
        NOW,
    )
    assert len(records[0].subtype) == dictation.SUBTYPE_LIMIT
    assert len(records[0].note) == dictation.NOTE_LIMIT


def test_a_question_keeps_its_intent_whatever_else_came():
    """Намерение не подменяется наличием записей.

    Модель, приложившая к вопросу мусорную запись, не должна превращать вопрос
    в карточку: «сколько должен спать» с карточкой сна вместо ответа — это
    молчание в ответ на прямой вопрос.
    """
    intent, _ = dictation.normalize(
        {"intent": "question", "records": [{"kind": FEED_BREAST}]}, NOW
    )
    assert intent == dictation.QUESTION

    # И обратно: пустые записи при намерении «запись» дают «не понял», а не
    # пустую карточку с кнопкой «Записать»
    intent, records = dictation.normalize({"intent": "record", "records": []}, NOW)
    assert (intent, records) == (dictation.UNCLEAR, [])


# --- Порядок отказов разбора ------------------------------------------------


@pytest.fixture
def model(monkeypatch):
    """Подмена модели. Возвращает список вызовов — проверять и сам факт вызова."""

    def apply(answer):
        calls: list[tuple[str, str, list[str] | None]] = []

        async def ask(system, user, *, models=None):
            # `models` записывается тоже: без него нельзя отличить, какую
            # цепочку разбор взял, а от этого зависит, какая модель тратится
            calls.append((system, user, models))
            return answer

        monkeypatch.setattr(dictation.llm, "ask", ask)
        return calls

    return apply


@pytest.fixture(autouse=True)
def _chain(monkeypatch):
    monkeypatch.setattr(dictation.llm.settings, "llm_models", "groq:test")
    monkeypatch.setattr(dictation.llm.settings, "llm_answer_models", "")
    monkeypatch.setattr(dictation.llm.settings, "groq_key", "gsk-test")


async def _interpret(text="покормила 20 минут левой"):
    return await dictation.interpret(
        text, now_local=NOW, tz="Asia/Yekaterinburg", child_name="Аня", age="6 дней"
    )


@pytest.mark.asyncio
async def test_without_a_chain_the_model_is_not_called(model, monkeypatch):
    calls = model(None)
    monkeypatch.setattr(dictation.llm.settings, "llm_models", "")
    monkeypatch.setattr(dictation.llm.settings, "llm_answer_models", "")

    result = await _interpret()
    assert result.kind == dictation.FAILED
    assert result.reason == dictation.llm.NO_MODEL
    assert calls == []


@pytest.mark.asyncio
async def test_the_answer_chain_is_the_fallback_not_the_source(model, monkeypatch):
    """`.env`, заполненный под этап 6, не должен оставлять разбор мёртвым.

    `llm.answer_chain()` падает на `model_chain()`, но не наоборот. Поэтому
    разбор берёт `model_chain() or answer_chain()`: иначе заполненный
    `LLM_ANSWER_MODELS` без `LLM_MODELS` давал бы работающие ответы по базе
    знаний и разбор, отвечающий «нет модели» всем и всегда.
    """
    from bot.services import llm

    calls = model(llm.Answer(data={"intent": "unclear", "records": []}, calls=1))
    monkeypatch.setattr(dictation.llm.settings, "llm_models", "")
    monkeypatch.setattr(dictation.llm.settings, "llm_answer_models", "groq:test")

    result = await _interpret()
    assert result.kind != dictation.FAILED
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_parse_takes_the_general_chain_when_both_are_set(model, monkeypatch):
    """Заполнены обе цепочки — разбор берёт общую, а не медицинскую.

    Цена ошибки у них разная, и об этом сказано прямо в `config.py`: разобрать
    «покормили 20 минут» может кто угодно, а отвечать на вопрос о ребёнке —
    нет. Взяв для разбора цепочку ответов, бот тратил бы на разбор кормления
    ровно ту модель, которую держат для медицинских ответов, и выедал бы её
    квоту.

    Проверяется по `models`, ушедшим в `llm.ask`, а не по одному факту вызова:
    прежняя версия этого теста ставила только `LLM_ANSWER_MODELS`, и в такой
    настройке обе цепочки дают одно и то же — мутация с их перестановкой
    оставалась зелёной.
    """
    from bot.services import llm

    calls = model(llm.Answer(data={"intent": "unclear", "records": []}, calls=1))
    monkeypatch.setattr(dictation.llm.settings, "llm_models", "groq:general")
    monkeypatch.setattr(dictation.llm.settings, "llm_answer_models", "groq:careful")

    await _interpret()
    assert calls[0][2] == ["groq:general"]


@pytest.mark.asyncio
async def test_a_spent_quota_stops_before_the_model(model, monkeypatch):
    calls = model(None)
    monkeypatch.setattr(dictation.qa_log.settings, "llm_daily_limit", 1)
    qa_log.write(event="voice", calls=5)

    result = await _interpret()
    assert result.kind == dictation.FAILED
    assert result.reason == dictation.llm.QUOTA
    assert calls == []


@pytest.mark.asyncio
async def test_a_refusing_provider_names_its_reason(model):
    from bot.services import llm

    model(llm.Answer(reason=llm.UNAVAILABLE, detail="503 ...", calls=3))
    result = await _interpret()
    assert result.kind == dictation.FAILED
    assert result.reason == llm.UNAVAILABLE


@pytest.mark.asyncio
async def test_interpret_lets_no_exception_out(monkeypatch):
    """Упавший хендлер — это потерянный навсегда апдейт."""

    async def boom(*args, **kwargs):
        raise RuntimeError("модель взорвалась")

    monkeypatch.setattr(dictation.llm, "ask", boom)
    result = await _interpret()
    assert result.kind == dictation.FAILED
    assert result.reason == "broken"


@pytest.mark.asyncio
async def test_the_calls_are_counted_against_the_shared_quota(model):
    """Квота одна на разбор и на ответы: лимит считается на аккаунт.

    Проверяется рост счётчика, а не наличие строки в логе: строка есть и при
    `calls=0`, и тест остался бы зелёным с потерянным `calls=reply.calls`.
    """
    from bot.services import llm

    model(llm.Answer(data={"intent": "unclear", "records": []}, model="m", calls=4))
    before = qa_log.calls_today()
    await _interpret()
    assert qa_log.calls_today() == before + 4


# --- Карточка ---------------------------------------------------------------


def _record(**fields):
    base = {"kind": FEED_BREAST, "at": NOW}
    return dictation.Record(**{**base, **fields})


def test_the_card_shows_the_number_that_was_said():
    """Карточка — единственный сторож чисел на этом пути, и он обязан их показывать.

    Сверять число с подстрокой расшифровки нельзя: «двадцать минут» Whisper
    может оставить словами, и такой сторож отверг бы верный разбор. А человек,
    который это число только что произнёс, узнаёт его на экране мгновенно —
    но только если оно там есть.
    """
    # Расшифровка нарочно без цифр: иначе «120» в карточке нашлось бы в эхе, и
    # тест был бы удовлетворён сам собой. Мутационный прогон поймал именно это —
    # с выключенным рендером числа проверка оставалась зелёной
    said = "дала сто двадцать миллилитров"
    record = _record(kind=FEED_BOTTLE, value=120)

    line = texts.voice_line(record, NOW)
    assert "120" in line
    assert "мл" in line

    card = texts.voice_card((record,), NOW, said)
    assert "120" in card


def test_a_dose_is_labelled_and_the_name_comes_first():
    """«Лекарство в 02:05 — 1, витамин D» читалось задом наперёд.

    Найдено глазами на первой же собранной карточке, а не тестом: голое «1» в
    строке про лекарство означает что угодно, а название после числа
    заставляет читать строку дважды. Уточнение вида ушло в скобки — туда же,
    где стоит сторона груди.
    """
    line = texts.voice_line(_record(kind=MED, subtype="витамин D", value=1), NOW)
    assert line.index("витамин D") < line.index("1")
    assert "доза 1" in line

    # У прикорма то же место занимает вид еды, и подписи дозы там нет
    solid = texts.voice_line(_record(kind=FEED_SOLID, subtype="кабачок"), NOW)
    assert "(кабачок)" in solid
    assert "доза" not in solid


def test_a_subtype_survives_the_save():
    """Название лекарства обязано быть видно и после записи, и в выгрузке.

    До голосового ввода `subtype` не заполнялся ни одной кнопкой и не
    показывался нигде: ни в подтверждении, ни в CSV для врача. Стоило модели
    начать его писать — и получилась колонка, которую пишет LLM и не читает ни
    один экран. Это хуже неиспользуемой: «дала витамин D» ложилось бы в базу и
    исчезало навсегда.
    """
    from bot.db.models import Event

    event = Event(
        kind=MED,
        started_at=datetime(2026, 9, 14, 10, 0),
        subtype="витамин D",
        value_num=1,
    )
    line = texts.logged(event, "Asia/Yekaterinburg")
    assert "витамин D" in line
    assert "доза 1" in line


@pytest.mark.asyncio
async def test_the_export_carries_the_subtype_to_the_doctor(
    session, child, mother, family
):
    """Шапка выгрузки называет колонку, и `subtype` действительно в неё попадает.

    Обе половины нужны: одна шапка без строки не доказывает, что в колонку
    что-то пишется, а строка без шапки — что врач поймёт, что это за столбец.
    """
    import csv

    from bot.db import repo
    from bot.handlers import export

    at = datetime(2026, 9, 7, 10, 0)
    await repo.add_event(
        session, child.id, mother.id, MED, started_at=at, subtype="витамин D", value_num=1
    )
    events = await repo.events_between(
        session, child.id, at - timedelta(days=1), at + timedelta(days=1)
    )

    text = export._csv(child, family.tz, events, [])[3:].decode("utf-8")
    rows = list(csv.reader(text.splitlines(), delimiter=";"))
    assert "уточнение" in rows[0]
    assert rows[1][rows[0].index("уточнение")] == "витамин D"


def test_the_card_names_an_open_timer_in_words():
    """Человек — единственный, кто различает «уснула» и «поспала».

    Молчаливый вывод даёт четырёхчасовой сон с пометкой «приблизительно» в
    отчёте педиатру.
    """
    card = texts.voice_card((_record(kind=SLEEP),), NOW, "уснула")
    assert texts.VOICE_OPEN_TIMER in card

    closed = texts.voice_card((_record(kind=SLEEP, minutes=40),), NOW, "поспала 40")
    assert texts.VOICE_OPEN_TIMER not in closed


def test_the_card_admits_a_guessed_time():
    card = texts.voice_card((_record(guessed_time=True),), NOW, "покормила")
    assert texts.VOICE_TIME_GUESSED in card


def test_the_card_escapes_what_came_from_outside():
    """Сноски приказов пишут как «<1>», и для Telegram это незакрытый тег.

    Он не ругается внятно — он просто отказывается отправить сообщение, то есть
    ответ исчезает. Расшифровка и поля разбора приходят извне проекта, значит
    экранируются оба.
    """
    card = texts.voice_card(
        (_record(kind=NOTE, note="<b>жирным</b>"),), NOW, "сказала <1> и <2>"
    )
    assert "&lt;1&gt;" in card
    assert "&lt;b&gt;" in card
    assert "<b>жирным</b>" not in card


def test_the_echo_is_cut_before_it_is_escaped():
    """Обрезка после экранирования рвала бы entity пополам.

    «&» превращается в пять знаков, и срез по середине «&amp;» даёт Telegram
    «can't parse entities» — то есть потерю всего сообщения, а не хвоста.
    """
    echoed = texts.voice_heard("&" * 5000)
    # Каждая «&» в результате — начало целой entity, ни одной оборванной.
    # Обрезка после экранирования дала бы хвост вида «&am», и вот его-то
    # Telegram и не прощает
    assert echoed.count("&") == echoed.count("&amp;")
    assert echoed.count("&amp;") == texts.VOICE_ECHO_LIMIT


def test_a_long_echo_keeps_the_card_sendable():
    """У Telegram потолок 4096 знаков, и карточку нельзя обрезать после сборки.

    Обрезанную человек подтвердил бы, не увидев, что именно он подтверждает.
    """
    card = texts.voice_card((_record(),), NOW, "слово " * 2000)
    assert len(card) < 4096


def test_the_card_of_a_weight_speaks_grams():
    card = texts.voice_card((_record(kind=WEIGHT, value=3200),), NOW, "вес 3 200")
    assert texts.weight_str(3200) in card


def test_yesterday_is_named_in_the_card():
    """Сдвинутая на сутки запись обязана показать день, а не только часы.

    Иначе «в 03:40» выглядит как сегодняшнее, и человек подтвердит запись не в
    те сутки — а заметить это можно будет только по сводке.
    """
    yesterday = _record(at=NOW - timedelta(days=1))
    card = texts.voice_card((yesterday,), NOW, "покормила в три сорок")
    assert "вчера" in card


def test_to_grams_is_the_only_gate_for_a_weight():
    """Разбор вес не проверяет намеренно: у него один вход в базу.

    Вторая проверка рядом разошлась бы с `to_grams` молча, а тот заодно решает
    «килограммы это или граммы».
    """
    assert measure.to_grams(3.2) == 3200
    assert measure.to_grams(3200) == 3200
    assert measure.to_grams(300) is None
    _, records = dictation.normalize(
        {"intent": "record", "records": [{"kind": WEIGHT, "value": 300}]}, NOW
    )
    assert records[0].value == 300


# --- Хендлер: прогон через настоящий Dispatcher -----------------------------
#
# Смысл раздела — то, что живёт не в коде одного файла, а в стыках: граница
# безопасности на голосовом канале, порядок «триаж раньше карточки ребёнка» и
# то, что голосовое вообще доезжает до своего роутера. Последнее не
# формальность: написанный, но не зарегистрированный код — подпись этого
# проекта, так уже было с `db/sqlite.py`, `kb.close` и `drop_wizard_state`.

from typing import Any  # noqa: E402

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.methods import GetMe, TelegramMethod  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery,
    Chat,
    Message,
    Update,
    User,
    Voice,
)

from bot import handlers, keyboards  # noqa: E402
from bot.db import repo  # noqa: E402
from bot.handlers import voice as handler  # noqa: E402
from bot.services import llm, pending  # noqa: E402

CHAT = Chat(id=-1001, type="supergroup", title="Малышка")
PRIVATE = Chat(id=222, type="private", first_name="Аня")
AUTHOR = User(id=222, is_bot=False, first_name="Аня")
# Имя бота нужно ссылке «добавить в группу» в гиде для лички
ME = User(id=42, is_bot=True, first_name="Бот", username="BabyBot")


class RecordingSession(BaseSession):
    """Сеть не трогаем: складываем отправленное в список."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []
        self.methods: list[str] = []
        self._next_id = 500

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Any, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if isinstance(method, GetMe):
            # Гид в личке собирает ссылку из имени бота
            return ME
        payload = method.model_dump(exclude_none=True)
        self.methods.append(type(method).__name__)
        text = payload.get("text")
        if text:
            self.sent.append(text)
        self._next_id += 1
        return Message(
            message_id=self._next_id,
            date=NOW,
            chat=CHAT,
            from_user=AUTHOR,
            text=text or "",
        )

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    @property
    def blob(self) -> str:
        return "\n".join(self.sent)


@pytest.fixture
def wired(session, family, child, mother, monkeypatch):
    """Диспетчер с настоящими роутерами в настоящем порядке.

    Боевой `handlers.routers` целиком, а не своя тройка: тест, собирающий свой
    порядок, доказывает работу механизма и молчит о том, каков порядок на самом
    деле. Проверено на этапе 6 переставлением роутеров — тест оставался зелёным.
    """
    talk = RecordingSession()
    bot = Bot(token="42:TEST", session=talk)
    dp = Dispatcher()

    state = {"child": child}

    async def inject(event, data):
        data.update(
            session=session, family=family, child=state["child"], member=mother
        )
        return None

    async def middleware(handler_fn, event, data):
        await inject(event, data)
        return await handler_fn(event, data)

    dp.update.outer_middleware(middleware)

    order = tuple(handlers.routers)
    for router in order:
        dp.include_router(router)

    heard: list[bytes] = []
    downloads: list[Any] = []

    async def download(media, destination=None, **kwargs):
        downloads.append(media)
        if destination is not None:
            destination.write(b"OggS-fake")
        return destination

    monkeypatch.setattr(bot, "download", download)

    stand = _Stand(dp, bot, talk, state, heard, downloads, monkeypatch)
    yield stand

    handler._drafts.clear()
    handler._told_off.clear()
    # Роутеры — синглтоны уровня модуля, и aiogram запоминает, к какому
    # диспетчеру их подключили. Без отвязки второй тест в файле падает на
    # «Router is already attached»
    for router in order:
        router._parent_router = None


class _Stand:
    """Всё, что нужно тесту, одним объектом — иначе распаковка на семь имён."""

    def __init__(self, dp, bot, talk, state, heard, downloads, monkeypatch):
        self.dp = dp
        self.bot = bot
        self.talk = talk
        self._state = state
        self.heard = heard
        self.downloads = downloads
        self._mp = monkeypatch
        self._update_id = 0
        self.parsed: list[str] = []
        self.asked: list[str] = []

    # --- подготовка ---------------------------------------------------------

    def no_child(self) -> None:
        self._state["child"] = None

    def transcribes(self, text: str | None) -> None:
        async def transcribe(audio, filename="voice.ogg"):
            self.heard.append(audio)
            return text

        self._mp.setattr(handler.stt, "transcribe", transcribe)

    def download_fails(self) -> None:
        async def boom(media, destination=None, **kwargs):
            raise RuntimeError("Telegram не отдал файл")

        self._mp.setattr(self.bot, "download", boom)

    def parses(self, result: dictation.Result) -> None:
        async def interpret(text, **kwargs):
            self.parsed.append(text)
            return result

        self._mp.setattr(handler.dictation, "interpret", interpret)

    def model_is_a_bomb(self) -> None:
        """Любое обращение к модели — провал теста, а не тихий пропуск."""

        async def forbidden(*args, **kwargs):
            raise AssertionError("до модели дойти было нельзя")

        self._mp.setattr(handler.dictation.llm, "ask", forbidden)

    def answers_questions(self) -> None:
        from bot.services import qa as qa_service

        async def answer(question):
            self.asked.append(question)
            return qa_service.Result(qa_service.NOT_FOUND, "", ())

        self._mp.setattr(handler.qa.qa, "answer", answer)

    # --- подача апдейтов ----------------------------------------------------

    async def speak(self, duration: int = 5, *, private: bool = False) -> None:
        self._update_id += 1
        message = Message(
            message_id=self._update_id,
            date=NOW,
            chat=PRIVATE if private else CHAT,
            from_user=AUTHOR,
            voice=Voice(
                file_id="f-1", file_unique_id="u-1", duration=duration, mime_type="audio/ogg"
            ),
        )
        await self.dp.feed_update(
            self.bot, Update(update_id=self._update_id, message=message)
        )

    async def write(self, text: str) -> None:
        self._update_id += 1
        message = Message(
            message_id=self._update_id,
            date=NOW,
            chat=CHAT,
            from_user=AUTHOR,
            text=text,
        )
        await self.dp.feed_update(
            self.bot, Update(update_id=self._update_id, message=message)
        )

    async def tap(self, action: str, card_id: int | None = None) -> None:
        self._update_id += 1
        target = card_id if card_id is not None else self.card_id
        call = CallbackQuery(
            id=f"t-{self._update_id}",
            from_user=AUTHOR,
            chat_instance="x",
            data=keyboards.VoiceCB(action=action).pack(),
            message=Message(
                message_id=target,
                date=NOW,
                chat=CHAT,
                from_user=AUTHOR,
                text="(карточка)",
            ),
        )
        await self.dp.feed_update(
            self.bot, Update(update_id=self._update_id, callback_query=call)
        )

    # --- чтение результата --------------------------------------------------

    @property
    def card_id(self) -> int:
        keys = [key for key in handler._drafts if key[0] == CHAT.id]
        assert keys, "карточки нет — черновик не сохранён"
        return keys[-1][1]


def _result(*records, kind=dictation.RECORD, reason=""):
    return dictation.Result(kind, tuple(records), reason)


def _feed_record(**fields):
    base = {"kind": FEED_BREAST, "at": NOW, "minutes": 20, "side": LEFT}
    return dictation.Record(**{**base, **fields})


# --- голосовое вообще доезжает ----------------------------------------------


@pytest.mark.asyncio
async def test_a_voice_reaches_its_router(wired):
    """Самый простой и самый важный тест раздела.

    Роутер, написанный и не зарегистрированный в `handlers/__init__.py`, — это
    подпись проекта: так было с `db/sqlite.py`, с `kb.close` и с
    `drop_wizard_state`. Голосовое при этом не падает и не ругается, оно просто
    проваливается мимо всех роутеров, и бот выглядит полностью исправным.
    """
    wired.transcribes("покормила 20 минут левой")
    wired.parses(_result(_feed_record()))

    await wired.speak()

    assert wired.parsed == ["покормила 20 минут левой"]
    assert "Записать?" in wired.talk.blob


@pytest.mark.asyncio
async def test_a_voice_in_private_gets_the_guide(wired):
    """В личке работает `common`, и он стоит первым — до голоса дело не доходит.

    Отказ стал гидом, но свойство осталось тем же и проверяется тем же: голос
    в личке не разбирается вовсе, то есть ни одна реплика из лички не уходит в
    чужой API.
    """
    wired.transcribes("покормила 20 минут левой")
    wired.parses(_result(_feed_record()))

    await wired.speak(private=True)

    assert texts.PRIVATE_GUIDE in wired.talk.blob
    assert wired.parsed == []


@pytest.mark.asyncio
async def test_a_voice_mid_wizard_does_not_become_the_baby_name(wired):
    """Мастер `/baby` голосовое не перехватывает.

    Сторожит фильтры `F.text` у шагов мастера, а НЕ порядок роутеров: на
    `F.voice` не претендует никто, и роутер голоса можно переставить куда угодно
    ниже `common` без изменения поведения. Написано прямо, чтобы тест не
    выглядел проверкой того, чего не проверяет.
    """
    wired.transcribes("подгузник мокрый")
    wired.parses(_result(_feed_record(kind=DIAPER_WET, minutes=None, side=None)))

    key = FSMContext.__name__  # только чтобы импорт не выглядел лишним
    assert key

    await wired.write("/baby")
    await wired.speak()

    assert "Записать?" in wired.talk.blob


# --- граница безопасности ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_red_flag_said_aloud_never_reaches_the_model(wired):
    """Главный тест этапа.

    `alarm` висит на `F.text | F.caption`, и голосового он не видит вовсе —
    подписей к голосовым Telegram не даёт. Значит порядок роутеров тут не
    защищает ничего, и триаж обязан отработать внутри хендлера, на расшифровке.
    Модель подменена бомбой: «до модели дойти было нельзя» — это не стиль, это
    условие.
    """
    wired.transcribes("у неё судороги, что делать")
    wired.model_is_a_bomb()

    await wired.speak()

    assert "103" in wired.talk.blob
    assert handler._drafts == {}


@pytest.mark.asyncio
async def test_the_voice_and_the_text_paths_answer_the_same_words(wired):
    """Ответ обязан совпадать побайтово — иначе это две копии границы.

    Копия расходится с оригиналом молча, и разойдётся она именно здесь: в
    формулировке, которую читают в три часа ночи.
    """
    said = "у неё судороги"
    wired.transcribes(said)
    wired.model_is_a_bomb()

    await wired.speak()
    by_voice = list(wired.talk.sent)

    wired.talk.sent.clear()
    await wired.write(said)
    by_text = list(wired.talk.sent)

    assert by_voice == by_text
    assert by_voice, "оба пути промолчали — тест был бы тавтологией"


@pytest.mark.asyncio
async def test_a_red_flag_works_without_a_child_card(wired):
    """Молчать из-за незаполненной анкеты нельзя.

    Порядок проверок в этом хендлере обратный остальному проекту: триаж раньше
    `child is None`. Проверка карточки в начале отняла бы триаж у семьи, у
    которой ребёнку несколько часов, — то есть у той, которой он нужнее всего.
    """
    wired.no_child()
    wired.transcribes("у неё судороги")
    wired.model_is_a_bomb()

    await wired.speak()

    assert "103" in wired.talk.blob
    assert texts.NO_CHILD not in wired.talk.blob


@pytest.mark.asyncio
async def test_a_red_flag_drops_the_waiting_question(wired):
    """Ответили мы — ждать вопроса больше нечего.

    Иначе ожидание висит ещё десять минут, и следующая реплика человека
    («звоню 103», «одеваемся, едем») уедет в поиск по базе знаний ровно в ту
    минуту, когда бот обязан молчать.
    """
    pending.remember(CHAT.id, AUTHOR.id, pending.QUESTION)
    wired.transcribes("у неё судороги")
    wired.model_is_a_bomb()

    await wired.speak()

    assert pending.peek(CHAT.id, AUTHOR.id) is None


def test_the_triage_uses_the_corrected_age():
    """Порог 38.0 у недоношенного держится дольше, и это единственная сторона.

    Тест зовёт `alarm.assess` по-настоящему — до выноса этой функции проверка
    выбора возраста была обещанием в докстроке `test_growth`, и подмена
    скорректированного возраста на паспортный оставляла весь прогон зелёным.

    Вторая половина обязательна: без неё тест был бы тавтологией — он доказывал
    бы только то, что у пары детей разные даты рождения.
    """
    from types import SimpleNamespace

    from bot.handlers import alarm
    from bot.services import growth

    born = datetime(2026, 9, 1, 4, 20)
    at = born + timedelta(days=95)
    preterm = SimpleNamespace(born_at=born, gestation_weeks=34)
    term = SimpleNamespace(born_at=born, gestation_weeks=40)

    limit = rf.THRESHOLDS["fever_age_days"]
    assert growth.age_days(preterm, at) > limit, "по паспорту окно уже закрыто"
    assert growth.corrected_age_days(preterm, at) <= limit, "по сроку — ещё открыто"

    said = "температура 38.2"
    hits, level = alarm.assess(said, preterm, at)
    assert level == "emergency", "у недоношенного порог 38.0 обязан работать"
    assert [hit.flag.id for hit in hits] == ["fever_young"]

    # У доношенного того же возраста окно уже закрыто — значит тест смотрит
    # именно на выбор возраста, а не на что-нибудь ещё
    _, term_level = alarm.assess(said, term, at)
    assert term_level != "emergency"


def test_the_filter_stays_quiet_on_ordinary_talk():
    """Фильтр, вернувший словарь на пустой список, съел бы весь чат.

    Тот же механизм, что у ожидания вопроса в `qa.py`, и тот же способ им всё
    сломать: aiogram считает сошедшимся любой фильтр, вернувший не-ложь.
    """
    from bot.handlers import alarm

    hits, level = alarm.assess("купи хлеба по дороге", None)
    assert (hits, level) == ([], "")

    # И команды не разбираем: их забирают свои роутеры
    assert alarm.assess("/weight 38.2", None) == ([], "")


# --- отказы до сети и на сети ------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_key_nothing_is_downloaded(wired, monkeypatch):
    """Ключ спрашивается ДО скачивания.

    `transcribe` отказывает без запроса к провайдеру, но файл к тому моменту
    уже скачан. Голосовые в семейном чате идут постоянно, и живой `getFile` с
    загрузкой через прокси на каждую реплику — при выключенном голосе — это
    трафик ради выброшенных байтов.
    """
    monkeypatch.setattr(handler.stt.settings, "stt_key", "")

    await wired.speak()

    assert wired.downloads == []
    assert texts.VOICE_OFF in wired.talk.blob


@pytest.mark.asyncio
async def test_the_refusal_is_said_once_per_chat(wired, monkeypatch):
    """Отвечать на каждую реплику «нет ключа» хуже, чем молчать.

    Но и молчать нельзя совсем: голос — вход без команды и без кнопки, то есть
    необнаружимый, а этот проект дважды пострадал от «бот молчит и выглядит
    исправным».
    """
    monkeypatch.setattr(handler.stt.settings, "stt_key", "")

    await wired.speak()
    first = len(wired.talk.sent)
    await wired.speak()

    assert len(wired.talk.sent) == first


@pytest.mark.asyncio
async def test_a_long_record_is_refused_before_any_call(wired, monkeypatch):
    """Длинную запись не за что качать — отказ до скачивания и до сети."""
    monkeypatch.setattr(handler.settings, "voice_max_seconds", 30)
    wired.transcribes("это не должно прозвучать")

    await wired.speak(duration=120)

    assert wired.downloads == []
    assert wired.heard == []
    assert "30" in wired.talk.blob


@pytest.mark.asyncio
async def test_a_failed_download_never_reaches_the_endpoint(wired):
    wired.download_fails()
    wired.transcribes("это не должно прозвучать")

    await wired.speak()

    assert wired.heard == []
    assert texts.VOICE_FAILED in wired.talk.blob


@pytest.mark.asyncio
async def test_a_failed_transcription_says_so(wired):
    wired.transcribes(None)
    wired.parses(_result(_feed_record()))

    await wired.speak()

    assert texts.VOICE_FAILED in wired.talk.blob
    assert wired.parsed == []


@pytest.mark.asyncio
async def test_numerals_are_converted_before_the_parse(wired):
    """В разбор уходит тот же текст, что человек увидел в эхе.

    Один текст на весь путь — требование `morph`: нормализация одна на проект.
    Две версии одной фразы разошлись бы, и ошибку конвертера стало бы не видно.
    """
    wired.transcribes("покормила двадцать минут левой")
    wired.parses(_result(_feed_record()))

    await wired.speak()

    assert wired.parsed == ["покормила 20 минут левой"]
    assert "покормила 20 минут левой" in wired.talk.blob


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (llm.NO_MODEL, texts.VOICE_NO_MODEL),
        (llm.QUOTA, texts.VOICE_QUOTA),
        (llm.UNAVAILABLE, texts.VOICE_NOT_PARSED),
    ],
)
async def test_each_failure_says_what_happened(wired, reason, expected):
    """Три текста, а не один.

    `llm.py` существует в нынешнем виде именно потому, что двенадцать разных
    отказов однажды слиплись в один `None`, и понять, сломан ли бот, было
    нельзя.
    """
    wired.transcribes("покормила 20 минут левой")
    wired.parses(_result(kind=dictation.FAILED, reason=reason))

    await wired.speak()

    assert expected in wired.talk.blob
    # И расшифровка всё равно показана: услышанное не пропадает
    assert "покормила 20 минут левой" in wired.talk.blob


def test_the_three_failures_are_different_texts():
    """Иначе параметризованный тест выше прошёл бы на одной строке трижды."""
    three = {texts.VOICE_NO_MODEL, texts.VOICE_QUOTA, texts.VOICE_NOT_PARSED}
    assert len(three) == 3


@pytest.mark.asyncio
async def test_an_unclear_phrase_is_not_a_card(wired):
    wired.transcribes("да ну не знаю даже")
    wired.parses(_result(kind=dictation.UNCLEAR))

    await wired.speak()

    assert texts.VOICE_UNCLEAR in wired.talk.blob
    assert handler._drafts == {}


@pytest.mark.asyncio
async def test_without_a_child_card_there_is_no_parse(wired):
    """Разбирать нечего: запись некуда положить, а карточку — не от чего считать."""
    wired.no_child()
    wired.transcribes("покормила 20 минут левой")
    wired.parses(_result(_feed_record()))

    await wired.speak()

    assert texts.NO_CHILD in wired.talk.blob
    assert wired.parsed == []


# --- вопрос голосом ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_question_said_aloud_goes_to_the_knowledge_base(wired):
    """В поиск уходит тот же текст, что и в разбор, — с числами цифрами.

    Это следствие правила «одна нормализация на путь», и здесь оно кстати: в
    клинических рекомендациях и приказах числа написаны цифрами, а не словами,
    так что «в 2 месяца» ищется лучше, чем «в два месяца». Записано тестом
    затем, чтобы разница не выглядела потом случайной.
    """
    wired.transcribes("сколько должен спать ребёнок в два месяца")
    wired.parses(_result(kind=dictation.QUESTION))
    wired.answers_questions()

    await wired.speak()

    assert wired.asked == ["сколько должен спать ребёнок в 2 месяца"]
    assert texts.QA_NOT_FOUND in wired.talk.blob


@pytest.mark.asyncio
async def test_both_question_paths_share_one_renderer(wired, monkeypatch):
    """Четыре ветки по `result.kind` не должны существовать в двух копиях.

    Проверяется вызовом, а не текстом: тест на текст остался бы зелёным против
    второй, независимо переписанной копии — до того дня, когда одна из них
    начнёт путать «не нашёл» с «базы нет».
    """
    seen: list[str] = []

    async def renderer(message, question):
        seen.append(question)

    monkeypatch.setattr(handler.qa, "answer_and_reply", renderer)

    wired.transcribes("а пустышка это нормально")
    wired.parses(_result(kind=dictation.QUESTION))
    await wired.speak()

    pending.remember(CHAT.id, AUTHOR.id, pending.QUESTION)
    await wired.write("а пустышка это нормально")

    assert seen == ["а пустышка это нормально"] * 2


# --- карточка и запись -------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_is_saved_before_the_tap(wired, session, child):
    """Инвариант проекта: между разбором и базой всегда стоит человек с кнопкой."""
    wired.transcribes("покормила 20 минут левой")
    wired.parses(_result(_feed_record()))

    await wired.speak()

    assert await repo.last_event(session, child.id, {FEED_BREAST}) is None


@pytest.mark.asyncio
async def test_the_draft_is_keyed_by_the_card_that_was_sent(wired):
    """Ключ черновика — message_id настоящей отправленной карточки.

    Разойдись они — и тап не найдёт черновика никогда: фича мертва на сто
    процентов, а прогон зелёный, потому что карточка-то отправляется.
    """
    wired.transcribes("покормила 20 минут левой")
    wired.parses(_result(_feed_record()))

    await wired.speak()

    # Последнее, что отправил бот, — карточка; её id и обязан быть ключом
    assert wired.card_id == wired.talk._next_id


@pytest.mark.asyncio
async def test_a_tap_writes_the_event_with_its_fields(wired, session, child, family):
    from bot.services import timeutil as tu

    wired.transcribes("покормила 20 минут левой в 3 40")
    at = datetime(2026, 9, 13, 3, 40)
    wired.parses(_result(_feed_record(at=at)))

    await wired.speak()
    await wired.tap("save")

    event = await repo.last_event(session, child.id, {FEED_BREAST})
    assert event is not None
    assert event.side == LEFT
    assert event.started_at == tu.to_utc(at, family.tz)
    # Конец считает код, а не модель: `minutes` плюс начало
    assert event.ended_at == event.started_at + timedelta(minutes=20)
    assert not event.estimated
    # Ссылка ведёт на голосовое, а не на карточку
    assert event.source_message_id == 1


@pytest.mark.asyncio
async def test_a_second_tap_saves_nothing_more(wired, session, child):
    """Двое могут нажать «Записать» на одной карточке."""
    wired.transcribes("подгузник мокрый")
    wired.parses(_result(_feed_record(kind=DIAPER_WET, minutes=None, side=None)))

    await wired.speak()
    card = wired.card_id
    await wired.tap("save", card)
    await wired.tap("save", card)

    rows = await repo.count_events_since(
        session, child.id, datetime(2000, 1, 1), {DIAPER_WET}
    )
    assert rows == 1


@pytest.mark.asyncio
async def test_a_stale_card_says_so(wired):
    """Черновики не переживают перезапуск, и тишина в ответ на тап — хуже всего."""
    handler._drafts.clear()
    await wired.tap("save", 12345)
    assert "AnswerCallbackQuery" in wired.talk.methods


@pytest.mark.asyncio
async def test_saying_no_writes_nothing(wired, session, child):
    wired.transcribes("подгузник мокрый")
    wired.parses(_result(_feed_record(kind=DIAPER_WET, minutes=None, side=None)))

    await wired.speak()
    await wired.tap("drop")

    assert await repo.last_event(session, child.id, {DIAPER_WET}) is None
    assert texts.VOICE_DROPPED in wired.talk.blob


@pytest.mark.asyncio
async def test_a_dictated_timer_closes_the_running_one(wired, session, child, mother):
    """«Уснула» поверх идущего кормления закрывает его, как и кнопка.

    И говорит об этом вслух: у кнопки панель меняется под пальцем, здесь
    перерисовка отложена дебаунсом, а сказали всего одно слово.
    """
    from bot.services import timeutil as tu

    earlier = tu.now_utc() - timedelta(hours=1)
    running = await repo.start_event(
        session, child.id, mother.id, FEED_BREAST, started_at=earlier
    )

    wired.transcribes("уснула")
    wired.parses(_result(_feed_record(kind=SLEEP, minutes=None, side=None)))

    await wired.speak()
    await wired.tap("save")

    await session.refresh(running)
    assert running.ended_at is not None
    assert not running.estimated
    # Ровно один идущий таймер, и это сон
    assert (await repo.any_open_event(session, child.id)).kind == SLEEP
    assert "Закрыл предыдущее" in wired.talk.blob


@pytest.mark.asyncio
async def test_a_dictated_weight_goes_to_measurements(wired, session, child):
    """Вес живёт в `measurements`, а не в `events`, и считает центили."""
    wired.transcribes("вес 3 200")
    wired.parses(_result(_feed_record(kind=WEIGHT, minutes=None, side=None, value=3200)))

    await wired.speak()
    await wired.tap("save")

    last = await repo.last_measurement(session, child.id, WEIGHT)
    assert last is not None
    assert last.value == 3200
    assert await repo.last_event(session, child.id, {WEIGHT}) is None


@pytest.mark.asyncio
async def test_two_records_from_one_phrase_are_both_written(wired, session, child):
    wired.transcribes("покормила 20 минут левой и подгузник мокрый")
    wired.parses(
        _result(
            _feed_record(),
            _feed_record(kind=DIAPER_WET, minutes=None, side=None),
        )
    )

    await wired.speak()
    await wired.tap("save")

    assert await repo.last_event(session, child.id, {FEED_BREAST}) is not None
    assert await repo.last_event(session, child.id, {DIAPER_WET}) is not None


@pytest.mark.asyncio
async def test_old_drafts_are_evicted(wired):
    wired.transcribes("подгузник мокрый")
    wired.parses(_result(_feed_record(kind=DIAPER_WET, minutes=None, side=None)))

    for _ in range(handler.MAX_DRAFTS + 3):
        await wired.speak()

    assert len(handler._drafts) <= handler.MAX_DRAFTS


@pytest.mark.asyncio
async def test_a_bare_number_still_reaches_measure(wired, session, child):
    """Регресс, который уже ловился на этапе 6.

    Голос не должен встать на пути голого числа: ввод веса числом — отдельный
    механизм, и сломался бы он молча.
    """
    wired.transcribes("не должно прозвучать")
    pending.remember(CHAT.id, AUTHOR.id, pending.WEIGHT_G)

    await wired.write("3560")

    assert await repo.last_measurement(session, child.id, WEIGHT) is not None
    assert wired.heard == []


def test_the_order_of_routers_still_holds():
    """Границы, заданные не кодом, а этим списком.

    Про место `voice` тут ничего не проверяется намеренно: оно свободно, и
    утверждение «voice выше qa» было бы тавтологией в одежде инварианта.
    """
    order = [router.name for router in handlers.routers]
    assert order[0] == "common"
    assert order.index("alarm") < order.index("qa") < order.index("measure")
    assert order[-1] == "measure"
    assert "voice" in order
