"""Триаж тревожных признаков.

Файл, в котором цена ошибки выше всего в проекте. Проверяется три вещи:

* каждый признак из `redflags.yaml` вообще срабатывает — иначе запись в файле
  есть, а защиты нет, и это худший из возможных исходов;
* обычная переписка семьи триаж не поднимает — ложные тревоги приводят к тому,
  что настоящую пролистают;
* экстренный ответ собирается без единого обращения к модели.
"""

from datetime import timedelta

import pytest

from bot import texts
from bot.services import redflags as rf

YOUNG = 20  # дней, младше трёх месяцев
OLD = 200


# --- Содержание файла -----------------------------------------------------


def test_every_flag_fires_on_its_examples():
    """Каждый признак поднимается на целых сообщениях из `redflags.yaml`.

    Единственная проверка достижимости, у которой есть зубы. Соблазнительный
    вариант — искать каждую фразу в ней же самой — тавтологичен: подстрока
    всегда находится в самой себе, и такой тест проходит даже с опечаткой
    посреди слова. Пример же ловит и опечатку, и сломавшееся подавление
    отрицаний, и съехавшую нормализацию «ё».
    """
    for flag in rf.FLAGS:
        assert flag.examples, f"у признака {flag.id} нет ни одного примера"
        for example in flag.examples:
            hits = rf.triage(example, YOUNG)
            assert any(hit.flag.id == flag.id for hit in hits), (
                f"признак {flag.id} не поднялся на примере {example!r}"
            )


def test_phrases_are_well_formed():
    """Фразы уже нормализованы и не содержат мусора по краям.

    Поиск идёт подстрокой по нормализованному тексту, поэтому фраза в верхнем
    регистре, с «ё» или с висящим пробелом не найдётся никогда — и признак
    молча перестанет работать.
    """
    for flag in rf.FLAGS:
        assert flag.phrases, f"у признака {flag.id} нет фраз"
        for phrase in flag.phrases:
            assert phrase == phrase.strip(), f"{flag.id}: пробел по краям {phrase!r}"
            assert phrase == rf.normalize(phrase), f"{flag.id}: не нормализована {phrase!r}"
            assert "  " not in phrase, f"{flag.id}: двойной пробел в {phrase!r}"
            assert len(phrase) >= 4, f"{flag.id}: слишком короткая фраза {phrase!r}"


def test_every_flag_has_action_and_source():
    """Ответ без указания, что делать, бесполезен; без источника — непроверяем."""
    for flag in rf.FLAGS:
        assert flag.level in (rf.EMERGENCY, rf.URGENT)
        assert len(flag.action) > 40
        assert flag.source
        assert flag.title


def test_thresholds_are_the_documented_ones():
    """Пороги — из клинических рекомендаций, а не подобранные на глаз.

    38.0 у младше трёх месяцев — консенсус AAP, NHS и NICE и самый важный
    порог во всём боте. Если он поедет, это должно сломать тест, а не тихо
    изменить поведение.
    """
    assert rf.THRESHOLDS["fever_c"] == 38.0
    assert rf.THRESHOLDS["fever_c_watch"] == 37.5
    assert rf.THRESHOLDS["fever_age_days"] == 90
    assert rf.THRESHOLDS["hypothermia_c"] == 36.0
    assert rf.THRESHOLDS["min_wet_diapers"] == 4
    assert rf.THRESHOLDS["weight_loss_percent"] == 10


# --- Температура ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("температура 38", 38.0),
        ("у неё 38.2", 38.2),
        ("38,6 намерили", 38.6),
        ("температура 37 и 2", 37.0),
        ("35.4 градуса", 35.4),
        ("темпа 39", 39.0),
    ],
)
def test_temperature_is_found(text, expected):
    assert rf.find_temperature(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    [
        "дала 38 мл из бутылочки",  # целое без слова о температуре — это мл
        "покормили 20 минут",
        "купи 2 пачки",
        "вес 3560",
        "температуры нет",
        "50 мл сцедила",
    ],
)
def test_not_a_temperature(text):
    """Целое число без слова о температуре в этом боте значит миллилитры.

    Самая вероятная ложная тревога: бот сам спрашивает «сколько мл?», и ответ
    «38» не должен вызывать скорую.
    """
    assert rf.find_temperature(text) is None


@pytest.mark.parametrize(
    ("value", "age", "level"),
    [
        (38.0, YOUNG, rf.EMERGENCY),  # ровно порог — уже тревога
        (38.2, YOUNG, rf.EMERGENCY),
        (37.9, YOUNG, rf.URGENT),  # российский порог 37.5 — связаться с врачом
        (37.4, YOUNG, None),
        (36.0, YOUNG, rf.EMERGENCY),  # гипотермия — такой же признак сепсиса
        (35.2, YOUNG, rf.EMERGENCY),
        (36.6, YOUNG, None),
        (38.2, OLD, None),  # после трёх месяцев порог выше
        (38.5, OLD, rf.URGENT),
        (39.0, OLD, rf.URGENT),
        (35.0, OLD, None),  # гипотермия — признак именно у младенца
    ],
)
def test_temperature_thresholds(value, age, level):
    hit = rf.temperature_hit(value, age)
    assert (hit.flag.level if hit else None) == level


def test_temperature_without_known_age_still_fires():
    """Карточка ребёнка не заполнена — молчать всё равно нельзя.

    Возраст неизвестен, значит возрастные пороги применить не к чему; но
    высокая температура остаётся поводом сказать хоть что-то.
    """
    assert rf.temperature_hit(39.5, None) is not None


# --- Отрицание ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "пожелтела? нет",
        "нет, не пожелтела",
        "рвота фонтаном? нет такого",
        "судороги? нет",
    ],
)
def test_negation_suppresses(text):
    """Отрицание рядом с признаком отменяет его.

    Тесты здесь подобраны так, чтобы фраза **действительно совпадала**, и
    подавление было единственной причиной молчания. Соблазнительные примеры
    вроде «судорог не было» проверяют не то: слова «судороги» в них просто нет,
    и они прошли бы даже с полностью выключенным разбором отрицаний.
    """
    matched = any(
        phrase in rf.normalize(text) for flag in rf.FLAGS for phrase in flag.phrases
    )
    assert matched, f"тест бессмысленный: фраза в {text!r} и так не совпадает"
    assert rf.triage(text, YOUNG) == []


@pytest.mark.parametrize(
    "text",
    [
        "температуры нет, спит спокойно",
        "судорог не было",
        "сыпи нет",
    ],
)
def test_calm_reassurance_is_quiet(text):
    """Фразы, которыми родители успокаивают друг друга, тревогу не поднимают."""
    assert rf.triage(text, YOUNG) == []


def test_negation_window_does_not_over_reach():
    """Отрицание из соседней части фразы признак не отменяет.

    «Запрокидывает голову назад когда не спит» — это сам признак, и «не»
    относится ко сну. При окне в три слова он подавлялся; тест на примерах из
    `redflags.yaml` это и поймал.
    """
    hits = rf.triage("запрокидывает голову назад когда не спит", YOUNG)
    assert [hit.flag.id for hit in hits] == ["head_tilt"]


def test_negation_does_not_reach_across_the_sentence():
    """«Температуры нет, но сыпь не бледнеет» — сыпь обязана сработать."""
    hits = rf.triage("температуры нет, но сыпь не бледнеет", YOUNG)
    assert [hit.flag.id for hit in hits] == ["rash"]


# --- Ложные тревоги -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "покормили 20 минут левой, уснула",
        "купи подгузники и смесь",
        "во сколько завтра к врачу?",
        "она сегодня так хорошо спала",
        "я сам не высыпаюсь совсем",
        "бутылочка горячая, подожди",
        "поставь чайник",
        "смотри как улыбается",
    ],
)
def test_ordinary_chat_is_quiet(text):
    """Обычная переписка триаж не поднимает.

    Это не про удобство. Ложные тревоги приводят к тому, что настоящую
    пролистают не читая, — и защита перестаёт работать вообще.
    """
    assert rf.triage(text, YOUNG) == []


def test_case_and_yo_do_not_matter():
    """«Ё» пишут как придётся, и триаж не должен на этом ломаться."""
    assert rf.triage("НЕ ПРОСЫПАЕТСЯ!!!", YOUNG)
    assert rf.triage("рвет фонтаном", YOUNG)
    assert rf.triage("рвёт фонтаном", YOUNG)


def test_one_flag_fires_once():
    """Две фразы одного признака дают одно срабатывание, а не два."""
    hits = rf.triage("посинела, синие губы", YOUNG)
    assert [hit.flag.id for hit in hits] == ["colour"]


def test_several_flags_are_all_reported():
    hits = rf.triage("тяжело дышит и посинела", YOUNG)
    assert {hit.flag.id for hit in hits} == {"breathing", "colour"}
    assert rf.worst(hits) == rf.EMERGENCY


def test_emergency_beats_urgent():
    """Уровень ответа — по самому тяжёлому из найденного."""
    hits = rf.triage("пожелтела и не могу разбудить", YOUNG)
    assert rf.worst(hits) == rf.EMERGENCY


# --- Текст ответа ---------------------------------------------------------


def test_alert_text_is_fixed_and_complete():
    """Ответ собирается из файла и содержит всё, что нужно ночью."""
    hits = rf.triage("у неё 38.4", YOUNG)
    text = texts.triage_alert(hits, rf.worst(hits))

    assert "103" in text
    assert "112" in text
    assert "38.4" in text
    assert "не врач" in text
    assert "Источники:" in text


def test_alert_text_is_deterministic():
    """Одни и те же слова в третьем часу ночи дают один и тот же ответ."""
    first = texts.triage_alert(rf.triage("судороги", YOUNG), rf.EMERGENCY)
    second = texts.triage_alert(rf.triage("судороги", YOUNG), rf.EMERGENCY)
    assert first == second


def test_urgent_alert_does_not_lead_with_the_ambulance():
    """У «свяжитесь с врачом сегодня» телефон скорой не на первой строке.

    Иначе два уровня тревоги перестают различаться, и экстренный ответ теряет
    свою силу.
    """
    hits = rf.triage("пожелтела", YOUNG)
    text = texts.triage_alert(hits, rf.URGENT)
    assert not text.splitlines()[2].startswith("☎️")
    assert "звоните 103, не дожидаясь приёма" in text


def test_alert_fits_telegram_limit():
    """Даже если сработали все признаки сразу, сообщение должно уйти."""
    hits = [rf.Hit(flag=flag, evidence="") for flag in rf.FLAGS]
    text = texts.triage_alert(hits, rf.EMERGENCY)
    assert len(text) < 4096


def test_redflags_list_mentions_both_levels():
    text = texts.redflags_list(list(rf.FLAGS))
    assert "Когда звонить 103" in text
    assert "в тот же день" in text
    assert len(text) < 4096


# --- Наблюдение за данными ------------------------------------------------


def test_wet_diapers_needs_records():
    """Отсутствие записей — не отсутствие подгузников.

    Самый вероятный источник ложной тревоги: день, когда просто забыли
    записывать. Молчать в этом случае правильнее, чем пугать.
    """
    assert rf.check_wet_diapers(0, age_days=10, has_records=False) is None


def test_wet_diapers_not_in_the_first_days():
    """В первые дни мокрых подгузников мало, и это норма."""
    assert rf.check_wet_diapers(1, age_days=1, has_records=True) is None


def test_wet_diapers_fires_when_too_few():
    watch = rf.check_wet_diapers(2, age_days=10, has_records=True)
    assert watch is not None
    assert watch.level == rf.URGENT
    assert "2" in watch.title


def test_wet_diapers_quiet_when_enough():
    assert rf.check_wet_diapers(6, age_days=10, has_records=True) is None


@pytest.mark.parametrize(
    ("percent", "fires"), [(None, False), (5.0, False), (9.9, False), (10.0, True), (12.5, True)]
)
def test_weight_loss_threshold(percent, fires):
    """Порог 10% — из клинрекомендаций «Здоровый новорождённый»."""
    assert (rf.check_weight_loss(percent) is not None) is fires


def test_feeding_gap_only_for_newborns():
    """После двух месяцев длинные перерывы — норма, а не повод для тревоги."""
    assert rf.check_feeding_gap(7.0, age_days=10) is not None
    assert rf.check_feeding_gap(7.0, age_days=120) is None


def test_feeding_gap_quiet_when_short():
    assert rf.check_feeding_gap(3.0, age_days=10) is None
    assert rf.check_feeding_gap(None, age_days=10) is None


def test_watch_alert_carries_the_disclaimer():
    watch = rf.check_wet_diapers(1, age_days=10, has_records=True)
    text = texts.watch_alert(watch)
    assert "не врач" in text
    assert len(text) < 4096


# --- Главное: модель на этом пути не участвует ----------------------------


def test_triage_makes_no_network_calls(monkeypatch):
    """Экстренный ответ собирается офлайн, целиком.

    Тест физический: рвём сокеты и httpx. Если когда-нибудь в путь триажа
    заползёт обращение к модели — упадёт здесь, а не у семьи в три часа ночи.

    Это же и есть причина, по которой `alarm.router` стоит вторым в списке:
    будущий Q&A окажется ПОСЛЕ него, и экстренный ответ так и останется
    фиксированным текстом из файла.
    """
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("триаж не имеет права ходить в сеть")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    hits = rf.triage("у неё 38.9, тяжело дышит, не просыпается", YOUNG)
    text = texts.triage_alert(hits, rf.worst(hits))

    assert rf.worst(hits) == rf.EMERGENCY
    assert "103" in text


@pytest.mark.asyncio
async def test_tracker_watch_speaks_once_a_day(session, family, child, mother, bot):
    """Тревога по данным поднимается раз в сутки, а не каждую минуту.

    Наблюдение идёт на каждом тике. Без отметки в БД семья получила бы одно и
    то же сообщение 1440 раз за сутки — то есть ровно один раз, потому что
    после третьего бота бы выключили.
    """
    from bot.db import repo
    from bot.db.models import DIAPER_WET, WEIGHT
    from bot.services import ticker
    from bot.services import timeutil as tu

    now = tu.now_utc()
    # Потеря 15% от веса при рождении — уверенно за порогом
    await repo.add_measurement(
        session, child.id, mother.id, WEIGHT, taken_at=now, value=2907.0
    )
    await repo.add_event(session, child.id, mother.id, DIAPER_WET, started_at=now)

    await ticker.watch_tracker(bot, session, now)
    assert len(bot.sent) == 1
    assert "весе при рождении" in bot.texts[0] or "рождении" in bot.texts[0]

    await ticker.watch_tracker(bot, session, now)
    assert len(bot.sent) == 1, "вторая тревога за те же сутки не нужна"

    # Назавтра признак сохраняется — сказать о нём стоит ещё раз
    await ticker.watch_tracker(bot, session, now + timedelta(days=1))
    assert len(bot.sent) == 2
