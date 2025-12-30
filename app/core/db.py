from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_database_settings


settings = get_database_settings()
database_url = settings.database_url
if database_url.startswith("mysql://"):
    database_url = database_url.replace("mysql://", "mysql+aiomysql://", 1)

engine = create_async_engine(database_url, pool_pre_ping=True)

AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)


async def get_async_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
