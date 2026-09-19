"""Карточка ребёнка: мастер `/baby`.

Четыре шага — имя, пол, момент рождения, вес при рождении. Меньше нельзя: без
пола и даты не посчитать ни одного центиля, а без веса при рождении не увидеть
потерю массы в первые дни, которая и есть главный повод для тревоги на первой
неделе.

Состояние живёт в MemoryStorage и умирает вместе с процессом. Это осознанно:
мастер проходят один раз за жизнь бота, и переживший рестарт наполовину
заполненный черновик принёс бы больше путаницы, чем пользы.
"""

import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.db import repo
from bot.db.models import WEIGHT, Child, Family, Member
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.keyboards import EditCB, SexCB, StartCB
from bot.services import panel, schedule, sending
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

router = Router(name="baby")

# Правдоподобные границы веса новорождённого в граммах. Не медицинская норма, а
# защита от опечатки: «356» вместо «3560» и «35600» вместо «3560» — обычные
# промахи, и оба дали бы дикий центиль вместо честного «проверьте число»
MIN_BIRTH_G = 400
MAX_BIRTH_G = 7000


class New(StatesGroup):
    name = State()
    sex = State()
    born = State()
    weight = State()


class Edit(StatesGroup):
    """Правка уже заведённой карточки.

    Отдельная группа состояний, а не переиспользование `New`: у мастера шаги
    идут цепочкой, а правка меняет ровно одно поле и заканчивается. Свалив их
    в одну группу, мы бы получили правку имени, которая продолжается вопросом
    про пол.
    """

    name = State()
    sex = State()
    born = State()
    weight = State()


def parse_moment(text: str, tz: str) -> datetime | None:
    """'13.09.2026' или '13.09.2026 04:20' → наивный UTC.

    Без `dateparser` намеренно: дату рождения вводят один раз, и «позавчера
    вечером» здесь не нужно, а вот принять `13.09.26` за `2026` и ошибиться
    на век — вполне реально. Строгий формат честнее.
    """
    cleaned = " ".join(text.split())
    for pattern in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            local = datetime.strptime(cleaned, pattern)
        except ValueError:
            continue
        return tu.to_utc(local, tz)
    return None


def parse_grams(text: str) -> int | None:
    """'3420', '3.42', '3,42 кг' → граммы.

    Килограммы принимаем потому, что весы дома показывают именно их, а роддом
    пишет граммы. Разделять по величине числа безопасно: 3.42 не может быть
    граммами, а 3420 не может быть килограммами.
    """
    cleaned = text.replace(",", ".").replace("кг", "").replace("г", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    grams = int(round(value * 1000)) if value < 100 else int(round(value))
    return grams if MIN_BIRTH_G <= grams <= MAX_BIRTH_G else None


async def _open(
    message: Message, state: FSMContext, child: Child | None, family: Family
) -> None:
    """Показать карточку или начать мастер, если её ещё нет.

    Тело общее у команды `/baby` и у кнопки из приветствия, и общее оно не ради
    экономии строк: кнопка живёт в истории чата неограниченно долго, и тап по
    годовалому приветствию обязан ответить осмысленно. Здесь он отвечает тем
    же, чем `/baby`, — показывает готовую карточку.

    **`state.clear()`, а не голый `set_state`.** Тап посреди мастера иначе
    вернул бы на первый шаг, но оставил бы в `state.get_data()` половину
    прежнего черновика — и «Готово» собрало бы карточку из двух разных заходов.
    Отдельного «предыдущий ввод отменил» не нужно: ответом служит «Как зовут?»,
    и это и есть видимое начало сначала.
    """
    if child is not None:
        await message.answer(
            texts.baby_card(child, tu.now_utc(), family.tz),
            reply_markup=keyboards.edit_child(),
        )
        return
    await state.clear()
    await state.set_state(New.name)
    await message.answer(texts.BABY_ASK_NAME, reply_markup=keyboards.home())


@router.message(IN_GROUP, Command("baby"))
async def baby(
    message: Message, state: FSMContext, child: Child | None, family: Family
) -> None:
    await _open(message, state, child, family)


@router.callback_query(IN_GROUP_CB, StartCB.filter())
async def start_from_button(
    call: CallbackQuery, state: FSMContext, child: Child | None, family: Family
) -> None:
    """Кнопка «Завести карточку» из приветствия — первый шаг в один тап.

    До неё вход в мастер был один: набрать `/baby` руками. Для семьи, которая
    только что добавила бота и ещё не знает ни одной его команды, это шаг,
    который должен выполнить человек, — то есть дыра.

    Состояние FSM снимает `_open`, а не middleware: `drop_wizard_state` висит на
    `dp.message` и колбэка не видит вовсе. Искать ветку там, по аналогии с
    метками постоянной клавиатуры, бесполезно.
    """
    await call.answer()
    if not isinstance(call.message, Message):
        return
    await _open(call.message, state, child, family)


@router.message(New.name, F.text)
async def got_name(message: Message, state: FSMContext) -> None:
    name = " ".join((message.text or "").split())[:64]
    if not name:
        await message.answer(texts.BABY_ASK_NAME, reply_markup=keyboards.home())
        return
    await state.update_data(name=name)
    await state.set_state(New.sex)
    await message.answer(texts.BABY_ASK_SEX, reply_markup=keyboards.sex())


@router.callback_query(IN_GROUP_CB, New.sex, SexCB.filter())
async def got_sex(
    call: CallbackQuery, callback_data: SexCB, state: FSMContext
) -> None:
    await state.update_data(sex=callback_data.value)
    await state.set_state(New.born)
    await call.answer()
    if isinstance(call.message, Message):
        await call.message.answer(texts.BABY_ASK_BORN, reply_markup=keyboards.home())


@router.message(New.born, F.text)
async def got_born(message: Message, state: FSMContext, family: Family) -> None:
    moment = parse_moment(message.text or "", family.tz)
    if moment is None:
        await message.answer(texts.BABY_BAD_DATE, reply_markup=keyboards.home())
        return
    if moment > tu.now_utc():
        # Отдельная проверка, а не общий «не понял»: перепутанный год — самая
        # частая опечатка, и сказать об этом прямо дешевле, чем заставлять
        # человека гадать, что не так с форматом
        await message.answer(texts.BABY_FUTURE_DATE, reply_markup=keyboards.home())
        return
    await state.update_data(born=moment.isoformat())
    await state.set_state(New.weight)
    await message.answer(texts.BABY_ASK_WEIGHT, reply_markup=keyboards.home())


@router.message(New.weight, Command("skip"))
async def skip_weight(
    message: Message, state: FSMContext, session: AsyncSession,
    family: Family, member: Member, bot: Bot,
) -> None:
    await _finish(message, state, session, family, member, bot, grams=None)


@router.message(New.weight, F.text)
async def got_weight(
    message: Message, state: FSMContext, session: AsyncSession,
    family: Family, member: Member, bot: Bot,
) -> None:
    grams = parse_grams(message.text or "")
    if grams is None:
        await message.answer(texts.BABY_BAD_WEIGHT, reply_markup=keyboards.home())
        return
    await _finish(message, state, session, family, member, bot, grams=grams)


async def _finish(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
    *,
    grams: int | None,
) -> None:
    data = await state.get_data()
    await state.clear()

    born = datetime.fromisoformat(data["born"])
    child = await repo.create_child(
        session,
        family.id,
        name=data["name"],
        sex=data["sex"],
        born_at=born,
        birth_weight_g=grams,
    )

    # Вес при рождении заводим и как измерение: иначе первая точка на графике
    # появилась бы только после первого домашнего взвешивания, и увидеть
    # потерю массы первых дней было бы не на чем
    if grams is not None:
        await repo.add_measurement(
            session, child.id, member.id, WEIGHT, taken_at=born, value=float(grams)
        )

    log.info("Заведён ребёнок #%s в семье #%s", child.id, family.id)
    # Клавиатура едет вместе с «Готово»: мастер проходят обязательно — панель
    # без карточки сама зовёт в `/baby`, — значит это надёжнейший первый
    # контакт новой семьи с кнопками под полем ввода
    await message.answer(texts.BABY_SAVED, reply_markup=keyboards.home())
    await panel.refresh(bot, family.id, force_new=True)

    # Единственное за всю жизнь семьи напоминание о правах, и единственный за ту
    # же жизнь зонд. «Один раз» здесь получается не отметкой в базе, а
    # устройством: мастер проходят однажды — ровно по этой причине в нём вообще
    # оставлен FSM. Отсюда же место: ни ветки в тикере, ни риска напомнить на
    # каждом тике.
    #
    # Зонд стоит ПОСЛЕ подтверждения и панели: это сетевой запрос через прокси,
    # и ставить его перед ответом в самый важный момент онбординга значило бы
    # нарушить «ответ человеку уходит раньше, чем перерисовывается панель».
    # Заодно напоминание ложится прямо под незакреплённой панелью — под
    # собственным доказательством.
    #
    # `None` — «спросить не удалось», и на нём молчим: напоминать про права из-за
    # сетевого сбоя хуже, чем не напомнить вовсе
    if await sending.is_admin(bot, family) is False:
        await message.answer(texts.ADMIN_REMINDER, reply_markup=keyboards.home())


# --- Правка карточки ------------------------------------------------------
#
# Мастер проходят один раз и в худших условиях, какие бывают: первые часы
# после родов, не выспавшись, одной рукой. Опечатка в моменте рождения тянет
# за собой все центили и порог «младше трёх месяцев» в триаже, поэтому
# чинить её надо из чата, а не из SQL.

# Что спросить, когда человек выбрал поле
_EDIT_PROMPT = {
    "name": texts.BABY_EDIT_ASK_NAME,
    "sex": texts.BABY_EDIT_ASK_SEX,
    "born": texts.BABY_EDIT_ASK_BORN,
    "weight": texts.BABY_EDIT_ASK_WEIGHT,
}

_EDIT_STATE = {
    "name": Edit.name,
    "sex": Edit.sex,
    "born": Edit.born,
    "weight": Edit.weight,
}


@router.callback_query(IN_GROUP_CB, EditCB.filter())
async def edit_pick(
    call: CallbackQuery,
    callback_data: EditCB,
    state: FSMContext,
    child: Child | None,
) -> None:
    """Спросить новое значение выбранного поля."""
    prompt = _EDIT_PROMPT.get(callback_data.field)
    target = _EDIT_STATE.get(callback_data.field)
    if prompt is None or target is None or child is None:
        # Кнопка из старого сообщения, пережившая удаление карточки
        await call.answer(texts.NO_CHILD, show_alert=True)
        return

    await state.set_state(target)
    await call.answer()
    if isinstance(call.message, Message):
        await call.message.answer(
            prompt, reply_markup=keyboards.sex() if callback_data.field == "sex" else None
        )


@router.message(Edit.name, F.text)
async def edit_name(
    message: Message, state: FSMContext, session: AsyncSession, child: Child
) -> None:
    name = " ".join((message.text or "").split())[:64]
    if not name:
        await message.answer(texts.BABY_EDIT_ASK_NAME, reply_markup=keyboards.home())
        return
    await state.clear()
    await repo.update_child(session, child, name=name)
    await message.answer(texts.baby_edited("Имя", name), reply_markup=keyboards.home())


@router.callback_query(IN_GROUP_CB, Edit.sex, SexCB.filter())
async def edit_sex(
    call: CallbackQuery,
    callback_data: SexCB,
    state: FSMContext,
    session: AsyncSession,
    child: Child,
) -> None:
    await state.clear()
    await repo.update_child(session, child, sex=callback_data.value)
    await call.answer()
    if isinstance(call.message, Message):
        spelled = "девочка" if callback_data.value == "f" else "мальчик"
        await call.message.answer(texts.baby_edited("Пол", spelled), reply_markup=keyboards.home())


@router.message(Edit.born, F.text)
async def edit_born(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    family: Family,
    child: Child,
) -> None:
    """Сменить момент рождения — и погасить то, что от него было посчитано."""
    moment = parse_moment(message.text or "", family.tz)
    if moment is None:
        await message.answer(texts.BABY_BAD_DATE, reply_markup=keyboards.home())
        return
    if moment > tu.now_utc():
        await message.answer(texts.BABY_FUTURE_DATE, reply_markup=keyboards.home())
        return

    await state.clear()

    # Точку веса при рождении ищем ДО правки: она привязана к старому моменту,
    # и после присваивания найти её будет уже не по чему
    birth_point = await repo.measurement_at(session, child.id, WEIGHT, child.born_at)

    await repo.update_child(session, child, born_at=moment)
    if birth_point is not None:
        await repo.update_measurement(session, birth_point, taken_at=moment)

    # Даты прививок и осмотров заморожены в момент включения подписки и теперь
    # посчитаны от несуществующей даты. Молчаливое гашение здесь так же
    # недопустимо, как молчаливое закрытие таймера
    dropped = 0
    for preset in (p.id for p in schedule.PRESETS if schedule.section_of(p)):
        dropped += await repo.deactivate_preset(session, family.id, preset)

    log.info("Поправлена дата рождения ребёнка #%s, погашено %s", child.id, dropped)

    local = tu.to_local(moment, family.tz)
    answer = texts.baby_edited("Дата рождения", f"{local:%d.%m.%Y в %H:%M}")
    if dropped:
        answer += texts.presets_dropped(dropped)
    await message.answer(answer, reply_markup=keyboards.home())


@router.message(Edit.weight, F.text)
async def edit_weight(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    member: Member,
    child: Child,
) -> None:
    """Записать вес при рождении — в оба места сразу.

    Он живёт и полем карточки, и точкой на графике: поправив одно и забыв
    другое, мы получили бы карточку и график, которые расходятся.
    """
    grams = parse_grams(message.text or "")
    if grams is None:
        await message.answer(texts.BABY_BAD_WEIGHT, reply_markup=keyboards.home())
        return

    await state.clear()
    birth_point = await repo.measurement_at(session, child.id, WEIGHT, child.born_at)
    await repo.update_child(session, child, birth_weight_g=grams)

    if birth_point is None:
        # Мастер прошли через /skip — теперь обещание «добавить позже»
        # становится правдой, а не остаётся ложью навсегда
        await repo.add_measurement(
            session, child.id, member.id, WEIGHT,
            taken_at=child.born_at, value=float(grams),
        )
    else:
        await repo.update_measurement(session, birth_point, value=float(grams))

    await message.answer(
        texts.baby_edited("Вес при рождении", texts.weight_str(grams)),
        reply_markup=keyboards.home(),
    )
