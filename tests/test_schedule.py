"""Календарь прививок и осмотров: содержание, даты и проверки на импорте.

Тест, который сверяет данные сами с собой, ничего не стоит. Поэтому сверка с
приказами идёт списком, зашитым сюда руками (тот же приём, что у окон моторных
вех ВОЗ в `test_stage.py`), а всё остальное проверяет свойства, которые
ломаются независимо от содержания: порядок, отсутствие дыр, отсутствие мусора
нормализации, поведение дат на краях месяца.

Возрасты и формулировки сверены с `kb/raw/docs/order_1122n.pdf` (приложение
N 1, страницы 4-5) и `kb/raw/docs/order_211n.pdf` (приложение N 1 к Порядку),
а не с пересказом.
"""

from datetime import date, datetime, timedelta

import pytest
import yaml

from bot.db.models import Child
from bot.services import growth
from bot.services import schedule as sch

MOSCOW = "Europe/Moscow"

# Одиннадцать строк национального календаря до трёх лет: (месяцы, дни).
# Дальше по приказу идут 6 лет, 6-7 лет и 14 лет — они за горизонтом файла
VACCINE_AGES = [
    (0, 0),
    (0, 3),
    (1, 0),
    (2, 0),
    (3, 0),
    (4, 15),
    (6, 0),
    (12, 0),
    (15, 0),
    (18, 0),
    (20, 0),
]

# Осмотры: период новорождённости, затем каждый месяц первого года, затем
# 1 год 3 месяца, 1 год 6 месяцев, 2 года и 3 года
CHECKUP_MONTHS = [0, *range(1, 13), 15, 18, 24, 36]


def _kid(born_at: datetime, gestation_weeks: int | None = None) -> Child:
    """Ребёнок без базы: `entry_day` читает только момент рождения."""
    return Child(name="Аня", sex="f", born_at=born_at, gestation_weeks=gestation_weeks)


# --- Содержание -------------------------------------------------------------


def test_vaccine_ages_match_the_order():
    """Список возрастов сверен с таблицей приказа руками.

    Ловит главную ошибку переноса таблицы из PDF — потерянную или выдуманную
    строку. Глазами такое в столбце из одиннадцати чисел не видно.
    """
    got = [(e.age_months, e.age_days) for e in sch.SECTIONS["vaccines"].entries]
    assert got == VACCINE_AGES


def test_checkups_are_monthly_through_the_first_year():
    """Осмотры идут каждый месяц до года без единого пропуска.

    Пропущенный месяц в ряду из двенадцати — ровно то, что не замечают при
    вычитке и замечают в поликлинике.
    """
    got = [e.age_months for e in sch.SECTIONS["checkups"].entries]
    assert got == CHECKUP_MONTHS
    assert all(e.age_days == 0 for e in sch.SECTIONS["checkups"].entries)


def test_risk_group_notes_survived_the_transfer():
    """«(группы риска)» стоит ровно там, где стоит в приказе.

    В таблице 1122н пометка есть у третьей и четвёртой вакцинаций против
    гепатита B — и больше ни у одной строки. Потерять её значит сказать всем
    семьям, что прививка положена всем; приписать лишней — наоборот.
    """
    marked = [
        item
        for entry in sch.SECTIONS["vaccines"].entries
        for item in entry.items
        if "групп" in item
    ]
    assert marked == [
        "Третья вакцинация против вирусного гепатита B (группы риска)",
        "Четвёртая вакцинация против вирусного гепатита B (группы риска)",
    ]


def test_no_pdf_garbage_leaked_in():
    """Колонтитулы КонсультантПлюс вклиниваются прямо между строк таблицы.

    В `kb/normalized/order_1122n.md` «Первая вакцинация против пневмококковой
    инфекции» оторвана от своей строки «Дети 2 месяца» четырьмя строками
    служебного текста. Перенести их вместе с содержанием — вопрос невнимания.
    """
    garbage = ("КонсультантПлюс", "consultant.ru", "Страница", "п/п", "Дата сохранения")
    for section in sch.SECTIONS.values():
        for entry in section.entries:
            haystack = " ".join((entry.age_label, *entry.items, *entry.doctors, *entry.tests))
            for word in garbage:
                assert word not in haystack, f"{entry.age_label}: {word}"


def test_hospital_entries_are_marked_and_only_them():
    """В роддоме — первые две прививки и период новорождённости. Больше ничего."""
    marked = {
        (section.id, entry.age_label)
        for section in sch.SECTIONS.values()
        for entry in section.entries
        if entry.in_hospital
    }
    assert marked == {
        ("vaccines", "первые 24 часа жизни"),
        ("vaccines", "3–7 день жизни"),
        ("checkups", "период новорождённости"),
    }


def test_every_entry_names_a_source_that_exists():
    """Строка календаря без источника — это мнение, а ему здесь не место."""
    for section in sch.SECTIONS.values():
        for entry in section.entries:
            assert entry.source in sch.SOURCES
            assert entry.items or entry.doctors, entry.age_label
    for preset in sch.PRESETS:
        assert preset.source in sch.SOURCES


def test_sources_name_themselves_and_open_in_a_browser():
    for source in sch.SOURCES.values():
        assert source.url.startswith("http")
        assert 1990 < source.year < 2100
        assert source.publisher


def test_calendar_stops_at_the_horizon():
    """Граница бота объявлена данными, а не константой где-то в коде."""
    for section in sch.SECTIONS.values():
        assert max(e.age_months for e in section.entries) <= sch.HORIZON_MONTHS


def test_presets_cover_the_tags_the_schema_promises():
    """Пять тегов, объявленных комментарием у `models.Reminder.preset`.

    Опечатка в `id` дала бы пресет, который рисуется на экране и не включает
    ничего: `preset` в базе — обычная строка, и на неё никто не смотрит.
    """
    assert {p.id for p in sch.PRESETS} == {"feed", "vitd", "weigh", "vaccine", "checkup"}


# --- Даты -------------------------------------------------------------------


def test_months_are_calendar_not_thirty_days():
    """Ребёнок родился 30 ноября: три месяца — это 28 февраля.

    Счёт по тридцать суток дал бы 28 февраля только случайно, а к году разошёлся
    бы на неделю. Педиатр считает месяцами, и бот обязан считать так же.
    """
    kid = _kid(datetime(2026, 11, 30, 12, 0))
    entry = next(e for e in sch.SECTIONS["vaccines"].entries if e.age_months == 3)
    assert sch.entry_day(kid, entry, MOSCOW) == date(2027, 2, 28)


def test_end_of_month_birthday_does_not_overflow():
    """31 января плюс месяц — это 28 февраля, а не 3 марта."""
    kid = _kid(datetime(2027, 1, 31, 12, 0))
    entry = next(e for e in sch.SECTIONS["checkups"].entries if e.age_months == 1)
    assert sch.entry_day(kid, entry, MOSCOW) == date(2027, 2, 28)


def test_half_a_month_is_fifteen_days():
    """«4,5 месяца» — четыре месяца и пятнадцать дней, и это решено в YAML."""
    kid = _kid(datetime(2026, 9, 1, 4, 20))
    entry = next(e for e in sch.SECTIONS["vaccines"].entries if e.age_days == 15)
    assert sch.entry_day(kid, entry, MOSCOW) == date(2027, 1, 16)


def test_birthday_is_read_in_the_family_timezone():
    """Роды в 02:00 по Москве лежат в базе вчерашним днём.

    `born_at` — момент в UTC. Считая календарь от `born_at.date()`, бот уехал
    бы на сутки у всех, кто родился ночью, — то есть у изрядной части детей.
    """
    kid = _kid(datetime(2026, 8, 31, 23, 0))  # это 1 сентября 02:00 в Москве
    entry = next(e for e in sch.SECTIONS["checkups"].entries if e.age_months == 1)
    assert sch.entry_day(kid, entry, MOSCOW) == date(2026, 10, 1)


def test_age_for_the_calendar_is_passport_not_corrected():
    """Главный инвариант этапа, и он противоположен правилу вех и центилей.

    Приказ 1122н говорит «Дети 3 месяца» и там, где недоношенность важна,
    меняет вакцину (п. 12 — инактивированная вместо живой), а не дату. Сдвинуть
    календарь на недоношенность значит развести бота с поликлиникой на месяц —
    и опоздать с прививкой ровно у того ребёнка, которому она нужнее.

    Вторая половина теста обязательна: без неё он тавтология. `schedule` просто
    не читает `gestation_weeks`, и «даты совпали» доказывало бы это само собой.
    Проверяем, что у той же пары детей скорректированный возраст РАЗЛИЧАЕТСЯ, —
    то есть два правила действительно расходятся, и календарь выбрал своё.
    """
    born = datetime(2026, 9, 1, 4, 20)
    term, preterm = _kid(born), _kid(born, gestation_weeks=32)

    for section in sch.SECTIONS.values():
        for entry in section.entries:
            assert sch.entry_day(term, entry, MOSCOW) == sch.entry_day(preterm, entry, MOSCOW)

    at = born + timedelta(days=120)
    assert growth.corrected_age_days(term, at) != growth.corrected_age_days(preterm, at)


def test_notice_lands_before_the_date_at_the_local_hour():
    kid = _kid(datetime(2026, 9, 1, 4, 20))
    section = sch.SECTIONS["vaccines"]
    entry = next(e for e in section.entries if e.age_months == 3)

    moment = sch.fire_at(section, entry, kid, MOSCOW)
    day = sch.entry_day(kid, entry, MOSCOW)

    assert moment.date() == day - timedelta(days=section.remind_before_days)
    assert moment.hour == 10 - 3  # 10:00 по Москве — это 07:00 UTC


def test_hospital_and_past_entries_never_become_reminders():
    """Ребёнку пять месяцев: прошедшее и роддомовское в подписку не попадает.

    `fire_at <= now` тикер разберёт как догонку и выстрелит немедленно. Для
    подписки это была бы пачка сообщений о пропущенном — худший первый экран,
    который бот может показать.
    """
    born = datetime(2026, 9, 1, 4, 20)
    kid = _kid(born)
    now = born + timedelta(days=150)
    section = sch.SECTIONS["vaccines"]

    ahead = sch.upcoming(section, kid, MOSCOW, now)

    assert [e.age_months for e, _ in ahead] == [6, 12, 15, 18, 20]
    assert all(moment > now for _, moment in ahead)
    assert not any(entry.in_hospital for entry, _ in ahead)


def test_a_child_older_than_the_calendar_gets_nothing():
    kid = _kid(datetime(2026, 9, 1, 4, 20))
    now = datetime(2030, 1, 1)
    assert sch.upcoming(sch.SECTIONS["checkups"], kid, MOSCOW, now) == ()


def test_timeline_shows_everything_and_marks_the_past():
    born = datetime(2026, 9, 1, 4, 20)
    kid = _kid(born)
    rows = sch.timeline(sch.SECTIONS["vaccines"], kid, MOSCOW, born + timedelta(days=150))

    assert len(rows) == len(VACCINE_AGES)
    assert [past for _, _, past in rows][:5] == [True] * 5
    assert rows[-1][2] is False


# --- Повторяющиеся пресеты --------------------------------------------------


def test_hourly_preset_starts_one_interval_from_now():
    """Кнопку нажимают сразу после кормления, а не в круглый час."""
    feed = sch.BY_ID["feed"]
    now = datetime(2026, 9, 7, 11, 30)

    assert sch.rrule_of(feed, 2) == "FREQ=HOURLY;INTERVAL=2"
    assert sch.rrule_of(feed) == "FREQ=HOURLY;INTERVAL=3"
    assert sch.first_fire_at(feed, MOSCOW, now, 2) == now + timedelta(hours=2)


def test_daily_preset_waits_for_the_hour_and_never_fires_in_the_past():
    vitd = sch.BY_ID["vitd"]
    morning = datetime(2026, 9, 7, 5, 0)  # 08:00 в Москве, до десяти
    evening = datetime(2026, 9, 7, 18, 0)  # 21:00 в Москве, после

    assert sch.rrule_of(vitd) == "FREQ=DAILY"
    assert sch.first_fire_at(vitd, MOSCOW, morning) == datetime(2026, 9, 7, 7, 0)
    assert sch.first_fire_at(vitd, MOSCOW, evening) == datetime(2026, 9, 8, 7, 0)


def test_weekly_preset_skips_a_whole_week_not_a_day():
    weigh = sch.BY_ID["weigh"]
    evening = datetime(2026, 9, 7, 18, 0)

    assert sch.rrule_of(weigh) == "FREQ=WEEKLY"
    assert sch.first_fire_at(weigh, MOSCOW, evening) == datetime(2026, 9, 14, 7, 0)


def test_calendar_presets_have_no_rule_of_their_own():
    """Календарь не выражается правилом RFC 5545: 0 → 1 → 2 → 3 → 4,5 → 6.

    Отсюда и решение раскрывать подписку в пачку разовых напоминаний: тикер
    умеет ровно два случая, и третьим календарь в него не лезет.
    """
    for preset_id in ("vaccine", "checkup"):
        preset = sch.BY_ID[preset_id]
        assert sch.rrule_of(preset) is None
        assert sch.section_of(preset) is not None
    assert sch.section_of(sch.BY_ID["vitd"]) is None


# --- Проверки на импорте ----------------------------------------------------


def _broken(tmp_path, monkeypatch, mangle):
    """Копия боевого файла с одной намеренной порчей."""
    raw = yaml.safe_load(sch.CONTENT.read_text(encoding="utf-8"))
    mangle(raw)
    path = tmp_path / "schedule_ru.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(sch, "CONTENT", path)


def test_live_content_passes_its_own_checks():
    """Сначала докажем, что проверка вообще проходима на боевом файле."""
    assert sch._load()


def test_entry_without_an_age_falls_at_import(tmp_path, monkeypatch):
    def mangle(raw):
        del raw["vaccines"]["entries"][2]["age_months"]

    _broken(tmp_path, monkeypatch, mangle)
    with pytest.raises(ValueError, match="не указан возраст"):
        sch._load()


def test_entries_out_of_order_fall_at_import(tmp_path, monkeypatch):
    """Порядок — не косметика: по нему выбирается «ближайшее» на экране."""

    def mangle(raw):
        entries = raw["vaccines"]["entries"]
        entries[3], entries[4] = entries[4], entries[3]

    _broken(tmp_path, monkeypatch, mangle)
    with pytest.raises(ValueError, match="по возрастанию"):
        sch._load()


def test_entry_beyond_the_horizon_falls_at_import(tmp_path, monkeypatch):
    def mangle(raw):
        last = dict(raw["vaccines"]["entries"][-1])
        last["age_months"] = 72
        last["age_label"] = "6 лет"
        raw["vaccines"]["entries"].append(last)

    _broken(tmp_path, monkeypatch, mangle)
    with pytest.raises(ValueError, match="за горизонтом"):
        sch._load()


def test_unknown_source_falls_at_import(tmp_path, monkeypatch):
    def mangle(raw):
        raw["checkups"]["entries"][0]["source"] = "prikaz_iz_golovy"

    _broken(tmp_path, monkeypatch, mangle)
    with pytest.raises(ValueError, match="prikaz_iz_golovy"):
        sch._load()


def test_preset_pointing_at_no_section_falls_at_import(tmp_path, monkeypatch):
    def mangle(raw):
        for preset in raw["presets"]:
            if preset["id"] == "vaccine":
                preset["section"] = "privivki"

    _broken(tmp_path, monkeypatch, mangle)
    with pytest.raises(ValueError, match="privivki"):
        sch._load()


def test_preset_without_time_falls_at_import(tmp_path, monkeypatch):
    """Пресет без времени молча сработал бы в полночь."""

    def mangle(raw):
        for preset in raw["presets"]:
            if preset["id"] == "vitd":
                del preset["at"]

    _broken(tmp_path, monkeypatch, mangle)
    with pytest.raises(ValueError, match="не указано время"):
        sch._load()


def test_interval_outside_the_choices_falls_at_import(tmp_path, monkeypatch):
    """Умолчание, которого нет среди кнопок, включилось бы неизвестно как."""

    def mangle(raw):
        for preset in raw["presets"]:
            if preset["id"] == "feed":
                preset["default_hours"] = 5

    _broken(tmp_path, monkeypatch, mangle)
    with pytest.raises(ValueError, match="default_hours"):
        sch._load()


def test_empty_section_falls_at_import(tmp_path, monkeypatch):
    def mangle(raw):
        raw["checkups"]["entries"] = []

    _broken(tmp_path, monkeypatch, mangle)
    with pytest.raises(ValueError, match="пуста"):
        sch._load()
