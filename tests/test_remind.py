"""Экран `/remind`: пресеты, подписки на календарь и их выключение.

Всё гоняется через настоящий `Dispatcher` с боевым списком роутеров. Свой
диспетчер из двух роутеров доказывал бы, что механизм работает при правильном
порядке, и молчал бы о том, каков порядок на самом деле, — этот урок в проекте
уже оплачен (см. летопись этапа 6).
"""

from datetime import datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import handlers, texts
from bot.db import repo
from bot.db.models import Reminder
from bot.keyboards import RemindCB
from bot.services import schedule as sch
from bot.services import timeutil as tu

CHAT = Chat(id=-1001, type="supergroup", title="Малышка")
AUTHOR = User(id=222, is_bot=False, first_name="Аня")
NOW = datetime(2026, 9, 13, 12, 0)


class RecordingSession(BaseSession):
    """Сеть не трогаем: складываем отправленное в список."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Any, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        payload = method.model_dump(exclude_none=True)
        if "text" in payload:
            self.sent.append(payload["text"])
        return Message(message_id=999, date=NOW, chat=CHAT, from_user=AUTHOR, text="")

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def wired(session, family, child, mother):
    """Диспетчер с боевыми роутерами и подстановкой, как у `FamilyMiddleware`."""
    bot = Bot(token="42:TEST", session=RecordingSession())
    dp = Dispatcher()

    async def inject(handler, event, data):
        data.update(session=session, family=family, child=child, member=mother)
        return await handler(event, data)

    dp.update.outer_middleware(inject)
    order = tuple(handlers.routers)
    for router in order:
        dp.include_router(router)

    yield dp, bot, bot.session

    # Роутеры — синглтоны уровня модуля: без отвязки второй тест в файле падает
    # на «Router is already attached»
    for router in order:
        router._parent_router = None


async def _command(dp, bot, text="/remind", message_id=1):
    message = Message(
        message_id=message_id, date=NOW, chat=CHAT, from_user=AUTHOR, text=text
    )
    await dp.feed_update(bot, Update(update_id=message_id, message=message))


async def _tap(dp, bot, action, preset="", hours=0, update_id=50):
    call = CallbackQuery(
        id=str(update_id),
        from_user=AUTHOR,
        chat_instance="x",
        data=RemindCB(action=action, preset=preset, hours=hours).pack(),
        message=Message(message_id=5, date=NOW, chat=CHAT, from_user=AUTHOR, text="⏰"),
    )
    await dp.feed_update(bot, Update(update_id=update_id, callback_query=call))


async def _live(session, family, preset: str) -> list[Reminder]:
    rows = await repo.active_reminders(session, family.id)
    return [row for row in rows if row.preset == preset]


# --- Экран ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remind_reaches_its_handler_through_the_real_dispatcher(wired):
    """Команда доходит сквозь боевой порядок роутеров, а не мимо всех."""
    dp, bot, session_bot = wired
    await _command(dp, bot)
    assert any("Напоминания" in text for text in session_bot.sent)


@pytest.mark.asyncio
async def test_every_preset_is_off_until_asked(wired, session, family):
    """Бот молчит, пока его не попросили. Особенно про уколы."""
    dp, bot, session_bot = wired
    await _command(dp, bot)

    screen = session_bot.sent[-1]
    assert screen.count("⬜") == len(sch.PRESETS)
    assert "✅" not in screen
    assert await repo.active_reminders(session, family.id) == []


@pytest.mark.asyncio
async def test_the_screen_is_built_from_the_file(wired, session_bot=None):
    """Пресет, дописанный в YAML, появится на экране сам, без правки Python."""
    dp, bot, session_bot = wired
    await _command(dp, bot)

    screen = session_bot.sent[-1]
    for preset in sch.PRESETS:
        assert preset.title in screen


# --- Повторяющиеся пресеты --------------------------------------------------


@pytest.mark.asyncio
async def test_vitd_creates_one_daily_reminder_with_its_dose(wired, session, family):
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", "vitd")

    rows = await _live(session, family, "vitd")
    assert len(rows) == 1
    assert rows[0].rrule == "FREQ=DAILY"
    assert "1000 МЕ" in rows[0].text
    assert "10:00" not in rows[0].text  # время — в fire_at, а не в тексте
    assert rows[0].fire_at.hour == 7  # 10:00 по Москве


@pytest.mark.asyncio
async def test_feeding_asks_for_the_interval_first(wired, session, family):
    """Интервал спрашиваем один раз, а не переключаем по кругу одной кнопкой."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "pick", "feed")

    assert texts.REMIND_PICK in session_bot.sent
    assert await _live(session, family, "feed") == []


@pytest.mark.asyncio
async def test_the_chosen_interval_survives_the_button(wired, session, family):
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", "feed", hours=2)

    rows = await _live(session, family, "feed")
    assert len(rows) == 1
    assert rows[0].rrule == "FREQ=HOURLY;INTERVAL=2"


@pytest.mark.asyncio
async def test_turning_a_preset_on_twice_makes_no_duplicate(wired, session, family):
    """Ради этого в схеме и заведён `preset`, а в repo — `reminder_by_preset`."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", "vitd", update_id=51)
    await _tap(dp, bot, "on", "vitd", update_id=52)

    assert len(await _live(session, family, "vitd")) == 1


@pytest.mark.asyncio
async def test_a_stale_on_button_never_turns_anything_off(wired, session, family):
    """Кнопка со вчерашнего экрана живёт вечно и обязана быть идемпотентной.

    Будь она переключателем, вчерашний тап «включить» сегодня выключил бы то,
    что вчера включили, — и человек узнал бы об этом, не получив напоминания.
    """
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", "weigh", update_id=53)
    await _tap(dp, bot, "on", "weigh", update_id=54)

    assert len(await _live(session, family, "weigh")) == 1


# --- Подписка на календарь --------------------------------------------------


@pytest.mark.asyncio
async def test_vaccine_subscription_creates_one_row_per_future_entry(
    wired, session, family, child
):
    """Заводится всё будущее, ничего роддомовского и ничего повторяющегося.

    Ожидание считается от того же «сейчас», что и у хендлера: ребёнок в
    фикстуре родился в фиксированный день, а часы идут, и зашитое сюда число
    строк протухло бы само собой через месяц.
    """
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", "vaccine")

    rows = await _live(session, family, "vaccine")
    ahead = sch.upcoming(sch.SECTIONS["vaccines"], child, family.tz, tu.now_utc())

    assert len(rows) == len(ahead)
    assert all(row.rrule is None for row in rows)
    assert all(row.child_id == child.id for row in rows)
    assert not any(entry.in_hospital for entry, _ in ahead)


@pytest.mark.asyncio
async def test_a_subscription_reminder_names_the_date_and_what_is_due(
    wired, session, family, child
):
    """Текст собран из той самой записи календаря, а не из соседней."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", "vaccine")

    rows = await _live(session, family, "vaccine")
    first = min(rows, key=lambda row: row.fire_at)
    entry, _ = sch.upcoming(sch.SECTIONS["vaccines"], child, family.tz, tu.now_utc())[0]

    assert entry.age_label in first.text
    assert entry.items[0] in first.text


@pytest.mark.asyncio
async def test_every_calendar_reminder_fits_the_column(wired, session, family):
    """`Reminder.text` — 500 знаков, а осмотр в год это шесть специалистов.

    Обрезание должно краснеть здесь, а не молча в базе.
    """
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", "vaccine", update_id=60)
    await _tap(dp, bot, "on", "checkup", update_id=61)

    rows = await repo.active_reminders(session, family.id)
    assert rows
    for row in rows:
        assert len(row.text) <= 500, row.text[:80]


@pytest.mark.asyncio
async def test_turning_a_subscription_off_kills_every_row(wired, session, family):
    """Выключение по одной строке дало бы бота, который «выключен» и звонит."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", "vaccine", update_id=62)
    assert len(await _live(session, family, "vaccine")) > 1

    await _tap(dp, bot, "off", "vaccine", update_id=63)
    assert await _live(session, family, "vaccine") == []


@pytest.mark.asyncio
async def test_the_answer_says_how_many_were_created(wired, session, family):
    """Полтора десятка строк от одного тапа — это надо сказать вслух."""
    dp, bot, session_bot = wired
    await _tap(dp, bot, "on", "checkup")

    count = len(await _live(session, family, "checkup"))
    assert any(str(count) in text for text in session_bot.sent)


# --- Календарь --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_calendar_shows_this_childs_dates(wired, session_bot=None):
    dp, bot, session_bot = wired
    await _tap(dp, bot, "cal", "vaccine")

    screen = session_bot.sent[-1]
    assert "Аня" in screen
    assert "1 сен" in screen  # день рождения из фикстуры
    assert "1122н" in screen  # источник назван


@pytest.mark.asyncio
async def test_the_calendar_marks_exactly_one_entry_as_next(wired):
    """Ближайшая дата видна, даже если напоминание по ней завести уже поздно.

    Это единственное, что закрывает дыру «включили подписку за день до
    прививки»: само напоминание не заведётся — его момент уже в прошлом.
    """
    dp, bot, session_bot = wired
    await _tap(dp, bot, "cal", "checkup")

    assert session_bot.sent[-1].count("➡️") == 1


@pytest.mark.asyncio
async def test_going_back_returns_to_the_presets(wired):
    dp, bot, session_bot = wired
    await _tap(dp, bot, "cal", "vaccine", update_id=70)
    await _tap(dp, bot, "back", update_id=71)

    assert "Напоминания" in session_bot.sent[-1]


# --- Границы ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_preset_from_an_old_button_answers_anyway(wired, session, family):
    """Иначе Telegram крутит «часики» до таймаута — то же правило, что в track."""
    dp, bot, session_bot = wired
    before = list(session_bot.sent)
    await _tap(dp, bot, "on", "чего-то-нет")

    assert session_bot.sent == before
    assert await repo.active_reminders(session, family.id) == []


@pytest.mark.asyncio
async def test_the_calendar_needs_a_birthday_but_vitamin_d_does_not(
    wired, session, family, monkeypatch
):
    """Витамин D включается и без карточки: ему нужен только будильник."""
    dp, bot, session_bot = wired

    async def no_child(handler, event, data):
        data["child"] = None
        return await handler(event, data)

    dp.update.outer_middleware(no_child)
    await _tap(dp, bot, "on", "vaccine", update_id=80)
    assert await _live(session, family, "vaccine") == []

    await _tap(dp, bot, "on", "vitd", update_id=81)
    assert len(await _live(session, family, "vitd")) == 1


@pytest.mark.asyncio
async def test_manual_reminders_do_not_break_the_screen(wired, session, family, mother):
    """На этапе 7 в той же таблице появятся строки без пресета.

    Группировка обязана их пропускать, а не падать ключом `None` и не рисовать
    шестой пресет с именем «None».
    """
    dp, bot, session_bot = wired
    await repo.create_reminder(
        session,
        family.id,
        mother.id,
        text="позвонить в поликлинику",
        fire_at=NOW + timedelta(hours=1),
    )

    await _command(dp, bot)
    screen = session_bot.sent[-1]
    assert "None" not in screen
    assert screen.count("⬜") + screen.count("✅") == len(sch.PRESETS)
