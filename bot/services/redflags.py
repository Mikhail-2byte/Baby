"""Триаж тревожных признаков. Ни одного обращения к модели.

Это самая ответственная часть бота, и устроена она нарочито тупо.

**Почему не LLM.** Исследования по red-teaming медицинских ассистентов дают
примерно 14% пробоев на адверсарных запросах даже у сильнейших моделей. Для
бота, к которому обращаются в три часа ночи, это неприемлемо: один пропуск
из семи — не та надёжность, при которой можно полагаться на молчание. Поиск
подстроки не ошибается никогда, и ровно поэтому он здесь и стоит.

**Где проходит граница ответственности.** Модуль не ставит диагноз и ничего
не оценивает. Он умеет одно: заметить, что в тексте названо то, про что в
клинических рекомендациях написано «осмотр немедленно», и напомнить об этом
правиле. Формулировки — из `content/redflags.yaml`, оттуда же берутся пороги.
В коде нет ни одного симптома и ни одного числа.

**Две ловушки русского текста**, из-за которых наивный поиск подстроки
работать не будет:

* отрицание бывает после слова — «температуры нет», «судорог не было»;
* сам признак часто сформулирован через отрицание — «не просыпается»,
  «не берёт грудь». Такие фразы подавлять нельзя, иначе модуль промолчит
  ровно там, где обязан кричать.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

CONTENT = Path(__file__).resolve().parent.parent / "content" / "redflags.yaml"

EMERGENCY = "emergency"
URGENT = "urgent"

# Слова, отменяющие найденный признак. Ищутся в окне вокруг совпадения.
#
# Прошедшего времени здесь нет намеренно. «Рвота была» означает, что она
# случилась, а не что её не было; «температура была 38» у младенца — тот же
# повод звонить, что и «температура 38». Отменяет признак только явное
# отрицание или прямое указание, что всё прошло
NEGATIONS = frozenset({"не", "нет", "без", "прошла", "прошел", "прошло"})

# Ширина окна в словах, в котором отрицание считается относящимся к признаку.
# Три слова — компромисс: «температуры нет» ловится, а «температура 38, сыпи
# нет» не подавляет температуру
NEGATION_WINDOW = 3

# Числа, похожие на температуру: 38, 38.2, 36,5. Дробная часть необязательна,
# но от неё зависит, нужно ли рядом слово «температура» — см. `find_temperature`
NUMBER = re.compile(r"\b(\d{2})(?:[.,](\d))?\b")

TEMP_WORDS = ("температур", "темпа", "градус", "°", "жар", "лихорад")


@dataclass(frozen=True)
class Flag:
    """Один признак из `redflags.yaml`."""

    id: str
    level: str
    title: str
    action: str
    source: str
    phrases: tuple[str, ...]
    # Целые сообщения, какими они приходят в чат. Нужны только тестам, но
    # живут рядом с фразами намеренно: правя одно, видишь другое
    examples: tuple[str, ...] = ()
    age_min_days: int | None = None
    age_max_days: int | None = None


@dataclass(frozen=True)
class Hit:
    """Сработавший признак и то, что именно его вызвало."""

    flag: Flag
    evidence: str


def _load() -> tuple[dict[str, float], tuple[Flag, ...]]:
    """Прочитать содержание триажа.

    Читается один раз на импорте. Ошибка здесь обязана уронить бота на старте,
    а не выясниться при первом тревожном сообщении: бот, молча потерявший
    триаж, опаснее бота, который не запустился.
    """
    raw: dict[str, Any] = yaml.safe_load(CONTENT.read_text(encoding="utf-8"))
    thresholds = {key: float(value) for key, value in raw["thresholds"].items()}
    flags = tuple(
        Flag(
            id=item["id"],
            level=item["level"],
            title=item["title"],
            action=" ".join(item["action"].split()),
            source=item["source"],
            phrases=tuple(normalize(phrase) for phrase in item["phrases"]),
            examples=tuple(item.get("examples", ())),
            age_min_days=item.get("age_min_days"),
            age_max_days=item.get("age_max_days"),
        )
        for item in raw["flags"]
    )
    if not flags:
        raise ValueError("В redflags.yaml нет ни одного признака")
    return thresholds, flags


def normalize(text: str) -> str:
    """Привести к виду, в котором ищем: нижний регистр, «ё» → «е».

    «Ё» разнобоем пишут все, и «не просыпается» против «не просыпаетcя» — не
    та разница, на которой должен ломаться триаж.
    """
    return text.lower().replace("ё", "е")


THRESHOLDS, FLAGS = _load()
BY_ID = {flag.id: flag for flag in FLAGS}


def _words(text: str) -> list[str]:
    return re.findall(r"[а-яa-z0-9]+", text)


def _negated(text: str, start: int, end: int) -> bool:
    """Отменяет ли окружение найденное совпадение.

    Окно берётся с обеих сторон: в русском отрицание ходит и перед словом
    («нет температуры»), и после него («температуры нет»).
    """
    before = _words(text[:start])[-NEGATION_WINDOW:]
    after = _words(text[end:])[:NEGATION_WINDOW]
    return any(word in NEGATIONS for word in before + after)


def find_phrases(text: str, age_days: int | None = None) -> list[Hit]:
    """Признаки, названные словами. Порядок — как в файле."""
    haystack = normalize(text)
    hits: list[Hit] = []

    for flag in FLAGS:
        if not _age_fits(flag, age_days):
            continue
        for phrase in flag.phrases:
            position = haystack.find(phrase)
            if position < 0:
                continue
            # Фразу, уже содержащую отрицание, подавлять нельзя: «не
            # просыпается» — это сам признак, а не его отсутствие. Без этой
            # оговорки модуль промолчал бы ровно на самых тяжёлых формулировках
            if " не " not in f" {phrase} " and _negated(
                haystack, position, position + len(phrase)
            ):
                continue
            hits.append(Hit(flag=flag, evidence=phrase))
            break  # один признак — одно срабатывание, даже если фраз совпало две
    return hits


def _age_fits(flag: Flag, age_days: int | None) -> bool:
    if age_days is None:
        return True
    if flag.age_min_days is not None and age_days < flag.age_min_days:
        return False
    return not (flag.age_max_days is not None and age_days > flag.age_max_days)


def find_temperature(text: str) -> float | None:
    """Температура, названная в тексте, или `None`.

    Целое число принимаем только рядом со словом о температуре: «38» само по
    себе в этом боте с равным успехом означает миллилитры в бутылочке. Дробное
    в диапазоне 34–43 берём и без слова — «38.2 мл» никто не пишет.
    """
    haystack = normalize(text)
    has_word = any(word in haystack for word in TEMP_WORDS)

    for match in NUMBER.finditer(haystack):
        whole, fraction = match.group(1), match.group(2)
        value = float(whole) + (float(fraction) / 10 if fraction else 0.0)
        if not THRESHOLDS["temp_min_c"] <= value <= THRESHOLDS["temp_max_c"]:
            continue
        if fraction is None and not has_word:
            continue
        if _negated(haystack, match.start(), match.end()):
            continue
        return value
    return None


def temperature_hit(value: float, age_days: int | None) -> Hit | None:
    """Пороги температуры. Возраст решает, где начинается тревога.

    38.0 у ребёнка младше трёх месяцев — единственный самый важный порог во
    всём боте: это консенсус AAP, NHS и NICE, и он срабатывает даже тогда,
    когда в остальном ребёнок выглядит спокойным.
    """
    young = age_days is not None and age_days <= THRESHOLDS["fever_age_days"]

    if value <= THRESHOLDS["hypothermia_c"] and young:
        return Hit(
            flag=Flag(
                id="hypothermia",
                level=EMERGENCY,
                title=f"Температура {value:g} °C у ребёнка младше 3 месяцев",
                action=(
                    "Звоните 103. Пониженная температура у младенца — такой же "
                    "признак тяжёлой инфекции, как и высокая. Пока ждёте: "
                    "согревайте контактом кожа к коже и продолжайте кормить."
                ),
                source="NHS Sepsis, WHO IMCI",
                phrases=(),
            ),
            evidence=f"{value:g} °C",
        )

    if value >= THRESHOLDS["fever_c"] and young:
        return Hit(
            flag=Flag(
                id="fever_young",
                level=EMERGENCY,
                title=f"Температура {value:g} °C у ребёнка младше 3 месяцев",
                action=(
                    "Звоните 103. У ребёнка младше трёх месяцев температура "
                    "38 °C и выше — повод для немедленного осмотра, даже если "
                    "в остальном всё выглядит спокойно."
                ),
                source="NICE NG143, AAP, NHS",
                phrases=(),
            ),
            evidence=f"{value:g} °C",
        )

    if value >= THRESHOLDS["fever_c_older"]:
        return Hit(
            flag=Flag(
                id="fever_high",
                level=URGENT,
                title=f"Температура {value:g} °C",
                action=(
                    "Свяжитесь с врачом сегодня. Вызывайте скорую не дожидаясь "
                    "приёма, если появятся вялость, трудное дыхание, изменение "
                    "цвета кожи или сыпь, не бледнеющая под стеклом."
                ),
                source="NICE NG143",
                phrases=(),
            ),
            evidence=f"{value:g} °C",
        )
    return None


def triage(text: str, age_days: int | None = None) -> list[Hit]:
    """Все признаки, найденные в сообщении. Пусто — значит ничего не нашли.

    «Ничего не нашли» никогда не означает «всё хорошо», и текст ответа обязан
    это проговаривать: модуль ищет названные словами признаки, а не оценивает
    состояние ребёнка.
    """
    hits = list(find_phrases(text, age_days))

    value = find_temperature(text)
    if value is not None:
        hit = temperature_hit(value, age_days)
        if hit is not None:
            # Температура идёт первой: из всех признаков у неё самый чёткий порог
            hits.insert(0, hit)
    return hits


def worst(hits: list[Hit]) -> str | None:
    """Самый тяжёлый уровень среди сработавших."""
    if any(hit.flag.level == EMERGENCY for hit in hits):
        return EMERGENCY
    if hits:
        return URGENT
    return None


# --- Наблюдение за данными трекера ---------------------------------------
#
# Второй путь срабатывания: человек ни о чём не спрашивал, но цифры сами
# показывают то, о чём стоит знать. Здесь особенно важно не поднимать тревогу
# на пустом месте: сообщение приходит без спроса, и ложная тревога в три часа
# ночи стоит дороже, чем в ответ на вопрос.


@dataclass(frozen=True)
class Watch:
    """Повод, найденный в данных, а не в тексте."""

    id: str
    level: str
    title: str
    action: str


def check_wet_diapers(count: int, age_days: int, has_records: bool) -> Watch | None:
    """Мало мокрых подгузников за сутки.

    Три оговорки, без которых это была бы машина ложных тревог:
    первые дни жизни подгузников мало и это норма; отсутствие записей — не
    отсутствие подгузников; считаем только когда сутки уже прошли.
    """
    if not has_records:
        return None
    if age_days < THRESHOLDS["wet_diapers_from_day"]:
        return None
    if count >= THRESHOLDS["min_wet_diapers"]:
        return None
    return Watch(
        id="wet_diapers",
        level=URGENT,
        title=f"За сутки отмечено мокрых подгузников: {count}",
        action=(
            "Начиная примерно с пятого дня жизни их обычно шесть и больше. "
            "Если записывали не всё — просто проверьте по факту. Если их "
            "действительно мало, свяжитесь с врачом: это способ заметить, что "
            "ребёнку не хватает молока."
        ),
    )


def check_weight_loss(percent: float | None) -> Watch | None:
    """Потеря массы больше 10% от веса при рождении."""
    if percent is None or percent < THRESHOLDS["weight_loss_percent"]:
        return None
    return Watch(
        id="weight_loss",
        level=URGENT,
        title=f"Вес ниже веса при рождении на {percent:.1f}%",
        action=(
            "Свяжитесь с врачом. Потеря больше 10% от массы при рождении — "
            "порог из клинических рекомендаций, при котором нужен осмотр и "
            "контрольное взвешивание."
        ),
    )


def check_feeding_gap(hours: float | None, age_days: int) -> Watch | None:
    """Долгий перерыв между кормлениями у новорождённого.

    Только для первых двух месяцев: дальше длинные промежутки — норма, а не
    повод для тревоги.
    """
    if hours is None:
        return None
    if age_days > THRESHOLDS["feeding_gap_age_days"]:
        return None
    if hours < THRESHOLDS["feeding_gap_hours"]:
        return None
    return Watch(
        id="feeding_gap",
        level=URGENT,
        title=f"С последнего записанного кормления прошло {hours:.0f} ч",
        action=(
            "Новорождённого будят на кормление, если он не просыпается сам: "
            "перерыв дольше шести часов в первые недели — повод разбудить и "
            "покормить. Если ребёнок вялый или не берёт грудь — звоните 103."
        ),
    )
