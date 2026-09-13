from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings

settings.db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(settings.db_url, echo=False)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
