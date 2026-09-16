"""Акт 1 — всё, что отвечает сразу.

Тикер здесь не запускается намеренно: в этом акте проверяется реакция на
нажатие, и фоновой цикл добавил бы в протокол события, которых никто не
вызывал. Всё временнóе — во втором акте, там тикер и работает.

Ребёнок заводится мастером вживую, мальчиком и девяти недель от роду. Возраст
выбран не наугад: старше 60 дней — сторож перерыва в кормлениях молчит,
младше 90 — порог температуры 37.5 для младенцев ещё действует, впереди
календарь прививок на три месяца, и `weeks.yaml` на этом возрасте содержателен.
Мальчиком — чтобы известное и не тронутое («Родилась» безусловно в
`texts.baby_card`) попало в протокол фактом, а не осталось строчкой в летописи.
"""

from __future__ import annotations

from datetime import timedelta

from bot import keyboards
from bot.db.models import (
    DIAPER_DIRTY,
    DIAPER_WET,
    FEED_BOTTLE,
    FEED_BREAST,
    PUMP,
    SLEEP,
)
from bot.keyboards import (
    AskCB,
    EditCB,
    MeasureCB,
    RemindCB,
    SexCB,
    StatsCB,
    TrackCB,
    UndoCB,
)
from bot.services import timeutil as tu
from tools.stand import seed
from tools.stand.harness import Stand

BORN_DAYS_AGO = 63
BIRTH_WEIGHT_G = 3420


def _last_event_id(stand: Stand) -> int:
    row = stand.one("SELECT max(id) FROM events")
    return int(row[0]) if row and row[0] else 0


def _last_measurement_id(stand: Stand) -> int:
    row = stand.one("SELECT max(id) FROM measurements")
    return int(row[0]) if row and row[0] else 0


async def run(stand: Stand) -> None:
    tz = "Asia/Yekaterinburg"
    born = tu.to_local(tu.now_utc(), tz) - timedelta(days=BORN_DAYS_AGO)
    born_text = born.strftime("%d.%m.%Y %H:%M")

    await stand.mark("Стенд: акт 1, всё что отвечает сразу")

    # --- знакомство ---------------------------------------------------

    async with stand.step("0", "бота добавили в группу: приветствие и клавиатура") as r:
        await stand.join()
        r.expect("/baby")
        r.expect_keyboard()
        r.note("Клавиатура обязана доехать до первой команды, а не после неё")
        r.note("Что шаг НЕ проверяет: что Telegram присылает my_chat_member —")
        r.note("это про allowed_updates, и закрыто юнит-сторожем")

    async with stand.step("1", "первый апдейт из группы: семья заводится сама") as r:
        await stand.say("/ping")
        r.expect("Живой")
        r.show("families", "SELECT id, chat_id, title, tz FROM families")
        r.show("members", "SELECT id, tg_user_id, display_name FROM members")
        r.note("Он же шаг 2: /ping в живом чате — открытый пункт этапа 0")

    async with stand.step("3", "/help и /panel до карточки ребёнка") as r:
        await stand.say("/help")
        await stand.say("/panel")
        r.expect("/baby")
        r.expect("Кнопки под полем ввода")
        r.show("панель", "SELECT panel_message_id, panel_day FROM families")
        r.expect_keyboard()

    async with stand.step("4", "тап по кнопке записи до карточки") as r:
        # Кнопок на панели сейчас нет вовсе, и это правильно. Нажатие
        # синтезируем нарочно: так выглядит тап по вчерашней панели
        await stand.tap(TrackCB(action="add", kind=DIAPER_WET).pack())
        r.expect("/baby")
        r.note("Ждём всплывашку NO_CHILD, а не тишину")

    # --- мастер ---------------------------------------------------------

    async with stand.step("5а", "мастер /baby: имя и пол") as r:
        await stand.say("/baby")
        await stand.say("Тимофей")
        r.expect("Девочка или мальчик")

    async with stand.step("5б", "мастер /baby: кнопка «Мальчик»") as r:
        await stand.tap(SexCB(value="m").pack(), on="last")
        r.expect("родил")

    async with stand.step("5в", "мастер /baby: мусор в дате отбивается") as r:
        await stand.say("вчера вечером")
        r.expect_absent("Вес при рождении")

    async with stand.step("5г", "мастер /baby: дата из будущего отбивается") as r:
        future = (born + timedelta(days=BORN_DAYS_AGO * 2)).strftime("%d.%m.%Y")
        await stand.say(future)
        r.expect("не наступила")

    async with stand.step("5д", "мастер /baby: верная дата, затем вес 300 г") as r:
        await stand.say(born_text)
        await stand.say("300")
        r.expect("Вес при рождении")

    async with stand.step("5е", "мастер /baby: вес 3420") as r:
        await stand.say(str(BIRTH_WEIGHT_G))
        r.expect("Панель ниже")
        r.expect_keyboard()
        r.note("С «Готово» едет клавиатура — она же ехала с приветствием")
        r.show(
            "children",
            "SELECT id, name, sex, born_at, birth_weight_g FROM children",
        )
        r.show(
            "точка веса при рождении",
            "SELECT id, kind, taken_at, value FROM measurements",
        )

    async with stand.step("6", "команда посреди правки обрывает её вслух") as r:
        await stand.say("/baby")
        await stand.tap(EditCB(field="name").pack(), on="last")
        await stand.say("/today")
        r.expect("отменил")

    async with stand.step("6б", "ПОДГОТОВКА: две недели истории прямо в базу") as r:
        # Это не живой ввод, и в протоколе он помечен как подготовка. На пустой
        # неделе /week, /chart, /export и дайджест показали бы прочерки и не
        # доказали бы ничего — материал нужен до того, как их спрашивать
        from bot.db.session import Session

        child_id = stand.child_id()
        member_id = stand.one("SELECT id FROM members ORDER BY id LIMIT 1")
        async with Session() as session:
            counts = await seed.history(
                session, int(child_id or 0), int(member_id[0] if member_id else 0),
                now=tu.now_utc(),
            )
        r.note(f"досыпано: {counts}")
        r.show("событий всего", "SELECT count(*) FROM events")

    # --- запись событий ---------------------------------------------------

    async with stand.step("7", "кормление: «Левая», затем «Закончить»") as r:
        await stand.tap(TrackCB(action="start", kind=FEED_BREAST, side="L").pack())
        await stand.settle(2.0)
        await stand.tap(TrackCB(action="stop", kind=FEED_BREAST).pack())
        r.expect("Пошло время", "Записал")
        r.show(
            "событие",
            "SELECT id, kind, side, started_at, ended_at, estimated FROM events "
            "ORDER BY id DESC LIMIT 1",
        )

    async with stand.step("8", "«Левая» при идущем таймере закрывает предыдущий") as r:
        await stand.tap(TrackCB(action="start", kind=FEED_BREAST, side="L").pack())
        await stand.settle(1.0)
        await stand.tap(TrackCB(action="start", kind=FEED_BREAST, side="R").pack())
        r.expect("Пошло время")
        r.expect("Закрыл предыдущее")
        r.show(
            "два последних",
            "SELECT id, side, ended_at FROM events ORDER BY id DESC LIMIT 2",
        )
        r.note("Ждём: предыдущий закрыт, новый открыт, ругани нет")
        r.note("«Пошло время» одного мало: эта всплывашка была и когда бот молчал")

    async with stand.step("9", "двойной «Закончить»") as r:
        await stand.tap(TrackCB(action="stop", kind=FEED_BREAST).pack())
        await stand.settle(1.0)
        await stand.tap(TrackCB(action="stop", kind=FEED_BREAST).pack())
        r.expect("Таймер уже закрыт")

    async with stand.step("9б", "случайное «Закончить» обратимо") as r:
        # Ради этого шага правка и делалась: до неё под ответом об окончании
        # стояла одна кнопка отмены, и она удаляла запись целиком — промах по
        # окончанию сна лечился потерей всего сна.
        #
        # Вердикт выносится ПО БАЗЕ, а не по протоколу, и это не перестраховка.
        # Inline-разметку протокол не пишет вовсе, так что «кнопка есть» им не
        # доказать; а `tap(on="last")` адресует вслепую — соседний акт уже
        # получил ложно-зелёный шаг ровно на этом. Если тап уедет не туда,
        # `ended_at` не обнулится и шаг покраснеет.
        await stand.tap(TrackCB(action="start", kind=SLEEP).pack())
        await stand.settle(1.5)
        doomed = _last_event_id(stand)
        await stand.tap(TrackCB(action="stop", kind=SLEEP).pack())
        await stand.settle(1.5)
        closed = stand.one(
            "SELECT ended_at FROM events WHERE id = ?", (doomed,)
        )
        r.note(f"закрыт: {closed}")
        await stand.tap(UndoCB(what="reopen", row_id=doomed).pack(), on="last")
        await stand.settle(1.5)
        r.expect("снова идёт")
        r.show(
            "тот же сон",
            "SELECT id, kind, ended_at, estimated FROM events WHERE id = ?",
            (doomed,),
        )
        again = stand.one(
            "SELECT ended_at FROM events WHERE id = ?", (doomed,)
        )
        if again is None:
            stand.protocol.fail("шаг 9б", "запись исчезла — это удаление, а не возврат")
        elif again[0] is not None:
            stand.protocol.fail("шаг 9б", "таймер не вернулся: ended_at на месте")
        r.note("Та же запись, не новая: id совпадает с закрытым")

    async with stand.step("9в", "вернуть ход нельзя поверх чужого таймера") as r:
        # Инвариант «ровно один открытый таймер» сильнее удобства: человек
        # просил вернуть один, а не оборвать другой
        await stand.tap(TrackCB(action="stop", kind=SLEEP).pack())
        await stand.settle(1.5)
        sleep_id = _last_event_id(stand)
        await stand.tap(TrackCB(action="start", kind=PUMP).pack())
        await stand.settle(1.5)
        await stand.tap(UndoCB(what="reopen", row_id=sleep_id).pack(), on="panel")
        r.expect("идёт другой таймер")
        r.show(
            "оба",
            "SELECT id, kind, ended_at FROM events ORDER BY id DESC LIMIT 2",
        )
        r.note("Ждём: сцеживание идёт, сон так и остался закрытым")

    async with stand.step("9г", "убираем сцеживание, чтобы не мешало дальше") as r:
        await stand.tap(TrackCB(action="stop", kind=PUMP).pack())
        r.expect("Записал")

    async with stand.step("10", "сон: начали и закончили") as r:
        await stand.tap(TrackCB(action="start", kind=SLEEP).pack())
        await stand.settle(1.0)
        await stand.tap(TrackCB(action="stop", kind=SLEEP).pack())
        r.expect("Сон")

    async with stand.step("11", "сцеживание: после «Закончить» спрашивают мл") as r:
        await stand.tap(TrackCB(action="start", kind=PUMP).pack())
        await stand.settle(1.0)
        await stand.tap(TrackCB(action="stop", kind=PUMP).pack())
        r.expect("Сколько мл сцедили")

    async with stand.step("12", "ответ числом 120 попадает в ту же запись") as r:
        await stand.say("120")
        r.expect("120")
        r.show(
            "сцеживание",
            "SELECT id, kind, value_num FROM events WHERE kind = ? "
            "ORDER BY id DESC LIMIT 1",
            (PUMP,),
        )

    async with stand.step("13", "бутылочка: вопрос есть, ответа нет — и это норма") as r:
        await stand.tap(TrackCB(action="add", kind=FEED_BOTTLE).pack())
        r.expect("Сколько мл")
        r.show(
            "бутылочка",
            "SELECT id, kind, value_num, ended_at FROM events WHERE kind = ? "
            "ORDER BY id DESC LIMIT 1",
            (FEED_BOTTLE,),
        )
        r.note("Ждём запись без объёма: промолчать здесь допустимо")

    async with stand.step("14", "подгузники: мокрый и стул") as r:
        await stand.tap(TrackCB(action="add", kind=DIAPER_WET).pack())
        await stand.settle(1.0)
        await stand.tap(TrackCB(action="add", kind=DIAPER_DIRTY).pack())
        r.show(
            "подгузники",
            "SELECT id, kind, started_at, ended_at FROM events "
            "WHERE kind LIKE 'diaper%' ORDER BY id DESC LIMIT 2",
        )
        r.note("Ждём пустой ended_at — это момент, а не отрезок")

    async with stand.step("15", "отмена записи, затем повторный тап по ней же") as r:
        doomed = _last_event_id(stand)
        await stand.tap(UndoCB(what="event", row_id=doomed).pack(), on="last")
        await stand.settle(1.0)
        await stand.tap(UndoCB(what="event", row_id=doomed).pack(), on="last")
        r.expect("удал")
        r.show("удалённое", "SELECT id FROM events WHERE id = ?", (doomed,))
        r.note("Ждём: первый тап удалил, второй сказал «уже удалили»")

    # --- вес и центили ----------------------------------------------------

    async with stand.step("16", "вес кнопкой: ⚖️ Вес, затем «5,1 кг»") as r:
        await stand.tap(MeasureCB(kind="weight").pack())
        await stand.settle(1.0)
        await stand.say("5,1 кг")
        r.expect("5")
        r.show(
            "измерение",
            "SELECT id, kind, taken_at, value FROM measurements ORDER BY id DESC LIMIT 1",
        )
        r.note("Ждём центиль от таблиц ВОЗ, а не процент от веса при рождении")

    async with stand.step("17", "вес отменяется той же кнопкой") as r:
        doomed = _last_measurement_id(stand)
        await stand.tap(UndoCB(what="measure", row_id=doomed).pack(), on="last")
        r.expect("удал")

    async with stand.step("18", "/weight 5100 записывает сразу") as r:
        await stand.say("/weight 5100")
        r.expect("5")

    async with stand.step("19", "негодный вес: 30000, буквы и пограничные 400 г") as r:
        await stand.say("/weight 30000")
        await stand.say("/weight абракадабра")
        # 400 г — нижняя граница MIN_G, то есть по своему правилу бот их примет.
        # Проверяем не отказ, а то, ЧТО он скажет девятинедельному ребёнку
        await stand.say("/weight 400")
        r.show(
            "последнее измерение",
            "SELECT id, value FROM measurements ORDER BY id DESC LIMIT 1",
        )
        r.note("Ждём два отказа и один принятый — граница MIN_G = 400")

    async with stand.step("19б", "пограничные 400 г убираем, чтобы не портить график") as r:
        doomed = _last_measurement_id(stand)
        await stand.tap(UndoCB(what="measure", row_id=doomed).pack(), on="last")
        r.expect("удал")

    async with stand.step("20", "голое число без ожидания — тишина") as r:
        await stand.say("4700")
        r.expect_silence()
        r.note("Бот не должен мешать переписке родителей")

    # --- постоянная клавиатура под полем ввода ----------------------------
    #
    # Нажатие reply-кнопки и напечатанная та же строка — для бота один и тот же
    # апдейт, поэтому `say` здесь не упрощение, а точная модель. Чего стенд
    # проверить НЕ может: отрисовку самой клавиатуры на экране — он видит
    # только `extra["keyboard"]` в шаге 3.

    async with stand.step("20а", "кнопка «Панель»: панель заново и нажатие убрано") as r:
        before = stand.panel_message_id()
        await stand.say(keyboards.HOME_PANEL)
        await stand.settle(1.0)
        after = stand.panel_message_id()
        r.expect_kind("delete")
        r.note(f"панель {before} -> {after}")
        if before == after:
            stand.protocol.fail("шаг 20а", "панель не выпущена заново")
        r.note("Если шаг красный на delete — значит у бота нет права удалять")

    async with stand.step("20а2", "обычный ответ несёт клавиатуру") as r:
        # Шаг, которого не хватило 15.09. Не /help, не мастер — рядовой ответ
        # бота, каких у семьи десятки в день. Ровно на них клавиатура и не
        # ехала, а протокол был зелёным
        await stand.say("/ping")
        r.expect("Живой")
        r.expect_keyboard()

    async with stand.step("20а3", "вопрос после тапа несёт, подтверждение — нет") as r:
        # Два сообщения из одного `track._say`, разведённые `markup or home()`:
        # у подтверждения своя кнопка отмены, у вопроса — клавиатура
        await stand.tap(TrackCB(action="add", kind=FEED_BOTTLE).pack())
        r.expect("Сколько мл")
        r.expect_keyboard()
        r.note("Какое из двух сообщений какую разметку несёт — видно построчно")

    async with stand.step("20б", "кнопка «Вопрос» ждёт вопрос следующей репликой") as r:
        await stand.say(keyboards.HOME_ASK)
        r.expect("Напишите вопрос")
        r.expect_kind("delete")

    async with stand.step("20в", "кнопка «Сутки» отвечает тем же, чем /today") as r:
        await stand.say(keyboards.HOME_TODAY)
        r.expect("сегодня")
        r.expect_kind("delete")
        r.note("Ожидание вопроса с шага 20б снято не будет — истечёт само")

    # --- сводки и графики -------------------------------------------------

    async with stand.step("21", "/today и переключение периодов") as r:
        await stand.say("/today")
        await stand.settle(1.0)
        first = stand.session.last_message_id
        await stand.tap(StatsCB(period="week").pack(), on=first or "last")
        await stand.settle(1.0)
        await stand.tap(StatsCB(period="month").pack(), on=first or "last")
        r.expect_kind("edit")
        r.note("Ждём правку того же сообщения, а не три новых")

    async with stand.step("22", "/week с разбивкой по дням") as r:
        await stand.say("/week")
        r.expect("По дням")

    async with stand.step("23", "/stage — что происходит на этой неделе") as r:
        await stand.say("/stage")
        r.expect("75")
        r.note("Ждём оговорку про 75% рядом с вехами CDC")

    await stand.mark("Дальше графики — смотрите глазами")

    async with stand.step("24", "/chart — вес с коридорами ВОЗ и ритм суток") as r:
        await stand.say("/chart")
        await stand.settle(6.0)
        r.expect_kind("file")
        r.note("Два PNG: сверить глазами коридоры и палитру")

    # --- напоминания ------------------------------------------------------

    async with stand.step("25", "/remind: экран пресетов") as r:
        await stand.say("/remind")
        r.expect("апомин")

    async with stand.step("26", "кормление: выбор интервала, затем 3 ч") as r:
        screen = stand.session.last_message_id
        await stand.tap(RemindCB(action="pick", preset="feed").pack(), on=screen or "last")
        await stand.settle(1.5)
        await stand.tap(
            RemindCB(action="on", preset="feed", hours=3).pack(), on="last"
        )
        r.show(
            "напоминание",
            "SELECT id, preset, fire_at, rrule, active FROM reminders WHERE preset = 'feed'",
        )

    async with stand.step("27", "прививки: пачка разовых по календарю") as r:
        await stand.say("/remind")
        await stand.settle(1.5)
        screen = stand.session.last_message_id
        await stand.tap(
            RemindCB(action="on", preset="vaccine").pack(), on=screen or "last"
        )
        r.show(
            "заведено",
            "SELECT count(*), min(fire_at), max(fire_at) FROM reminders "
            "WHERE preset = 'vaccine' AND active = 1",
        )
        r.show(
            "в прошлом",
            "SELECT count(*) FROM reminders WHERE preset = 'vaccine' "
            "AND fire_at < datetime('now')",
        )
        r.note("Ждём ноль в прошлом: роддомовские записи напоминаний не порождают")

    async with stand.step("28", "календарь прививок и переключение на осмотры") as r:
        await stand.say("/remind")
        await stand.settle(1.5)
        screen = stand.session.last_message_id
        await stand.tap(RemindCB(action="cal", preset="vaccine").pack(), on=screen or "last")
        await stand.settle(1.5)
        await stand.tap(RemindCB(action="cal", preset="checkup").pack(), on=screen or "last")
        r.expect_kind("edit")
        r.note("Ждём стрелку на ближайшей дате и значок роддома")

    async with stand.step("29", "выключение прививок гасит ВСЕ строки") as r:
        await stand.say("/remind")
        await stand.settle(1.5)
        screen = stand.session.last_message_id
        await stand.tap(
            RemindCB(action="off", preset="vaccine").pack(), on=screen or "last"
        )
        r.show(
            "осталось живых",
            "SELECT count(*) FROM reminders WHERE preset = 'vaccine' AND active = 1",
        )
        r.note("Ждём ноль: reminder_by_preset гасит одну, нужна deactivate_preset")

    async with stand.step("30", "правка даты рождения гасит календарь вслух") as r:
        await stand.say("/remind")
        await stand.settle(1.5)
        screen = stand.session.last_message_id
        await stand.tap(
            RemindCB(action="on", preset="checkup").pack(), on=screen or "last"
        )
        await stand.settle(2.0)
        await stand.say("/baby")
        await stand.settle(1.5)
        await stand.tap(EditCB(field="born").pack(), on="last")
        await stand.settle(1.5)
        moved = (born + timedelta(days=1)).strftime("%d.%m.%Y %H:%M")
        await stand.say(moved)
        r.expect("выключил")
        r.show(
            "осмотры после правки",
            "SELECT count(*) FROM reminders WHERE preset = 'checkup' AND active = 1",
        )
        r.show(
            "точка веса при рождении",
            "SELECT taken_at, value FROM measurements ORDER BY taken_at LIMIT 1",
        )
        r.note("Точка веса обязана переехать вместе с датой")

    # --- красные флаги ----------------------------------------------------

    async with stand.step("31", "/redflags — весь список") as r:
        await stand.say("/redflags")
        r.expect("103")

    async with stand.step("32", "«у неё судороги!!!» — скорая") as r:
        await stand.say("у неё судороги!!!")
        r.expect("103")

    async with stand.step("33", "«температура 38.2» у младенца") as r:
        await stand.say("температура 38.2")
        r.expect("103")

    async with stand.step("34", "«температуры нет» — отрицание подавляет") as r:
        await stand.say("температуры нет, всё хорошо")
        r.expect_silence()

    async with stand.step("35", "«Температура нормальная, съела 40 мл» — молчит") as r:
        await stand.say("Температура нормальная, съела 40 мл")
        r.expect_silence()
        r.note("Боевой баг из летописи: чужая единица рядом с числом — не температура")

    # --- вопросы ----------------------------------------------------------

    async with stand.step("36", "❓ Вопрос → вопрос по базе знаний") as r:
        await stand.tap(AskCB().pack())
        await stand.settle(1.5)
        await stand.say("сколько раз в день менять подгузник")
        r.note("Без ключей LLM ждём режим цитат: источник и ссылка")

    async with stand.step("37", "❓ Вопрос → тревожный признак перехватывает alarm") as r:
        await stand.tap(AskCB().pack())
        await stand.settle(1.5)
        await stand.say("у неё судороги")
        await stand.settle(1.5)
        # Ожидание обязано быть снято: следующая реплика не должна уехать в поиск
        await stand.say("звоню 103, одеваемся")
        r.expect("103")
        r.note("Вторая реплика не должна попасть в базу знаний")

    async with stand.step("38", "ожидание числа: голое число доходит до measure") as r:
        await stand.tap(MeasureCB(kind="weight").pack())
        await stand.settle(1.5)
        await stand.say("5150")
        r.expect("5")
        r.show(
            "измерение",
            "SELECT id, value FROM measurements ORDER BY id DESC LIMIT 1",
        )
        r.note("Тот самый стык: ожидание — фильтр, а не проверка внутри хендлера")

    # --- панель, выгрузка, личка ------------------------------------------

    async with stand.step("39", "панель уехала на 16 сообщений — перевыпуск") as r:
        before = stand.panel_message_id()
        r.note(f"панель была #{before}")
        for i in range(16):
            await stand.bot.send_message(stand.chat.id, f"<i>· {i + 1} ·</i>")
        # Тапом по панели смещение не проверить: `_last_id(call)` берёт
        # message_id самой панели, и он всегда равен её собственному. Порог
        # живёт на другом пути — когда panel.schedule получает message_id
        # реплики человека
        await stand.tap(MeasureCB(kind="weight").pack())
        await stand.settle(1.5)
        await stand.say("5200")
        await stand.settle(3.0)
        after = stand.panel_message_id()
        r.note(f"панель стала #{after}")
        if before is not None and after is not None and after == before:
            stand.protocol.fail("шаг 39", "панель не перевыпущена, хотя уехала")

    await stand.mark("Дальше выгрузка — откройте файлы")

    async with stand.step("40", "/export — CSV и PDF") as r:
        await stand.say("/export")
        await stand.settle(8.0)
        r.expect_kind("file")

    async with stand.step("41", "/backup — копия базы файлом") as r:
        await stand.say("/backup")
        await stand.settle(5.0)
        r.expect_kind("file")

    async with stand.step("42", "личка боту — отказ") as r:
        await stand.say("привет", private=True)
        r.expect("группу")

    await stand.mark("Акт 1 закончен")
