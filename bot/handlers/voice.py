"""Голосовые: расшифровка, триаж, разбор, карточка подтверждения (этап 7).

Единственный путь свободного ввода в этом боте. Разбора печатного текста нет и
не будет: печатать в три часа ночи одной рукой хуже, чем сказать вслух, а два
входных канала означали бы два пути разбора, две причины отказа и две копии
карточки. Текстовый путь остаётся прежним — кнопки, голое число в ответ на
вопрос бота, кнопка «❓ Вопрос».

Голосовое обрабатывается **сразу**, без кнопки-приглашения. Это разворот
относительно Family_planer, где приглашение было и где в докстроке записано
«ни одной строки семейной болтовни в чужом API». Здесь наоборот: с заполненным
`STT_KEY` **каждая** голосовая реплика этого чата уходит в Groq. Разворот
сознательный — чат у бота один и он про ребёнка, а лишний тап перед каждой
записью отменяет весь смысл говорить вслух, — но он обязан быть названным, а
не обнаруженным потом.

**Граница безопасности живёт здесь, в хендлере, а не в порядке роутеров, и это
главное, что стоит знать про этот файл.** `alarm` висит на
`F.text | F.caption`, а у голосового нет ни текста, ни подписи: подписей к
голосовым Telegram не даёт. Значит роутер триажа голосового **не видит**, и то,
что мы стоим ниже него, не даёт нам ровно ничего. Красный флаг, сказанный
вслух, перехватывается вызовом `alarm.assess`/`alarm.respond` на расшифровке —
до обращения к модели. Это не копия границы: код один, вызывающих два. Убрать
эти вызовы как «дублирующие» нельзя — их некому заменить.

Порядок проверок в хендлере **обратный** остальному проекту: триаж идёт раньше
проверки «есть ли карточка ребёнка». В `track` и `measure` проверка `child is
None` стоит первой, и там это правильно; здесь она отняла бы триаж у семьи,
которая ещё не успела завести карточку, — то есть у той, у которой ребёнку
несколько часов. Без карточки возраст неизвестен, и возрастные признаки
срабатывают все: в сторону лишней тревоги, единственную допустимую.

Сеть трогаем до базы. `FamilyMiddleware` открывает сессию на апдейт и держит её
всё время работы хендлера, а здесь это до трёх минут: расшифровка и модель по
полторы. Поэтому ни одного обращения к БД до того, как разбор готов.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import settings
from bot.db import repo
from bot.db.models import TIMED_KINDS, WEIGHT, Child, Family, Member
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.handlers import alarm, measure, qa
from bot.keyboards import VoiceCB
from bot.services import dictation, llm, numerals, panel
from bot.services import timeutil as tu
from bot.services import voice as stt

log = logging.getLogger(__name__)

router = Router(name="voice")

# Столько карточек одновременно в семейном чате не висит. Потолок нужен не ради
# памяти на одну карточку, а чтобы брошенные разборы не копились всё время
# работы бота
MAX_DRAFTS = 20

# Ключ — (chat_id, message_id) карточки. Не FSM: его ключ «чат + пользователь»,
# и «Записать», нажатое вторым родителем, черновика бы не нашло — а в семейном
# чате это обычное дело. Живёт в памяти и умирает с перезапуском: тап по
# вчерашней карточке получит «устарела», и это лучше тишины
_drafts: dict[tuple[int, int], "Draft"] = {}

# Чаты, которым уже сказали, что голос выключен. Отвечать на каждую голосовую
# реплику «нет ключа» хуже, чем молчать, но и молчать нельзя: голос — вход без
# команды и без кнопки, то есть необнаружимый, а этот проект дважды пострадал
# от «бот молчит и выглядит исправным»
_told_off: set[int] = set()


@dataclass(frozen=True, slots=True)
class Draft:
    """Разбор, ждущий нажатия. Времена внутри — локальные, не UTC."""

    family_id: int
    records: tuple[dictation.Record, ...]
    # Исходное голосовое, а не карточка: по нему запись можно найти в чате
    source_message_id: int
    now_local: datetime
    heard: str


def _remember(key: tuple[int, int], draft: Draft) -> None:
    if len(_drafts) >= MAX_DRAFTS:
        # Словари Python держат порядок вставки — первый ключ самый старый
        _drafts.pop(next(iter(_drafts)), None)
    _drafts[key] = draft


# `F.from_user` — не украшение: в группе, привязанной к каналу, сообщение
# приходит от имени канала и автора у него нет. `FamilyMiddleware` тогда не
# кладёт `member`, aiogram падает на резолве аргумента, и апдейт теряется
# навсегда — offset Telegram сдвигается независимо от исхода. Это уже
# случалось, см. комментарий в `middlewares.py`.
#
# `F.audio` рядом с `F.voice`: «голосовое» в Telegram бывает и файлом, если
# записывали не кнопкой микрофона. Кружок-видео (`video_note`) оставлен за
# бортом сознательно — поддержка контейнера у провайдеров неровная
@router.message(IN_GROUP, F.voice | F.audio, F.from_user)
async def dictate(
    message: Message,
    bot: Bot,
    family: Family,
    child: Child | None,
) -> None:
    """Голосовое: скачать → расшифровать → цифры → триаж → разбор → карточка."""
    if not stt.available():
        # ДО скачивания, а не внутри `transcribe`: тот отказывает без запроса к
        # провайдеру, но файл к тому моменту уже скачан. Голосовые в семейном
        # чате идут постоянно, и живой `getFile` с загрузкой через прокси на
        # каждую реплику — при выключенном голосе — это трафик ради
        # выброшенных байтов
        if family.chat_id not in _told_off:
            _told_off.add(family.chat_id)
            await message.reply(texts.VOICE_OFF, reply_markup=keyboards.home())
        return

    media = message.voice or message.audio
    if media is None:  # pragma: no cover — фильтр этого не пропустит
        return
    if (media.duration or 0) > settings.voice_max_seconds:
        # До скачивания и до сети: длинную запись не за что качать
        await message.reply(
            texts.voice_too_long(settings.voice_max_seconds),
            reply_markup=keyboards.home(),
        )
        return

    buffer = BytesIO()
    try:
        # Скачивание идёт сессией бота, а у неё уже настроен TELEGRAM_PROXY
        await bot.download(media, destination=buffer)
    except Exception:
        log.warning("Не удалось скачать голосовое", exc_info=True)
        await message.reply(texts.VOICE_FAILED, reply_markup=keyboards.home())
        return

    # Файл уходит как есть: Whisper-совместимый эндпоинт принимает ogg/opus,
    # поэтому конвертации (и ffmpeg) в проекте нет
    raw = await stt.transcribe(buffer.getvalue())
    if raw is None:
        await message.reply(texts.VOICE_FAILED, reply_markup=keyboards.home())
        return

    # Числительные словами → цифры, один раз и на весь дальнейший путь: и в эхо,
    # и в триаж, и в промпт уходит один текст. Без этого «температура тридцать
    # девять» не поднимает ничего — триаж ищет цифры
    text = numerals.digits(raw)

    # Триаж раньше всего, и раньше проверки карточки ребёнка
    hits, level = alarm.assess(text, child)
    if hits:
        await alarm.respond(message, hits, level, family)
        return

    # Эхо расшифровки — самый частый текстовый ответ бота у семьи с включённым
    # голосом, а значит и самый вероятный носитель клавиатуры
    await message.reply(texts.voice_heard(text), reply_markup=keyboards.home())

    if child is None:
        await message.answer(texts.NO_CHILD, reply_markup=keyboards.home())
        return

    now = tu.now_utc()
    now_local = tu.to_local(now, family.tz)
    result = await dictation.interpret(
        text,
        now_local=now_local,
        tz=family.tz,
        child_name=child.name,
        age=texts.age_phrase(child, now),
    )

    if result.kind == dictation.QUESTION:
        await qa.answer_and_reply(message, text)
        return
    if result.kind == dictation.FAILED:
        await message.answer(_failure(result.reason), reply_markup=keyboards.home())
        return
    if result.kind != dictation.RECORD or not result.records:
        await message.answer(texts.VOICE_UNCLEAR, reply_markup=keyboards.home())
        return

    card = await message.answer(
        texts.voice_card(result.records, now_local, text),
        reply_markup=keyboards.voice_card(),
    )
    _remember(
        (message.chat.id, card.message_id),
        Draft(family.id, result.records, message.message_id, now_local, text),
    )


def _failure(reason: str) -> str:
    """Почему разбора не будет — своими словами на каждый случай.

    Три текста, а не один: `llm.py` существует в нынешнем виде именно потому,
    что двенадцать разных отказов однажды слиплись в один `None`, и понять,
    сломан ли бот, было нельзя.
    """
    if reason == llm.NO_MODEL:
        return texts.VOICE_NO_MODEL
    if reason == llm.QUOTA:
        return texts.VOICE_QUOTA
    return texts.VOICE_NOT_PARSED


@router.callback_query(IN_GROUP_CB, VoiceCB.filter())
async def tap(
    call: CallbackQuery,
    callback_data: VoiceCB,
    session: AsyncSession,
    bot: Bot,
    family: Family,
    member: Member,
    child: Child | None,
) -> None:
    if not isinstance(call.message, Message):
        await call.answer()
        return

    key = (call.message.chat.id, call.message.message_id)
    # Черновик снимается ДО сохранения, а не после: двое могут нажать
    # «Записать» на одной карточке, и второй тап обязан не создать вторую пачку
    # записей, а получить «карточка устарела»
    draft = _drafts.pop(key, None)
    if draft is None or draft.family_id != family.id:
        # Перезапуск бота, вытеснение по MAX_DRAFTS или второй тап
        await call.answer(texts.VOICE_STALE, show_alert=True)
        return

    if callback_data.action != "save":
        await call.answer()
        await call.message.edit_text(texts.VOICE_DROPPED)
        return

    if child is None:
        await call.answer(texts.NO_CHILD, show_alert=True)
        return

    lines = await _save(session, family, member, child, draft)
    await call.answer("Записал")
    # Раньше перерисовки карточки: записи уже в базе, и сбой рендера не должен
    # оставить панель без них
    panel.schedule(bot, family.id, call.message.message_id)
    await call.message.edit_text(texts.voice_saved(lines))


async def _save(
    session: AsyncSession,
    family: Family,
    member: Member,
    child: Child,
    draft: Draft,
) -> list[str]:
    """Черновик → записи. Возвращает строки отчёта, по одной на запись.

    Строки собираются теми же `started`/`finished`/`logged`, что у кнопок: чат
    обязан говорить о кормлении одно и то же, пришло оно с кнопки или с голоса.
    """
    lines: list[str] = []
    for record in draft.records:
        started_at = tu.to_utc(record.at, family.tz)

        if record.kind == WEIGHT:
            lines += await _save_weight(session, family, member, child, record)
            continue

        if record.open_timer:
            # Тот же случай, что кнопка «начали»: сперва закрываем идущий
            closed = await repo.close_running(session, child.id, started_at)
            if closed is not None:
                lines.append(texts.voice_closed(closed, family.tz))
            event = await repo.start_event(
                session,
                child.id,
                member.id,
                record.kind,
                started_at=started_at,
                side=record.side,
                subtype=record.subtype,
                source_chat_id=family.chat_id,
                source_message_id=draft.source_message_id,
            )
            lines.append(texts.started(event, family.tz))
            continue

        # Длительность складывается в UTC, а не в локальном времени. Сейчас в
        # России перехода на летнее время нет и оба варианта совпадают — то есть
        # это ровно та ошибка, которая обнаружилась бы раз в полгода
        ended_at = (
            started_at + timedelta(minutes=record.minutes)
            if record.minutes is not None and record.kind in TIMED_KINDS
            else None
        )
        event = await repo.add_event(
            session,
            child.id,
            member.id,
            record.kind,
            started_at=started_at,
            ended_at=ended_at,
            value_num=record.value,
            side=record.side,
            subtype=record.subtype,
            note=record.note,
            source_chat_id=family.chat_id,
            source_message_id=draft.source_message_id,
        )
        lines.append(
            texts.finished(event, family.tz)
            if ended_at is not None
            else texts.logged(event, family.tz)
        )
    return lines


async def _save_weight(
    session: AsyncSession,
    family: Family,
    member: Member,
    child: Child,
    record: dictation.Record,
) -> list[str]:
    """Вес — в `measurements`, и через тот же путь, что ответ числом.

    `to_grams` — единственный вход веса в базу, он же решает «килограммы это
    или граммы». Своя проверка рядом разошлась бы с ним молча.
    """
    if record.value is None:
        return [texts.VOICE_UNCLEAR]
    grams = measure.to_grams(record.value)
    if grams is None:
        return [texts.WEIGHT_OUT_OF_RANGE]
    _, text = await measure.record_weight(
        session, family, member, child, grams, tu.to_utc(record.at, family.tz)
    )
    return [text]
