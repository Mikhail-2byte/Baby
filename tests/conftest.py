"""Фикстуры. Сети в тестах нет никогда — настоящий `Bot` не создаётся.

Три autouse-фикстуры уводят в tmp модульное состояние, которое иначе течёт
между тестами и делает их зелёными или красными в зависимости от порядка
файлов. Это не паранойя: `panel._tasks` и `pending._asks` — обычные словари
уровня модуля, и один тест видит остатки другого.
"""

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db import repo
from bot.db.models import Base
from bot.services import backup, panel, pending, ticker


@pytest.fixture(autouse=True)
def _heartbeat(tmp_path, monkeypatch):
    """Отметка живого цикла на время теста уводится в tmp.

    Без подмены прогон трогал бы `data/heartbeat`, по которому внешний сторож
    судит о здоровье боевого бота, — и сторож перестал бы ловить настоящую
    остановку, пока идут тесты.
    """
    monkeypatch.setattr(ticker, "HEARTBEAT", tmp_path / "heartbeat")


@pytest.fixture(autouse=True)
def _backup_dir(tmp_path, monkeypatch):
    """Каталог снимков на время теста уводится в tmp.

    `_failed_on` тоже сбрасываем: это модульная отметка «сегодня уже не
    получилось», и без сброса тест, проверяющий успешный бэкап, зависел бы от
    того, падал ли бэкап в предыдущем тесте.
    """
    monkeypatch.setattr(backup, "DIR", tmp_path / "backups")
    monkeypatch.setattr(backup, "TMP", tmp_path / "backups" / "snapshot.part")
    monkeypatch.setattr(backup, "_failed_on", None)


@pytest.fixture(autouse=True)
def _pending_state():
    """Ожидания числа — модульный словарь, между тестами он течёт."""
    pending.clear()
    yield
    pending.clear()


@pytest_asyncio.fixture(autouse=True)
async def _panel_state():
    """Панель держит состояние в модульных словарях.

    Без уборки один тест видит лок и незавершённый дебаунс другого, а
    оставшаяся задача печатает «Task was destroyed but it is pending».
    """
    yield
    tasks = list(panel._tasks.values())
    panel._tasks.clear()
    panel._locks.clear()
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


@pytest_asyncio.fixture
async def session_maker():
    """Фабрика сессий на общей БД в памяти.

    Движок для `:memory:` держит StaticPool — одно соединение на всех, поэтому
    разные сессии видят одни и те же данные. Это нужно там, где сессию открывает
    не тест, а сам код (панель, тикер).
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(session_maker):
    async with session_maker() as s:
        yield s


@pytest_asyncio.fixture
async def family(session):
    fam = await repo.get_or_create_family(session, -1001, "Малышка")
    fam.tz = "Europe/Moscow"
    await session.commit()
    return fam


@pytest_asyncio.fixture
async def mother(session, family):
    return await repo.get_or_create_member(session, family.id, 222, "Аня")


@pytest.fixture
def born():
    """Момент рождения: 1 сентября 2026, 04:20 UTC. Фиксированный намеренно.

    Возраст, центили и границы суток — всё считается от него, и плавающая
    «сейчас минус неделя» сделала бы тесты мигающими на переводе часов.
    """
    return datetime(2026, 9, 1, 4, 20)


@pytest.fixture
def now(born):
    """«Сейчас» для тестов: ребёнку шесть суток."""
    return born + timedelta(days=6, hours=8)


@pytest_asyncio.fixture
async def child(session, family, born):
    return await repo.create_child(
        session,
        family.id,
        name="Аня",
        sex="f",
        born_at=born,
        birth_weight_g=3420,
    )


class FakeBot:
    """Заглушка вместо `Bot`: складывает отправленное в список.

    `fail_on` / `fail_on_edit` позволяют проверить, что сбой одной отправки или
    правки не срывает остальные; ключ — порядковый номер вызова.
    """

    def __init__(self, fail_on=None, fail_on_edit=None, fail_on_pin=None):
        self.sent: list[tuple[int, str]] = []
        self.kwargs: list[dict] = []  # чем сопровождалась отправка
        self.edited: list[tuple[int, int, str]] = []
        self.edit_kwargs: list[dict] = []
        self.pinned: list[int] = []
        self.unpinned: list[int] = []
        self._fail_on = fail_on or {}
        self._fail_on_edit = fail_on_edit or {}
        self._fail_on_pin = fail_on_pin or {}
        # message_id последовательны, как в настоящем чате: на этом держится
        # подсчёт «на сколько сообщений уехала панель»
        self._next_id = 100

    async def send_message(self, chat_id: int, text: str, **kwargs):
        error = self._fail_on.get(len(self.sent))
        self.sent.append((chat_id, text))
        self.kwargs.append(kwargs)
        if error is not None:
            raise error
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text: str, chat_id=None, message_id=None, **kwargs):
        error = self._fail_on_edit.get(len(self.edited))
        self.edited.append((chat_id, message_id, text))
        self.edit_kwargs.append(kwargs)
        if error is not None:
            raise error
        return None

    async def pin_chat_message(self, chat_id: int, message_id: int, **kwargs):
        error = self._fail_on_pin.get(len(self.pinned))
        self.pinned.append(message_id)
        if error is not None:
            raise error
        return None

    async def unpin_chat_message(self, chat_id: int, message_id=None, **kwargs):
        self.unpinned.append(message_id)
        return None

    @property
    def texts(self) -> list[str]:
        return [text for _, text in self.sent]


@pytest.fixture
def bot():
    return FakeBot()
