"""Акт 2 — всё, что живёт по времени.

Тикер здесь настоящий и работает по-настоящему; ускорен только период цикла
(`TICK_SECONDS=5` вместо 60). Ни один порог не правится: выкрученный в минуту
`OPEN_TIMER_MAX_HOURS` проверял бы не боевое значение, а открытый пять часов
назад таймер при боевых четырёх — ровно его. Поэтому всё ускорение здесь —
сдвиг данных, и только данных.
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

from bot.config import settings
from bot.db.models import SLEEP
from bot.keyboards import CheckinCB
from bot.services import timeutil as tu
from tools.stand import seed
from tools.stand.harness import Stand

# Период тика 5 с, но сам тик не мгновенный: тот, что отправляет дайджест с
# двумя графиками, длится около двадцати пяти секунд. На двенадцати секундах
# ожидания напоминания засчитывались неотправленными, хотя уходили следующим
# тиком — расхождение было в стенде, а не в боте
TICK_WAIT = 40.0


def _member_id(stand: Stand) -> int:
    row = stand.one("SELECT id FROM members ORDER BY id LIMIT 1")
    return int(row[0]) if row else 0


def _heartbeat_age(path: Path) -> float:
    return time.time() - path.stat().st_mtime if path.exists() else -1.0


async def run(stand: Stand) -> None:
    db = Path(settings.db_path)
    family_id = stand.family_id()
    child_id = stand.child_id()
    member_id = _member_id(stand)
    if not (family_id and child_id and member_id):
        raise RuntimeError("Нет семьи, ребёнка или участника — сначала акт 1")

    now = tu.now_utc()
    tz_row = stand.one("SELECT tz FROM families WHERE id = ?", (family_id,))
    tz = tz_row[0] if tz_row else settings.tz_default
    local_now = tu.to_local(now, tz)

    await stand.mark("Стенд: акт 2, всё что живёт по времени")

    async with stand.step("43", "догонка пропущенного в три ступени") as r:
        plan = seed.stage_overdue_reminders(db, family_id, member_id, now)
        for name, text in plan:
            r.note(f"{name}: {text}")
        await stand.settle(TICK_WAIT)
        r.expect("Стенд: свежее напоминание")
        r.expect("Стенд: опоздало на сорок минут")
        r.expect("Стенд: опоздало на двадцать часов")
        r.show(
            "отправлено",
            "SELECT text, sent_at, active FROM reminders WHERE text LIKE 'Стенд%'",
        )
        r.note("Ждём: свежее молча, сорок минут с припиской, двадцать часов сводкой")

    async with stand.step("44", "забытый таймер закрывается с пометкой") as r:
        seed.stage_stale_timer(db, child_id, member_id, now)
        opened = now - timedelta(hours=5)
        r.note(
            f"сон открыт в {opened:%H:%M} UTC, "
            f"порог {settings.open_timer_max_hours} ч"
        )
        await stand.settle(TICK_WAIT)
        r.show(
            "событие",
            "SELECT id, kind, started_at, ended_at, estimated FROM events "
            "WHERE kind = 'sleep' ORDER BY id DESC LIMIT 1",
        )
        r.note("Ждём estimated = 1: молча закрытый выглядел бы как измеренный")

    async with stand.step("45", "тревога по данным: мало мокрых подгузников") as r:
        left = seed.stage_few_wet_diapers(db, child_id, now)
        r.note(f"в скользящих сутках оставлено мокрых: {left}")
        await stand.settle(TICK_WAIT)
        r.show("alerts", "SELECT flag_id, raised_on FROM alerts")

    async with stand.step("46", "она же на следующем тике — молчит") as r:
        await stand.settle(TICK_WAIT)
        r.expect_silence()
        r.show("alerts", "SELECT flag_id, raised_on FROM alerts")
        r.note("Без отметки в БД семья получала бы это 1440 раз за сутки")

    async with stand.step("47", "тревога по данным: потеря веса") as r:
        value = seed.stage_weight_loss(db, child_id, member_id, now, 3420)
        r.note(f"свежее взвешивание {value} г при весе при рождении 3420 г")
        await stand.settle(TICK_WAIT)
        r.show("alerts", "SELECT flag_id, raised_on FROM alerts")

    await stand.mark("Дальше недельный дайджест")

    async with stand.step("48", "недельный дайджест по расписанию") as r:
        seed.stage_digest(db, family_id, local_now)
        r.note(f"дайджест переставлен на сегодня, {local_now:%H:%M} минус пять минут")
        await stand.settle(30.0)
        r.expect_kind("file")
        r.show("отметка", "SELECT last_digest_on FROM families WHERE id = ?", (family_id,))

    async with stand.step("49", "дайджест не повторяется на следующем тике") as r:
        await stand.settle(TICK_WAIT)
        r.expect_silence()

    async with stand.step("50", "панель за вчера перевыпускается тикером") as r:
        before = stand.panel_message_id()
        seed.stage_yesterday_panel(db, family_id, local_now.date())
        r.note(f"панель была #{before}, panel_day помечен вчерашним")
        await stand.settle(TICK_WAIT)
        after = stand.panel_message_id()
        r.note(f"панель стала #{after}")
        r.show("panel_day", "SELECT panel_day FROM families WHERE id = ?", (family_id,))
        if before is not None and after == before:
            stand.protocol.fail("шаг 50", "панель не перевыпущена, хотя помечена вчерашней")

    async with stand.step("51", "ежедневный бэкап и heartbeat") as r:
        backups = sorted(Path(settings.db_path).parent.joinpath("backups").glob("*.db"))
        for item in backups:
            r.note(f"снимок {item.name}, {item.stat().st_size} байт")
        if not backups:
            stand.protocol.fail("шаг 51", "ежедневный снимок не снят ни разу")
        age = _heartbeat_age(Path(settings.db_path).parent / "heartbeat")
        r.note(f"heartbeat обновлён {age:.0f} с назад")
        if age < 0 or age > 30:
            stand.protocol.fail("шаг 51", f"heartbeat не живой: {age:.0f} с")

    await stand.mark("Дальше периодический опрос")

    async with stand.step("52", "опрос созрел и пришёл карточкой") as r:
        seed.stage_checkin(db, family_id, 3, tu.now_utc())
        r.note("опрос включён на 3 ч, отметка отодвинута на 3 ч 1 мин назад")
        await stand.settle(TICK_WAIT)
        r.expect("Что было с прошлого раза")
        r.show(
            "опрос",
            "SELECT checkin_hours, checkin_last_at FROM families WHERE id = ?",
            (family_id,),
        )

    # Карточку тапаем по её собственному номеру, а не по «последнему
    # отправленному»: между шагами в чат уходят отбивки, и «последним» станет
    # отбивка. Номер берётся сразу после шага, до первого `mark`
    card_id = stand.session.last_message_id

    async with stand.step("53", "опрос не повторяется на следующем тике") as r:
        await stand.settle(TICK_WAIT)
        r.expect_silence()

    async with stand.step("54", "опрос молчит ночью") as r:
        previous_tz = seed.stage_checkin_night(db, family_id, tu.now_utc())
        try:
            seed.stage_checkin(db, family_id, 3, tu.now_utc())
            r.note(f"зона подменена на ту, где сейчас ночь; была {previous_tz}")
            await stand.settle(TICK_WAIT)
            r.expect_silence()
        finally:
            # Сначала гасим опрос, потом возвращаем зону: вернув зону при
            # созревшем опросе, мы дали бы ему выстрелить в те три секунды,
            # которые шаг досиживает перед вердиктом
            seed.stage_checkin(db, family_id, 0, tu.now_utc())
            seed.set_timezone(db, family_id, previous_tz)

    async with stand.step("55", "опрос молчит, если записи есть") as r:
        moment = tu.now_utc()
        seed.stage_checkin_silent(db, child_id, member_id, moment)
        seed.stage_checkin(db, family_id, 3, moment)
        r.note("подгузник пять минут назад — окно в три часа не пустое")
        await stand.settle(TICK_WAIT)
        r.expect_silence()

    async with stand.step("56", "три тапа по карточке пишут сон задним числом") as r:
        if card_id is None:
            raise RuntimeError("Карточка опроса не дошла — нечего нажимать")
        await stand.tap(CheckinCB(action="kind", kind=SLEEP).pack(), on=card_id)
        await stand.tap(CheckinCB(action="when", kind=SLEEP, back=60).pack(), on=card_id)
        await stand.tap(
            CheckinCB(action="dur", kind=SLEEP, back=60, mins=30).pack(), on=card_id
        )
        r.expect("Сон")
        r.show(
            "последний сон",
            "SELECT started_at, ended_at, estimated FROM events "
            "WHERE kind = ? ORDER BY id DESC LIMIT 1",
            (SLEEP,),
        )
        r.note("ждём estimated = 0: длительность назвал человек, а не тикер")
        r.note("ждём начало час назад и конец через полчаса после начала")

    async with stand.step("57", "«Не спрашивать» выключает опрос и говорит вслух") as r:
        await stand.tap(CheckinCB(action="mute").pack(), on=card_id)
        r.expect("спрашивать не буду")
        r.show("опрос", "SELECT checkin_hours FROM families WHERE id = ?", (family_id,))

    await stand.mark("Акт 2 закончен")
