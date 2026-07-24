"""SQLAlchemy async engine / session / DeclarativeBase。"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """建表（create_all，决策记录第 1 条）。"""
    from app.db import models  # noqa: F401  确保模型已注册到 metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """关闭引擎（lifecycle 中 reset_all 之后调用，第 3.3 节）。"""
    await engine.dispose()
