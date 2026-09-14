"""Акт 3 — доводка того, что не сошлось во втором акте.

Два шага, оба про мою же ошибку в подготовке, а не про бота:

* сторож потери веса судит по ПОСЛЕДНЕМУ взвешиванию, а подложенное назад во
  времени оказалось старше живого из акта 1;
* тик, отправляющий дайджест с двумя графиками, длится около двадцати пяти
  секунд — двенадцати секунд ожидания не хватало, и напоминания засчитывались
  неотправленными, хотя уходили следующим тиком.
"""

from __future__ import annotations

from pathlib import Path

from bot.config import settings
from bot.services import timeutil as tu
from tools.stand import seed
from tools.stand.harness import Stand

TICK_WAIT = 40.0


async def run(stand: Stand) -> None:
    db = Path(settings.db_path)
    family_id = stand.family_id()
    child_id = stand.child_id()
    row = stand.one("SELECT id FROM members ORDER BY id LIMIT 1")
    member_id = int(row[0]) if row else 0
    if not (family_id and child_id and member_id):
        raise RuntimeError("Нет семьи, ребёнка или участника — сначала акт 1")

    now = tu.now_utc()
    birth = stand.one("SELECT birth_weight_g FROM children WHERE id = ?", (child_id,))
    birth_weight = int(birth[0]) if birth and birth[0] else 3420

    await stand.mark("Стенд: акт 3, доводка")

    async with stand.step("47б", "тревога по данным: потеря веса") as r:
        value = seed.stage_weight_loss(db, child_id, member_id, now, birth_weight)
        percent = (birth_weight - value) / birth_weight * 100
        r.note(f"взвешивание {value} г при {birth_weight} г — потеря {percent:.1f}%")
        r.note("порог weight_loss_percent = 10, свежесть не старше 3 дней")
        r.show(
            "последнее взвешивание",
            "SELECT id, taken_at, value FROM measurements ORDER BY taken_at DESC LIMIT 1",
        )
        await stand.settle(TICK_WAIT)
        r.expect("ниже веса при рождении")
        r.show("alerts", "SELECT flag_id, raised_on FROM alerts")

    async with stand.step("47в", "она же на следующем тике — молчит") as r:
        await stand.settle(TICK_WAIT)
        r.expect_silence()
        r.show("alerts", "SELECT flag_id, raised_on FROM alerts")

    await stand.mark("Акт 3 закончен")
