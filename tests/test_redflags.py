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


@pytest.mark.parametrize(
    ("value", "level"),
    [
        (38.0, rf.EMERGENCY),  # тот самый порог, ради которого всё и затевалось
        (38.2, rf.EMERGENCY),
        (37.9, rf.URGENT),
        (36.0, rf.EMERGENCY),
        (39.5, rf.EMERGENCY),
    ],
)
def test_temperature_without_known_age_assumes_a_newborn(value, level):
    """Карточка не заполнена — считаем младенцем, а не отменяем проверку.

    Тест переписан после находки: раньше он проверял ровно 39.5, то есть
    проходил вхолостую через ветку «после трёх месяцев» (порог 38.5), а весь
    диапазон 38.0–38.4 при неизвестном возрасте молчал. «Температура 38» без
    карточки ребёнка не поднимала ничего вовсе — при том что 38.0 у младше трёх
    месяцев назван в этом же файле самым важным порогом во всём боте.

    Отсюда и параметры: 38.0 и 38.2 обязаны быть здесь, иначе тест снова станет
    вхолостую проходящим.
    """
    hit = rf.temperature_hit(value, None)
    assert hit is not None
    assert hit.flag.level == level


# --- Разбор температуры: что поймано после аудита -------------------------


@pytest.mark.parametrize(
    "text",
    [
        "температура нормальная, съела 40 мл",
        "спала 40 минут, потом померили температуру",
        "температура в норме, дали 35 мл смеси",
    ],
)
def test_foreign_units_are_not_a_temperature(text):
    """Число с чужой единицей — не температура, даже если слово рядом есть.

    Слово о температуре искалось по всему сообщению, и любое двузначное число
    в 34–43 становилось температурой. «Температура нормальная, съела 40 мл»
    поднимало EMERGENCY со словами «звоните 103» — в ответ на сообщение, где
    прямо сказано, что температура нормальная.

    Ложная тревога в три часа ночи стоит доверия ко всем остальным.
    """
    assert rf.find_temperatures(text) == []
    assert rf.triage(text, YOUNG) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("утром было 36,6 а сейчас 39,5", 39.5),
        ("температура поднялась: 37.2, потом 38.9", 38.9),
        ("была 38.0, сейчас 36.6", 38.0),
    ],
)
def test_the_worst_of_several_temperatures_wins(text, expected):
    """Из нескольких названных чисел берём то, что тяжелее, а не первое.

    Функция выходила на первом подходящем числе, и сообщение про РОСТ
    температуры разбиралось по её началу: «утром было 36,6 а сейчас 39,5» →
    36.6 → молчание. Пропуск в опасную сторону, ровно тот, ради которого здесь
    стоит поиск подстроки, а не модель.
    """
    assert rf.find_temperature(text) == pytest.approx(expected)
    assert rf.worst(rf.triage(text, YOUNG)) == rf.EMERGENCY


def test_degrees_word_is_not_grams():
    """«35.4 градуса» — температура, а не 35.4 грамма.

    Список чужих единиц сравнивается на равенство, а не по основе: «гр» как
    основа съедала «градуса». Поймано тестом в тот же прогон, в который список
    и появился.
    """
    assert rf.find_temperatures("35.4 градуса") == [35.4]


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
    assert (rf.check_weight_loss(percent, reading_age_days=0.0) is not None) is fires


def test_weight_loss_ignores_a_stale_reading():
    """По старому взвешиванию не судят.

    Тревога поднимается раз в сутки, и без этой границы одно измерение с
    потерей за порогом слало бы одно и то же сообщение каждый день до
    следующего взвешивания — месяцами, если взвешиваться перестали.
    """
    assert rf.check_weight_loss(12.5, reading_age_days=0.5) is not None
    assert rf.check_weight_loss(12.5, reading_age_days=30.0) is None


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
    # Подгузников достаточно: иначе поднимется вторая, независимая тревога, и
    # тест перестанет быть про «раз в сутки»
    for _ in range(6):
        await repo.add_event(session, child.id, mother.id, DIAPER_WET, started_at=now)

    await ticker.watch_tracker(bot, session, now)
    assert len(bot.sent) == 1
    assert "рождении" in bot.texts[0]

    await ticker.watch_tracker(bot, session, now)
    assert len(bot.sent) == 1, "вторая тревога за те же сутки не нужна"

    # Назавтра признак сохраняется — сказать о нём стоит ещё раз
    await ticker.watch_tracker(bot, session, now + timedelta(days=1))
    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_tracker_watch_notices_too_few_wet_diapers(
    session, family, child, mother, bot
):
    """Сторож подгузников доходит до отправки, а не только до юнит-теста.

    Тест заведён по факту: в `_collect_watches` стояло условие
    `if end <= now`, где `end` — конец СЕГОДНЯШНИХ суток, а `now` всегда внутри
    них. Условие было ложно всегда, и `check_wet_diapers` в бою не вызывался
    ни разу — при том что юнит-тесты самой функции были зелёные, а
    `docs/TASKS.md` числил сторож сделанным.

    Отсюда и форма теста: он идёт через `watch_tracker`, а не через
    `check_wet_diapers`. Проверять надо стык, он и сломался.
    """
    from bot.db import repo
    from bot.db.models import DIAPER_WET
    from bot.services import ticker
    from bot.services import timeutil as tu

    now = tu.now_utc()
    # Два за сутки при пороге в четыре, начиная с пятого дня жизни
    for _ in range(2):
        await repo.add_event(session, child.id, mother.id, DIAPER_WET, started_at=now)

    await ticker.watch_tracker(bot, session, now)
    assert len(bot.sent) == 1
    assert "подгузник" in bot.texts[0].lower()

    await ticker.watch_tracker(bot, session, now)
    assert len(bot.sent) == 1, "вторая тревога за те же сутки не нужна"


@pytest.mark.asyncio
async def test_tracker_watch_counts_a_sliding_day(session, family, child, mother, bot):
    """Окно — скользящие 24 часа, а не календарный день.

    Шесть подгузников, записанных позавчера, не делают последние сутки
    благополучными. Именно из-за календарных суток в прежней версии и появилось
    условие «судим, только если день уже кончился», которое всё и сломало.
    """
    from bot.db import repo
    from bot.db.models import DIAPER_WET
    from bot.services import ticker
    from bot.services import timeutil as tu

    now = tu.now_utc()
    for _ in range(6):
        await repo.add_event(
            session, child.id, mother.id, DIAPER_WET,
            started_at=now - timedelta(hours=30),
        )
    # Два внутри окна: семья пишет, и писать есть что
    for _ in range(2):
        await repo.add_event(
            session, child.id, mother.id, DIAPER_WET,
            started_at=now - timedelta(hours=2),
        )

    await ticker.watch_tracker(bot, session, now)
    assert len(bot.sent) == 1, "шесть штук старше суток в счёт идти не должны"
    assert "подгузник" in bot.texts[0].lower()
    assert ": 2" in bot.texts[0], "считаться должны только двое свежих"


@pytest.mark.asyncio
async def test_tracker_watch_is_quiet_when_nothing_was_logged(
    session, family, child, mother, bot
):
    """Перестали записывать — это молчание, а не ноль подгузников.

    Без этого сторож превращался бы в машину ежедневного нытья: семья,
    записавшая подгузники один раз и бросившая, получала бы «за сутки отмечено
    мокрых: 0» каждый день до конца года. Та же болезнь, от которой заведена
    граница свежести у взвешивания.

    Проверка «записи вообще были когда-нибудь» здесь не годится именно
    поэтому — смотреть надо на то же окно, по которому судим.
    """
    from bot.db import repo
    from bot.db.models import DIAPER_WET
    from bot.services import ticker
    from bot.services import timeutil as tu

    now = tu.now_utc()
    for _ in range(6):
        await repo.add_event(
            session, child.id, mother.id, DIAPER_WET,
            started_at=now - timedelta(days=20),
        )

    await ticker.watch_tracker(bot, session, now)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_tracker_watch_counts_dirty_as_evidence_of_logging(
    session, family, child, mother, bot
):
    """Грязные записаны, мокрых нет — это признак, а не забывчивость.

    «Записи есть» считаются по подгузникам ЛЮБОГО вида, а сравнивается с
    порогом счётчик мокрых. Иначе сутки без единого мокрого — то самое, ради
    чего проверка и заведена, — уходили бы в «наверное, просто не записали».
    """
    from bot.db import repo
    from bot.db.models import DIAPER_DIRTY
    from bot.services import ticker
    from bot.services import timeutil as tu

    now = tu.now_utc()
    for _ in range(3):
        await repo.add_event(
            session, child.id, mother.id, DIAPER_DIRTY,
            started_at=now - timedelta(hours=3),
        )

    await ticker.watch_tracker(bot, session, now)
    assert len(bot.sent) == 1
    assert "подгузник" in bot.texts[0].lower()


# --- Содержание проверяется на импорте ------------------------------------


def _yaml_without(tmp_path, monkeypatch, drop_threshold=None, break_level=None):
    """Копия боевого redflags.yaml с одной намеренной порчей."""
    import yaml

    raw = yaml.safe_load(rf.CONTENT.read_text(encoding="utf-8"))
    if drop_threshold is not None:
        del raw["thresholds"][drop_threshold]
    if break_level is not None:
        raw["flags"][0]["level"] = break_level

    path = tmp_path / "redflags.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(rf, "CONTENT", path)
    return path


def test_missing_threshold_falls_at_import(tmp_path, monkeypatch):
    """Выпавший порог обязан ронять бота на старте, а не ночью.

    Раньше `_load` проверял только «флагов не ноль». Опечатка в имени ключа
    или потерянная строка давали `KeyError` на первом тревожном сообщении —
    то есть у семьи и в три часа ночи. Бот, молча потерявший триаж, опаснее
    бота, который не запустился: второе заметно сразу.
    """
    _yaml_without(tmp_path, monkeypatch, drop_threshold="fever_c")
    with pytest.raises(ValueError, match="fever_c"):
        rf._load()


def test_unknown_level_falls_at_import(tmp_path, monkeypatch):
    """Опечатка в `level` не должна молча превращаться в «urgent».

    `worst()` отдаёт EMERGENCY только по точному совпадению строки, а всё
    остальное считает urgent. Признак, которому написали «emergancy», тихо
    понижался бы в срочности — то есть «звоните 103» превратилось бы в
    «свяжитесь с врачом сегодня».
    """
    _yaml_without(tmp_path, monkeypatch, break_level="emergancy")
    with pytest.raises(ValueError, match="level"):
        rf._load()


def test_live_content_passes_its_own_checks():
    """Боевой файл проходит те же проверки — иначе бот не стартовал бы."""
    thresholds, flags = rf._load()
    assert set(thresholds) >= rf.REQUIRED_THRESHOLDS
    assert {f.level for f in flags} <= rf.LEVELS
