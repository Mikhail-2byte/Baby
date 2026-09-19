"""Форматирование. Мелочь, которую читают чаще всего остального."""

from datetime import datetime, timedelta

import pytest

from bot import texts
from bot.services import growth


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=40), "40 с"),
        (timedelta(minutes=12), "12 мин"),
        (timedelta(hours=1, minutes=40), "1 ч 40 мин"),
        (timedelta(hours=3), "3 ч"),
        (timedelta(), "0 с"),
    ],
)
def test_duration(delta, expected):
    assert texts.duration(delta) == expected


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "день"), (2, "дня"), (5, "дней"),
        # Подводный камень русского счёта: 11-14 всегда «дней», как бы ни
        # кончалось число. «11 день» — самая заметная ошибка в боте
        (11, "дней"), (12, "дней"), (14, "дней"),
        (21, "день"), (22, "дня"), (101, "день"), (111, "дней"),
    ],
)
def test_plural(count, expected):
    assert texts.plural(count, "день", "дня", "дней") == expected


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (0, "0 дней"),
        (1, "1 день"),
        (6, "6 дней"),
        (13, "13 дней"),
        (14, "2 недели"),
        (16, "2 недели 2 дня"),
        (21, "3 недели"),
        (90, "3 месяца"),
        (180, "6 месяцев"),
    ],
)
def test_age_phrase(child, born, days, expected):
    """Единица возраста меняется вместе с тем, чем меряют родители.

    Первые две недели считают днями, до трёх месяцев — неделями, дальше
    месяцами. «97 дней» никто не говорит.
    """
    assert texts.age_phrase(child, born + timedelta(days=days)) == expected


def test_weight_str_uses_thin_space():
    """Разряды через узкий неразрывный пробел — иначе Telegram рвёт число."""
    assert texts.weight_str(3420) == f"3{texts.THIN_SPACE}420 г"
    assert texts.weight_str(980) == "980 г"


@pytest.mark.parametrize(
    ("z", "expect_warning"),
    [(-2.5, True), (-1.9, False), (0, False), (1.9, False), (2.5, True)],
)
def test_percentile_warns_outside_two_sd(z, expect_warning):
    """±2 SD — граница, по которой ВОЗ отделяет вариант нормы от разговора с врачом."""
    text = texts.percentile_str(z, growth.percentile(z))
    assert ("⚠️" in text) is expect_warning


def test_ago():
    now = datetime(2026, 9, 7, 12, 0)
    assert texts.ago(now - timedelta(seconds=30), now) == "только что"
    assert texts.ago(now - timedelta(hours=2), now) == "2 ч назад"


def test_weight_delta_shows_daily_gain():
    """Прибавка в граммах за сутки — то, что спрашивает педиатр первым делом."""
    text = texts.weight_delta(180, 6)
    assert "+180 г" in text
    assert "6 дней" in text
    assert "+30 г/сутки" in text


def test_weight_delta_handles_loss():
    """Потеря веса в первые дни — норма, и минус обязан быть виден."""
    assert texts.weight_delta(-120, 3).startswith("−120 г")


def test_missed_summary_truncates_long_lists(family):
    """Сводка после долгого простоя не должна упираться в лимит Telegram."""
    from bot.db.models import Reminder

    now = datetime(2026, 9, 7, 12, 0)
    reminders = [
        Reminder(text=f"Напоминание {i}", fire_at=now - timedelta(hours=i))
        for i in range(1, 31)
    ]
    text = texts.missed_summary(reminders, family.tz, now)
    assert "30 напоминаний" in text
    assert "и ещё 20" in text
    assert len(text) < 4096


def test_angle_brackets_in_a_reminder_do_not_eat_the_message(family):
    """В нормативных актах сноски пишут как «<1>», и это незакрытый тег.

    Telegram на сломанной разметке не ругается внятно — он просто отказывается
    отправить сообщение, то есть напоминание исчезает, а в логе остаётся
    `BadRequest`. Тот же класс поломки уже стоил одного ответа Q&A.
    """
    from bot.db.models import Reminder

    now = datetime(2026, 9, 7, 12, 0)
    reminder = Reminder(text="Осмотр <1> и анализы", fire_at=now)

    one = texts.reminder_message(reminder, family.tz, now, late=False)
    many = texts.missed_summary([reminder], family.tz, now)

    assert "<1>" not in one and "&lt;1&gt;" in one
    assert "<1>" not in many and "&lt;1&gt;" in many
    # Своя разметка при этом на месте: экранируется только чужой текст
    assert "<i>" in texts.reminder_message(reminder, family.tz, now, late=True)


def test_no_medical_numbers_live_in_python():
    """Правило «числа не через LLM» имеет вторую половину: и не в коде.

    Доза витамина D, сроки прививок и возрасты осмотров лежат в
    `schedule_ru.yaml` — там, где их может прочитать и поправить врач. Проверка
    механическая, потому что на честное слово такие вещи и уезжают в Python.
    """
    from pathlib import Path

    root = Path(texts.__file__).resolve().parent
    marks = ("МЕ/сут", "1000 МЕ", " МЕ ", "мкг")
    for path in root.rglob("*.py"):
        body = path.read_text(encoding="utf-8")
        for mark in marks:
            assert mark not in body, f"{path.name}: {mark}"


# --- Меню команд ----------------------------------------------------------


def _registered_commands() -> set[str]:
    """Все команды, у которых в проекте действительно есть обработчик."""
    from aiogram.filters import Command

    from bot.handlers import routers

    found: set[str] = set()
    for router in routers:
        for handler in router.message.handlers:
            for item in handler.filters or ():
                if isinstance(item.callback, Command):
                    found |= {str(c) for c in item.callback.commands}
    return found


def test_menu_promises_only_what_exists():
    """Всё из `COMMANDS` обязано иметь обработчик.

    Тест заведён по факту: `/remind` больше года стоял в `COMMANDS`, уезжал в
    меню группы через `set_my_commands`, а обработчика не было — тап по пункту
    меню проваливался мимо всех роутеров и не отвечал ничего. Молчание бота на
    им же предложенную кнопку хуже отсутствия кнопки.

    Обратное неверно и проверяться не должно: `/ping` и `/sos` работают, но в
    меню им делать нечего — одно диагностическое, второе синоним.
    """
    promised = {name for name, _ in texts.COMMANDS}
    missing = promised - _registered_commands()
    assert not missing, (
        f"в меню обещаны команды без обработчика: {sorted(missing)}. "
        "Либо напишите обработчик, либо уберите из COMMANDS"
    )


def test_help_mentions_only_what_exists():
    """То же для /help: текст помощи — тоже обещание."""
    import re

    # Отрицательный просмотр назад на «<» отсекает закрывающие теги
    # разметки Telegram: «</b>» — это не команда /b
    mentioned = set(re.findall(r"(?<!<)/([a-z_]+)", texts.HELP))
    missing = mentioned - _registered_commands()
    assert not missing, f"/help обещает несуществующее: {sorted(missing)}"


# --- Описание бота: то, что он рассказывает о себе сам ----------------------


def test_the_descriptions_fit_what_telegram_accepts():
    """Лимиты API: 120 знаков на короткое описание, 512 на длинное.

    Превышение обрезает не проект, а Telegram, — то есть молча и посреди фразы.
    Запас оставлен намеренно: строка, упирающаяся в лимит ровно, роняет первую
    же будущую правку.
    """
    assert len(texts.SHORT_DESCRIPTION) <= 120
    assert len(texts.DESCRIPTION) <= 512


def test_the_description_mentions_only_what_exists():
    """Описание — обещание, как меню и /help, и сторожится так же."""
    import re

    mentioned = set(re.findall(r"(?<!<)/([a-z_]+)", texts.DESCRIPTION))
    missing = mentioned - _registered_commands()
    assert not missing, f"описание обещает несуществующее: {sorted(missing)}"


def test_the_description_tells_about_the_voice():
    """Про голос обязано быть сказано, и это не вкусовщина.

    У голоса нет ни команды, ни кнопки — это единственная возможность бота, о
    которой нельзя догадаться, глядя на экран. Не сказав о ней, бот получает
    функцию, которой никто не воспользуется: жена узнает о ней только если ей
    расскажут словами.
    """
    for text in (texts.DESCRIPTION, texts.SHORT_DESCRIPTION, texts.HELP):
        assert "голос" in text.lower()


def test_the_description_says_where_the_bot_works():
    """Описание видно только в личке — а там бот работы не делает.

    Значит единственный, кто его читает, — человек, открывший личный чат, и
    ему надо сказать про группу прежде, чем он напишет и получит отказ.
    """
    assert "групп" in texts.DESCRIPTION.lower() or "чат" in texts.DESCRIPTION.lower()


def test_the_profile_is_published_at_startup():
    """Описание уезжает в Telegram кодом, а не выставляется руками.

    Выставленное руками у BotFather, оно расходится с тем, что бот умеет, и
    заметить это можно только открыв BotFather. До этапа 7 оба описания были
    пусты — именно потому, что публиковать их было некому.
    """
    import inspect

    from bot import __main__ as entry

    source = inspect.getsource(entry._publish_profile)
    assert "set_my_description" in source
    assert "set_my_short_description" in source
    assert "set_my_commands" in source
    # И зовётся из старта, а не просто написано
    assert "_publish_profile(bot)" in inspect.getsource(entry.main)


def test_a_failed_publish_does_not_take_the_others_down():
    """Три вызова — три `try`. Упавший первый не отменяет остальные.

    Это тот же контракт, что у порядка остановки в `main`: сбой одного шага не
    должен съедать следующие. Проверяется фактом — с одним общим `try` второй и
    третий вызовы не состоялись бы вовсе.
    """
    import asyncio
    from types import SimpleNamespace

    from bot import __main__ as entry

    done: list[str] = []

    async def ok(name):
        done.append(name)

    async def boom(name):
        done.append(name)
        raise RuntimeError("Telegram отказал")

    bot = SimpleNamespace(
        set_my_commands=lambda *a, **k: boom("commands"),
        set_my_description=lambda **k: ok("description"),
        set_my_short_description=lambda **k: ok("short"),
    )
    asyncio.run(entry._publish_profile(bot))
    assert done == ["commands", "description", "short"]
