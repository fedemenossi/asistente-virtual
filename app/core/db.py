from __future__ import annotations

import logging
from urllib.parse import quote_plus

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_database_settings

logger = logging.getLogger(__name__)

settings = get_database_settings()


def _build_database_url() -> str:
    if settings.database_url:
        url = settings.database_url
        if url.startswith("mysql://"):
            return url.replace("mysql://", "mysql+aiomysql://", 1)
        return url

    missing = [
        name
        for name, value in {
            "DB_HOST": settings.db_host,
            "DB_USER": settings.db_user,
            "DB_PASSWORD": settings.db_password,
            "DB_NAME": settings.db_name,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Faltan variables para la conexion MySQL: " + ", ".join(missing)
        )

    host = settings.db_host or ""
    user = quote_plus(settings.db_user or "")
    password = quote_plus(settings.db_password or "")
    name = settings.db_name or ""
    port = settings.db_port or 3306
    return f"mysql+aiomysql://{user}:{password}@{host}:{port}/{name}"


database_url = _build_database_url()

engine = create_async_engine(database_url, pool_pre_ping=True)

AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)


async def get_async_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            if session.in_transaction():
                await session.commit()
        except Exception:
            logger.exception("db_session_error_rolling_back")
            if session.in_transaction():
                await session.rollback()
            raise
